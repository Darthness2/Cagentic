"""Persistent reminders — to-dos that survive across sessions.

Stored as a single JSON list at ~/.config/cagentic/reminders.json. Each
reminder has an id, text, optional due timestamp, and status. Differs from
state.todos (which is per-session scratch): these are meant to live for
days/weeks until you mark them done.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import config_dir


def _path() -> Path:
    return config_dir() / "reminders.json"


def _new_id() -> str:
    return "r" + secrets.token_hex(4)


@dataclass
class Reminder:
    id: str
    text: str
    status: str = "pending"          # pending | done | snoozed | cancelled
    due_at: float | None = None      # unix timestamp; None = no specific time
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    def short(self) -> str:
        mark = {"done": "✓", "pending": " ", "snoozed": "z", "cancelled": "✗"}.get(self.status, "?")
        when = ""
        if self.due_at:
            d = self.due_at - time.time()
            if d < 0:
                when = f"  (overdue {_fmt_dt(-d)})"
            elif d < 86400 * 2:
                when = f"  (in {_fmt_dt(d)})"
            else:
                when = f"  (due {time.strftime('%a %b %d', time.localtime(self.due_at))})"
        tags = f"  [{','.join(self.tags)}]" if self.tags else ""
        return f"  [{mark}] {self.id}  {self.text}{tags}{when}"


def _fmt_dt(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _load_all() -> list[Reminder]:
    p = _path()
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    out: list[Reminder] = []
    if isinstance(raw, list):
        for item in raw:
            try:
                out.append(Reminder(**item))
            except TypeError:
                continue
    return out


def _save_all(rems: list[Reminder]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps([r.to_dict() for r in rems], indent=2))
    tmp.replace(p)
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def add(text: str, *, due_at: float | None = None, tags: list[str] | None = None) -> Reminder:
    rems = _load_all()
    r = Reminder(id=_new_id(), text=text.strip(), due_at=due_at, tags=list(tags or []))
    rems.append(r)
    _save_all(rems)
    return r


def update(rid: str, **changes) -> Reminder | None:
    rems = _load_all()
    target = None
    for r in rems:
        if r.id == rid or r.id.startswith(rid):
            target = r
            break
    if not target:
        return None
    for k, v in changes.items():
        if hasattr(target, k):
            setattr(target, k, v)
    target.updated_at = time.time()
    _save_all(rems)
    return target


def delete(rid: str) -> bool:
    rems = _load_all()
    n = len(rems)
    rems = [r for r in rems if not (r.id == rid or r.id.startswith(rid))]
    if len(rems) == n:
        return False
    _save_all(rems)
    return True


def list_all(*, status: str | None = None, include_done: bool = False) -> list[Reminder]:
    rems = _load_all()
    if status:
        rems = [r for r in rems if r.status == status]
    elif not include_done:
        rems = [r for r in rems if r.status != "done"]
    # Active first (pending), then snoozed, then done; within each, due soonest first
    def _key(r: Reminder) -> tuple:
        order = {"pending": 0, "snoozed": 1, "done": 2, "cancelled": 3}.get(r.status, 4)
        due = r.due_at if r.due_at is not None else float("inf")
        return (order, due, r.created_at)
    rems.sort(key=_key)
    return rems


def parse_when(text: str) -> float | None:
    """Crude due-time parser. Returns a unix timestamp or None.

    Handles: 'in 10m', 'in 2h', 'in 3 days', 'tomorrow', 'tonight',
    'YYYY-MM-DD', 'YYYY-MM-DD HH:MM'.
    """
    if not text:
        return None
    s = text.strip().lower()
    now = time.time()
    if s in ("tomorrow",):
        # 9am tomorrow
        t = time.localtime(now + 86400)
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 9, 0, 0, 0, 0, -1))
    if s in ("tonight",):
        t = time.localtime(now)
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 20, 0, 0, 0, 0, -1))
    # 'in N <unit>'
    import re as _re
    m = _re.match(r"in\s+(\d+)\s*([smhd])", s) or _re.match(r"in\s+(\d+)\s+(seconds?|minutes?|hours?|days?)", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)[0]
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 0)
        if mult:
            return now + n * mult
    # ISO dates
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except ValueError:
            continue
    return None
