"""Honour `.gitignore` in `glob` and in `grep`'s non-ripgrep path.

`grep` shells out to ripgrep when it's installed, and ripgrep skips ignored
files automatically. The pure-Python fallback did not — it used a small
hardcoded list of directory names. So the *same* search returned different
results depending on whether the user happened to have `rg` on PATH, and on a
machine without it the model got pages of `node_modules` and `.venv` noise.

Scope. This implements the subset of gitignore syntax that accounts for
essentially all real files:

    # comments and blank lines
    build/            directory-only
    *.log             glob, matched against the name at any depth
    /dist             anchored to the file containing the rule
    !keep.log         negation (last matching rule wins)
    src/**/tmp        `**` spanning directories

Not implemented: character classes with ranges beyond fnmatch's own support,
and `\\` escaping of magic characters. Those are rare enough that the cost of
a full reimplementation isn't worth it — and when ripgrep IS installed it does
the complete job anyway. The goal here is that the two paths agree on real
repositories, not that this becomes a second git.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from pathlib import Path

_log = logging.getLogger(__name__)

# Never worth walking into, ignored or not. `.git` in particular is enormous
# and contains nothing the model wants.
ALWAYS_SKIP = frozenset({".git", ".hg", ".svn"})


class _Rule:
    __slots__ = ("pattern", "negated", "dir_only", "anchored", "base")

    def __init__(self, pattern: str, base: str) -> None:
        self.base = base
        self.negated = pattern.startswith("!")
        if self.negated:
            pattern = pattern[1:]
        self.dir_only = pattern.endswith("/")
        if self.dir_only:
            pattern = pattern[:-1]
        # A slash anywhere but the end anchors the pattern to the .gitignore's
        # own directory; otherwise it matches at any depth.
        self.anchored = "/" in pattern
        self.pattern = pattern.lstrip("/")

    def matches(self, rel: str, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        if self.anchored:
            return _fnmatch_path(rel, self.pattern)
        # Unanchored: any path component (or trailing run of them) may match.
        parts = rel.split("/")
        for i in range(len(parts)):
            if _fnmatch_path("/".join(parts[i:]), self.pattern):
                return True
            if fnmatch.fnmatch(parts[i], self.pattern):
                return True
        return False


def _fnmatch_path(path: str, pattern: str) -> bool:
    """fnmatch, but `*` does not cross a directory separator and `**` does."""
    if "**" in pattern:
        return fnmatch.fnmatch(path, pattern.replace("**/", "*").replace("**", "*"))
    # Same number of segments, each matched independently — this is what stops
    # `*.py` matching `a/b.py` the way plain fnmatch would.
    p_parts = pattern.split("/")
    v_parts = path.split("/")
    if len(p_parts) != len(v_parts):
        return False
    return all(fnmatch.fnmatch(v, p) for v, p in zip(v_parts, p_parts))


class IgnoreMatcher:
    """Decides whether a path under `root` is ignored. Cheap to reuse."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self._rules: list[_Rule] = []
        self._load()

    def _load(self) -> None:
        # Root .gitignore plus any nested ones, which is where monorepos put
        # most of their real rules.
        candidates: list[Path] = []
        try:
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = [d for d in dirnames if d not in ALWAYS_SKIP]
                if ".gitignore" in filenames:
                    candidates.append(Path(dirpath) / ".gitignore")
                # Don't descend forever looking for ignore files.
                if len(candidates) > 50:
                    break
        except OSError:
            return
        for path in candidates:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                base = str(path.parent.relative_to(self.root)).replace(os.sep, "/")
            except ValueError:
                continue
            base = "" if base == "." else base
            for raw in text.splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                self._rules.append(_Rule(line, base))

    def is_ignored(self, path: Path) -> bool:
        try:
            rel = str(Path(path).resolve().relative_to(self.root)).replace(os.sep, "/")
        except (ValueError, OSError):
            return False
        if not rel or rel == ".":
            return False
        parts = rel.split("/")
        if any(part in ALWAYS_SKIP for part in parts):
            return True
        try:
            is_dir = Path(path).is_dir()
        except OSError:
            is_dir = False

        ignored = False
        for rule in self._rules:
            # A rule only governs paths beneath the .gitignore that declared it.
            if rule.base:
                if not rel.startswith(rule.base + "/"):
                    continue
                target = rel[len(rule.base) + 1 :]
            else:
                target = rel
            # A file inside an ignored directory is ignored, so test each
            # ancestor too rather than only the full path.
            probe = target
            hit = False
            while probe:
                if rule.matches(probe, is_dir or probe != target):
                    hit = True
                    break
                probe = probe.rpartition("/")[0]
            if hit:
                # Last matching rule wins — that's how negation works.
                ignored = not rule.negated
        return ignored


_CACHE: dict[str, tuple[float, IgnoreMatcher]] = {}
_CACHE_TTL = 30.0


def matcher_for(root: Path) -> IgnoreMatcher:
    """A cached matcher for `root`, rebuilt when the cache entry ages out.

    Building one walks the tree looking for .gitignore files, which is far too
    expensive to redo per candidate path during a single grep.
    """
    import time

    key = str(Path(root).resolve())
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is not None and (now - hit[0]) < _CACHE_TTL:
        return hit[1]
    built = IgnoreMatcher(root)
    _CACHE[key] = (now, built)
    return built


# ---------------------------------------------------------------- status ----

_STATUS_CACHE: dict[str, tuple[float, str]] = {}
_STATUS_TTL = 5.0


def branch_label(root: Path) -> str:
    """ "main" / "main*" (dirty) / "" when this isn't a git work tree.

    Cached for a few seconds because the prompt toolbar repaints on every
    keystroke and shelling out to git that often would be absurd.
    """
    import subprocess
    import time

    key = str(root)
    now = time.monotonic()
    hit = _STATUS_CACHE.get(key)
    if hit is not None and (now - hit[0]) < _STATUS_TTL:
        return hit[1]

    label = ""
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if head.returncode == 0:
            label = head.stdout.strip()
            # --porcelain is the stable, parseable form; anything at all on
            # stdout means the tree has uncommitted changes.
            dirty = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if dirty.returncode == 0 and dirty.stdout.strip():
                label += "*"
    except (OSError, subprocess.SubprocessError):
        label = ""
    _STATUS_CACHE[key] = (now, label)
    return label
