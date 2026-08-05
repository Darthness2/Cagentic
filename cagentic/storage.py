"""Small shared helpers used by every persistence module.

Centralizes the things that were copy-pasted across config.py, sessions.py,
projects.py, reminders.py, and tasks.py:
- atomic_write_json: unique temp file -> fsync -> replace, at 0600
- read_json: load with graceful fallback on missing/bad files
- fmt_duration / fmt_ago: the s/m/h/d ladder that sessions + reminders both had
- STATUS_MARK: the done/pending/active/blocked glyph map
"""
from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

# A safe default for files that may hold tokens (config, sessions, reminders,
# projects, tasks). Failure to chmod is fine on Windows / FAT / some sandboxes.
_PRIVATE_MODE = stat.S_IRUSR | stat.S_IWUSR

# Serializes concurrent saves (gateway thread + REPL autosave) so the
# write-temp-then-replace dance can't interleave and corrupt a file.
_SAVE_LOCK = threading.Lock()

T = TypeVar("T")


def atomic_write_json(path: Path, payload: Any, *, private: bool = True) -> Path:
    """Write `payload` to `path` as JSON, atomically and privately.

    Writes to a uniquely-named temp file in the same directory, fsyncs it, then
    os.replace()s it into place — so a crash or a concurrent writer can never
    leave a half-written file behind, and readers only ever see the old or the
    new content.

    `private` tightens the temp file to 0600 *before* any bytes are written, so
    a file holding secrets (API tokens, MCP env values) is never briefly
    world-readable. fchmod is POSIX-only; on Windows the file inherits the
    user's own config directory ACL, which is already user-private.

    Raises OSError if the write fails, having cleaned up the temp file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2)
    with _SAVE_LOCK:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            if private and hasattr(os, "fchmod"):
                os.fchmod(fd, _PRIVATE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        except OSError:
            logger.warning("atomic write failed for %s", path, exc_info=True)
            try:
                os.unlink(tmp_name)
            except OSError:
                logger.warning("could not clean up temp file %s", tmp_name, exc_info=True)
            raise
    return path


def read_json(path: Path, default: T, *, loader: Callable[[str], T] = json.loads) -> T:
    """Read JSON from `path`; return `default` on missing/broken/IO errors."""
    if not path.exists():
        return default
    try:
        return loader(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        return default


def fmt_duration(seconds: float) -> str:
    """Compact "<n>s|m|h|d" duration. Negative values get the same magnitude."""
    s = abs(int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def fmt_ago(ts: int | float | None) -> str:
    """Relative time string ("3m ago", "2d ago"). Returns "?" for falsy input."""
    if not ts:
        return "?"
    delta = int(time.time()) - int(ts)
    return f"{fmt_duration(delta)} ago"


# Shared status glyph map. Keys are the canonical status strings used by
# reminders (pending/done/snoozed/cancelled), tasks (pending/active/done/
# blocked/failed/cancelled), and the REPL's /todo command.
STATUS_MARK: dict[str, str] = {
    "done":      "✓",
    "pending":   " ",
    "active":    "→",
    "blocked":   "✗",
    "snoozed":   "z",
    "failed":    "!",
    "cancelled": "✗",
}


def status_mark(status: str, default: str = "?") -> str:
    return STATUS_MARK.get(status or "", default)
