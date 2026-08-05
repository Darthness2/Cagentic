from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

from cagentic import inbox, integrations, personal_os, proactive, reminders, routines, storage


def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    storage._MIGRATED.clear()


ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:meeting-1
DTSTART:20260714T170000Z
DTEND:20260714T180000Z
SUMMARY:Product review
LOCATION:Studio
DESCRIPTION:Review the roadmap
END:VEVENT
BEGIN:VEVENT
UID:holiday-1
DTSTART;VALUE=DATE:20260715
DTEND;VALUE=DATE:20260716
SUMMARY:Focus day
END:VEVENT
END:VCALENDAR
"""


class FakeResponse:
    def __init__(self, text="", status=200):
        self.text = text
        self.content = text.encode()
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class FakeSession:
    def __init__(self, text=ICS):
        self.text = text
        self.gets = []
        self.puts = []
        self.reports = []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return FakeResponse(self.text)

    def put(self, url, **kwargs):
        self.puts.append((url, kwargs))
        return FakeResponse()

    def request(self, method, url, **kwargs):
        self.reports.append((method, url, kwargs))
        xml = (
            '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            f"<d:response><d:propstat><d:prop><c:calendar-data>{self.text}</c:calendar-data>"
            "</d:prop></d:propstat></d:response></d:multistatus>"
        )
        return FakeResponse(xml)


def test_ical_parse_export_and_feed_sync(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    parsed = integrations.parse_ical(ICS)
    assert [event["title"] for event in parsed] == ["Product review", "Focus day"]
    assert parsed[1]["all_day"] is True

    connection = integrations.create_connection(
        "Work", "ical", "webcal://example.com/private/calendar.ics"
    )
    assert "private/calendar.ics" in connection["endpoint"]
    assert "url" not in connection

    result = integrations.sync_connection(connection["id"], session=FakeSession())
    assert result["ok"] is True
    assert result["imported"] == 2
    events = personal_os.list_events(include_cancelled=True)
    assert {event["external_id"] for event in events} == {"meeting-1", "holiday-1"}

    exported = integrations.export_calendar(events)
    assert exported.count("BEGIN:VEVENT") == 2
    assert exported.count("END:VEVENT") == 2
    assert "Product review" in exported


def test_caldav_pushes_local_events_and_redacts_password(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    personal_os.create_event("Deep work", start_at="2026-07-14 09:00")
    connection = integrations.create_connection(
        "Fastmail",
        "caldav",
        "https://calendar.example.test/dav/main/",
        username="me@example.test",
        password="super-secret",
        direction="push",
    )
    assert "password" not in connection
    assert "username" not in connection
    if os.name != "nt":
        assert storage.database_path().stat().st_mode & 0o777 == 0o600

    session = FakeSession()
    result = integrations.sync_connection(connection["id"], session=session)
    assert result["ok"] is True
    assert result["pushed"] == 1
    assert len(session.puts) == 1
    assert b"SUMMARY:Deep work" in session.puts[0][1]["data"]


def test_remote_event_removal_is_reflected_locally(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    connection = integrations.create_connection("Team", "ical", "https://example.test/team.ics")
    first = integrations.sync_connection(connection["id"], session=FakeSession())
    assert first["imported"] == 2

    only_one = ICS.replace(
        "BEGIN:VEVENT\nUID:holiday-1\nDTSTART;VALUE=DATE:20260715\n"
        "DTEND;VALUE=DATE:20260716\nSUMMARY:Focus day\nEND:VEVENT\n",
        "",
    )
    second = integrations.sync_connection(connection["id"], session=FakeSession(only_one))
    assert second["removed"] == 1
    cancelled = [e for e in personal_os.list_events(include_cancelled=True) if e["status"] == "cancelled"]
    assert cancelled[0]["external_id"] == "holiday-1"

    integrations.sync_connection(connection["id"], session=FakeSession())
    restored = [
        e
        for e in personal_os.list_events(include_cancelled=True)
        if e["external_id"] == "holiday-1"
    ][0]
    assert restored["status"] == "confirmed"


def test_proactive_notifications_are_deduplicated_and_actionable(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    now = time.time()
    reminders.add("Submit the proposal", due_at=now - 60, tags=["deadline"])

    created = proactive.scan(now=now)
    assert len(created) == 1
    assert created[0]["severity"] == "urgent"
    assert proactive.scan(now=now + 30) == []
    assert proactive.unread_count() == 1

    notification_id = created[0]["id"]
    assert proactive.mark_read(notification_id)["read"] is True
    assert proactive.unread_count() == 0
    assert proactive.dismiss(notification_id) is True
    assert proactive.list_notifications() == []


class FakeIMAP:
    HEADER = (
        b"Subject: A useful update\r\n"
        b"From: Teammate <team@example.test>\r\n"
        b"Date: Tue, 14 Jul 2026 10:00:00 -0700\r\n"
        b"Message-ID: <message-1@example.test>\r\n\r\n"
    )

    def __init__(self, host, port):
        self.host = host
        self.port = port

    def login(self, username, password):
        assert username == "me@example.test"
        assert password == "app-password"
        return "OK", []

    def select(self, folder, readonly=True):
        assert folder == "INBOX"
        assert readonly is True
        return "OK", []

    def uid(self, operation, *args):
        if operation == "search":
            return "OK", [b"41"]
        assert operation == "fetch"
        assert b"BODY.PEEK" in args[1].encode()
        return "OK", [(b"41 (RFC822.SIZE 200)", self.HEADER)]

    def logout(self):
        return "BYE", []


def test_email_headers_sync_into_unified_inbox_without_exposing_credentials(
    tmp_path, monkeypatch
):
    isolate(tmp_path, monkeypatch)
    connection = inbox.create_email_connection(
        "Work mail",
        "imap.example.test",
        "me@example.test",
        "app-password",
    )
    assert "password" not in connection
    assert "username" not in connection

    result = inbox.sync_email_connection(connection["id"], imap_factory=FakeIMAP)
    assert result["ok"] is True
    assert result["imported"] == 1
    item = inbox.list_items()[0]
    assert item["title"] == "A useful update"
    assert item["kind"] == "email"
    assert item["status"] == "new"

    inbox.update_item(item["id"], status="done")
    again = inbox.sync_email_connection(connection["id"], imap_factory=FakeIMAP)
    assert again["updated"] == 1
    assert inbox.list_items()[0]["status"] == "done"


def test_due_routine_runs_once_and_creates_notification(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    now = datetime.now().replace(second=0, microsecond=0)
    schedule = (now - timedelta(minutes=1)).strftime("%H:%M")
    routine = routines.create_routine(
        "Morning orientation",
        schedule_time=schedule,
        days=[now.weekday()],
    )
    assert [item["id"] for item in routines.due_routines()] == [routine["id"]]

    monitor = proactive.ProactiveMonitor(
        {"proactive": {"desktop_notifications": False}},
        on_routine=lambda item: "Here is your focused plan.",
    )
    first = monitor.tick()
    assert first["routine_results"] == [{"id": routine["id"], "ok": True, "error": None}]
    assert proactive.list_notifications()[0]["body"] == "Here is your focused plan."
    assert routines.due_routines() == []
    assert monitor.tick()["routine_results"] == []
