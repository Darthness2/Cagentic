from __future__ import annotations

import errno
import time

from cagentic import capabilities, personal_os, storage
from cagentic.agent import Agent
from cagentic.gateway import Gateway
from cagentic.ollama_client import OllamaClient


def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    storage._MIGRATED.clear()


def test_personal_os_goal_event_and_briefing_roundtrip(tmp_path, monkeypatch):
    _isolated_config(tmp_path, monkeypatch)
    current = time.localtime()
    now = time.mktime((current.tm_year, current.tm_mon, current.tm_mday, 12, 0, 0, 0, 0, -1))
    goal = personal_os.create_goal(
        "Launch the personal OS", target_at=now + 5 * 86400, progress=20, category="work"
    )
    event = personal_os.create_event(
        "Design review",
        start_at=now + 1800,
        end_at=now + 5400,
        location="Studio",
    )

    assert personal_os.update_goal(goal["id"][:5], progress=55)["progress"] == 55
    assert personal_os.list_events(start_at=now, end_at=now + 86400)[0]["id"] == event["id"]

    data = personal_os.briefing(now=now)
    assert data["stats"]["active_goals"] == 1
    assert data["stats"]["events_today"] >= 1
    assert any(item["kind"] == "event" for item in data["agenda"])
    assert "Launch the personal OS" in personal_os.system_context(now=now)


def test_external_calendar_event_upserts_by_source_and_id(tmp_path, monkeypatch):
    _isolated_config(tmp_path, monkeypatch)
    first = personal_os.create_event(
        "Original", start_at="2026-07-14 09:00", source="google", external_id="abc"
    )
    second = personal_os.create_event(
        "Updated", start_at="2026-07-14 10:00", source="google", external_id="abc"
    )
    assert second["id"] == first["id"]
    assert second["title"] == "Updated"
    assert len(personal_os.list_events(include_cancelled=True)) == 1


def test_gateway_uses_next_port_when_configured_port_is_busy(tmp_path, monkeypatch):
    _isolated_config(tmp_path, monkeypatch)
    attempts = []

    class FakeServer:
        def __init__(self, address, handler):
            attempts.append(address[1])
            if address[1] == 8700:
                raise OSError(errno.EADDRINUSE, "Address already in use")
            self.server_address = address
            self.daemon_threads = False

        def serve_forever(self):
            return None

        def shutdown(self):
            return None

        def server_close(self):
            return None

    monkeypatch.setattr("cagentic.gateway._GatewayHTTPServer", FakeServer)
    agent = Agent(OllamaClient("http://127.0.0.1:1"), "test", tmp_path)
    gateway = Gateway(agent, {"gateway": {"port": 8700, "auto_port": True}}, port=8700)
    assert gateway.start()
    assert attempts == [8700, 8701]
    assert gateway.port == 8701
    assert gateway.port_fallback is True
    assert gateway.url() == "http://localhost:8701"
    assert "already in use" in gateway.start_notice
    gateway.stop()


def test_gateway_bootstrap_exposes_personal_os(tmp_path, monkeypatch):
    _isolated_config(tmp_path, monkeypatch)
    agent = Agent(OllamaClient("http://127.0.0.1:1"), "test", tmp_path)
    gateway = Gateway(agent, {"gateway": {"port": 8700}}, port=8700)
    result = gateway.create_goal({"title": "Stay focused", "progress": 10})
    assert result["ok"] is True
    assert gateway.create_inbox_item({"title": "Review the brief"})["ok"] is True
    assert gateway.create_routine(
        {
            "name": "Daily orientation",
            "schedule_time": "08:00",
            "days": [0, 1, 2, 3, 4],
        }
    )["ok"] is True
    bootstrap = gateway.bootstrap()
    assert bootstrap["os"]["goals"][0]["title"] == "Stay focused"
    assert bootstrap["os"]["stats"]["active_goals"] == 1
    assert bootstrap["os"]["inbox"][0]["title"] == "Review the brief"
    assert bootstrap["os"]["unread_inbox"] == 1
    assert bootstrap["os"]["routines"][0]["name"] == "Daily orientation"
    architecture = bootstrap["os"]["architecture"]
    assert architecture["conductor"]["name"] == "Cagentic"
    assert {branch["id"] for branch in architecture["branches"]} >= {
        "memory",
        "productivity",
        "research",
        "content",
        "community",
        "agency",
        "sales",
        "finance",
        "ops",
    }
    assert architecture["status"]["routines"] == 1


def test_capability_catalog_is_grounded_in_available_tools():
    catalog = capabilities.build_architecture(
        available_tools={"note_write", "note_get", "note_search"},
        active_tools={"note_write", "note_get"},
        installed_skills=["meeting-notes"],
    )
    memory = next(branch for branch in catalog["branches"] if branch["id"] == "memory")
    vault = next(item for item in memory["capabilities"] if item["name"] == "Personal Vault")
    recall = next(
        item for item in memory["capabilities"] if item["name"] == "Conversation Recall"
    )
    assert vault["available"] is True
    assert vault["active"] is False
    assert recall["available"] is False
    assert catalog["status"]["available_tools"] == 3
    assert catalog["status"]["active_tools"] == 2
    assert catalog["status"]["installed_skills"] == 1
    ops = next(branch for branch in catalog["branches"] if branch["id"] == "ops")
    assert any(item["name"] == "meeting-notes" for item in ops["capabilities"])
