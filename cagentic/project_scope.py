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
    from .config import config_dir

    seen: dict[str, str] = {}
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
            # First writer wins: the project's copy shadows the user's.
            seen.setdefault(name, origin)
    return sorted(seen.items())
