"""Local-first platform connections and calendar synchronization adapters.

Calendar connections deliberately use open standards: read-only iCalendar
feeds work with Google, Outlook, Apple, and most scheduling products; CalDAV
adds two-way event synchronization for providers that expose a calendar URL.
Credentials stay in Cagentic's local SQLite store and are never returned by
the public/listing APIs.
"""

from __future__ import annotations

import calendar
import secrets
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from . import personal_os, storage

KINDS = {"ical", "caldav"}
DIRECTIONS = {"pull", "push", "both"}
_NS = "os_integrations"
_SYNC_LOCK = threading.RLock()


def _new_id() -> str:
    return "i" + secrets.token_hex(4)


def _match(connection_id: str) -> dict | None:
    if not connection_id:
        return None
    exact = storage.get(_NS, connection_id)
    if isinstance(exact, dict):
        return exact
    matches = [
        item
        for item in storage.list_values(_NS)
        if isinstance(item, dict) and str(item.get("id", "")).startswith(connection_id)
    ]
    return matches[0] if len(matches) == 1 else None


def _validate_url(value: str) -> str:
    url = str(value or "").strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://") :]
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("connection URL must use http, https, or webcal")
    return url


def _safe_endpoint(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or "calendar"
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/")
    if len(path) > 48:
        path = "…" + path[-47:]
    return f"{host}{port}{path}"


def _public(connection: dict) -> dict:
    kind = str(connection.get("kind") or "ical")
    status = str(connection.get("status") or "not_synced")
    last_error = str(connection.get("last_error") or "")
    if last_error:
        detail = last_error[:160]
    elif connection.get("last_synced_at"):
        detail = "Last synced " + time.strftime(
            "%b %d at %I:%M %p", time.localtime(float(connection["last_synced_at"]))
        ).replace(" 0", " ")
    else:
        detail = "Ready to sync"
    return {
        "id": connection.get("id"),
        "name": connection.get("name"),
        "kind": "CalDAV" if kind == "caldav" else "Calendar feed",
        "adapter": kind,
        "managed": True,
        "connected": status == "synced",
        "status": status,
        "detail": detail,
        "endpoint": _safe_endpoint(str(connection.get("url") or "")),
        "direction": connection.get("direction", "pull"),
        "auto_sync": bool(connection.get("auto_sync", True)),
        "sync_interval": int(connection.get("sync_interval") or 900),
        "last_synced_at": connection.get("last_synced_at"),
        "last_error": last_error[:300],
        "created_at": connection.get("created_at"),
        "updated_at": connection.get("updated_at"),
    }


def create_connection(
    name: str,
    kind: str,
    url: str,
    *,
    username: str = "",
    password: str = "",
    direction: str | None = None,
    auto_sync: bool = True,
    sync_interval: Any = 900,
    verify_ssl: bool = True,
) -> dict:
    clean_name = str(name or "").strip()
    clean_kind = str(kind or "ical").strip().lower()
    if not clean_name:
        raise ValueError("connection name is required")
    if clean_kind not in KINDS:
        raise ValueError(f"unsupported connection type: {clean_kind}")
    clean_direction = str(direction or ("both" if clean_kind == "caldav" else "pull")).lower()
    if clean_direction not in DIRECTIONS:
        raise ValueError(f"invalid sync direction: {clean_direction}")
    if clean_kind == "ical" and clean_direction != "pull":
        raise ValueError("iCalendar feeds are read-only; use CalDAV for push or two-way sync")
    interval = max(300, min(86400, int(sync_interval or 900)))
    now = time.time()
    connection = {
        "id": _new_id(),
        "name": clean_name[:120],
        "kind": clean_kind,
        "url": _validate_url(url),
        "username": str(username or "")[:240],
        "password": str(password or "")[:2000],
        "direction": clean_direction,
        "auto_sync": bool(auto_sync),
        "sync_interval": interval,
        "verify_ssl": bool(verify_ssl),
        "status": "not_synced",
        "last_synced_at": None,
        "last_attempt_at": None,
        "last_error": "",
        "created_at": now,
        "updated_at": now,
    }
    storage.put(_NS, str(connection["id"]), connection, now)
    return _public(connection)


def list_connections() -> list[dict]:
    items = [item for item in storage.list_values(_NS) if isinstance(item, dict)]
    items.sort(key=lambda item: str(item.get("name") or "").casefold())
    return [_public(item) for item in items]


def update_connection(connection_id: str, **changes: Any) -> dict | None:
    current = _match(connection_id)
    if current is None:
        return None
    if "name" in changes:
        name = str(changes["name"] or "").strip()
        if not name:
            raise ValueError("connection name is required")
        current["name"] = name[:120]
    if "url" in changes:
        current["url"] = _validate_url(str(changes["url"]))
    for key, limit in (("username", 240), ("password", 2000)):
        if key in changes and changes[key] is not None:
            current[key] = str(changes[key])[:limit]
    if "direction" in changes:
        direction = str(changes["direction"] or "pull").lower()
        if direction not in DIRECTIONS:
            raise ValueError(f"invalid sync direction: {direction}")
        if current.get("kind") == "ical" and direction != "pull":
            raise ValueError("iCalendar feeds are read-only")
        current["direction"] = direction
    if "auto_sync" in changes:
        current["auto_sync"] = bool(changes["auto_sync"])
    if "sync_interval" in changes:
        current["sync_interval"] = max(300, min(86400, int(changes["sync_interval"])))
    if "verify_ssl" in changes:
        current["verify_ssl"] = bool(changes["verify_ssl"])
    current["updated_at"] = time.time()
    storage.put(_NS, str(current["id"]), current, float(current["updated_at"]))
    return _public(current)


def delete_connection(connection_id: str, *, remove_imported_events: bool = False) -> bool:
    current = _match(connection_id)
    if current is None:
        return False
    if remove_imported_events:
        source = f"calendar:{current['id']}"
        for event in personal_os.list_events(include_cancelled=True):
            if event.get("source") == source:
                personal_os.delete_event(str(event["id"]))
    return storage.delete(_NS, str(current["id"]))


def _unfold_ical(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for raw in normalized.split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _ical_unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _parse_ical_time(value: str, params: dict[str, str]) -> tuple[float, bool]:
    raw = value.strip()
    all_day = params.get("VALUE", "").upper() == "DATE" or len(raw) == 8
    if all_day:
        parsed = datetime.strptime(raw[:8], "%Y%m%d")
        return parsed.timestamp(), True
    if raw.endswith("Z"):
        parsed = datetime.strptime(raw, "%Y%m%dT%H%M%SZ")
        return float(calendar.timegm(parsed.timetuple())), False
    parsed = datetime.strptime(raw[:15], "%Y%m%dT%H%M%S")
    tzid = params.get("TZID")
    if tzid:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(tzid))
        except ZoneInfoNotFoundError:
            pass
    return parsed.timestamp(), False


def parse_ical(text: str) -> list[dict]:
    """Parse the VEVENT subset needed for a unified local agenda."""
    events: list[dict] = []
    current: dict[str, tuple[str, dict[str, str]]] | None = None
    for line in _unfold_ical(text):
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            current = {}
            continue
        if upper == "END:VEVENT":
            if current is None:
                continue
            try:
                start, all_day = _parse_ical_time(*current["DTSTART"])
            except (KeyError, ValueError):
                current = None
                continue
            try:
                end, _ = _parse_ical_time(*current["DTEND"])
            except (KeyError, ValueError):
                end = start + (86400 if all_day else 3600)
            title = _ical_unescape(current.get("SUMMARY", ("Untitled event", {}))[0])
            uid = _ical_unescape(current.get("UID", (f"event-{len(events)}", {}))[0])
            events.append(
                {
                    "uid": uid,
                    "title": title or "Untitled event",
                    "start_at": start,
                    "end_at": max(start, end),
                    "all_day": all_day,
                    "description": _ical_unescape(current.get("DESCRIPTION", ("", {}))[0]),
                    "location": _ical_unescape(current.get("LOCATION", ("", {}))[0]),
                    "status": current.get("STATUS", ("CONFIRMED", {}))[0].lower(),
                }
            )
            current = None
            continue
        if current is None or ":" not in line:
            continue
        lhs, value = line.split(":", 1)
        parts = lhs.split(";")
        name = parts[0].upper()
        params: dict[str, str] = {}
        for part in parts[1:]:
            if "=" in part:
                key, param_value = part.split("=", 1)
                params[key.upper()] = param_value.strip('"')
        if name in {"UID", "SUMMARY", "DTSTART", "DTEND", "DESCRIPTION", "LOCATION", "STATUS"}:
            current[name] = (value, params)
    return events


def _ical_escape(value: str) -> str:
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def event_to_ical(event: dict, *, calendar_name: str = "Cagentic") -> str:
    uid = str(event.get("external_id") or f"{event.get('id', _new_id())}@cagentic.local")
    start = float(event.get("start_at") or time.time())
    end = float(event.get("end_at") or start + 3600)
    if event.get("all_day"):
        dtstart = "DTSTART;VALUE=DATE:" + time.strftime("%Y%m%d", time.localtime(start))
        dtend = "DTEND;VALUE=DATE:" + time.strftime("%Y%m%d", time.localtime(end))
    else:
        dtstart = "DTSTART:" + datetime.fromtimestamp(start, timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        dtend = "DTEND:" + datetime.fromtimestamp(end, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Cagentic//Personal OS//EN",
        f"X-WR-CALNAME:{_ical_escape(calendar_name)}",
        "BEGIN:VEVENT",
        f"UID:{_ical_escape(uid)}",
        "DTSTAMP:" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        dtstart,
        dtend,
        f"SUMMARY:{_ical_escape(str(event.get('title') or 'Untitled event'))}",
    ]
    if event.get("description"):
        lines.append(f"DESCRIPTION:{_ical_escape(str(event['description']))}")
    if event.get("location"):
        lines.append(f"LOCATION:{_ical_escape(str(event['location']))}")
    lines.extend(["END:VEVENT", "END:VCALENDAR", ""])
    return "\r\n".join(lines)


def export_calendar(events: list[dict] | None = None, *, name: str = "Cagentic") -> str:
    items = events if events is not None else personal_os.list_events(include_cancelled=False)
    body = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Cagentic//Personal OS//EN"]
    body.append(f"X-WR-CALNAME:{_ical_escape(name)}")
    for event in items:
        block = event_to_ical(event, calendar_name=name).split("\r\n")
        body.extend(block[4:-2])
    body.extend(["END:VCALENDAR", ""])
    return "\r\n".join(body)


def _calendar_payloads_from_caldav(content: bytes | str) -> list[str]:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    root = ET.fromstring(raw)
    payloads: list[str] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "calendar-data" and element.text:
            payloads.append(element.text)
    return payloads


def _pull(connection: dict, session) -> tuple[list[dict], int]:
    auth = None
    if connection.get("username") or connection.get("password"):
        auth = (str(connection.get("username") or ""), str(connection.get("password") or ""))
    verify = bool(connection.get("verify_ssl", True))
    if connection.get("kind") == "ical":
        response = session.get(
            str(connection["url"]),
            auth=auth,
            timeout=20,
            verify=verify,
            headers={"Accept": "text/calendar"},
        )
        response.raise_for_status()
        return parse_ical(response.text), 1
    report = """<?xml version="1.0" encoding="utf-8" ?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"/></c:comp-filter></c:filter>
</c:calendar-query>"""
    response = session.request(
        "REPORT",
        str(connection["url"]),
        data=report.encode("utf-8"),
        auth=auth,
        timeout=25,
        verify=verify,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
    )
    response.raise_for_status()
    payloads = _calendar_payloads_from_caldav(response.content)
    events: list[dict] = []
    for payload in payloads:
        events.extend(parse_ical(payload))
    return events, len(payloads)


def _push_caldav(connection: dict, session) -> int:
    auth = None
    if connection.get("username") or connection.get("password"):
        auth = (str(connection.get("username") or ""), str(connection.get("password") or ""))
    verify = bool(connection.get("verify_ssl", True))
    pushed = 0
    for event in personal_os.list_events(include_cancelled=False):
        if event.get("source") != "cagentic":
            continue
        uid = str(event.get("external_id") or f"{event['id']}@cagentic.local")
        destination = urljoin(
            str(connection["url"]).rstrip("/") + "/", quote(uid, safe="@") + ".ics"
        )
        response = session.put(
            destination,
            data=event_to_ical(event).encode("utf-8"),
            auth=auth,
            timeout=20,
            verify=verify,
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        response.raise_for_status()
        pushed += 1
    return pushed


def sync_connection(connection_id: str, *, session=None) -> dict:
    # The background monitor and a manual Sync button can fire together.
    # Serialize adapters so they cannot race event reconciliation or status.
    with _SYNC_LOCK:
        return _sync_connection(connection_id, session=session)


def _sync_connection(connection_id: str, *, session=None) -> dict:
    connection = _match(connection_id)
    if connection is None:
        raise ValueError("connection not found")
    client = session or requests.Session()
    now = time.time()
    connection["last_attempt_at"] = now
    connection["status"] = "syncing"
    connection["last_error"] = ""
    storage.put(_NS, str(connection["id"]), connection, now)
    imported = updated = removed = pushed = payloads = 0
    try:
        source = f"calendar:{connection['id']}"
        seen: set[str] = set()
        if connection.get("direction") in ("pull", "both"):
            remote_events, payloads = _pull(connection, client)
            existing = {
                str(event.get("external_id")): event
                for event in personal_os.list_events(include_cancelled=True)
                if event.get("source") == source and event.get("external_id")
            }
            for remote in remote_events:
                uid = str(remote["uid"])
                seen.add(uid)
                was_present = uid in existing
                synced_event = personal_os.create_event(
                    remote["title"],
                    start_at=remote["start_at"],
                    end_at=remote["end_at"],
                    description=remote.get("description", ""),
                    location=remote.get("location", ""),
                    all_day=bool(remote.get("all_day")),
                    source=source,
                    external_id=uid,
                    color="#9f8cff",
                )
                remote_status = str(remote.get("status") or "confirmed")
                if remote_status not in personal_os.EVENT_STATUSES:
                    remote_status = "confirmed"
                # Reactivate a formerly cancelled event if it reappears in the
                # provider feed, as well as honoring remote cancellations.
                personal_os.update_event(str(synced_event["id"]), status=remote_status)
                if was_present:
                    updated += 1
                else:
                    imported += 1
            # A successful full feed is authoritative: events missing remotely
            # are cancelled locally instead of silently lingering forever.
            for uid, event in existing.items():
                if uid not in seen and event.get("status") != "cancelled":
                    personal_os.update_event(str(event["id"]), status="cancelled")
                    removed += 1
        if connection.get("kind") == "caldav" and connection.get("direction") in ("push", "both"):
            pushed = _push_caldav(connection, client)
        connection["status"] = "synced"
        connection["last_synced_at"] = time.time()
        connection["last_error"] = ""
    except Exception as exc:
        connection["status"] = "error"
        connection["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
        connection["updated_at"] = time.time()
        storage.put(_NS, str(connection["id"]), connection, float(connection["updated_at"]))
        return {
            "ok": False,
            "error": connection["last_error"],
            "connection": _public(connection),
            "imported": imported,
            "updated": updated,
            "removed": removed,
            "pushed": pushed,
        }
    connection["updated_at"] = time.time()
    storage.put(_NS, str(connection["id"]), connection, float(connection["updated_at"]))
    return {
        "ok": True,
        "connection": _public(connection),
        "imported": imported,
        "updated": updated,
        "removed": removed,
        "pushed": pushed,
        "payloads": payloads,
    }


def due_connections(now: float | None = None) -> list[dict]:
    now = float(now or time.time())
    due: list[dict] = []
    for raw in storage.list_values(_NS):
        if not isinstance(raw, dict) or not raw.get("auto_sync", True):
            continue
        last = float(raw.get("last_attempt_at") or 0)
        interval = max(300, int(raw.get("sync_interval") or 900))
        if now - last >= interval:
            due.append(raw)
    return due


def sync_due(*, session=None) -> list[dict]:
    return [sync_connection(str(item["id"]), session=session) for item in due_connections()]
