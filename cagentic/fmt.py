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


def fmt_tokens(n: int) -> str:
    """Compact token count: 1200 -> '1.2k', 340 -> '340'.

    A per-turn line competes with the reply for attention, so the exact digit
    count matters less than the magnitude being readable at a glance.
    """
    n = int(n)
    if abs(n) < 1000:
        return str(n)
    if abs(n) < 1_000_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")


def fmt_cost(usd: float | None) -> str:
    """USD for display. Sub-cent turns are the common case on cached traffic,
    so round to 4dp there rather than showing an uninformative '$0.00'."""
    if usd is None:
        return ""
    if usd == 0:
        return "$0.00"
    if usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:,.2f}"


def fmt_usage_line(usage: dict, cost: dict | None = None) -> str:
    """The per-turn footer: '↑1.2k ↓340 · 11.1k cached · $0.011'.

    Cache tokens get their own segment because they are the visible proof that
    prompt caching is working — folded into the input count they'd be invisible.
    """
    parts = [f"↑{fmt_tokens(usage.get('input', 0))} ↓{fmt_tokens(usage.get('output', 0))}"]
    cached = int(usage.get("cache_read", 0) or 0)
    if cached:
        parts.append(f"{fmt_tokens(cached)} cached")
    if cost:
        spent = cost.get("spent")
        if spent is not None:
            money = fmt_cost(spent)
            saved = cost.get("saved") or 0.0
            # Only claim a saving worth a cent — rounding noise reads as spin.
            if saved >= 0.005:
                money += f" (saved {fmt_cost(saved)})"
            parts.append(money)
    ms = int(usage.get("ms", 0) or 0)
    # Below a second `fmt_duration` renders "0s", which is noise rather than
    # information — a turn that fast doesn't need a timing at all.
    if ms >= 1000:
        parts.append(fmt_duration(ms / 1000.0))
    return " · ".join(parts)
