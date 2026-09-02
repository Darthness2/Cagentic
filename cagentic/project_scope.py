"""Per-project configuration under `.cagentic/` in the workspace.

Everything used to live in `~/.config/cagentic/`, which meant a team could not
check agent configuration into version control: prompts, skills and approvals
were per-machine and invisible to everyone else on the repo.

Layout (all optional):

    .cagentic/
        settings.json         shared, committed — permission rules (see 1d)
        settings.local.json   personal, gitignored — overrides the above
        commands/*.md         custom slash commands
        skills/*.md           project skills, shadowing the user's own

Project files take precedence over the user's global ones. A repo saying "use
tabs here" should win over a personal default, and a teammate cloning the repo
should get the project's behaviour without configuring anything.

This module sits above `config` and below everything else: it reads files and
returns plain data, so it can't participate in an import cycle.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

_log = logging.getLogger(__name__)

PROJECT_DIR = ".cagentic"
# Slash-command names have to be safe as both a filename and a typed token.
_NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$", re.IGNORECASE)
# A command body is a prompt, not a program — cap it so a stray binary file
# dropped in commands/ can't blow up the context window.
MAX_BODY_BYTES = 64 * 1024


def project_dir(workspace: Path) -> Path:
    return Path(workspace) / PROJECT_DIR


def _read_markdown_dir(directory: Path) -> dict[str, str]:
    """Map stem → body for every *.md in `directory`. Missing dir → {}."""
    out: dict[str, str] = {}
    try:
        entries = sorted(directory.glob("*.md"))
    except OSError:
        return out
    for path in entries:
        name = path.stem.lower()
        if not _NAME_RX.match(name):
            _log.warning("ignoring %s: not a usable command/skill name", path)
            continue
        try:
            if path.stat().st_size > MAX_BODY_BYTES:
                _log.warning("ignoring %s: larger than %d bytes", path, MAX_BODY_BYTES)
                continue
            out[name] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            _log.warning("could not read %s", path, exc_info=True)
    return out


def discover_commands(workspace: Path) -> dict[str, str]:
    """Custom slash commands defined by the project: name → prompt template."""
    return _read_markdown_dir(project_dir(workspace) / "commands")


def command_summary(body: str) -> str:
    """One-line description for /help and the completion popup.

    Prefers a YAML-ish `description:` line, then the first non-heading line of
    prose — so a command file can either declare its summary or just start with
    a sentence, and both read correctly in the catalog.
    """
    lines = body.splitlines()
    for line in lines[:10]:
        stripped = line.strip()
        if stripped.lower().startswith("description:"):
            return stripped.split(":", 1)[1].strip()[:80]
    # Skip fenced blocks entirely, not just their markers — otherwise the
    # first line of a shell example becomes the command's description.
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped or stripped.startswith(("#", "---", ">")):
            continue
        return stripped[:80]
    return "project command"


def render_command(body: str, arguments: str) -> str:
    """Substitute the caller's arguments into a command template.

    `$ARGUMENTS` is replaced wherever it appears; a template that doesn't
    mention it gets the arguments appended, so `/review src/x.py` does the
    obvious thing even for a body that never anticipated an argument.
    """
    # Strip a leading YAML-ish front matter block — it's metadata for the
    # catalog, not instructions for the model.
    text = body
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]
    text = re.sub(r"^\s*description:.*$", "", text, count=1, flags=re.MULTILINE)
    text = text.strip()

    arguments = (arguments or "").strip()
    if "$ARGUMENTS" in text:
        return text.replace("$ARGUMENTS", arguments).strip()
    if arguments:
        return f"{text}\n\n{arguments}"
    return text


# name -> summary, keyed by a cheap signature of the commands directory.
_SUMMARY_CACHE: dict[str, tuple[tuple, dict[str, str]]] = {}


def _dir_signature(directory: Path) -> tuple:
    """(name, mtime, size) per file — catches adds, removes and edits.

    Cheaper than reading the bodies, which is what discover_commands does and
    what the completion popup must not do on every keystroke.
    """
    try:
        return tuple(
            sorted(
                (e.name, int(e.stat().st_mtime_ns), e.stat().st_size)
                for e in os.scandir(directory)
                if e.is_file() and e.name.endswith(".md")
            )
        )
    except OSError:
        return ()


def command_summaries(workspace: Path) -> dict[str, str]:
    """Project command names and their one-line summaries, for the popup.

    Separate from discover_commands because the popup wants labels on every
    keystroke and the dispatcher wants a body once per invocation. The cache is
    invalidated by the directory's own contents rather than a timer, so a
    command file added mid-session shows up as soon as it exists.
    """
    directory = project_dir(workspace) / "commands"
    key = str(directory)
    signature = _dir_signature(directory)
    cached = _SUMMARY_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    summaries = {name: command_summary(body) for name, body in discover_commands(workspace).items()}
    _SUMMARY_CACHE[key] = (signature, summaries)
    return summaries


def find_skill(workspace: Path, name: str) -> Path | None:
    """Locate a skill, preferring the project's copy over the user's.

    A repo that ships `.cagentic/skills/review.md` should get *its* review
    process, not whatever the individual developer happens to have globally.
    """
    from .config import config_dir

    name = (name or "").strip().lower()
    if not _NAME_RX.match(name):
        return None
    for base in (project_dir(workspace) / "skills", config_dir() / "skills"):
        for ext in (".md", ".txt", ""):
            candidate = base / f"{name}{ext}"
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    return None


def list_skills(workspace: Path) -> list[tuple[str, str]]:
    """(name, origin) for every reachable skill, project first."""
    return [(name, origin) for name, origin, _summary in describe_skills(workspace)]


def describe_skills(workspace: Path) -> list[tuple[str, str, str]]:
    """(name, origin, summary) for every reachable skill, project first.

    The summary is what makes skills usable at all. Without it the model saw a
    bare list of filenames and had to guess from the name whether `review` meant
    a code review, a performance review, or a restaurant one — so in practice it
    guessed "not relevant" and never called them.
    """
    from .config import config_dir

    seen: dict[str, tuple[str, str]] = {}
    for origin, base in (
        ("project", project_dir(workspace) / "skills"),
        ("user", config_dir() / "skills"),
    ):
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for path in entries:
            if not path.is_file():
                continue
            name = path.stem.lower()
            if name in seen:
                # First writer wins: the project's copy shadows the user's.
                continue
            summary = ""
            try:
                if path.stat().st_size <= MAX_BODY_BYTES:
                    summary = command_summary(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                summary = ""
            seen[name] = (origin, summary)
    return sorted((name, origin, summary) for name, (origin, summary) in seen.items())


def apply_project_settings(state, cfg: dict | None = None) -> list[str]:
    """Apply the workspace's own `.cagentic/settings.*.json` to live state.

    Returns one human-readable line per setting applied, so the caller can say
    what a directory did rather than changing behaviour silently.

    Only a deliberately small surface is honoured, and the two files are not
    equally trusted:

    * `tool_groups` and `model` come from either file. Neither can run
      anything: the tool groups only decide which schemas the model is offered,
      and every one of those tools still passes the permission gate.
    * `mcp.servers` is honoured **only** from `settings.local.json`. An MCP
      server is a command line Cagentic spawns, so accepting one from a
      committed file would mean `git clone` could execute arbitrary code the
      first time you opened the directory. That is not a trade worth making for
      the convenience, so the shared file cannot do it and says so in the log.
    """
    from .permissions import project_settings

    applied: list[str] = []
    workspace = getattr(state, "workspace", None)
    for filename, data in project_settings(workspace):
        local = filename.endswith(".local.json")

        groups = data.get("tool_groups")
        if isinstance(groups, list):
            from .tools import TOOL_GROUPS, resolve_groups

            wanted = resolve_groups(g for g in groups if isinstance(g, str))
            known = {g for g in wanted if g in TOOL_GROUPS}
            if known:
                state.update(tool_groups=known)
                applied.append(f"tool groups: {', '.join(sorted(known))}")

        model = data.get("model")
        if isinstance(model, str) and model.strip() and cfg is not None:
            cfg["model"] = model.strip()
            applied.append(f"model: {model.strip()}")

        servers = (
            (data.get("mcp") or {}).get("servers") if isinstance(data.get("mcp"), dict) else None
        )
        if isinstance(servers, dict) and servers:
            if not local:
                _log.warning(
                    "ignoring mcp.servers in %s: an MCP server is a command this would "
                    "spawn, so it is only honoured from settings.local.json",
                    filename,
                )
                applied.append(f"ignored mcp.servers in {filename} (committed file)")
            elif cfg is not None:
                existing = cfg.setdefault("mcp", {}).setdefault("servers", {})
                existing.update(servers)
                applied.append(f"mcp servers: {', '.join(sorted(servers))}")
    return applied
