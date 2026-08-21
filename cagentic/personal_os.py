"""Persistent personal-OS data and deterministic proactive briefings.

The gateway and the agent tools share this module so goals and calendar
events are real user data rather than UI-only state.  Reminders remain the
single source of truth for deadlines; this module combines all three into a
small, local-first planning view.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from . import reminders, storage

GOAL_STATUSES = {"active", "paused", "completed", "cancelled"}
EVENT_STATUSES = {"confirmed", "tentative", "cancelled"}


def _new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(4)


def parse_timestamp(value: Any) -> float | None:
    """Accept unix seconds or the ISO strings emitted by datetime-local inputs."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return reminders.parse_when(raw)


def _match(namespace: str, item_id: str) -> dict | None:
    if not item_id:
        return None
    exact = storage.get(namespace, item_id)
    if isinstance(exact, dict):
        return exact
    matches = [
        item
        for item in storage.list_values(namespace)
        if isinstance(item, dict) and str(item.get("id", "")).startswith(item_id)
    ]
    return matches[0] if len(matches) == 1 else None


@dataclass
class Goal:
    id: str
    title: str
    description: str = ""
    status: str = "active"
    progress: int = 0
    target_at: float | None = None
    category: str = "personal"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CalendarEvent:
    id: str
    title: str
    start_at: float
    end_at: float
    description: str = ""
    location: str = ""
    all_day: bool = False
    status: str = "confirmed"
    source: str = "cagentic"
    external_id: str = ""
    color: str = "#c79bd8"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


def create_goal(
    title: str,
    *,
    description: str = "",
    target_at: Any = None,
    category: str = "personal",
    progress: Any = 0,
) -> dict:
    clean_title = str(title or "").strip()
    if not clean_title:
        raise ValueError("goal title is required")
    goal = Goal(
        id=_new_id("g"),
        title=clean_title[:240],
        description=str(description or "").strip()[:4000],
        target_at=parse_timestamp(target_at),
        category=str(category or "personal").strip()[:48] or "personal",
        progress=max(0, min(100, int(progress or 0))),
    )
    if goal.progress >= 100:
        goal.status = "completed"
    storage.put("os_goals", goal.id, goal.to_dict(), goal.updated_at)
    return goal.to_dict()


def list_goals(*, include_completed: bool = True) -> list[dict]:
    goals = [item for item in storage.list_values("os_goals") if isinstance(item, dict)]
    if not include_completed:
        goals = [g for g in goals if g.get("status") not in ("completed", "cancelled")]
    status_order = {"active": 0, "paused": 1, "completed": 2, "cancelled": 3}
    goals.sort(
        key=lambda g: (
            status_order.get(str(g.get("status")), 4),
            g.get("target_at") if g.get("target_at") is not None else float("inf"),
            -float(g.get("updated_at") or 0),
        )
    )
    return goals


def update_goal(goal_id: str, **changes: Any) -> dict | None:
    current = _match("os_goals", goal_id)
    if current is None:
        return None
    if "title" in changes:
        title = str(changes["title"] or "").strip()
        if not title:
            raise ValueError("goal title is required")
        current["title"] = title[:240]
    if "description" in changes:
        current["description"] = str(changes["description"] or "").strip()[:4000]
    if "category" in changes:
        current["category"] = str(changes["category"] or "personal").strip()[:48]
    if "target_at" in changes:
        current["target_at"] = parse_timestamp(changes["target_at"])
    if "progress" in changes:
        current["progress"] = max(0, min(100, int(changes["progress"] or 0)))
        if current["progress"] >= 100:
            current["status"] = "completed"
        elif current.get("status") == "completed":
            current["status"] = "active"
    if "status" in changes:
        status = str(changes["status"] or "active").lower()
        if status not in GOAL_STATUSES:
            raise ValueError(f"invalid goal status: {status}")
        current["status"] = status
        if status == "completed":
            current["progress"] = 100
    current["updated_at"] = time.time()
    storage.put("os_goals", str(current["id"]), current, float(current["updated_at"]))
    return current


def delete_goal(goal_id: str) -> bool:
    current = _match("os_goals", goal_id)
    return bool(current and storage.delete("os_goals", str(current["id"])))


def create_event(
    title: str,
    *,
    start_at: Any,
    end_at: Any = None,
    description: str = "",
    location: str = "",
    all_day: bool = False,
    source: str = "cagentic",
    external_id: str = "",
    color: str = "#c79bd8",
) -> dict:
    clean_title = str(title or "").strip()
    start = parse_timestamp(start_at)
    end = parse_timestamp(end_at)
    if not clean_title:
        raise ValueError("event title is required")
    if start is None:
        raise ValueError("event start time is required")
    if end is None:
        end = start + (86400 if all_day else 3600)
    if end < start:
        raise ValueError("event end time must be after its start time")
    # Imported calendars can safely upsert the same external event.
    clean_source = str(source or "cagentic").strip()[:80] or "cagentic"
    clean_external = str(external_id or "").strip()[:240]
    if clean_external:
        for item in storage.list_values("os_events"):
            if (
                isinstance(item, dict)
                and item.get("source") == clean_source
                and item.get("external_id") == clean_external
            ):
                return (
                    update_event(
                        str(item["id"]),
                        title=clean_title,
                        start_at=start,
                        end_at=end,
                        description=description,
                        location=location,
                        all_day=all_day,
                        color=color,
                    )
                    or item
                )
    event = CalendarEvent(
        id=_new_id("e"),
        title=clean_title[:240],
        start_at=start,
        end_at=end,
        description=str(description or "").strip()[:4000],
        location=str(location or "").strip()[:400],
        all_day=bool(all_day),
        source=clean_source,
        external_id=clean_external,
        color=str(color or "#c79bd8")[:24],
    )
    storage.put("os_events", event.id, event.to_dict(), event.updated_at)
    return event.to_dict()


def list_events(
    *, start_at: Any = None, end_at: Any = None, include_cancelled: bool = False
) -> list[dict]:
    start = parse_timestamp(start_at)
    end = parse_timestamp(end_at)
    events = [item for item in storage.list_values("os_events") if isinstance(item, dict)]
    if not include_cancelled:
        events = [event for event in events if event.get("status") != "cancelled"]
    if start is not None:
        events = [event for event in events if float(event.get("end_at") or 0) >= start]
    if end is not None:
        events = [event for event in events if float(event.get("start_at") or 0) <= end]
    events.sort(key=lambda event: (float(event.get("start_at") or 0), event.get("title", "")))
    return events


def update_event(event_id: str, **changes: Any) -> dict | None:
    current = _match("os_events", event_id)
    if current is None:
        return None
    for field_name, limit in (
        ("title", 240),
        ("description", 4000),
        ("location", 400),
        ("source", 80),
        ("external_id", 240),
        ("color", 24),
    ):
        if field_name in changes:
            value = str(changes[field_name] or "").strip()[:limit]
            if field_name == "title" and not value:
                raise ValueError("event title is required")
            current[field_name] = value
    if "start_at" in changes:
        start = parse_timestamp(changes["start_at"])
        if start is None:
            raise ValueError("event start time is required")
        current["start_at"] = start
    if "end_at" in changes:
        current["end_at"] = parse_timestamp(changes["end_at"])
    if "all_day" in changes:
        current["all_day"] = bool(changes["all_day"])
    if "status" in changes:
        status = str(changes["status"] or "confirmed").lower()
        if status not in EVENT_STATUSES:
            raise ValueError(f"invalid event status: {status}")
        current["status"] = status
    if current.get("end_at") is None:
        current["end_at"] = float(current["start_at"]) + (86400 if current.get("all_day") else 3600)
    if float(current["end_at"]) < float(current["start_at"]):
        raise ValueError("event end time must be after its start time")
    current["updated_at"] = time.time()
    storage.put("os_events", str(current["id"]), current, float(current["updated_at"]))
    return current


def delete_event(event_id: str) -> bool:
    current = _match("os_events", event_id)
    return bool(current and storage.delete("os_events", str(current["id"])))


def _fmt_time(timestamp: float | None) -> str:
    if timestamp is None:
        return ""
    return time.strftime("%a %b %d, %I:%M %p", time.localtime(timestamp)).replace(" 0", " ")


def integration_summary(config: dict, *, browser_connected: bool = False) -> list[dict]:
    items: list[dict] = []
    mcp = (config.get("mcp") or {}).get("servers") or {}
    for name, spec in sorted(mcp.items()):
        enabled = bool(isinstance(spec, dict) and spec.get("enabled", True))
        items.append(
            {
                "id": f"mcp:{name}",
                "name": str(name).replace("-", " ").title(),
                "kind": "MCP",
                "connected": enabled,
                "detail": "Enabled" if enabled else "Disabled",
            }
        )
    items.append(
        {
            "id": "browser",
            "name": "Chrome",
            "kind": "Browser",
            "connected": bool(browser_connected),
            "detail": "Extension connected" if browser_connected else "Extension available",
        }
    )
    github_ready = bool((config.get("github") or {}).get("token"))
    items.append(
        {
            "id": "github",
            "name": "GitHub",
            "kind": "Developer",
            "connected": github_ready,
            "detail": "Authenticated" if github_ready else "Not configured",
        }
    )
    for name, spec in sorted((config.get("providers") or {}).items()):
        if isinstance(spec, dict) and spec.get("api_key"):
            items.append(
                {
                    "id": f"provider:{name}",
                    "name": str(name).title(),
                    "kind": "AI provider",
                    "connected": True,
                    "detail": "Configured",
                }
            )
    return items


def briefing(
    config: dict | None = None,
    *,
    now: float | None = None,
    browser_connected: bool = False,
) -> dict:
    """Combine life data into a useful, zero-network proactive briefing."""
    now = float(now or time.time())
    today = time.localtime(now)
    day_start = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))
    day_end = day_start + 86400
    week_end = day_end + 6 * 86400
    goals = list_goals(include_completed=True)
    active_goals = [g for g in goals if g.get("status") == "active"]
    events = list_events(start_at=day_start, end_at=week_end)
    today_events = [e for e in events if float(e.get("start_at") or 0) < day_end]
    pending_reminders = reminders.list_all(include_done=False)
    deadline_items = [r.to_dict() for r in pending_reminders]
    insights: list[dict] = []

    overdue = [r for r in pending_reminders if r.due_at is not None and r.due_at < now]
    if overdue:
        insights.append(
            {
                "id": "overdue",
                "severity": "urgent",
                "title": f"{len(overdue)} overdue deadline{'s' if len(overdue) != 1 else ''}",
                "body": overdue[0].text,
                "action": "Review deadlines",
                "action_prompt": "Help me triage my overdue deadlines and make a realistic plan.",
            }
        )

    upcoming = [e for e in events if now <= float(e.get("start_at") or 0) <= now + 3600]
    if upcoming:
        event = upcoming[0]
        mins = max(0, int((float(event["start_at"]) - now) / 60))
        insights.append(
            {
                "id": "starting-soon",
                "severity": "attention",
                "title": f"{event['title']} starts in {mins} min",
                "body": event.get("location") or "Your next event is approaching.",
                "action": "Prepare with Cagentic",
                "action_prompt": f"Help me prepare for {event['title']}.",
            }
        )

    ordered_today = sorted(today_events, key=lambda e: float(e.get("start_at") or 0))
    conflicts = []
    for previous, current in zip(ordered_today, ordered_today[1:]):
        if float(current.get("start_at") or 0) < float(previous.get("end_at") or 0):
            conflicts.append((previous, current))
    if conflicts:
        first, second = conflicts[0]
        insights.append(
            {
                "id": "conflict",
                "severity": "urgent",
                "title": "Schedule conflict detected",
                "body": f"{first['title']} overlaps {second['title']}.",
                "action": "Resolve conflict",
                "action_prompt": "Help me resolve today's calendar conflict.",
            }
        )

    at_risk = [
        g
        for g in active_goals
        if g.get("target_at")
        and float(g["target_at"]) < now + 7 * 86400
        and int(g.get("progress") or 0) < 80
    ]
    if at_risk:
        goal = at_risk[0]
        insights.append(
            {
                "id": f"goal:{goal['id']}",
                "severity": "attention",
                "title": "Goal needs attention",
                "body": f"{goal['title']} is {goal.get('progress', 0)}% complete and due soon.",
                "action": "Make a recovery plan",
                "action_prompt": f"Make a practical recovery plan for my goal: {goal['title']}.",
            }
        )

    if not insights:
        if today_events:
            insights.append(
                {
                    "id": "steady-day",
                    "severity": "calm",
                    "title": "Your day is on track",
                    "body": f"You have {len(today_events)} event{'s' if len(today_events) != 1 else ''} today and no detected conflicts.",
                    "action": "Plan my day",
                    "action_prompt": "Help me plan my day around my calendar and goals.",
                }
            )
        else:
            insights.append(
                {
                    "id": "open-day",
                    "severity": "calm",
                    "title": "You have an open day",
                    "body": "No calendar events are scheduled. This is a good focus window.",
                    "action": "Choose a focus",
                    "action_prompt": "Look at my goals and help me choose the best focus for today.",
                }
            )

    agenda: list[dict] = []
    for event in events:
        agenda.append({"kind": "event", **event})
    for reminder in pending_reminders:
        if reminder.due_at is not None and reminder.due_at <= week_end:
            agenda.append(
                {
                    "kind": "deadline",
                    "id": reminder.id,
                    "title": reminder.text,
                    "start_at": reminder.due_at,
                    "end_at": reminder.due_at,
                    "status": reminder.status,
                    "tags": reminder.tags,
                }
            )
    agenda.sort(key=lambda item: float(item.get("start_at") or float("inf")))

    hour = time.localtime(now).tm_hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    integrations = integration_summary(config or {}, browser_connected=browser_connected)
    completed_goals = len([g for g in goals if g.get("status") == "completed"])
    return {
        "generated_at": now,
        "greeting": greeting,
        "date_label": time.strftime("%A, %B %d", time.localtime(now)),
        "stats": {
            "events_today": len(today_events),
            "open_deadlines": len(pending_reminders),
            "overdue": len(overdue),
            "active_goals": len(active_goals),
            "completed_goals": completed_goals,
            "connected_services": len([i for i in integrations if i.get("connected")]),
        },
        "insights": insights[:5],
        "agenda": agenda,
        "events": events,
        "deadlines": deadline_items,
        "goals": goals,
        "integrations": integrations,
    }


def system_context(now: float | None = None) -> str:
    """A compact life snapshot that lets the assistant reason proactively."""
    data = briefing(now=now)
    active_goals = [g for g in data["goals"] if g.get("status") == "active"][:6]
    upcoming = data["agenda"][:8]
    lines = [
        "=== PERSONAL OS CONTEXT ===",
        "Use goal and calendar tools to keep this information current. Proactively flag "
        "conflicts, approaching deadlines, and realistic next actions when relevant.",
    ]
    if active_goals:
        lines.append("Active goals:")
        for goal in active_goals:
            target = f", target {_fmt_time(goal.get('target_at'))}" if goal.get("target_at") else ""
            lines.append(f"- {goal['title']} ({goal.get('progress', 0)}%{target})")
    if upcoming:
        lines.append("Upcoming schedule and deadlines:")
        for item in upcoming:
            lines.append(f"- {_fmt_time(item.get('start_at'))}: {item.get('title', '')}")
    return "\n".join(lines)
