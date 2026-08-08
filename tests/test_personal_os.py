from __future__ import annotations

import errno
import time

from cagentic import personal_os, storage
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


def test_gateway_no_longer_serves_the_personal_os_dashboard(tmp_path, monkeypatch):
    """The gateway is a chat surface again, not a personal-OS dashboard.

    The sidebar's "SYSTEM ROUTES" pages (Core, Inbox, Planner, Goals, Routines,
    Skills, Connections) and the ~25 /api/os/* endpoints behind them are gone.
    Bootstrap no longer ships a dashboard payload either — the browser stopped
    reading it, and building one meant a briefing + architecture scan on every
    page load.
    """
    _isolated_config(tmp_path, monkeypatch)
    agent = Agent(OllamaClient("http://127.0.0.1:1"), "test", tmp_path)
    gateway = Gateway(agent, {"gateway": {"port": 8700}}, port=8700)

    assert "os" not in gateway.bootstrap()
    for gone in ("personal_os_snapshot", "create_goal", "create_inbox_item", "create_routine"):
        assert not hasattr(gateway, gone), f"{gone} should have gone with the dashboard"

    # The background proactive runner is NOT part of the dashboard and must
    # survive — the monitor is wired to it in Gateway.__init__.
    assert callable(gateway._run_proactive_routine)


def test_the_agent_can_still_reach_personal_os_data(tmp_path, monkeypatch):
    """Removing the dashboard must not remove the underlying capability.

    personal_os is shared by the (deleted) dashboard and the agent's `life`
    tool group. Only the former went; the model can still create and read a
    goal, which is what "ask Cagentic about my goals" depends on.
    """
    _isolated_config(tmp_path, monkeypatch)
    from cagentic.state import AppState
    from cagentic.tools import TOOL_GROUPS, ToolContext, dispatch

    assert "goal_create" in TOOL_GROUPS["life"]
    ctx = ToolContext(root=tmp_path, state=AppState(workspace=tmp_path, home=tmp_path))
    created = dispatch("goal_create", {"title": "Ship the rework"}, ctx)
    assert not created.startswith("ERROR:"), created
    assert "Ship the rework" in dispatch("goal_list", {}, ctx)
