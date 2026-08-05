"""Tool implementations the model can call.

Every tool receives a dict of arguments and returns a string result that gets
fed back to the model as the tool's output.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from . import diff as _diff
from . import documents as _documents
from . import integrations as _integrations
from . import inbox as _inbox
from . import notes as _notes
from . import personal_os as _personal_os
from . import proactive as _proactive
from . import reminders as _reminders
from . import routines as _routines
from . import ui

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 16000


def _truncate(s: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n…[truncated, {len(s) - limit} more chars]"


class PathEscapeError(Exception):
    """Raised when a resolved path would escape the workspace root."""


def _is_within(child: Path, root: Path) -> bool:
    """True if `child` is `root` or lives somewhere underneath it.

    Both arguments must already be resolved (real, absolute) paths. We avoid
    Path.is_relative_to (3.9+ and historically buggy on some builds) and use a
    normalized string-prefix check that is robust across platforms.
    """
    root_s = str(root)
    child_s = str(child)
    if child_s == root_s:
        return True
    # Ensure we compare on a path-component boundary so that e.g.
    # /home/user/work-evil is not considered inside /home/user/work.
    prefix = root_s if root_s.endswith(os.sep) else root_s + os.sep
    return child_s.startswith(prefix)


def _contain(p: Path, root: Path) -> Path:
    """Resolve `p` and require the real path stays inside `root`.

    Resolves symlinks (Path.resolve(strict=False)) so a symlink that points
    outside the workspace is rejected even if the link itself sits inside.
    Raises PathEscapeError when the target escapes `root`.
    """
    root_real = root.resolve()
    target_real = p.resolve()
    if not _is_within(target_real, root_real):
        raise PathEscapeError(
            f"path escapes workspace: {p} resolves to {target_real}, which is outside {root_real}"
        )
    return target_real


def _resolve(path: str, root: Path) -> Path:
    expanded = os.path.expanduser(os.path.expandvars(path))
    p = Path(expanded)
    if not p.is_absolute():
        p = root / p
    return p


def _resolve_contained(path: str, root: Path) -> Path:
    """_resolve + containment check. Raises PathEscapeError on escape.

    Returns the resolved (real) path, which callers may use directly.
    """
    return _contain(_resolve(path, root), root)


@dataclass
class ToolContext:
    root: Path
    yolo: bool = False
    github_token: str | None = None
    default_repository: str | None = None
    workspace_boundary: Path | None = None
    insecure_ssl: bool = False
    # Plumbing populated by the QueryEngine.
    state: object | None = None
    engine: object | None = None
    background: object | None = None
    tasks: object | None = None
    teams: object | None = None
    read_cache: dict | None = None
    # A tool that produces an image (e.g. browser_screenshot) appends raw
    # base64 PNG data here; the engine attaches it to the tool result
    # message so vision-capable models actually see the picture.
    pending_images: list = field(default_factory=list)

    def confirm(self, action: str, detail: str) -> bool:
        """Approval hook for inside a tool.

        Every tool is already gated by can_use_tool() + the permission
        resolver BEFORE it runs — the terminal resolver prompts in the
        REPL, the gateway resolver prompts in the web UI. So by the time a
        tool body calls confirm(), the action is approved; this returns
        True rather than prompting again (a second input() prompt would
        double-ask in the REPL and block the thread in the web gateway).
        """
        return True


# ============================================================================
# Files: read, write, edit, replace_lines, list_dir, grep, glob, set_workspace
# ============================================================================


def t_read_file(args: dict, ctx: ToolContext) -> str:
    path = args["path"]
    start = int(args.get("start_line", 1))
    end = args.get("end_line")
    try:
        p = _resolve_contained(path, ctx.root)
    except PathEscapeError as e:
        return f"ERROR: {e}"
    if not p.exists():
        return f"ERROR: file not found: {path}"
    if not p.is_file():
        return f"ERROR: not a file: {path}"

    abs_path = str(p.resolve())
    # The cache key includes the requested range: re-reading the SAME slice is
    # the wasteful repeat we want to suppress, but asking for a different range
    # (paging through a long file, or fetching the tail after a truncated read)
    # is legitimate — keying on the path alone made those later pages
    # unreachable, since every one came back as "[CACHED]".
    range_key = f"{abs_path}::{max(1, start)}-{'' if end is None else int(end)}"
    cache = ctx.read_cache if isinstance(ctx.read_cache, dict) else None
    state = getattr(ctx, "state", None)
    already_read = (cache is not None and abs_path in cache) or (
        state is not None and abs_path in getattr(state, "files_read", set())
    )
    if already_read:
        return (
            f"[CACHED — you already read {path} (lines "
            f"{max(1, start)}-{end if end is not None else 'end'}) earlier in "
            f"this turn. Scroll back, or request a different line range.]"
        )

    # PDF / DOCX get their text extracted; everything else is read as text.
    if _documents.is_document(p):
        try:
            text = _documents.extract_text(p)
        except _documents.DocumentError as e:
            return f"ERROR: {e}"
        except Exception as e:
            return f"ERROR: could not read {path}: {type(e).__name__}: {e}"
        kind = p.suffix.lower().lstrip(".")
    else:
        try:
            text = p.read_text(errors="replace")
        except Exception as e:
            return f"ERROR: {e}"
        kind = None
    lines = text.splitlines()
    s = max(1, start) - 1
    e = len(lines) if end is None else min(len(lines), int(end))
    selected = lines[s:e]
    numbered = "\n".join(f"{i + s + 1:>5}  {ln}" for i, ln in enumerate(selected))
    if kind:
        header = f"{path}  ({len(lines)} lines of text extracted from {kind})"
    else:
        header = f"{path}  ({len(lines)} lines)"
    out = _truncate(f"{header}\n{numbered}")
    if cache is not None:
        cache[range_key] = out
    if state is not None:
        files_read = set(getattr(state, "files_read", set()) or set())
        files_read.add(range_key)
        state.update(files_read=files_read)
    return out


MAX_EDIT_HISTORY = 50


def _record_edit(ctx: ToolContext, path: Path, before: str, after: str, op: str) -> None:
    state = getattr(ctx, "state", None)
    if state is not None:
        import time as _t

        hist: list = getattr(state, "edit_history", None) or []
        hist.append(
            {
                "ts": _t.time(),
                "path": str(path.resolve()),
                "before": before,
                "after": after,
                "op": op,
            }
        )
        del hist[:-MAX_EDIT_HISTORY]
        state.update(edit_history=hist)
    # Editing the file invalidates EVERY cached range of it, not just the one
    # keyed by the whole file — read_file caches per requested line range.
    rp = str(path.resolve())
    prefix = rp + "::"
    def _stale(key: str) -> bool:
        return key == rp or key.startswith(prefix)
    cache = getattr(ctx, "read_cache", None)
    if isinstance(cache, dict):
        for key in [k for k in cache if _stale(k)]:
            cache.pop(key, None)
    if state is not None:
        files_read = set(getattr(state, "files_read", set()) or set())
        remaining = {k for k in files_read if not _stale(k)}
        if remaining != files_read:
            state.update(files_read=remaining)


def t_write_file(args: dict, ctx: ToolContext) -> str:
    path = args["path"]
    content = args.get("content")
    if content is None:
        return "ERROR: missing argument 'content'"
    if not isinstance(content, str):
        return f"ERROR: 'content' must be a string, got {type(content).__name__}"
    try:
        p = _resolve_contained(path, ctx.root)
    except PathEscapeError as e:
        return f"ERROR: {e}"
    existed = p.is_file()
    if p.exists() and not existed:
        return f"ERROR: not a file: {path}"
    # Read verbatim so the undo snapshot restores the original bytes, line
    # endings included.
    old_text = _read_text_robust(p) if existed else ""
    if existed and old_text.strip() and not content.strip() and not args.get("allow_empty"):
        return (
            f"ERROR: refusing to write empty content to existing file {path}. "
            f"If you really meant to empty it, pass allow_empty=true."
        )
    detail = f"{'overwrite' if existed else 'create'} {path} ({len(content)} bytes)"
    if not ctx.confirm("file write", detail):
        return "ERROR: user denied write"
    if existed:
        # Overwriting a CRLF file with the model's LF text would flip every
        # line ending in it; keep the file's own convention.
        content = _restore_eol(old_text, _norm_eol(content))
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_text_raw(p, content)
    _record_edit(ctx, p, old_text, content, "write")
    adds, dels = _diff.stats(old_text, content)
    return f"OK: wrote {path} +{adds} -{dels}"


def _norm_eol(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _restore_eol(original: str, normalized: str) -> str:
    """Put `normalized` (all-LF) back onto the line endings `original` used.

    The whitespace-tolerant edit paths work on an EOL-normalized copy of the
    file. Writing that copy back verbatim rewrites EVERY line of a CRLF file to
    LF — a one-line edit lands as a whole-file diff. Restore the dominant ending
    so only the edited region actually changes.
    """
    crlf = original.count("\r\n")
    bare_lf = original.count("\n") - crlf
    if crlf and crlf >= bare_lf:
        return normalized.replace("\n", "\r\n")
    # Classic Mac (CR-only) files: no "\n" at all, but "\r" line breaks.
    if "\r" in original and "\n" not in original:
        return normalized.replace("\n", "\r")
    return normalized


def _fuzzy_span(text: str, old: str):
    """Locate the single line range matching `old` (whitespace-tolerant).

    Returns (start, end, exact) where `exact` is True only if the matched
    region is byte-identical to `old` (no whitespace normalization was needed).
    Returns None if there is not exactly one match. Callers should refuse a
    non-exact (`exact is False`) match rather than silently rewriting a region
    whose whitespace differs from what the model supplied.
    """
    text_lines = text.split("\n")
    old_lines = old.split("\n")
    while len(old_lines) > 1 and old_lines[-1] == "":
        old_lines = old_lines[:-1]
    while len(old_lines) > 1 and old_lines[0] == "":
        old_lines = old_lines[1:]
    n = len(old_lines)
    if n == 0:
        return None
    norm_old = [ln.rstrip() for ln in old_lines]
    hits = [
        i
        for i in range(len(text_lines) - n + 1)
        if [ln.rstrip() for ln in text_lines[i : i + n]] == norm_old
    ]
    if len(hits) != 1:
        return None
    start = hits[0]
    region = text_lines[start : start + n]
    exact = region == old_lines
    return (start, start + n, exact)


def _closest_region(text: str, old: str) -> str:
    import difflib

    first = ""
    for ln in old.split("\n"):
        if ln.strip():
            first = ln.strip()
            break
    if not first:
        return ""
    text_lines = text.split("\n")
    best_i, best = 0, 0.0
    for i, ln in enumerate(text_lines):
        score = difflib.SequenceMatcher(None, ln.strip(), first).ratio()
        if score > best:
            best, best_i = score, i
    if best < 0.4:
        return ""
    lo, hi = max(0, best_i - 2), min(len(text_lines), best_i + 6)
    return "\n".join(f"{j + 1:>5}  {text_lines[j]}" for j in range(lo, hi))


def _read_text_robust(p: Path) -> str:
    """Read a file for editing WITHOUT newline translation.

    Path.read_text opens in universal-newline mode, which turns every "\\r\\n"
    into "\\n" in the returned string. Paired with _write_text_raw (which writes
    verbatim), that quietly converted every CRLF file to LF on the first edit —
    a one-line change landed as a whole-file diff. Reading with newline="" keeps
    the real line endings, so the edit tools can preserve them.
    """
    try:
        with p.open("r", encoding="utf-8", errors="replace", newline="") as f:
            text = f.read()
    except (UnicodeDecodeError, OSError):
        with p.open("r", errors="replace", newline="") as f:
            text = f.read()
    if text and text[0] == "﻿":
        text = text[1:]
    return text


def _write_text_raw(p: Path, text: str) -> None:
    """Write UTF-8 with NO newline translation.

    Path.write_text defaults to newline=None, which translates "\\n"→
    os.linesep ("\\r\\n" on Windows). That silently turns every LF file into
    CRLF after an edit (noisy git diffs, broken LF-only repos) and corrupts
    already-CRLF content into "\\r\\r\\n". newline="" writes the string's
    bytes verbatim, so files keep whatever line endings the in-memory text has.
    Pair it with _read_text_robust, which reads verbatim too — the two together
    are what keep a CRLF file CRLF across an edit.
    """
    p.write_text(text, encoding="utf-8", newline="")


def t_edit_file(args: dict, ctx: ToolContext) -> str:
    path = args["path"]
    old = args["old_string"]
    new = args["new_string"]
    replace_all = bool(args.get("replace_all", False))
    try:
        p = _resolve_contained(path, ctx.root)
    except PathEscapeError as e:
        return f"ERROR: {e}"
    if not p.exists():
        return f"ERROR: file not found: {path}"
    raw = _read_text_robust(p)

    count = raw.count(old)
    if count == 1 or (count > 1 and replace_all):
        if not ctx.confirm("file edit", f"{path}: replace {count} occurrence(s)"):
            return "ERROR: user denied edit"
        # The model writes LF; splicing that into a CRLF file would leave mixed
        # endings on the new lines. Match the replacement to the file.
        new = _restore_eol(raw, _norm_eol(new))
        new_text = raw.replace(old, new) if replace_all else raw.replace(old, new, 1)
        if raw.strip() and not new_text.strip() and not args.get("allow_empty"):
            return f"ERROR: refusing to empty {path}. Pass allow_empty=true to confirm."
        _write_text_raw(p, new_text)
        _record_edit(ctx, p, raw, new_text, "edit")
        adds, dels = _diff.stats(raw, new_text)
        return f"OK: edited {path} +{adds} -{dels}"
    if count > 1:
        return f"ERROR: old_string matches {count} times — pass replace_all=true or supply more context"

    # Recovery: line-ending + whitespace tolerant
    text = _norm_eol(raw)
    old_n = _norm_eol(old)
    new_n = _norm_eol(new)
    if text.count(old_n) == 1:
        if not ctx.confirm("file edit", f"{path}: replace 1 occurrence (line-ending normalized)"):
            return "ERROR: user denied edit"
        new_text = text.replace(old_n, new_n, 1)
    else:
        span = _fuzzy_span(text, old_n)
        if span is None:
            lines = len(text.splitlines())
            state = getattr(ctx, "state", None)
            fail_count = 1
            if state is not None:
                fails = dict(getattr(state, "edit_fails", {}) or {})
                rp = str(p.resolve())
                fails[rp] = fails.get(rp, 0) + 1
                fail_count = fails[rp]
                state.update(edit_fails=fails)
            msg = (
                f"ERROR: old_string not found in {path} (file has {lines} lines). "
                f"old_string must match the file EXACTLY, including indentation."
            )
            hint = _closest_region(text, old_n)
            if hint:
                msg += f"\n\nClosest region:\n{hint}"
            if fail_count >= 2:
                msg += (
                    f"\n\nSwitch to replace_lines(path, start_line, end_line, new_content) "
                    f"— surgical line-range edit, no string matching."
                )
            return msg
        i, j, exact = span
        if not exact:
            # The only match differs from old_string by whitespace alone.
            # Refuse rather than silently rewriting a region the model didn't
            # supply verbatim — point it at the surgical line-range tool.
            return (
                f"ERROR: no exact match for old_string in {path}; the closest "
                f"region (lines {i + 1}-{j}) differs only in whitespace. "
                f"Re-supply old_string matching the file EXACTLY (including "
                f"indentation), or use replace_lines(path, start_line, end_line, "
                f"new_content) for a surgical edit."
            )
        if not ctx.confirm("file edit", f"{path}: replace lines {i + 1}-{j} (whitespace-tolerant)"):
            return "ERROR: user denied edit"
        file_lines = text.split("\n")
        new_text = "\n".join(file_lines[:i] + new_n.split("\n") + file_lines[j:])

    if raw.strip() and not new_text.strip() and not args.get("allow_empty"):
        return f"ERROR: refusing to empty {path}. Pass allow_empty=true."
    adds, dels = _diff.stats(text, new_text)
    new_text = _restore_eol(raw, new_text)
    _write_text_raw(p, new_text)
    _record_edit(ctx, p, raw, new_text, "edit")
    return f"OK: edited {path} +{adds} -{dels}"


def t_replace_lines(args: dict, ctx: ToolContext) -> str:
    path = args["path"]
    start = args.get("start_line")
    end = args.get("end_line")
    new_content = args.get("new_content", "")
    if start is None or end is None:
        return "ERROR: replace_lines requires start_line and end_line (1-indexed, inclusive)"
    try:
        p = _resolve_contained(path, ctx.root)
    except PathEscapeError as exc:
        return f"ERROR: {exc}"
    if not p.exists():
        return f"ERROR: file not found: {path}"
    raw = _read_text_robust(p)
    lines = raw.splitlines(keepends=True)
    s = max(1, int(start)) - 1
    e = min(len(lines), int(end))
    if s >= len(lines):
        return f"ERROR: start_line {start} is past end of file ({len(lines)} lines)"
    if e < s + 1:
        return f"ERROR: end_line ({end}) must be >= start_line ({start})"
    if not ctx.confirm("file edit (replace_lines)", f"{path}: lines {s + 1}-{e}"):
        return "ERROR: user denied edit"
    # Preserve the original per-line EOL of the replaced region rather than
    # forcing a whole-file CRLF heuristic (which corrupts mixed-EOL files).
    # We look at the first line being replaced; fall back to the dominant EOL
    # only when the replaced region has no detectable line ending of its own.
    eol = "\n"
    first_replaced = lines[s] if s < len(lines) else ""
    if first_replaced.endswith("\r\n"):
        eol = "\r\n"
    elif first_replaced.endswith("\r"):
        eol = "\r"
    elif first_replaced.endswith("\n"):
        eol = "\n"
    elif raw and "\r\n" in raw and raw.count("\r\n") >= raw.count("\n") / 2:
        eol = "\r\n"
    if new_content == "":
        # Empty new_content means "delete these lines". Appending an EOL to it
        # instead left a stray blank line behind, so the range could never
        # actually be removed.
        replacement: list[str] = []
    else:
        replacement = [
            new_content if new_content.endswith(("\n", "\r")) else new_content + eol
        ]
    new_lines = lines[:s] + replacement + lines[e:]
    new_text = "".join(new_lines)
    if raw.strip() and not new_text.strip() and not args.get("allow_empty"):
        return f"ERROR: refusing to empty {path}. Pass allow_empty=true."
    _write_text_raw(p, new_text)
    _record_edit(ctx, p, raw, new_text, "replace_lines")
    adds, dels = _diff.stats(raw, new_text)
    return f"OK: replaced {path}:{s + 1}-{e} +{adds} -{dels}"


def t_list_dir(args: dict, ctx: ToolContext) -> str:
    path = args.get("path", ".")
    try:
        p = _resolve_contained(path, ctx.root)
    except PathEscapeError as exc:
        return f"ERROR: {exc}"
    if not p.exists():
        return f"ERROR: not found: {path}"
    if not p.is_dir():
        return f"ERROR: not a directory: {path}"
    rows = []
    for entry in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name)):
        if entry.name.startswith("."):
            continue
        kind = "dir " if entry.is_dir() else "file"
        try:
            size = entry.stat().st_size if entry.is_file() else 0
        except OSError:
            size = 0
        rows.append(f"  {kind}  {size:>9}  {entry.name}")
    header = f"{path}  ({len(rows)} entries)"
    body = "\n".join(rows) if rows else "(empty or only dotfiles)"
    return _truncate(f"{header}\n{body}")


def t_grep(args: dict, ctx: ToolContext) -> str:
    pattern = args.get("pattern") or args.get("query") or args.get("regex")
    if not pattern:
        return "ERROR: missing argument 'pattern'"
    path = args.get("path", ".")
    case_insensitive = bool(args.get("case_insensitive", False))
    try:
        p = _resolve_contained(path, ctx.root)
    except PathEscapeError as exc:
        return f"ERROR: {exc}"

    home = Path.home().resolve()
    target = p.resolve()
    if target == home or target == Path(home.anchor):
        return f"ERROR: grep target {p} is too broad. Specify a narrower path."

    import shutil as _sh

    rg = _sh.which("rg")
    if rg and p.exists():
        cmd = [rg, "-n", "--no-heading", "--color=never", "-m", "200"]
        if case_insensitive:
            cmd.append("-i")
        cmd.extend(["--", pattern, str(p)])
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return "ERROR: rg timed out after 30s"
        if proc.returncode in (0, 1):
            out = proc.stdout.strip()
            return _truncate(out) if out else "(no matches)"

    flags = re.IGNORECASE if case_insensitive else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return f"ERROR: bad regex: {e}"
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    matches: list[str] = []
    targets = [p] if p.is_file() else list(p.rglob("*")) if p.is_dir() else []
    scanned = 0
    MAX_SCAN = 2000
    for f in targets:
        if not f.is_file():
            continue
        if any(part in skip_dirs for part in f.parts):
            continue
        if any(s in f.parts for s in (".cache", "site-packages", ".tox", ".pytest_cache")):
            continue
        scanned += 1
        if scanned > MAX_SCAN:
            matches.append(f"…[scanned {MAX_SCAN}+ files, stopping]")
            return _truncate("\n".join(matches))
        try:
            with f.open("r", errors="replace") as fp:
                for i, line in enumerate(fp, 1):
                    if rx.search(line):
                        rel = f.relative_to(ctx.root) if f.is_relative_to(ctx.root) else f
                        matches.append(f"{rel}:{i}: {line.rstrip()}")
                        if len(matches) >= 200:
                            matches.append("…[200 match cap]")
                            return _truncate("\n".join(matches))
        except (OSError, UnicodeDecodeError):
            continue
    return _truncate("\n".join(matches) if matches else "(no matches)")


def t_glob(args: dict, ctx: ToolContext) -> str:
    import fnmatch

    pattern = args["pattern"]
    try:
        base = _resolve_contained(args.get("path", "."), ctx.root)
    except PathEscapeError as exc:
        return f"ERROR: {exc}"
    if not base.exists() or not base.is_dir():
        return f"ERROR: not a directory: {base}"
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    matches: list[str] = []
    if "**" in pattern or "/" in pattern:
        for p in base.rglob("*"):
            if any(part in skip_dirs for part in p.parts):
                continue
            rel = p.relative_to(base)
            if fnmatch.fnmatch(str(rel), pattern):
                matches.append(str(rel))
    else:
        for p in base.iterdir():
            if fnmatch.fnmatch(p.name, pattern):
                matches.append(p.name)
    matches.sort()
    return _truncate("\n".join(matches[:500]) if matches else "(no matches)")


def t_set_workspace(args: dict, ctx: ToolContext) -> str:
    path = args["path"]
    create = bool(args.get("create", False))
    p = _resolve(path, ctx.root)
    if ctx.workspace_boundary is not None:
        try:
            p = _contain(p, ctx.workspace_boundary)
        except PathEscapeError as e:
            return f"ERROR: pinned project boundary: {e}"
    if not p.exists():
        if not create:
            return f"ERROR: directory does not exist: {p}. Pass create=true to mkdir it."
        p.mkdir(parents=True, exist_ok=True)
    elif not p.is_dir():
        return f"ERROR: not a directory: {p}"
    ctx.root = p.resolve()
    return f"OK: workspace set to {ctx.root}"


# ============================================================================
# Notes — persistent knowledge base
# ============================================================================


def t_note_write(args: dict, ctx: ToolContext) -> str:
    name = args.get("name")
    body = args.get("body") or args.get("content")
    append = bool(args.get("append", False))
    if not name or body is None:
        return "ERROR: note_write requires 'name' and 'body'"
    if not ctx.confirm(
        "save note", f"{name} ({len(body)} chars, {'append' if append else 'overwrite'})"
    ):
        return "ERROR: user denied"
    note = _notes.write(name, body, append=append)
    return f"OK: saved note '{note.name}' ({len(note.body)} chars) at {note.path}"


def t_note_get(args: dict, ctx: ToolContext) -> str:
    name = args.get("name")
    if not name:
        return "ERROR: note_get requires 'name'"
    note = _notes.get(name)
    if not note:
        return f"(no note named '{name}')"
    return _truncate(f"--- {note.name} ({note.path}) ---\n{note.body}")


def t_note_list(args: dict, ctx: ToolContext) -> str:
    items = _notes.list_all()
    if not items:
        return "(no notes saved yet)"
    return "\n".join(n.short() for n in items[:80])


def t_note_search(args: dict, ctx: ToolContext) -> str:
    query = args.get("query") or args.get("q")
    if not query:
        return "ERROR: note_search requires 'query'"
    hits = _notes.search(query)
    if not hits:
        return f"(no notes match '{query}')"
    out: list[str] = []
    for note, lines in hits[:20]:
        out.append(f"--- {note.name} ---")
        for ln in lines:
            out.append(f"  {ln}")
    return _truncate("\n".join(out))


def t_note_delete(args: dict, ctx: ToolContext) -> str:
    name = args.get("name")
    if not name:
        return "ERROR: note_delete requires 'name'"
    if not ctx.confirm("delete note", name):
        return "ERROR: user denied"
    return "OK: deleted" if _notes.delete(name) else f"ERROR: no note named '{name}'"


def t_chat_search(args: dict, ctx: ToolContext) -> str:
    """Search the user's previous conversations for a phrase."""
    from . import sessions as _sessions

    query = (args.get("query") or args.get("q") or "").strip()
    if not query:
        return "ERROR: chat_search requires 'query'"
    q = query.lower()
    out: list[str] = []
    for meta in _sessions.list_all():  # newest first
        data = _sessions.load(meta["id"])
        if not data:
            continue
        hits: list[str] = []
        for m in data.get("messages", []):
            if m.get("role") not in ("user", "assistant"):
                continue
            content = m.get("content") or ""
            idx = content.lower().find(q)
            if idx < 0:
                continue
            start = max(0, idx - 80)
            snippet = " ".join(content[start : idx + len(query) + 160].split())
            hits.append(f"  [{m['role']}] …{snippet}…")
            if len(hits) >= 3:
                break
        if hits:
            out.append(
                f"--- chat '{meta['title']}' "
                f"({_sessions.fmt_time(meta['updated_at'])}, id {meta['id']}) ---"
            )
            out.extend(hits)
        if len(out) >= 40:
            break
    if not out:
        return f"(no previous chats mention '{query}')"
    return _truncate("\n".join(out))


def t_chat_get(args: dict, ctx: ToolContext) -> str:
    """Read one previous conversation in full (by id from chat_search)."""
    from . import sessions as _sessions

    sid = (args.get("id") or "").strip()
    if not sid:
        return "ERROR: chat_get requires 'id' (from chat_search results)"
    data = _sessions.load(sid)
    if not data:
        return f"ERROR: no chat with id '{sid}'"
    lines = [f"# {data.get('title') or 'untitled'}"]
    for m in data.get("messages", []):
        content = (m.get("content") or "").strip()
        if m.get("role") in ("user", "assistant") and content:
            lines.append(f"[{m['role']}] {content[:600]}")
    return _truncate("\n".join(lines))


# ============================================================================
# Reminders — persistent to-do across sessions
# ============================================================================


def t_reminder_add(args: dict, ctx: ToolContext) -> str:
    text = args.get("text")
    if not text:
        return "ERROR: reminder_add requires 'text'"
    when_raw = args.get("when")
    due_at = _reminders.parse_when(when_raw) if when_raw else None
    if when_raw and due_at is None:
        # Not fatal — keep the reminder, just without a due time, and note it.
        note = f"  (couldn't parse 'when' = {when_raw!r}; saved without due time)"
    else:
        note = ""
    tags = _as_tag_list(args.get("tags"))
    if not ctx.confirm("add reminder", text[:80] + (f" @ {when_raw}" if when_raw else "")):
        return "ERROR: user denied"
    r = _reminders.add(text, due_at=due_at, tags=tags)
    return f"OK: {r.short().strip()}{note}"


def t_reminder_list(args: dict, ctx: ToolContext) -> str:
    include_done = bool(args.get("include_done", False))
    status = args.get("status")
    rems = _reminders.list_all(status=status, include_done=include_done)
    if not rems:
        return "(no reminders)"
    return "\n".join(r.short() for r in rems[:80])


def t_reminder_done(args: dict, ctx: ToolContext) -> str:
    rid = args.get("id")
    if not rid:
        return "ERROR: reminder_done requires 'id'"
    r = _reminders.update(rid, status="done")
    return f"OK: marked done — {r.short().strip()}" if r else _no_reminder(rid)


def t_reminder_delete(args: dict, ctx: ToolContext) -> str:
    rid = args.get("id")
    if not rid:
        return "ERROR: reminder_delete requires 'id'"
    if not ctx.confirm("delete reminder", rid):
        return "ERROR: user denied"
    return "OK: deleted" if _reminders.delete(rid) else _no_reminder(rid)


def t_reminder_update(args: dict, ctx: ToolContext) -> str:
    rid = args.get("id")
    if not rid:
        return "ERROR: reminder_update requires 'id'"
    changes: dict = {}
    if "text" in args:
        changes["text"] = args["text"]
    if "status" in args:
        changes["status"] = args["status"]
    if "when" in args:
        due = _reminders.parse_when(args["when"]) if args["when"] else None
        changes["due_at"] = due
    if "tags" in args:
        changes["tags"] = _as_tag_list(args["tags"])
    if not changes:
        return "ERROR: nothing to update"
    r = _reminders.update(rid, **changes)
    return f"OK: {r.short().strip()}" if r else _no_reminder(rid)


# ============================================================================
# Personal OS — goals, calendar, and proactive planning context
# ============================================================================


def t_goal_create(args: dict, ctx: ToolContext) -> str:
    if not args.get("title"):
        return "ERROR: goal_create requires 'title'"
    goal = _personal_os.create_goal(
        args["title"],
        description=args.get("description", ""),
        target_at=args.get("target"),
        category=args.get("category", "personal"),
        progress=args.get("progress", 0),
    )
    return f"OK: {goal['id']} — {goal['title']} ({goal['progress']}%)"


def t_goal_list(args: dict, ctx: ToolContext) -> str:
    goals = _personal_os.list_goals(include_completed=bool(args.get("include_completed", True)))
    if not goals:
        return "(no goals)"
    return "\n".join(
        f"{g['id']}  [{g.get('status', 'active')}]  {g['title']}  {g.get('progress', 0)}%"
        for g in goals[:80]
    )


def t_goal_update(args: dict, ctx: ToolContext) -> str:
    goal_id = str(args.get("id", ""))
    if not goal_id:
        return "ERROR: goal_update requires 'id'"
    changes = {
        key: args[key]
        for key in ("title", "description", "category", "progress", "status")
        if key in args
    }
    if "target" in args:
        changes["target_at"] = args["target"]
    goal = _personal_os.update_goal(goal_id, **changes)
    return (
        f"OK: {goal['id']} — {goal['title']} ({goal.get('progress', 0)}%)"
        if goal
        else f"ERROR: no goal {goal_id}"
    )


def t_goal_delete(args: dict, ctx: ToolContext) -> str:
    goal_id = str(args.get("id", ""))
    if not goal_id:
        return "ERROR: goal_delete requires 'id'"
    return "OK: deleted" if _personal_os.delete_goal(goal_id) else f"ERROR: no goal {goal_id}"


def t_calendar_event_add(args: dict, ctx: ToolContext) -> str:
    if not args.get("title") or not args.get("start"):
        return "ERROR: calendar_event_add requires 'title' and 'start'"
    event = _personal_os.create_event(
        args["title"],
        start_at=args["start"],
        end_at=args.get("end"),
        description=args.get("description", ""),
        location=args.get("location", ""),
        all_day=bool(args.get("all_day", False)),
        source=args.get("source", "cagentic"),
        external_id=args.get("external_id", ""),
    )
    return f"OK: {event['id']} — {event['title']}"


def t_calendar_event_list(args: dict, ctx: ToolContext) -> str:
    start = args.get("start", time.time() - 86400)
    end = args.get("end", time.time() + 30 * 86400)
    events = _personal_os.list_events(start_at=start, end_at=end)
    if not events:
        return "(no calendar events)"
    return "\n".join(
        f"{e['id']}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(e['start_at']))}  "
        f"{e['title']}" + (f" @ {e['location']}" if e.get("location") else "")
        for e in events[:100]
    )


def t_calendar_event_update(args: dict, ctx: ToolContext) -> str:
    event_id = str(args.get("id", ""))
    if not event_id:
        return "ERROR: calendar_event_update requires 'id'"
    changes = {
        key: args[key]
        for key in ("title", "description", "location", "all_day", "status")
        if key in args
    }
    if "start" in args:
        changes["start_at"] = args["start"]
    if "end" in args:
        changes["end_at"] = args["end"]
    event = _personal_os.update_event(event_id, **changes)
    return f"OK: {event['id']} — {event['title']}" if event else f"ERROR: no event {event_id}"


def t_calendar_event_delete(args: dict, ctx: ToolContext) -> str:
    event_id = str(args.get("id", ""))
    if not event_id:
        return "ERROR: calendar_event_delete requires 'id'"
    return (
        "OK: deleted"
        if _personal_os.delete_event(event_id)
        else f"ERROR: no calendar event {event_id}"
    )


def t_personal_briefing(args: dict, ctx: ToolContext) -> str:
    data = _personal_os.briefing()
    lines = [
        f"{data['greeting']} — {data['date_label']}",
        f"Today: {data['stats']['events_today']} events, "
        f"{data['stats']['open_deadlines']} open deadlines, "
        f"{data['stats']['active_goals']} active goals",
    ]
    lines.extend(f"- {item['title']}: {item['body']}" for item in data["insights"])
    if data["agenda"]:
        lines.append("Upcoming:")
        lines.extend(
            f"- {time.strftime('%a %H:%M', time.localtime(item['start_at']))} "
            f"{item['title']}"
            for item in data["agenda"][:12]
        )
    return "\n".join(lines)


def t_calendar_connection_create(args: dict, ctx: ToolContext) -> str:
    if not args.get("name") or not args.get("url"):
        return "ERROR: calendar_connection_create requires 'name' and 'url'"
    connection = _integrations.create_connection(
        args["name"],
        args.get("kind", "ical"),
        args["url"],
        username=args.get("username", ""),
        password=args.get("password", ""),
        direction=args.get("direction"),
        auto_sync=bool(args.get("auto_sync", True)),
        sync_interval=args.get("sync_interval", 900),
    )
    return f"OK: {connection['id']} — {connection['name']} ({connection['kind']})"


def t_calendar_connection_list(args: dict, ctx: ToolContext) -> str:
    connections = _integrations.list_connections()
    if not connections:
        return "(no calendar connections)"
    return "\n".join(
        f"{item['id']}  [{item['status']}]  {item['name']}  {item['kind']}  {item['detail']}"
        for item in connections
    )


def t_calendar_connection_sync(args: dict, ctx: ToolContext) -> str:
    connection_id = str(args.get("id", ""))
    if not connection_id:
        return "ERROR: calendar_connection_sync requires 'id'"
    result = _integrations.sync_connection(connection_id)
    if not result.get("ok"):
        return f"ERROR: {result.get('error', 'sync failed')}"
    return (
        f"OK: imported {result['imported']}, updated {result['updated']}, "
        f"removed {result['removed']}, pushed {result['pushed']}"
    )


def t_calendar_connection_delete(args: dict, ctx: ToolContext) -> str:
    connection_id = str(args.get("id", ""))
    if not connection_id:
        return "ERROR: calendar_connection_delete requires 'id'"
    deleted = _integrations.delete_connection(
        connection_id,
        remove_imported_events=bool(args.get("remove_imported_events", False)),
    )
    return "OK: deleted" if deleted else f"ERROR: no connection {connection_id}"


def t_notification_list(args: dict, ctx: ToolContext) -> str:
    notifications = _proactive.list_notifications(
        include_dismissed=bool(args.get("include_dismissed", False))
    )
    if not notifications:
        return "(no proactive notifications)"
    return "\n".join(
        f"{item['id']}  [{'new' if not item.get('read') else 'read'}]  "
        f"{item['title']} — {item['body']}"
        for item in notifications
    )


def t_notification_update(args: dict, ctx: ToolContext) -> str:
    notification_id = str(args.get("id", ""))
    action = str(args.get("action", "read"))
    if action == "read_all":
        return f"OK: marked {_proactive.mark_all_read()} notifications read"
    if not notification_id:
        return "ERROR: notification_update requires 'id'"
    if action == "dismiss":
        return "OK: dismissed" if _proactive.dismiss(notification_id) else "ERROR: not found"
    item = _proactive.mark_read(notification_id, read=bool(args.get("read", True)))
    return "OK: updated" if item else "ERROR: not found"


def t_inbox_capture(args: dict, ctx: ToolContext) -> str:
    if not args.get("title"):
        return "ERROR: inbox_capture requires 'title'"
    item = _inbox.create_item(
        args["title"],
        summary=args.get("summary", ""),
        kind=args.get("kind", "capture"),
        priority=args.get("priority", 0),
        tags=args.get("tags") or [],
    )
    return f"OK: {item['id']} — {item['title']}"


def t_inbox_list(args: dict, ctx: ToolContext) -> str:
    items = _inbox.list_items(
        status=args.get("status"),
        include_archived=bool(args.get("include_archived", False)),
        limit=args.get("limit", 100),
    )
    if not items:
        return "(inbox is empty)"
    return "\n".join(
        f"{item['id']}  [{item.get('status', 'new')}/{item.get('kind', 'item')}]  "
        f"{item['title']}" + (f" — {item['sender']}" if item.get("sender") else "")
        for item in items
    )


def t_inbox_update(args: dict, ctx: ToolContext) -> str:
    item_id = str(args.get("id", ""))
    if not item_id:
        return "ERROR: inbox_update requires 'id'"
    changes = {
        key: args[key]
        for key in ("title", "summary", "status", "priority", "tags", "snoozed_until")
        if key in args
    }
    item = _inbox.update_item(item_id, **changes)
    return f"OK: {item['id']} — {item['title']}" if item else f"ERROR: no inbox item {item_id}"


def t_inbox_delete(args: dict, ctx: ToolContext) -> str:
    item_id = str(args.get("id", ""))
    if not item_id:
        return "ERROR: inbox_delete requires 'id'"
    return "OK: deleted" if _inbox.delete_item(item_id) else f"ERROR: no inbox item {item_id}"


def t_email_connection_create(args: dict, ctx: ToolContext) -> str:
    if not args.get("name") or not args.get("host") or not args.get("username"):
        return "ERROR: email_connection_create requires name, host, and username"
    connection = _inbox.create_email_connection(
        args["name"],
        args["host"],
        args["username"],
        args.get("password", ""),
        port=args.get("port", 993),
        use_ssl=bool(args.get("use_ssl", True)),
        folder=args.get("folder", "INBOX"),
        auto_sync=bool(args.get("auto_sync", True)),
        sync_interval=args.get("sync_interval", 900),
    )
    return f"OK: {connection['id']} — {connection['name']}"


def t_email_connection_list(args: dict, ctx: ToolContext) -> str:
    connections = _inbox.list_email_connections()
    if not connections:
        return "(no email connections)"
    return "\n".join(
        f"{item['id']}  [{item['status']}]  {item['name']}  {item['detail']}"
        for item in connections
    )


def t_email_connection_sync(args: dict, ctx: ToolContext) -> str:
    connection_id = str(args.get("id", ""))
    if not connection_id:
        return "ERROR: email_connection_sync requires 'id'"
    result = _inbox.sync_email_connection(connection_id)
    if not result.get("ok"):
        return f"ERROR: {result.get('error', 'sync failed')}"
    return f"OK: imported {result['imported']}, updated {result['updated']}"


def t_email_connection_delete(args: dict, ctx: ToolContext) -> str:
    connection_id = str(args.get("id", ""))
    if not connection_id:
        return "ERROR: email_connection_delete requires 'id'"
    deleted = _inbox.delete_email_connection(
        connection_id, remove_items=bool(args.get("remove_items", False))
    )
    return "OK: deleted" if deleted else f"ERROR: no email connection {connection_id}"


def t_routine_create(args: dict, ctx: ToolContext) -> str:
    if not args.get("name"):
        return "ERROR: routine_create requires 'name'"
    routine = _routines.create_routine(
        args["name"],
        kind=args.get("kind", "daily_plan"),
        schedule_time=args.get("schedule_time", "08:00"),
        days=args.get("days"),
        prompt=args.get("prompt", ""),
        enabled=bool(args.get("enabled", True)),
    )
    return f"OK: {routine['id']} — {routine['name']} at {routine['schedule_time']}"


def t_routine_list(args: dict, ctx: ToolContext) -> str:
    items = _routines.list_routines(include_disabled=bool(args.get("include_disabled", True)))
    if not items:
        return "(no proactive routines)"
    return "\n".join(
        f"{item['id']}  [{'on' if item.get('enabled') else 'off'}]  "
        f"{item['schedule_time']}  {item['name']} ({item['kind']})"
        for item in items
    )


def t_routine_update(args: dict, ctx: ToolContext) -> str:
    routine_id = str(args.get("id", ""))
    if not routine_id:
        return "ERROR: routine_update requires 'id'"
    changes = {
        key: args[key]
        for key in ("name", "kind", "schedule_time", "days", "prompt", "enabled")
        if key in args
    }
    routine = _routines.update_routine(routine_id, **changes)
    return f"OK: {routine['id']} — {routine['name']}" if routine else f"ERROR: no routine {routine_id}"


def t_routine_delete(args: dict, ctx: ToolContext) -> str:
    routine_id = str(args.get("id", ""))
    if not routine_id:
        return "ERROR: routine_delete requires 'id'"
    return "OK: deleted" if _routines.delete_routine(routine_id) else f"ERROR: no routine {routine_id}"


# ============================================================================
# MCP — Model Context Protocol bridge
# ============================================================================


def _mcp_manager(ctx: ToolContext):
    """Lazy-init the MCPManager on the engine state."""
    state = getattr(ctx, "state", None)
    if state is None:
        return None
    if getattr(state, "mcp", None) is None:
        from .mcp_client import MCPManager

        engine = getattr(ctx, "engine", None)
        cfg = engine.config if engine is not None else {}
        state.mcp = MCPManager(cfg or {})
    return state.mcp


def t_mcp_list_servers(args: dict, ctx: ToolContext) -> str:
    mgr = _mcp_manager(ctx)
    if mgr is None:
        return "ERROR: MCP manager unavailable"
    names = mgr.names()
    if not names:
        return (
            "(no MCP servers configured — add one under mcp.servers in "
            "~/.config/cagentic/config.json, e.g. notion / gdrive / slack)"
        )
    return "\n".join(f"  - {n}" for n in names)


def t_mcp_list_tools(args: dict, ctx: ToolContext) -> str:
    mgr = _mcp_manager(ctx)
    if mgr is None:
        return "ERROR: MCP manager unavailable"
    server = args.get("server")
    if not server:
        return "ERROR: mcp_list_tools requires 'server'"
    try:
        tools = mgr.list_tools(server)
    except Exception as e:
        return f"ERROR: {e}"
    if not tools:
        return f"(server '{server}' exposes no tools)"
    lines = []
    for t in tools[:60]:
        n = t.get("name", "?")
        d = (t.get("description") or "").splitlines()[0][:160]
        lines.append(f"  - {n}  —  {d}")
    return "\n".join(lines)


def t_mcp_call(args: dict, ctx: ToolContext) -> str:
    mgr = _mcp_manager(ctx)
    if mgr is None:
        return "ERROR: MCP manager unavailable"
    server = args.get("server")
    name = args.get("tool") or args.get("name")
    arguments = args.get("arguments") or {}
    if not server or not name:
        return "ERROR: mcp_call requires 'server' and 'tool'"
    if not ctx.confirm("MCP call", f"{server}/{name}  {str(arguments)[:80]}"):
        return "ERROR: user denied"
    try:
        from .mcp_client import format_tool_result

        result = mgr.call_tool(server, name, arguments)
    except Exception as e:
        return f"ERROR: {e}"
    return _truncate(format_tool_result(result))


def t_mcp_list_resources(args: dict, ctx: ToolContext) -> str:
    mgr = _mcp_manager(ctx)
    if mgr is None:
        return "ERROR: MCP manager unavailable"
    server = args.get("server")
    if not server:
        return "ERROR: mcp_list_resources requires 'server'"
    try:
        items = mgr.list_resources(server)
    except Exception as e:
        return f"ERROR: {e}"
    if not items:
        return f"(server '{server}' exposes no resources)"
    lines = []
    for r in items[:60]:
        uri = r.get("uri", "?")
        name = r.get("name") or ""
        mime = r.get("mimeType") or ""
        lines.append(f"  - {uri}  {name}  [{mime}]".rstrip())
    return "\n".join(lines)


def t_mcp_read_resource(args: dict, ctx: ToolContext) -> str:
    mgr = _mcp_manager(ctx)
    if mgr is None:
        return "ERROR: MCP manager unavailable"
    server = args.get("server")
    uri = args.get("uri")
    if not server or not uri:
        return "ERROR: mcp_read_resource requires 'server' and 'uri'"
    try:
        res = mgr.read_resource(server, uri)
    except Exception as e:
        return f"ERROR: {e}"
    contents = res.get("contents") or []
    parts: list[str] = []
    for c in contents:
        if not isinstance(c, dict):
            continue
        if "text" in c:
            parts.append(c["text"])
        elif "blob" in c:
            parts.append(f"[binary blob: {len(c['blob'])} chars b64]")
    return _truncate("\n".join(parts) if parts else "(empty resource)")


def t_mcp_restart(args: dict, ctx: ToolContext) -> str:
    """Stop and re-spawn one configured MCP server. Handy after editing its
    environment or upgrading the server binary."""
    mgr = _mcp_manager(ctx)
    if mgr is None:
        return "ERROR: MCP manager unavailable"
    name = args.get("name") or args.get("server")
    if not name:
        return "ERROR: missing argument 'name'"
    if not ctx.confirm("restart MCP server", str(name)):
        return "ERROR: user denied"
    try:
        srv = mgr.get(str(name), start=False)
        srv.stop()
        srv.start()
    except Exception as e:
        return f"ERROR: {e}"
    return f"OK: restarted MCP server '{name}'"


# ============================================================================
# Browser — control Chrome through the companion extension
# ============================================================================


def _browser(ctx: ToolContext):
    """Get (lazily creating + starting) the BrowserBridge on the state."""
    state = getattr(ctx, "state", None)
    if state is None:
        return None
    if getattr(state, "browser", None) is None:
        from .browser import BrowserBridge

        engine = getattr(ctx, "engine", None)
        cfg = (engine.config if engine is not None else {}) or {}
        port = int((cfg.get("browser") or {}).get("port", 8765))
        bridge = BrowserBridge(port=port)
        bridge.start()
        state.browser = bridge
    return state.browser


def _browser_setup_hint(bridge) -> str:
    return (
        f"the Cagentic Chrome extension isn't connected (bridge listening on "
        f"port {bridge.port}). To connect it: open chrome://extensions, turn on "
        f"Developer mode, click 'Load unpacked', and select the 'extension/' "
        f"folder in the Cagentic repo. Run /browser for the exact path."
    )


def t_browser_status(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    if b.error:
        return f"ERROR: browser bridge could not start — {b.error}"
    if b.is_connected():
        return f"OK: the Chrome extension is connected (bridge on port {b.port})."
    return _browser_setup_hint(b)


def t_browser_tabs(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    r = b.send("tabs", {})
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    tabs = r.get("result") or []
    if not tabs:
        return "(no open browser tabs)"
    lines = []
    for t in tabs:
        mark = "*" if t.get("active") else " "
        title = (t.get("title") or "")[:60]
        lines.append(f"  [{mark}] tab {t.get('id')}  {title}  — {t.get('url', '')}")
    return _truncate("\n".join(lines))


def t_browser_read(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    r = b.send("read", {"tab_id": args.get("tab_id")})
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    res = r.get("result") or {}
    return _truncate(f"{res.get('title', '')}\n{res.get('url', '')}\n\n{res.get('text', '')}")


def t_browser_open(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    url = args.get("url")
    if not url:
        return "ERROR: browser_open requires 'url'"
    if not ctx.confirm("open a browser tab", url):
        return "ERROR: user denied"
    r = b.send("open", {"url": url, "active": args.get("active", True)})
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    res = r.get("result") or {}
    return f"OK: opened tab {res.get('id')} → {res.get('url', url)}"


def t_browser_navigate(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    url = args.get("url")
    if not url:
        return "ERROR: browser_navigate requires 'url'"
    if not ctx.confirm("navigate the browser", url):
        return "ERROR: user denied"
    r = b.send("navigate", {"url": url, "tab_id": args.get("tab_id")})
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    return f"OK: navigated to {url}"


def t_browser_click(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    selector = args.get("selector")
    text = args.get("text")
    if not selector and not text:
        return "ERROR: browser_click requires 'selector' or 'text'"
    target = selector or f"text:{text}"
    if not ctx.confirm("click in the browser", target):
        return "ERROR: user denied"
    r = b.send("click", {"selector": selector, "text": text, "tab_id": args.get("tab_id")})
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    res = r.get("result") or {}
    if not res.get("ok"):
        return f"ERROR: {res.get('error', 'click failed')}"
    return f"OK: clicked {res.get('clicked', target)}"


def t_browser_fill(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    selector = args.get("selector")
    value = args.get("value")
    if not selector or value is None:
        return "ERROR: browser_fill requires 'selector' and 'value'"
    if not ctx.confirm("fill a browser field", f"{selector} = {str(value)[:60]}"):
        return "ERROR: user denied"
    r = b.send("fill", {"selector": selector, "value": value, "tab_id": args.get("tab_id")})
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    res = r.get("result") or {}
    if not res.get("ok"):
        return f"ERROR: {res.get('error', 'fill failed')}"
    return f"OK: filled {selector}"


def t_browser_eval(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    code = args.get("code")
    if not code:
        return "ERROR: browser_eval requires 'code'"
    # Show the full script in the approval detail so a dangerous tail isn't
    # hidden behind a truncation; flag very long scripts explicitly.
    if len(code) > 2000:
        detail = f"[long script — {len(code)} chars]\n{code}"
    else:
        detail = code
    if not ctx.confirm("run JavaScript in the browser", detail):
        return "ERROR: user denied"
    r = b.send("eval", {"code": code, "tab_id": args.get("tab_id")})
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    res = r.get("result") or {}
    if not res.get("ok"):
        return f"ERROR: {res.get('error', 'eval failed')}"
    return _truncate(f"OK: {res.get('value', '')}")


def t_browser_close(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    tab_id = args.get("tab_id")
    if tab_id is None:
        return "ERROR: browser_close requires 'tab_id' (use browser_tabs to find it)"
    if not ctx.confirm("close a browser tab", f"tab {tab_id}"):
        return "ERROR: user denied"
    r = b.send("close", {"tab_id": tab_id})
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    return f"OK: closed tab {tab_id}"


def t_browser_scroll(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    to = args.get("to")
    y = args.get("y")
    selector = args.get("selector")
    target = selector or (f"y={y}" if y is not None else (to or "bottom"))
    if not ctx.confirm("scroll the page", target):
        return "ERROR: user denied"
    r = b.send(
        "scroll",
        {
            "to": to,
            "y": y,
            "selector": selector,
            "tab_id": args.get("tab_id"),
        },
    )
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    res = r.get("result") or {}
    if not res.get("ok"):
        return f"ERROR: {res.get('error', 'scroll failed')}"
    return f"OK: scrolled {res.get('scrolled', target)}"


def t_browser_screenshot(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    r = b.send("screenshot", {"tab_id": args.get("tab_id")}, timeout=15)
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    res = r.get("result") or {}
    img_b64 = res.get("data")
    if not img_b64:
        return "ERROR: screenshot returned no image data"
    w = res.get("width") or 0
    h = res.get("height") or 0
    # Hand the raw base64 to the engine so it can attach the image to the
    # tool result message — vision-capable Ollama models will see it.
    try:
        ctx.pending_images.append(img_b64)
    except Exception:
        logger.warning("browser_screenshot: could not queue image for vision", exc_info=True)
    # Also save a copy to disk so the user can open it directly.
    saved = ""
    try:
        import base64 as _b64
        import time as _t
        from pathlib import Path

        sdir = Path.home() / ".config" / "cagentic" / "screenshots"
        sdir.mkdir(parents=True, exist_ok=True)
        path = sdir / f"shot-{int(_t.time())}.png"
        path.write_bytes(_b64.b64decode(img_b64))
        saved = f"  ·  saved to {path}"
    except Exception:
        logger.warning("browser_screenshot: failed saving screenshot to disk", exc_info=True)
    return (
        f"OK: captured the viewport ({w}×{h}). The image is attached for "
        f"vision models — use browser_click_at with x,y in this coordinate "
        f"space (origin top-left).{saved}"
    )


def t_browser_links(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    r = b.send("links", {"tab_id": args.get("tab_id")}, timeout=15)
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    res = r.get("result") or {}
    links = res.get("links") or []
    if not links:
        return "(no links found on this page)"
    contains = (args.get("contains") or "").lower().strip()
    if contains:
        links = [
            ln
            for ln in links
            if contains in (ln.get("text", "").lower())
            or contains in (ln.get("aria", "").lower())
            or contains in (ln.get("href", "").lower())
        ]
    out = []
    for ln in links[:120]:
        label = (ln.get("text") or ln.get("aria") or "").strip()
        label = label.replace("\n", " ")
        out.append(f"  {label[:80]}  →  {ln.get('href', '')}")
    head = f"{len(links)} link(s){' matching ' + contains if contains else ''}:"
    return _truncate(head + "\n" + "\n".join(out))


def t_browser_download(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    url = args.get("url")
    if not url:
        return "ERROR: browser_download requires 'url'"
    if not ctx.confirm("download via browser", url[:80]):
        return "ERROR: user denied"
    r = b.send("download", {"url": url}, timeout=120)
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    res = r.get("result") or {}
    if not res.get("ok"):
        return f"ERROR: {res.get('error', 'download failed')}"
    data_b64 = res.get("data") or ""
    if not data_b64:
        return "ERROR: download returned no bytes"

    # Hard ceiling on the decoded blob. Base64 inflates by ~4/3, so a too-large
    # payload would otherwise be held fully in memory. Reject early using the
    # encoded length, then re-check the decoded length defensively.
    MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
    approx_decoded = (len(data_b64) * 3) // 4
    if approx_decoded > MAX_DOWNLOAD_BYTES:
        return (
            f"ERROR: download too large (~{approx_decoded:,} bytes, cap "
            f"{MAX_DOWNLOAD_BYTES:,}). Fetch a smaller resource."
        )

    import base64 as _b64

    try:
        raw = _b64.b64decode(data_b64)
    except Exception as e:
        logger.warning("browser_download: base64 decode failed", exc_info=True)
        return f"ERROR: bad base64 from extension: {e}"
    if len(raw) > MAX_DOWNLOAD_BYTES:
        return (
            f"ERROR: download too large ({len(raw):,} bytes, cap "
            f"{MAX_DOWNLOAD_BYTES:,}). Fetch a smaller resource."
        )

    # Decide on the destination path.
    out_path = args.get("path")
    if out_path:
        try:
            p = _resolve_contained(out_path, ctx.root)
        except PathEscapeError as exc:
            return f"ERROR: {exc}"
        p.parent.mkdir(parents=True, exist_ok=True)
    else:
        import re as _re
        import time as _t

        ddir = Path.home() / ".config" / "cagentic" / "downloads"
        ddir.mkdir(parents=True, exist_ok=True)
        ext = ""
        ct = (res.get("contentType") or "").lower()
        if "pdf" in ct:
            ext = ".pdf"
        elif "html" in ct:
            ext = ".html"
        elif "json" in ct:
            ext = ".json"
        elif "text/plain" in ct:
            ext = ".txt"
        elif "csv" in ct:
            ext = ".csv"
        elif "presentation" in ct or "powerpoint" in ct:
            ext = ".pptx"
        elif "wordprocessing" in ct or "msword" in ct:
            ext = ".docx"
        elif "png" in ct:
            ext = ".png"
        elif "jpeg" in ct or "jpg" in ct:
            ext = ".jpg"
        elif "zip" in ct:
            ext = ".zip"
        # Try to lift a name out of the URL.
        slug = _re.search(r"[?&]title=([^&]+)", url)
        name = slug.group(1) if slug else f"download-{int(_t.time())}"
        name = _re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:80]
        p = ddir / (name + ext)

    p.write_bytes(raw)
    sz = len(raw)
    return f"OK: saved {sz:,} bytes to {p}  (content-type: {res.get('contentType', 'unknown')})"


def t_browser_click_at(args: dict, ctx: ToolContext) -> str:
    b = _browser(ctx)
    if b is None:
        return "ERROR: browser bridge unavailable"
    x = args.get("x")
    y = args.get("y")
    if x is None or y is None:
        return (
            "ERROR: browser_click_at needs 'x' and 'y' — call "
            "browser_screenshot first to see where things are"
        )
    if not ctx.confirm("click at coordinates", f"({x}, {y})"):
        return "ERROR: user denied"
    r = b.send(
        "click_at",
        {
            "x": int(x),
            "y": int(y),
            "tab_id": args.get("tab_id"),
        },
    )
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    res = r.get("result") or {}
    if not res.get("ok"):
        return f"ERROR: {res.get('error', 'click failed')}"
    return f"OK: clicked {res.get('clicked', '')} at ({x}, {y})"


# ============================================================================
# Web — fetch + search
# ============================================================================


def _ip_is_blocked(ip: str) -> bool:
    """True if `ip` is loopback/link-local/private/reserved/multicast.

    Reusable SSRF guard: refuses 127/8, 10/8, 172.16/12, 192.168/16,
    169.254/16, ::1, fc00::/7, etc. so a URL (or a redirect hop) can't be
    used to reach internal services.
    """
    import ipaddress

    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        # Not a literal IP — treat as unsafe; we only ever pass resolved IPs.
        return True
    # ipaddress flags cover loopback, link-local, private, reserved,
    # multicast and the unspecified address (0.0.0.0 / ::).
    return (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _host_is_blocked(host: str) -> tuple[bool, str]:
    """Resolve `host` and report whether any resolved IP is in a blocked range.

    Returns (blocked, detail). Blocks when resolution fails or any address
    falls in a private/loopback/link-local/reserved range. We block if *any*
    resolved address is unsafe (DNS rebinding / multi-record defence).
    """
    import socket

    if not host:
        return True, "missing host"
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        logger.warning("web_fetch: DNS resolution failed for %r", host, exc_info=True)
        return True, f"could not resolve host {host!r}: {exc}"
    for info in infos:
        ip = info[4][0]
        if _ip_is_blocked(ip):
            return True, f"host {host!r} resolves to blocked address {ip}"
    return False, ""


def t_web_fetch(args: dict, ctx: ToolContext) -> str:
    from urllib.parse import urljoin, urlparse

    import requests

    url = args["url"]
    if not url.startswith(("http://", "https://")):
        return "ERROR: url must be http:// or https://"
    timeout = int(args.get("timeout", 20))
    max_bytes = int(args.get("max_bytes", 200_000))
    headers = {"User-Agent": "cagentic/0.1"}
    max_redirects = 5

    # Disable automatic redirect following so we can re-validate every hop
    # against the SSRF guard (a public URL can 30x to a private one).
    current = url
    r = None
    for _ in range(max_redirects + 1):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https"):
            return f"ERROR: refusing non-http(s) redirect target: {current}"
        blocked, detail = _host_is_blocked(parsed.hostname or "")
        if blocked:
            return f"ERROR: refusing to fetch internal/blocked address ({detail})"
        try:
            r = requests.get(
                current,
                timeout=timeout,
                headers=headers,
                verify=not ctx.insecure_ssl,
                stream=True,
                allow_redirects=False,
            )
        except requests.RequestException as e:
            logger.warning("web_fetch: request to %r failed", current, exc_info=True)
            return f"ERROR: fetch failed: {e}"
        if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
            location = r.headers.get("Location")
            try:
                r.close()
            except Exception:
                logger.warning("web_fetch: failed closing redirect response", exc_info=True)
            if not location:
                return f"ERROR: redirect with no Location header from {current}"
            current = urljoin(current, location)
            continue
        break
    else:
        return f"ERROR: too many redirects (>{max_redirects})"

    if r is None:
        return "ERROR: fetch failed: no response"

    chunks: list[bytes] = []
    seen = 0
    try:
        for chunk in r.iter_content(8192):
            if not chunk:
                continue
            # Hard total ceiling: never keep more than max_bytes overall.
            remaining = max_bytes - seen
            if remaining <= 0:
                break
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            chunks.append(chunk)
            seen += len(chunk)
            if seen >= max_bytes:
                break
    finally:
        # Close in finally so a connection drop mid-stream (iter_content
        # raising) still releases the socket instead of leaking it.
        try:
            r.close()
        except Exception:
            logger.warning("web_fetch: failed closing response", exc_info=True)
    raw = b"".join(chunks)
    try:
        body = raw.decode(r.encoding or "utf-8", errors="replace")
    except Exception:
        logger.warning("web_fetch: decode with declared encoding failed", exc_info=True)
        body = raw.decode("utf-8", errors="replace")
    # Optional: strip HTML tags for readability.
    if args.get("text_only") and ("<html" in body.lower() or "<body" in body.lower()):
        body = _strip_html(body)
    return _truncate(f"HTTP {r.status_code}  {current}\n{body}")


_HTML_TAG_RX = re.compile(r"<[^>]+>")
_HTML_SCRIPT_RX = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_WS_RX = re.compile(r"\n[ \t]*\n[ \t]*\n+")

# Cap the input handed to backtracking regexes. Adversarial/huge HTML can make
# the DOTALL script/style pattern backtrack pathologically; only scan a bounded
# prefix so a hostile page can't hang the process.
_HTML_SCAN_CAP = 512 * 1024  # 512 KB


def _strip_html(html: str) -> str:
    if len(html) > _HTML_SCAN_CAP:
        html = html[:_HTML_SCAN_CAP]
    text = _HTML_SCRIPT_RX.sub("", html)
    text = _HTML_TAG_RX.sub("", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = _HTML_WS_RX.sub("\n\n", text)
    return text.strip()


def t_web_search(args: dict, ctx: ToolContext) -> str:
    """DuckDuckGo HTML-frontend scrape (no API key needed)."""
    import requests

    q = args["query"]
    n = int(args.get("limit", 10))
    try:
        r = requests.get(
            "https://duckduckgo.com/html/",
            params={"q": q},
            headers={"User-Agent": "Mozilla/5.0 cagentic/0.1"},
            timeout=15,
            verify=not ctx.insecure_ssl,
        )
    except requests.RequestException as e:
        return f"ERROR: search failed: {e}"
    if r.status_code != 200:
        return f"ERROR: HTTP {r.status_code}"
    # Bound the HTML we run the (DOTALL, backtracking) result regex over so a
    # huge/adversarial response can't trigger pathological backtracking.
    html = r.text
    if len(html) > _HTML_SCAN_CAP:
        html = html[:_HTML_SCAN_CAP]
    rx = re.compile(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL
    )
    items = rx.findall(html)
    out: list[str] = []
    for href, title in items[:n]:
        title_text = re.sub(r"<[^>]+>", "", title).strip()
        out.append(f"- {title_text}\n  {href}")
    return _truncate("\n".join(out) if out else "(no results)")


# ============================================================================
# Shell — run_bash (with confirmation) and async variant
# ============================================================================

_ERR_LOC_PATTERNS = [
    re.compile(r'File "([^"]+)", line (\d+)'),
    re.compile(r"-->\s+([^\s:]+):(\d+):\d+"),
    re.compile(r"\(([^()\s]+):(\d+):\d+\)"),
    re.compile(r"([\w./\\+-]+\.\w+):(\d+):\d+"),
    re.compile(r"([\w./\\+-]+\.\w+):(\d+)\b"),
]
_ERR_MSG_RX = re.compile(r"^\s*([A-Z]\w*(?:Error|Exception|Warning|Fault)): ?(.*)$", re.M)


def _analyze_failure(stdout: str, stderr: str) -> str:
    blob = (stderr or "") + "\n" + (stdout or "")
    hints: list[str] = []
    msgs = _ERR_MSG_RX.findall(blob)
    if msgs:
        kind, detail = msgs[-1]
        hints.append(f"{kind}: {detail.strip()[:200]}")
    loc = None
    for pat in _ERR_LOC_PATTERNS:
        found = pat.findall(blob)
        if found:
            loc = found[-1]
            break
    if loc:
        hints.append(f"likely at {loc[0]}:{loc[1]}")
    return ("  ↳ " + "  ·  ".join(hints)) if hints else ""


def _shell_run_invocation(cmd: str):
    """Return (args, shell) for subprocess.run to execute `cmd` under a shell.

    On POSIX, shell=True uses /bin/sh. On Windows, shell=True uses cmd.exe,
    which rejects the Unix shell syntax the model often emits (&&, $VAR,
    pipes, redirects) — so a command like `grep foo file | head` fails. When
    a POSIX shell (bash from Git Bash, or sh) is on PATH, route through it so
    those commands work; otherwise fall back to cmd.exe (basic commands still
    run, Unix syntax won't).
    """
    if os.name == "nt":
        posix_shell = shutil.which("bash") or shutil.which("sh")
        if posix_shell:
            return ([posix_shell, "-c", cmd], False)
        return (["cmd", "/c", cmd], False)
    return (cmd, True)


def t_run_bash(args: dict, ctx: ToolContext) -> str:
    cmd = args["command"]
    timeout = int(args.get("timeout", 60))
    if not ctx.confirm("shell command", cmd):
        return "ERROR: user denied command"
    run_cmd, use_shell = _shell_run_invocation(cmd)
    try:
        with ui.Spinner(f"running: {cmd[:40] + ('…' if len(cmd) > 40 else '')}"):
            proc = subprocess.run(
                run_cmd,
                shell=use_shell,
                cwd=str(ctx.root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        return f"ERROR: timed out after {timeout}s"
    status = "PASS" if proc.returncode == 0 else "FAIL"
    parts = [f"{status} (exit code {proc.returncode})"]
    if proc.stdout:
        parts.append(f"--- stdout ---\n{proc.stdout}")
    if proc.stderr:
        parts.append(f"--- stderr ---\n{proc.stderr}")
    if proc.returncode != 0:
        hint = _analyze_failure(proc.stdout, proc.stderr)
        if hint:
            parts.append(hint)
    return _truncate("\n".join(parts))


def t_bash_async(args: dict, ctx: ToolContext) -> str:
    bg = getattr(ctx, "background", None)
    if bg is None:
        return "ERROR: background executor not available"
    cmd = args["command"]
    timeout = int(args.get("timeout", 600))
    if not ctx.confirm("background shell command", cmd):
        return "ERROR: user denied command"
    job_id = bg.submit_bash(cmd, ctx.root, timeout=timeout)
    return f"OK: queued {job_id}  (poll with task_status / task_wait)"


# ============================================================================
# Tasks (light, kept for background-job tracking)
# ============================================================================


def _tasks(ctx: ToolContext):
    return getattr(ctx, "tasks", None)


def t_task_get(args: dict, ctx: ToolContext) -> str:
    tg = _tasks(ctx)
    if tg is None:
        return "ERROR: task graph not available"
    task = tg.get(args["id"])
    if not task:
        return f"ERROR: no task with id {args['id']}"
    import json as _json

    return _json.dumps(task.to_dict(), indent=2)


def t_task_list(args: dict, ctx: ToolContext) -> str:
    tg = _tasks(ctx)
    if tg is None:
        return "ERROR: task graph not available"
    tasks = tg.list(status=args.get("status"))
    if not tasks:
        return "(no tasks)"
    return "\n".join(t.short() for t in tasks[:60])


def t_task_status(args: dict, ctx: ToolContext) -> str:
    bg = getattr(ctx, "background", None)
    if bg is None:
        return "ERROR: background executor not available"
    job = bg.status(args["task_id"])
    if not job:
        return f"ERROR: no background job {args['task_id']}"
    summary = job.result.splitlines()[0][:160] if job.result else ""
    return f"{job.id}  status={job.status}  kind={job.kind}  label={job.label[:80]}\n{summary}"


def t_task_wait(args: dict, ctx: ToolContext) -> str:
    bg = getattr(ctx, "background", None)
    if bg is None:
        return "ERROR: background executor not available"
    timeout = float(args.get("timeout", 60))
    job = bg.wait(args["task_id"], timeout=timeout)
    if not job:
        return f"ERROR: no background job {args['task_id']}"
    if job.status == "running":
        return f"still running after {timeout}s"
    return _truncate(f"{job.id} finished {job.status}\n\n{job.result}")


def t_task_output(args: dict, ctx: ToolContext) -> str:
    bg = getattr(ctx, "background", None)
    tasks = getattr(ctx, "tasks", None)
    tid = args["id"]
    if bg is not None:
        job = bg.status(tid)
        if job:
            return _truncate(f"[bg {job.id} status={job.status}]\n{job.result}")
    if tasks is not None:
        t = tasks.get(tid)
        if t:
            return _truncate(f"[task {t.id} status={t.status}]\n{t.result}")
    return f"ERROR: no task/job with id {tid}"


# ============================================================================
# Interaction, planning, todo, config, sleep
# ============================================================================


def t_ask_user_question(args: dict, ctx: ToolContext) -> str:
    question = args.get("question") or args.get("prompt") or args.get("q")
    if not question:
        return "ERROR: missing argument 'question'"
    options = args.get("options") or []

    # This prompt reads the terminal's stdin, which only belongs to the REPL.
    # The web gateway runs its own engine in this same process, on an HTTP
    # worker thread: calling input() there either steals the line the REPL user
    # is typing or blocks the request forever. The terminal resolver is what
    # marks an engine as the one that owns the tty, so anything else gets told
    # to ask in its reply instead.
    engine = getattr(ctx, "engine", None)
    if engine is not None:
        from .permissions import terminal_resolver
        if getattr(engine, "permission_resolver", None) is not terminal_resolver:
            opts = ("  Options: " + " / ".join(str(o) for o in options)) if options else ""
            return (
                "ERROR: no terminal to prompt on. Ask the user directly in your "
                f"reply and wait for their answer.\n  Question: {question}{opts}"
            )

    # Yolo mode skips APPROVAL prompts — but asking the user a question
    # is a separate channel (the model needs information, not permission),
    # so it's allowed regardless. EOFError still handles non-interactive runs.
    ui.stop_all_spinners()
    import sys as _sys

    if _sys.stdout.isatty():
        _sys.stdout.write("\033[?25h")
        _sys.stdout.flush()
    print()
    ui.warn("? " + question)
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    try:
        ans = input("  > ").strip()
    except EOFError:
        return "ERROR: no tty"
    if options and ans.isdigit():
        idx = int(ans) - 1
        if 0 <= idx < len(options):
            return options[idx]
    return ans or "(empty)"


def t_enter_plan_mode(args: dict, ctx: ToolContext) -> str:
    state = getattr(ctx, "state", None)
    if state is None:
        return "ERROR: state not available"
    state.update(plan_mode=True)
    engine = getattr(ctx, "engine", None)
    if engine is not None:
        try:
            engine.refresh_system_prompt()
        except Exception:
            logger.warning("enter_plan_mode: refresh_system_prompt failed", exc_info=True)
    return "OK: PLAN MODE entered. Mutating tools blocked. Use exit_plan_mode to resume."


def t_exit_plan_mode(args: dict, ctx: ToolContext) -> str:
    state = getattr(ctx, "state", None)
    if state is None:
        return "ERROR: state not available"
    state.update(plan_mode=False)
    engine = getattr(ctx, "engine", None)
    if engine is not None:
        try:
            engine.refresh_system_prompt()
        except Exception:
            logger.warning("exit_plan_mode: refresh_system_prompt failed", exc_info=True)
    return "OK: plan mode OFF."


def t_todo_write(args: dict, ctx: ToolContext) -> str:
    state = getattr(ctx, "state", None)
    if state is None:
        return "ERROR: state not available"
    items = args.get("items")
    if not isinstance(items, list):
        return "ERROR: items must be a list of {text, status?}"
    todos: list[dict] = []
    for it in items:
        if isinstance(it, str):
            todos.append({"text": it, "status": "pending"})
        elif isinstance(it, dict) and "text" in it:
            # `get("status", "pending")` only defaults when the key is absent;
            # the model can send "status": "" or null, which would then crash
            # t['status'][0] below. Coerce falsy to "pending".
            status = it.get("status") or "pending"
            todos.append({"text": it["text"], "status": status})
    state.update(todos=todos)
    out = "\n".join(f"  [{t['status'][0]}] {t['text']}" for t in todos)
    return f"OK: {len(todos)} todo(s):\n{out}"


def t_tool_search(args: dict, ctx: ToolContext) -> str:
    from .github import GITHUB_TOOL_SCHEMAS
    q = (args.get("query") or "").lower().strip()
    # Search every schema, GitHub's included — searching only TOOL_SCHEMAS made
    # the gh_* tools undiscoverable through the very tool meant to find them.
    out: list[str] = []
    for s in TOOL_SCHEMAS + GITHUB_TOOL_SCHEMAS:
        fn = s.get("function") or {}
        name = fn.get("name", "")
        desc = fn.get("description", "")
        hay = f"{name} {desc}".lower()
        if not q or q in hay:
            out.append(f"{name}  —  {desc.splitlines()[0][:140] if desc else ''}")
    if not out:
        return f"(no tool matches {q!r}) — call tool_search with no query to list them all"
    return _truncate("\n".join(out))


def t_config_get(args: dict, ctx: ToolContext) -> str:
    engine = getattr(ctx, "engine", None)
    if engine is None or engine.config is None:
        return "ERROR: config not available"
    from .config import get_value

    key = args["key"]
    v = get_value(engine.config, key, None)
    if v is None:
        return f"(unset: {key})"
    # Redact obvious secrets
    if "token" in key.lower() or "secret" in key.lower() or "key" in key.lower():
        s = str(v)
        v = s[:4] + "…" + s[-4:] if len(s) > 8 else "••••"
    return f"{key} = {v}"


# Keys the model is allowed to set via the config_set tool. Anything that
# affects security posture (insecure_ssl), networking (ports/hosts), secrets
# (tokens/keys), or process execution (MCP commands) is deliberately excluded —
# those must be edited in the config file by the user directly.
_CONFIG_SET_ALLOWLIST = frozenset(
    {
        "model",
        "temperature",
        "max_tokens",
        "system_prompt",
        "theme",
        "editor",
        "default_workspace",
        "yolo",
        "auto_continue",
    }
)


def t_config_set(args: dict, ctx: ToolContext) -> str:
    engine = getattr(ctx, "engine", None)
    if engine is None or engine.config is None:
        return "ERROR: config not available"
    from .config import save, set_value

    key = args["key"]
    val = args["value"]
    if key not in _CONFIG_SET_ALLOWLIST:
        return (
            f"ERROR: config_set refuses to write '{key}'. Only user-facing keys "
            f"are settable here ({', '.join(sorted(_CONFIG_SET_ALLOWLIST))}). "
            f"Security/networking/secret/MCP keys must be edited in the config "
            f"file manually."
        )
    if not ctx.confirm("config set", f"{key} = {val}"):
        return "ERROR: user denied"
    set_value(engine.config, key, val)
    save(engine.config)
    return f"OK: {key} = {val} (saved)"


def t_sleep(args: dict, ctx: ToolContext) -> str:
    import time as _time

    secs = float(args.get("seconds", 1))
    secs = max(0.0, min(60.0, secs))
    _time.sleep(secs)
    return f"OK: slept {secs}s"


def t_skill(args: dict, ctx: ToolContext) -> str:
    """Append a named skill's instructions onto the engine for the rest of
    the session. Skills live at ~/.config/cagentic/skills/<name>.md."""
    from .config import config_dir

    op = args.get("op", "use")
    name = args.get("name", "")
    skills_dir = config_dir() / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    if op == "list":
        files = sorted(skills_dir.glob("*"))
        if not files:
            return "(no skills installed; drop *.md files in " + str(skills_dir) + ")"
        return "\n".join(f"- {f.stem}  ({f.stat().st_size} bytes)" for f in files)

    if not name:
        return "ERROR: skill name required"
    candidate = None
    for ext in (".md", ".txt", ""):
        c = skills_dir / f"{name}{ext}"
        if c.exists():
            candidate = c
            break
    if op == "get":
        if not candidate:
            return f"ERROR: no skill '{name}'"
        return _truncate(candidate.read_text(errors="replace"))
    if op == "use":
        if not candidate:
            return f"ERROR: no skill '{name}'"
        engine = getattr(ctx, "engine", None)
        if engine is None:
            return "ERROR: engine not available"
        body = candidate.read_text(errors="replace")
        if not engine.messages or engine.messages[0].get("role") != "system":
            # Nothing to attach to — reporting OK here made an attach that
            # never happened look successful.
            return "ERROR: no system prompt to attach the skill to"
        marker = f"=== SKILL: {name} ==="
        if marker in (engine.messages[0].get("content") or ""):
            return f"OK: skill '{name}' is already attached"
        engine.messages[0]["content"] += f"\n\n{marker}\n{body}"
        return f"OK: skill '{name}' attached ({len(body)} chars)"
    return f"ERROR: unknown op '{op}'"


# ============================================================================
# Registry + schemas
# ============================================================================

ToolFn = Callable[[dict, ToolContext], str]

TOOLS: dict[str, ToolFn] = {
    # files
    "read_file": t_read_file,
    "write_file": t_write_file,
    "edit_file": t_edit_file,
    "replace_lines": t_replace_lines,
    "list_dir": t_list_dir,
    "grep": t_grep,
    "glob": t_glob,
    "set_workspace": t_set_workspace,
    # notes
    "note_write": t_note_write,
    "note_get": t_note_get,
    "note_list": t_note_list,
    "note_search": t_note_search,
    "note_delete": t_note_delete,
    # past conversations
    "chat_search": t_chat_search,
    "chat_get": t_chat_get,
    # reminders
    "reminder_add": t_reminder_add,
    "reminder_list": t_reminder_list,
    "reminder_done": t_reminder_done,
    "reminder_delete": t_reminder_delete,
    "reminder_update": t_reminder_update,
    # personal OS
    "goal_create": t_goal_create,
    "goal_list": t_goal_list,
    "goal_update": t_goal_update,
    "goal_delete": t_goal_delete,
    "calendar_event_add": t_calendar_event_add,
    "calendar_event_list": t_calendar_event_list,
    "calendar_event_update": t_calendar_event_update,
    "calendar_event_delete": t_calendar_event_delete,
    "personal_briefing": t_personal_briefing,
    "calendar_connection_create": t_calendar_connection_create,
    "calendar_connection_list": t_calendar_connection_list,
    "calendar_connection_sync": t_calendar_connection_sync,
    "calendar_connection_delete": t_calendar_connection_delete,
    "notification_list": t_notification_list,
    "notification_update": t_notification_update,
    "inbox_capture": t_inbox_capture,
    "inbox_list": t_inbox_list,
    "inbox_update": t_inbox_update,
    "inbox_delete": t_inbox_delete,
    "email_connection_create": t_email_connection_create,
    "email_connection_list": t_email_connection_list,
    "email_connection_sync": t_email_connection_sync,
    "email_connection_delete": t_email_connection_delete,
    "routine_create": t_routine_create,
    "routine_list": t_routine_list,
    "routine_update": t_routine_update,
    "routine_delete": t_routine_delete,
    # mcp
    "mcp_list_servers": t_mcp_list_servers,
    "mcp_list_tools": t_mcp_list_tools,
    "mcp_call": t_mcp_call,
    "mcp_list_resources": t_mcp_list_resources,
    "mcp_read_resource": t_mcp_read_resource,
    "mcp_restart": t_mcp_restart,
    # browser
    "browser_status": t_browser_status,
    "browser_tabs": t_browser_tabs,
    "browser_read": t_browser_read,
    "browser_open": t_browser_open,
    "browser_navigate": t_browser_navigate,
    "browser_click": t_browser_click,
    "browser_fill": t_browser_fill,
    "browser_eval": t_browser_eval,
    "browser_scroll": t_browser_scroll,
    "browser_screenshot": t_browser_screenshot,
    "browser_click_at": t_browser_click_at,
    "browser_links": t_browser_links,
    "browser_download": t_browser_download,
    "browser_close": t_browser_close,
    # web
    "web_fetch": t_web_fetch,
    "web_search": t_web_search,
    # shell
    "run_bash": t_run_bash,
    "bash_async": t_bash_async,
    # tasks (light)
    "task_get": t_task_get,
    "task_list": t_task_list,
    "task_status": t_task_status,
    "task_wait": t_task_wait,
    "task_output": t_task_output,
    # interaction / planning / system
    "ask_user_question": t_ask_user_question,
    "enter_plan_mode": t_enter_plan_mode,
    "exit_plan_mode": t_exit_plan_mode,
    "todo_write": t_todo_write,
    "tool_search": t_tool_search,
    "config_get": t_config_get,
    "config_set": t_config_set,
    "sleep": t_sleep,
    "skill": t_skill,
}


TOOL_SCHEMAS: list[dict[str, Any]] = [
    # ---------- files ----------
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file, or extract the text from a PDF or Word (.docx) document. Returns line-numbered content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given content. Asks for approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact string in a file. old_string must be unique unless replace_all=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_lines",
            "description": "Surgical line-range replacement (1-indexed, inclusive). Use when edit_file fails on string matching.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "new_content": {"type": "string"},
                },
                "required": ["path", "start_line", "end_line", "new_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List entries in a directory (skips dotfiles).",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Recursive regex search. Skips .git, node_modules, build dirs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "case_insensitive": {"type": "boolean"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "File pattern matching (supports ** for recursive globs).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_workspace",
            "description": "Change the workspace directory used to resolve relative paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "create": {"type": "boolean"},
                },
                "required": ["path"],
            },
        },
    },
    # ---------- notes ----------
    {
        "type": "function",
        "function": {
            "name": "note_write",
            "description": "Save or update a markdown note in the assistant's knowledge base. Use for facts the user wants you to remember across sessions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short name like 'home-wifi' or 'travel-prefs'",
                    },
                    "body": {"type": "string"},
                    "append": {
                        "type": "boolean",
                        "description": "Prepend a dated entry instead of overwriting",
                    },
                },
                "required": ["name", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "note_get",
            "description": "Read a saved note by name.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "note_list",
            "description": "List all saved notes (most recently updated first).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "note_search",
            "description": "Substring search across saved notes.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "note_delete",
            "description": "Delete a saved note. Asks for approval.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chat_search",
            "description": "Search the user's previous conversations for a phrase. Returns matching chats with snippets and ids.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chat_get",
            "description": "Read one previous conversation in full, by id from chat_search.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    # ---------- reminders ----------
    {
        "type": "function",
        "function": {
            "name": "reminder_add",
            "description": "Add a persistent reminder. 'when' accepts 'in 10m', 'in 2h', 'tomorrow', 'tonight', or YYYY-MM-DD[ HH:MM].",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "when": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminder_list",
            "description": "List active reminders (use include_done=true for all).",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_done": {"type": "boolean"},
                    "status": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminder_done",
            "description": "Mark a reminder done by id.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminder_delete",
            "description": "Delete a reminder by id. Asks for approval.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminder_update",
            "description": "Update a reminder's text, status, when, or tags by id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "status": {"type": "string"},
                    "when": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id"],
            },
        },
    },
    # ---------- personal OS ----------
    {
        "type": "function",
        "function": {
            "name": "goal_create",
            "description": "Create a persistent personal goal with optional target date and progress.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "target": {"type": "string"},
                    "category": {"type": "string"},
                    "progress": {"type": "integer", "minimum": 0, "maximum": 100},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_list",
            "description": "List the user's persistent goals and progress.",
            "parameters": {
                "type": "object",
                "properties": {"include_completed": {"type": "boolean"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_update",
            "description": "Update a goal's title, target, progress, category, or status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "target": {"type": "string"},
                    "category": {"type": "string"},
                    "progress": {"type": "integer", "minimum": 0, "maximum": 100},
                    "status": {
                        "type": "string",
                        "enum": ["active", "paused", "completed", "cancelled"],
                    },
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_delete",
            "description": "Delete a personal goal by id.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_event_add",
            "description": "Add an event to the local personal calendar. start/end accept ISO date-times or natural reminder dates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                    "all_day": {"type": "boolean"},
                    "source": {"type": "string"},
                    "external_id": {"type": "string"},
                },
                "required": ["title", "start"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_event_list",
            "description": "List calendar events in an optional start/end window.",
            "parameters": {
                "type": "object",
                "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_event_update",
            "description": "Update a calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                    "all_day": {"type": "boolean"},
                    "status": {"type": "string"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_event_delete",
            "description": "Delete a calendar event by id.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "personal_briefing",
            "description": "Read today's proactive briefing across calendar events, deadlines, and goals.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_connection_create",
            "description": "Connect an iCalendar feed or CalDAV calendar. Credentials remain local. Ask the user for approval before creating it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["ical", "caldav"]},
                    "url": {"type": "string"},
                    "username": {"type": "string"},
                    "password": {"type": "string"},
                    "direction": {"type": "string", "enum": ["pull", "push", "both"]},
                    "auto_sync": {"type": "boolean"},
                    "sync_interval": {"type": "integer"},
                },
                "required": ["name", "url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_connection_list",
            "description": "List calendar connections and their last sync state without exposing credentials.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_connection_sync",
            "description": "Synchronize one calendar connection now.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_connection_delete",
            "description": "Delete a calendar connection, optionally removing its imported events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "remove_imported_events": {"type": "boolean"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notification_list",
            "description": "Read proactive personal-OS notifications.",
            "parameters": {
                "type": "object",
                "properties": {"include_dismissed": {"type": "boolean"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notification_update",
            "description": "Mark a proactive notification read, unread, dismissed, or mark all read.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "action": {"type": "string", "enum": ["read", "dismiss", "read_all"]},
                    "read": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inbox_capture",
            "description": "Capture a thought, task, message, or document reference in the local unified inbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "kind": {"type": "string", "enum": ["capture", "task", "message", "document"]},
                    "priority": {"type": "integer", "minimum": 0, "maximum": 3},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inbox_list",
            "description": "List active items in the local unified inbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["new", "read", "done", "archived"]},
                    "include_archived": {"type": "boolean"},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inbox_update",
            "description": "Update an inbox item's content, priority, status, tags, or snooze time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "status": {"type": "string", "enum": ["new", "read", "done", "archived"]},
                    "priority": {"type": "integer", "minimum": 0, "maximum": 3},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "snoozed_until": {"type": "number"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inbox_delete",
            "description": "Permanently delete a unified inbox item.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_connection_create",
            "description": "Connect an email inbox over IMAP. Credentials remain local and sync fetches headers only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "host": {"type": "string"},
                    "port": {"type": "integer"},
                    "username": {"type": "string"},
                    "password": {"type": "string"},
                    "use_ssl": {"type": "boolean"},
                    "folder": {"type": "string"},
                    "auto_sync": {"type": "boolean"},
                    "sync_interval": {"type": "integer"},
                },
                "required": ["name", "host", "username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_connection_list",
            "description": "List email connections and sync state without exposing credentials.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_connection_sync",
            "description": "Synchronize unread email headers from one IMAP connection now.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_connection_delete",
            "description": "Delete an email connection, optionally removing its imported inbox items.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "remove_items": {"type": "boolean"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "routine_create",
            "description": "Create a scheduled proactive personal-OS routine.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["daily_plan", "inbox_digest", "weekly_review", "custom"]},
                    "schedule_time": {"type": "string", "description": "Local 24-hour HH:MM"},
                    "days": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}},
                    "prompt": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "routine_list",
            "description": "List configured proactive routines and schedules.",
            "parameters": {
                "type": "object",
                "properties": {"include_disabled": {"type": "boolean"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "routine_update",
            "description": "Update a proactive routine's schedule, prompt, type, or enabled state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["daily_plan", "inbox_digest", "weekly_review", "custom"]},
                    "schedule_time": {"type": "string"},
                    "days": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}},
                    "prompt": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "routine_delete",
            "description": "Delete a proactive routine.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    # ---------- mcp ----------
    {
        "type": "function",
        "function": {
            "name": "mcp_list_servers",
            "description": "List configured MCP servers (Notion, Google Drive, Slack, etc.).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_list_tools",
            "description": "List the tools an MCP server exposes.",
            "parameters": {
                "type": "object",
                "properties": {"server": {"type": "string"}},
                "required": ["server"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_call",
            "description": "Call a tool on an MCP server. Asks for approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {"type": "string"},
                    "tool": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["server", "tool"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_list_resources",
            "description": "List URI-addressable resources exposed by an MCP server.",
            "parameters": {
                "type": "object",
                "properties": {"server": {"type": "string"}},
                "required": ["server"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_read_resource",
            "description": "Read a resource by URI from an MCP server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {"type": "string"},
                    "uri": {"type": "string"},
                },
                "required": ["server", "uri"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_restart",
            "description": "Stop and respawn one configured MCP server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    # ---------- browser ----------
    {
        "type": "function",
        "function": {
            "name": "browser_status",
            "description": "Check whether the Cagentic Chrome extension is connected. Call this before other browser_* tools.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_tabs",
            "description": "List the open browser tabs (id, title, url, which is active).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_read",
            "description": "Read the title, URL and visible text of a browser tab (the active tab if tab_id is omitted).",
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_open",
            "description": "Open a URL in a new browser tab. Asks for approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "active": {"type": "boolean"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Navigate a tab to a URL (active tab if tab_id omitted). Asks for approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "tab_id": {"type": "integer"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element in a tab — by CSS 'selector' or by visible 'text'. Asks for approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                    "tab_id": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_fill",
            "description": "Set the value of a form field matched by CSS selector. Asks for approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                    "tab_id": {"type": "integer"},
                },
                "required": ["selector", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_eval",
            "description": "Run a JavaScript expression in a browser tab and return its result. Prefer the dedicated browser_scroll, browser_click, and browser_fill for those actions — pages with strict Content Security Policy reject eval. Asks for approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "tab_id": {"type": "integer"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_scroll",
            "description": "Scroll a browser tab. Set 'to' to 'top' or 'bottom', OR pass a 'selector' to scroll an element into view, OR pass a numeric 'y' pixel offset. CSP-safe — works on every page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "enum": ["top", "bottom"]},
                    "selector": {"type": "string"},
                    "y": {"type": "integer"},
                    "tab_id": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Capture the visible viewport of the current browser tab as a PNG. Vision-capable models receive the image inline (attached to the tool result); a copy is also saved to ~/.config/cagentic/screenshots/. Use this with browser_click_at when CSS selectors aren't enough.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click_at",
            "description": "Click at exact viewport coordinates (x, y) — the fallback when browser_click can't find the element by CSS. Coordinates are in the same space as the most recent browser_screenshot (origin top-left, pixels). Bypasses CSP and works with framework-rendered apps. Asks for approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "tab_id": {"type": "integer"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_links",
            "description": "List every clickable thing on the page — text, href, aria-label — without using eval. Use this on dynamically-rendered pages (Google Drive, Classroom, React apps) where browser_read returns very little. Pass 'contains' to filter to matching links.",
            "parameters": {
                "type": "object",
                "properties": {
                    "contains": {
                        "type": "string",
                        "description": "Case-insensitive substring filter.",
                    },
                    "tab_id": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_download",
            "description": "Download a URL through the browser session and save the bytes to disk. Authenticated as the user (their cookies are sent), so it works with Google Drive / Docs export URLs and any other login-walled file. Saves to ~/.config/cagentic/downloads/ unless 'path' is given; returns the local file path so you can read_file it. Asks for approval. Google export URL patterns:  https://docs.google.com/document/d/<ID>/export?format=txt  ·  https://docs.google.com/presentation/d/<ID>/export/txt  ·  https://docs.google.com/presentation/d/<ID>/export/pdf  ·  https://drive.google.com/uc?export=download&id=<ID>",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "path": {
                        "type": "string",
                        "description": "Optional destination path; defaults to ~/.config/cagentic/downloads/.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_close",
            "description": "Close a browser tab by id. Asks for approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "integer"},
                },
                "required": ["tab_id"],
            },
        },
    },
    # ---------- web ----------
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a URL and return the body. Pass text_only=true to strip HTML for readability.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "timeout": {"type": "integer"},
                    "max_bytes": {"type": "integer"},
                    "text_only": {"type": "boolean"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web (DuckDuckGo HTML frontend). Returns title + URL pairs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
    },
    # ---------- shell ----------
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Run a shell command in the workspace. Requires user approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_async",
            "description": "Run a shell command in the background. Returns a job id; poll with task_status / task_wait.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            },
        },
    },
    # ---------- tasks ----------
    {
        "type": "function",
        "function": {
            "name": "task_get",
            "description": "Get one task by id.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": "List tasks, optionally filtered by status.",
            "parameters": {"type": "object", "properties": {"status": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_status",
            "description": "Check the status of a background job.",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_wait",
            "description": "Block until a background job finishes or timeout elapses.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "timeout": {"type": "number"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_output",
            "description": "Read the result/output of a task or background job.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    # ---------- interaction / planning ----------
    {
        "type": "function",
        "function": {
            "name": "ask_user_question",
            "description": "Pause and ask the user a question, with optional multiple-choice options.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "enter_plan_mode",
            "description": "Enter PLAN MODE: read-only, no mutating tools.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exit_plan_mode",
            "description": "Leave plan mode.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": "Replace this session's todo list. Use reminder_add for persistent reminders.",
            "parameters": {
                "type": "object",
                "properties": {"items": {"type": "array", "items": {}}},
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_search",
            "description": "Search the registered tools by keyword.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    },
    # ---------- system ----------
    {
        "type": "function",
        "function": {
            "name": "config_get",
            "description": "Read a value from the persistent config (e.g. 'user_name').",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "config_set",
            "description": "Set a config value. Asks for approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sleep",
            "description": "Pause for `seconds` (capped at 60).",
            "parameters": {"type": "object", "properties": {"seconds": {"type": "number"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill",
            "description": "Manage and apply skills (markdown bundles in ~/.config/cagentic/skills/). op: list | get | use.",
            "parameters": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["list", "get", "use"]},
                    "name": {"type": "string"},
                },
            },
        },
    },
]


def _all_tools() -> dict[str, ToolFn]:
    from .coding_tools import CODING_TOOLS
    from .github import GITHUB_TOOLS

    return {**TOOLS, **CODING_TOOLS, **GITHUB_TOOLS}


# Tool groups — bundle related tools so the user can keep the prompt lean.
TOOL_GROUPS: dict[str, list[str]] = {
    "files": [
        "read_file",
        "write_file",
        "edit_file",
        "replace_lines",
        "list_dir",
        "grep",
        "glob",
        "set_workspace",
    ],
    "web": ["web_fetch", "web_search"],
    "notes": [
        "note_write",
        "note_get",
        "note_list",
        "note_search",
        "note_delete",
        "chat_search",
        "chat_get",
    ],
    "reminders": [
        "reminder_add",
        "reminder_list",
        "reminder_done",
        "reminder_delete",
        "reminder_update",
    ],
    "life": [
        "goal_create",
        "goal_list",
        "goal_update",
        "goal_delete",
        "calendar_event_add",
        "calendar_event_list",
        "calendar_event_update",
        "calendar_event_delete",
        "personal_briefing",
        "calendar_connection_create",
        "calendar_connection_list",
        "calendar_connection_sync",
        "calendar_connection_delete",
        "notification_list",
        "notification_update",
        "inbox_capture",
        "inbox_list",
        "inbox_update",
        "inbox_delete",
        "email_connection_create",
        "email_connection_list",
        "email_connection_sync",
        "email_connection_delete",
        "routine_create",
        "routine_list",
        "routine_update",
        "routine_delete",
    ],
    "mcp": [
        "mcp_list_servers",
        "mcp_list_tools",
        "mcp_call",
        "mcp_list_resources",
        "mcp_read_resource",
        "mcp_restart",
    ],
    "browser": [
        "browser_status",
        "browser_tabs",
        "browser_read",
        "browser_open",
        "browser_navigate",
        "browser_click",
        "browser_fill",
        "browser_scroll",
        "browser_screenshot",
        "browser_click_at",
        "browser_links",
        "browser_download",
        "browser_eval",
        "browser_close",
    ],
    "shell": ["run_bash", "bash_async", "powershell"],
    "tasks": [
        "task_create",
        "task_update",
        "task_get",
        "task_list",
        "task_delete",
        "task_status",
        "task_wait",
        "task_stop",
        "task_output",
        "brief",
    ],
    "interaction": ["ask_user_question"],
    "planning": ["enter_plan_mode", "exit_plan_mode", "todo_write"],
    "system": ["config_get", "config_set", "sleep", "skill", "tool_search"],
    # Coding-agent tools absorbed from Collama.
    "coding": ["check_syntax", "multi_edit", "notebook_edit"],
    "worktree": ["enter_worktree", "exit_worktree"],
    "subagent": ["agent_call", "agent_call_async"],
    # off by default
    "teams": [
        "team_create",
        "team_delete",
        "team_list",
        "teammate_create",
        "teammate_delete",
        "teammate_list",
        "send_message",
        "inbox",
        "coordinator_tick",
        "coordinator_run",
    ],
    "github": [
        "gh_whoami",
        "gh_list_repos",
        "gh_get_repo",
        "gh_get_file",
        "gh_list_issues",
        "gh_create_issue",
        "gh_list_pulls",
        "gh_get_pull",
        "gh_search_code",
        "github_api",
    ],
}

# Personal-assistant defaults. Shell uses run_bash's per-call confirm;
# browser tools gate their mutating actions the same way.
DEFAULT_GROUPS: set[str] = {
    "files",
    "web",
    "notes",
    "reminders",
    "life",
    "mcp",
    "browser",
    "shell",
    "tasks",
    "interaction",
    "planning",
    "system",
    "coding",
    "worktree",
    "subagent",
}


def _compact_schema(schema: dict) -> dict:
    fn = schema.get("function", {}) or {}
    desc = (fn.get("description") or "").strip()
    short_desc = desc.splitlines()[0].strip() if desc else ""
    if len(short_desc) > 140:
        short_desc = short_desc[:137] + "…"
    new_fn = {**fn, "description": short_desc}
    params = new_fn.get("parameters") or {}
    if isinstance(params, dict) and isinstance(params.get("properties"), dict):
        new_props = {}
        for pname, pspec in params["properties"].items():
            if isinstance(pspec, dict):
                new_props[pname] = {k: v for k, v in pspec.items() if k != "description"}
            else:
                new_props[pname] = pspec
        new_fn["parameters"] = {**params, "properties": new_props}
    return {**schema, "function": new_fn}


def all_tool_schemas(
    enabled_groups: set[str] | None = None,
    compact: bool = True,
) -> list[dict]:
    groups = DEFAULT_GROUPS if enabled_groups is None else set(enabled_groups)
    return list(_cached_tool_schemas(frozenset(groups), compact))


@lru_cache(maxsize=32)
def _cached_tool_schemas(groups: frozenset[str], compact: bool) -> tuple[dict, ...]:
    from .coding_tools import CODING_TOOL_SCHEMAS
    from .github import GITHUB_TOOL_SCHEMAS

    allowed = {n for g in groups for n in TOOL_GROUPS.get(g, ())}
    schemas = TOOL_SCHEMAS + CODING_TOOL_SCHEMAS + GITHUB_TOOL_SCHEMAS
    filtered = [s for s in schemas if s.get("function", {}).get("name") in allowed]
    result = [_compact_schema(s) for s in filtered] if compact else filtered
    return tuple(result)


TOOL_ALIASES: dict[str, str] = {
    "read": "read_file",
    "open": "read_file",
    "view": "read_file",
    "cat": "read_file",
    "write": "write_file",
    "create": "write_file",
    "edit": "edit_file",
    "patch": "edit_file",
    "replace": "edit_file",
    "ls": "list_dir",
    "list": "list_dir",
    "dir": "list_dir",
    "search": "grep",
    "find": "glob",
    "bash": "run_bash",
    "shell": "run_bash",
    "exec": "run_bash",
    "run": "run_bash",
    "cd": "set_workspace",
    "fetch": "web_fetch",
    "curl": "web_fetch",
    "wget": "web_fetch",
    "search_web": "web_search",
    "todo": "todo_write",
    "todos": "todo_write",
    # personal-assistant friendly aliases
    "note": "note_write",
    "save_note": "note_write",
    "remember": "note_write",
    "remind": "reminder_add",
    "add_reminder": "reminder_add",
    "todo_persistent": "reminder_add",
    # Collama-era name for the server listing.
    "mcp_servers": "mcp_list_servers",
}


def dispatch(name: str, args: dict, ctx: ToolContext) -> str:
    all_tools = _all_tools()
    fn = all_tools.get(name)
    if fn is None:
        canonical = TOOL_ALIASES.get(name.lower())
        if canonical and canonical in all_tools:
            # Run the aliased tool under the same exception guard as a canonical
            # call so a KeyError/ValueError surfaces as an "ERROR: ..." string
            # instead of propagating uncaught out of the tool dispatch.
            try:
                result = all_tools[canonical](args, ctx)
            except KeyError as e:
                logger.warning(
                    "tool %r (alias %r) missing argument %s", canonical, name, e, exc_info=True
                )
                return f"ERROR: missing argument {e}"
            except Exception as e:
                logger.warning(
                    "tool %r (alias %r) raised %s", canonical, name, type(e).__name__, exc_info=True
                )
                return f"ERROR: {type(e).__name__}: {e}"
            if isinstance(result, str) and not result.startswith("ERROR"):
                result = f"[note: '{name}' is an alias for '{canonical}']\n{result}"
            return result
        import difflib

        pool = list(all_tools.keys()) + list(TOOL_ALIASES.keys())
        suggestions = difflib.get_close_matches(name.lower(), pool, n=3, cutoff=0.4)
        canonical_suggestions: list[str] = []
        for s in suggestions:
            c = TOOL_ALIASES.get(s, s)
            if c in all_tools and c not in canonical_suggestions:
                canonical_suggestions.append(c)
        hint = (
            f"  Did you mean: {', '.join(canonical_suggestions)}?" if canonical_suggestions else ""
        )
        return f"ERROR: unknown tool '{name}'.{hint}  Use /tools to see the full list."
    try:
        return fn(args, ctx)
    except KeyError as e:
        logger.warning("tool %r missing argument %s", name, e, exc_info=True)
        return f"ERROR: missing argument {e}"
    except Exception as e:
        logger.warning("tool %r raised %s", name, type(e).__name__, exc_info=True)
        return f"ERROR: {type(e).__name__}: {e}"
