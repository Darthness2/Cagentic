"""Per-chat profiles and approval modes.

A chat carries its own persona and its own approval rules, and both are applied for the duration
of one turn and then put back. The putting back is the part that matters: the primary engine and
its `AppState` are shared with the terminal REPL and the web page, so a chat that ran unrestricted
must not leave approvals disarmed for whatever talks to this gateway next.
"""

from __future__ import annotations

from pathlib import Path

from cagentic.agent import Agent
from cagentic.gateway import Gateway
from cagentic.ollama_client import OllamaClient
from cagentic.permissions import can_use_tool


def make_gateway(tmp_path: Path, monkeypatch) -> Gateway:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CAGENTIC_WORKSPACE_ROOTS", str(tmp_path / "root"))
    (tmp_path / "root").mkdir(exist_ok=True)
    agent = Agent(OllamaClient("http://127.0.0.1:1"), "test", tmp_path / "root")
    return Gateway(agent, {"gateway": {"port": 19513}}, port=19513)


def always_deny(name, args, state):
    return "no"


def test_a_chat_starts_restricted(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)

    config = gw.chat_config(gw.session)

    assert config["mode"] == "restricted"
    assert config["profile"]["system_prompt"] == ""


def test_setting_a_mode_reads_back(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)

    result = gw.set_chat_config(gw.session["id"], mode="unrestricted")

    assert result["ok"]
    assert gw.chat_config(gw.session)["mode"] == "unrestricted"
    # And it is visible to a client without one having to remember what it sent.
    assert gw.current_chat()["chat_config"]["mode"] == "unrestricted"


def test_an_unknown_mode_falls_back_rather_than_erroring(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)

    gw.set_chat_config(gw.session["id"], mode="do-whatever-you-want")

    assert gw.chat_config(gw.session)["mode"] == "restricted"


def test_the_mode_applies_during_a_turn(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    gw.set_chat_config(gw.session["id"], mode="unrestricted")
    state = gw.agent.state

    with gw._chat_settings_applied(gw.session, gw.engine, state):
        allowed, reason = can_use_tool("write_file", {"path": "a.txt"}, state, always_deny)
        assert allowed and reason == "yolo mode"


def test_the_mode_is_put_back_afterwards(tmp_path, monkeypatch):
    # The whole safety argument for per-chat modes rests on this.
    gw = make_gateway(tmp_path, monkeypatch)
    gw.set_chat_config(gw.session["id"], mode="unrestricted")
    state = gw.agent.state
    assert not state.yolo

    with gw._chat_settings_applied(gw.session, gw.engine, state):
        assert state.yolo

    assert not state.yolo, "an unrestricted chat left approvals disarmed for everything else"
    allowed, _ = can_use_tool("write_file", {"path": "a.txt"}, state, always_deny)
    assert not allowed


def test_the_mode_is_put_back_even_when_the_turn_raises(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    gw.set_chat_config(gw.session["id"], mode="unrestricted")
    state = gw.agent.state

    try:
        with gw._chat_settings_applied(gw.session, gw.engine, state):
            raise RuntimeError("the model blew up mid-turn")
    except RuntimeError:
        pass

    assert not state.yolo


def test_ask_all_applies_and_restores(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    gw.set_chat_config(gw.session["id"], mode="ask_all")
    state = gw.agent.state

    with gw._chat_settings_applied(gw.session, gw.engine, state):
        allowed, _ = can_use_tool("read_file", {"path": "a.txt"}, state, always_deny)
        assert not allowed, "ask-all should send even a read to the resolver"

    assert not state.ask_all
    allowed, _ = can_use_tool("read_file", {"path": "a.txt"}, state, always_deny)
    assert allowed


def test_a_profile_reaches_the_prompt_and_is_removed_again(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    gw.set_chat_config(
        gw.session["id"],
        profile={"name": "Doctor", "system_prompt": "You are a careful clinician."},
    )
    before = gw.engine.system_suffix

    with gw._chat_settings_applied(gw.session, gw.engine, gw.agent.state):
        assert "careful clinician" in gw.engine.system_suffix

    # The suffix is rebuilt per turn, so what matters is that the persona is not still in it.
    gw._rebuild_suffix()
    assert "careful clinician" not in gw.engine.system_suffix
    assert isinstance(before, str)


def test_a_profile_replaces_the_standing_prompt_rather_than_stacking(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    gw.update_settings({"system_prompt": "Always answer in French."})
    gw.set_chat_config(
        gw.session["id"], profile={"name": "Doctor", "system_prompt": "Be a clinician."}
    )

    with gw._chat_settings_applied(gw.session, gw.engine, gw.agent.state):
        suffix = gw.engine.system_suffix

    # A profile is a persona, not an addition to one.
    assert "Be a clinician." in suffix
    assert "Always answer in French." not in suffix


def test_temperature_and_effort_are_restored(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    gw.engine.temperature = 0.4
    gw.agent.state.update(effort="medium")
    gw.set_chat_config(
        gw.session["id"], profile={"name": "Precise", "temperature": 0.05, "effort": "high"}
    )

    with gw._chat_settings_applied(gw.session, gw.engine, gw.agent.state):
        assert gw.engine.temperature == 0.05
        assert gw.agent.state.effort == "high"

    assert gw.engine.temperature == 0.4
    assert gw.agent.state.effort == "medium"


def test_an_out_of_range_temperature_is_ignored(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    gw.engine.temperature = 0.4
    gw.set_chat_config(gw.session["id"], profile={"name": "Broken", "temperature": 99})

    with gw._chat_settings_applied(gw.session, gw.engine, gw.agent.state):
        assert gw.engine.temperature == 0.4


def test_setting_one_field_leaves_the_other_alone(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    gw.set_chat_config(gw.session["id"], profile={"name": "Doctor", "system_prompt": "Careful."})

    gw.set_chat_config(gw.session["id"], mode="ask_all")

    config = gw.chat_config(gw.session)
    assert config["mode"] == "ask_all"
    assert config["profile"]["name"] == "Doctor"


def test_config_is_per_chat(tmp_path, monkeypatch):
    from cagentic import sessions

    gw = make_gateway(tmp_path, monkeypatch)
    other = sessions.make("ollama:test")
    other["messages"] = [{"role": "user", "content": "hi"}]
    sessions.save(other)

    gw.set_chat_config(gw.session["id"], mode="unrestricted")
    gw.set_chat_config(other["id"], profile={"name": "Doctor", "system_prompt": "Careful."})

    # Neither chat picked up the other's setting.
    assert gw.chat_config(gw.session)["mode"] == "unrestricted"
    assert gw.chat_config(gw.session)["profile"]["name"] == ""
    stored = sessions.load(other["id"])
    assert gw.chat_config(stored)["mode"] == "restricted"
    assert gw.chat_config(stored)["profile"]["name"] == "Doctor"
