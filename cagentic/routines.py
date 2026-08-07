"""User-defined proactive routines and local schedule evaluation."""

from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Any

from . import inbox, personal_os, storage

_NS = "os_routines"
KINDS = {"daily_plan", "inbox_digest", "weekly_review", "custom"}
DEFAULT_PROMPTS = {
    "daily_plan": "Create a concise, realistic plan for today. Prioritize urgent deadlines, calendar commitments, one meaningful goal action, and protected focus time.",
    "inbox_digest": "Triage my unified inbox. Summarize what is urgent, what can wait, and the three next actions that would reduce the most mental load.",
    "weekly_review": "Review my week across goals, deadlines, calendar, and inbox. Celebrate progress, identify drift, and propose the most useful adjustments for next week.",
    "custom": "Review my personal OS and tell me what deserves attention.",
}


def _new_id() -> str:
    return "u" + secrets.token_hex(4)


def _match(routine_id: str) -> dict | None:
    if not routine_id:
        return None
    exact = storage.get(_NS, routine_id)
    if isinstance(exact, dict):
        return exact
    matches = [
        item
        for item in storage.list_values(_NS)
        if isinstance(item, dict) and str(item.get("id", "")).startswith(routine_id)
    ]
    return matches[0] if len(matches) == 1 else None


def _clean_time(value: str) -> str:
    raw = str(value or "08:00").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw):
        raise ValueError("routine time must use 24-hour HH:MM format")
    return raw


def _clean_days(value: list[Any] | None) -> list[int]:
    days = sorted({int(day) for day in (value if value is not None else range(7))})
    if not days or any(day < 0 or day > 6 for day in days):
        raise ValueError("routine days must contain weekday numbers 0 through 6")
    return days


def create_routine(
    name: str,
    *,
    kind: str = "daily_plan",
    schedule_time: str = "08:00",
    days: list[Any] | None = None,
    prompt: str = "",
    enabled: bool = True,
) -> dict:
    clean_name = str(name or "").strip()
    clean_kind = str(kind or "daily_plan").lower()
    if not clean_name:
        raise ValueError("routine name is required")
    if clean_kind not in KINDS:
        raise ValueError(f"invalid routine kind: {clean_kind}")
    now = time.time()
    routine = {
        "id": _new_id(),
        "name": clean_name[:160],
        "kind": clean_kind,
        "schedule_time": _clean_time(schedule_time),
        "days": _clean_days(days),
        "prompt": str(prompt or DEFAULT_PROMPTS[clean_kind]).strip()[:8000],
        "enabled": bool(enabled),
        "last_run_key": "",
        "last_run_at": None,
        "last_output": "",
        "last_error": "",
        "created_at": now,
        "updated_at": now,
    }
    storage.put(_NS, str(routine["id"]), routine, now)
    return _public(routine)


def _next_run(routine: dict, now: float | None = None) -> float | None:
    if not routine.get("enabled", True):
        return None
    current = datetime.fromtimestamp(float(now or time.time()))
    hour, minute = (int(part) for part in str(routine.get("schedule_time") or "08:00").split(":"))
    days = set(int(day) for day in (routine.get("days") or range(7)))
    for offset in range(8):
        candidate_day = current + timedelta(days=offset)
        if candidate_day.weekday() not in days:
            continue
        candidate = candidate_day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate.timestamp() > current.timestamp():
            return candidate.timestamp()
    return None


def _public(routine: dict, now: float | None = None) -> dict:
    return {
        **routine,
        "next_run_at": _next_run(routine, now=now),
    }


def list_routines(*, include_disabled: bool = True, now: float | None = None) -> list[dict]:
    items = [item for item in storage.list_values(_NS) if isinstance(item, dict)]
    if not include_disabled:
        items = [item for item in items if item.get("enabled", True)]
    items.sort(key=lambda item: (str(item.get("schedule_time") or ""), str(item.get("name") or "")))
    return [_public(item, now=now) for item in items]


def get_routine(routine_id: str, *, now: float | None = None) -> dict | None:
    routine = _match(routine_id)
    return _public(routine, now=now) if routine else None


def update_routine(routine_id: str, **changes: Any) -> dict | None:
    routine = _match(routine_id)
    if routine is None:
        return None
    if "name" in changes:
        name = str(changes["name"] or "").strip()
        if not name:
            raise ValueError("routine name is required")
        routine["name"] = name[:160]
    if "kind" in changes:
        kind = str(changes["kind"] or "custom").lower()
        if kind not in KINDS:
            raise ValueError(f"invalid routine kind: {kind}")
        routine["kind"] = kind
    if "schedule_time" in changes:
        routine["schedule_time"] = _clean_time(str(changes["schedule_time"]))
    if "days" in changes:
        routine["days"] = _clean_days(changes["days"])
    if "prompt" in changes:
        routine["prompt"] = str(changes["prompt"] or "").strip()[:8000]
    if "enabled" in changes:
        routine["enabled"] = bool(changes["enabled"])
    routine["updated_at"] = time.time()
    storage.put(_NS, str(routine["id"]), routine, float(routine["updated_at"]))
    return _public(routine)


def delete_routine(routine_id: str) -> bool:
    routine = _match(routine_id)
    return bool(routine and storage.delete(_NS, str(routine["id"])))


def due_routines(now: float | None = None) -> list[dict]:
    now = float(now or time.time())
    current = datetime.fromtimestamp(now)
    current_key = current.strftime("%Y-%m-%d")
    current_minutes = current.hour * 60 + current.minute
    due: list[dict] = []
    for routine in storage.list_values(_NS):
        if not isinstance(routine, dict) or not routine.get("enabled", True):
            continue
        if current.weekday() not in set(routine.get("days") or range(7)):
            continue
        hour, minute = (
            int(part) for part in str(routine.get("schedule_time") or "08:00").split(":")
        )
        if current_minutes < hour * 60 + minute:
            continue
        if routine.get("last_run_key") == current_key:
            continue
        due.append(routine)
    return due


def mark_run(
    routine_id: str, *, output: str = "", error: str = "", now: float | None = None
) -> dict | None:
    routine = _match(routine_id)
    if routine is None:
        return None
    now = float(now or time.time())
    routine["last_run_key"] = time.strftime("%Y-%m-%d", time.localtime(now))
    routine["last_run_at"] = now
    routine["last_output"] = str(output or "")[:12000]
    routine["last_error"] = str(error or "")[:1000]
    routine["updated_at"] = now
    storage.put(_NS, str(routine["id"]), routine, now)
    return _public(routine, now=now)


def build_prompt(routine: dict) -> str:
    return "\n\n".join(
        (
            str(
                routine.get("prompt")
                or DEFAULT_PROMPTS.get(str(routine.get("kind")), DEFAULT_PROMPTS["custom"])
            ),
            personal_os.system_context(),
            inbox.system_context(),
            "Return a short proactive briefing with concrete next actions. Do not claim you changed anything unless you actually did.",
        )
    )


def fallback_output(routine: dict) -> str:
    briefing = personal_os.briefing()
    stats = briefing["stats"]
    insight = (briefing.get("insights") or [{}])[0]
    unread = inbox.unread_count()
    return (
        f"{insight.get('title', 'Your personal OS is ready')}. "
        f"{insight.get('body', '')} You have {stats['events_today']} events today, "
        f"{stats['open_deadlines']} open deadlines, {stats['active_goals']} active goals, "
        f"and {unread} unread inbox items."
    ).strip()


def system_context() -> str:
    items = list_routines(include_disabled=False)
    if not items:
        return "=== PROACTIVE ROUTINES ===\nNo proactive routines configured."
    lines = ["=== PROACTIVE ROUTINES ==="]
    for item in items[:20]:
        days = ",".join(str(day) for day in item.get("days") or [])
        lines.append(
            f"- {item['name']} ({item['kind']}) at {item['schedule_time']} on weekdays {days}"
        )
    return "\n".join(lines)
