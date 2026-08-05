"""Small display helpers shared by the persistence modules and the REPL.

These used to live in storage.py, which is now the SQLite object store — and
storage.py imports config, so anything importing it back (config did) closes an
import cycle. This module imports nothing from the package, so it is safe for
every layer to use.
"""

from __future__ import annotations

import time


def fmt_duration(seconds: float) -> str:
    """Compact "<n>s|m|h|d" duration. Negative values get the same magnitude,
    so an overdue reminder reads "2m" rather than "0m"."""
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
    "done": "✓",
    "pending": " ",
    "active": "→",
    "blocked": "✗",
    "snoozed": "z",
    "failed": "!",
    "cancelled": "✗",
}


def status_mark(status: str, default: str = "?") -> str:
    return STATUS_MARK.get(status or "", default)
