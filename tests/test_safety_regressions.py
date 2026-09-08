"""Safety properties that were claimed but not true.

Each of these was verified broken before the fix. They are grouped here rather
than scattered because they share a shape: a restriction that existed in the
documentation, the docstring, or the schema the model is shown — but not in the
code path that actually decides.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from cagentic.permissions import can_use_tool, effective_rules, tool_is_enabled
from cagentic.state import AppState

APPROVE = lambda *_a: "yes"  # noqa: E731 - a one-line stub reads better inline
DENY = lambda *_a: "no"  # noqa: E731


def project(tmp_path: Path, filename: str, doc: dict) -> AppState:
    d = tmp_path / ".cagentic"
    d.mkdir(exist_ok=True)
    (d / filename).write_text(json.dumps(doc), encoding="utf-8")
    return AppState(workspace=tmp_path, home=tmp_path)


# ------------------------------- a clone must not be able to grant itself --


def test_a_committed_file_cannot_grant_shell(tmp_path):
    """The whole hole in one line: an allow rule returns True above yolo and
    above the resolver, and settings.json arrives with `git clone`."""
    state = project(tmp_path, "settings.json", {"permissions": {"allow": ["run_bash(*)"]}})

    assert effective_rules(state)["allow"] == []
    allowed, _why = can_use_tool("run_bash", {"command": "rm -rf /"}, state, DENY)
    assert allowed is False


def test_a_committed_file_can_still_deny(tmp_path):
    """Restricting is always safe, from either file."""
    state = project(tmp_path, "settings.json", {"permissions": {"deny": ["run_bash"]}})
    state.update(yolo=True)

    allowed, why = can_use_tool("run_bash", {"command": "ls"}, state, APPROVE)
    assert allowed is False
    assert "denied by rule" in why


def test_the_gitignored_file_may_grant(tmp_path):
    state = project(tmp_path, "settings.local.json", {"permissions": {"allow": ["run_bash(ls*)"]}})
    allowed, why = can_use_tool("run_bash", {"command": "ls -l"}, state, DENY)
    assert allowed is True
    assert "allowed by rule" in why


# ----------------------------------- a tool group has to actually restrict --


def narrowed(**kw) -> AppState:
    return AppState(workspace=Path(tempfile.mkdtemp()), home=Path.home(), **kw)


def test_a_group_the_agent_does_not_have_is_refused():
    """tool_groups used to be a schema filter only — neither the executor nor
    dispatch consulted it — so a child told "files only" that emitted run_bash
    anyway simply ran it."""
    state = narrowed(tool_groups={"files"}, yolo=True)

    assert can_use_tool("read_file", {"path": "x"}, state, APPROVE)[0] is True
    allowed, why = can_use_tool("run_bash", {"command": "rm -rf /"}, state, APPROVE)
    assert allowed is False
    assert "not in an enabled tool group" in why


def test_an_alias_cannot_walk_around_the_group_check():
    """`bash` dispatches to run_bash, so checking the raw name would have been
    a check on a name nothing enforces."""
    state = narrowed(tool_groups={"files"}, yolo=True)
    for alias in ("bash", "shell", "exec"):
        allowed, _why = can_use_tool(alias, {"command": "rm -rf /"}, state, APPROVE)
        assert allowed is False, f"{alias} got through"


def test_the_default_agent_is_unaffected():
    state = narrowed(yolo=True)
    for name, args in (("read_file", {"path": "x"}), ("run_bash", {"command": "ls"})):
        assert can_use_tool(name, args, state, APPROVE)[0] is True


def test_tool_is_enabled_resolves_renamed_groups():
    state = narrowed(tool_groups={"worktree"})  # renamed to "workspace"
    assert tool_is_enabled("push_workspace", state) is True


# ------------------------------------- parallel-safe must mean what it says --


def test_no_group_called_parallel_safe_can_write():
    from cagentic.permissions import CONCURRENT_SAFE
    from cagentic.subagent import parallel_safe_groups
    from cagentic.tools import TOOL_GROUPS

    for group in parallel_safe_groups():
        writers = [t for t in TOOL_GROUPS[group] if t not in CONCURRENT_SAFE]
        assert not writers, f"{group} is parallel-safe but holds {writers}"


@pytest.mark.parametrize("group", ["files", "mcp", "system", "shell"])
def test_the_groups_that_were_wrongly_listed_are_excluded(group):
    from cagentic.subagent import subagent_is_read_only

    assert subagent_is_read_only({group}) is False


# ---------------------------------------------- a hook must not break a turn --


@pytest.mark.parametrize("timeout", ["ten", None, [], {"a": 1}, float("nan")])
def test_a_malformed_hook_timeout_cannot_kill_the_turn(timeout):
    """float() sat outside the guard, so a hand-edited config raised straight
    through decision() — which, unlike emit(), has no guard of its own."""
    from cagentic import hooks

    cfg = {"hooks": {"PreToolUse": [{"command": "true", "timeout": timeout}]}}
    verdict, _message = hooks.decision("PreToolUse", {}, cfg)
    assert verdict in ("allow", "warn", "deny")


# ------------------------------------------- a skill must survive a refresh --


def test_an_attached_skill_survives_a_prompt_rebuild(tmp_path, monkeypatch):
    """refresh_system_prompt replaces messages[0] wholesale, and it is called
    from ~20 places including enter_plan_mode, /effort and switching model. The
    skill used to be concatenated into that string, so it vanished silently."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    skills = tmp_path / ".cagentic" / "skills"
    skills.mkdir(parents=True)
    (skills / "review.md").write_text("description: the checklist\n\nStep one.\n")

    from cagentic.engine import QueryEngine
    from cagentic.tools import ToolContext, dispatch

    class NullClient:
        def chat(self, *_a, **_k):
            raise AssertionError("no network in tests")

    state = AppState(workspace=tmp_path, home=tmp_path)
    engine = QueryEngine(client=NullClient(), state=state, model="test", config={})
    ctx = ToolContext(root=tmp_path, state=state, engine=engine)

    assert dispatch("skill", {"op": "use", "name": "review"}, ctx).startswith("OK:")

    def attached() -> bool:
        return "=== SKILL: review ===" in engine.messages[0]["content"]

    assert attached()
    engine.refresh_system_prompt()
    assert attached(), "a prompt rebuild dropped the skill"
    dispatch("enter_plan_mode", {}, ctx)
    assert attached(), "entering plan mode dropped the skill"
    assert engine.messages[0]["content"].count("=== SKILL: review ===") == 1


# --------------------------------------- every write path, notebooks included --


def notebook(tmp_path: Path) -> tuple[Path, "ToolContext"]:  # noqa: F821
    from cagentic.tools import ToolContext

    path = tmp_path / "n.ipynb"
    path.write_text(
        json.dumps(
            {
                "cells": [
                    {"cell_type": "code", "source": "x = 1", "metadata": {}},
                    {"cell_type": "code", "source": "y = 2", "metadata": {}},
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        )
    )
    state = AppState(workspace=tmp_path, home=tmp_path)
    return path, ToolContext(root=tmp_path, state=state, read_cache={})


def test_notebook_edit_needs_a_read_first(tmp_path):
    """It was the one write path outside the precondition — and it deletes by
    index, which is the sharpest form of editing from a guess."""
    from cagentic.coding_tools import t_notebook_edit

    path, ctx = notebook(tmp_path)
    out = t_notebook_edit({"path": "n.ipynb", "op": "delete", "cell_index": 1}, ctx)

    assert out.startswith("ERROR:")
    assert len(json.loads(path.read_text())["cells"]) == 2, "nothing should have been written"


def test_notebook_edit_works_after_a_read(tmp_path):
    from cagentic.coding_tools import t_notebook_edit
    from cagentic.tools import t_read_file

    path, ctx = notebook(tmp_path)
    t_read_file({"path": "n.ipynb"}, ctx)

    assert t_notebook_edit({"path": "n.ipynb", "op": "delete", "cell_index": 1}, ctx).startswith(
        "OK:"
    )
    assert len(json.loads(path.read_text())["cells"]) == 1


def test_notebook_edit_notices_an_external_change(tmp_path):
    """A notebook saved in Jupyter mid-turn used to be clobbered silently."""
    from cagentic.coding_tools import t_notebook_edit
    from cagentic.tools import t_read_file

    path, ctx = notebook(tmp_path)
    t_read_file({"path": "n.ipynb"}, ctx)
    path.write_text(json.dumps({"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}))

    out = t_notebook_edit({"path": "n.ipynb", "op": "insert", "source": "z = 3"}, ctx)
    assert out.startswith("ERROR:")
    assert "changed on disk" in out


def test_reading_a_notebook_never_needs_a_read(tmp_path):
    from cagentic.coding_tools import t_notebook_edit

    _path, ctx = notebook(tmp_path)
    assert not t_notebook_edit({"path": "n.ipynb", "op": "get"}, ctx).startswith("ERROR:")


def test_a_malformed_argument_still_reports_itself(tmp_path):
    """The guard must not mask validation — it sits before the write, not
    before the parse."""
    from cagentic.coding_tools import t_notebook_edit

    _path, ctx = notebook(tmp_path)
    out = t_notebook_edit({"path": "n.ipynb", "op": "delete", "cell_index": "x"}, ctx)
    assert "cell_index must be an integer" in out


def test_creating_a_notebook_is_never_blocked(tmp_path):
    from cagentic.coding_tools import t_notebook_edit

    _path, ctx = notebook(tmp_path)
    assert t_notebook_edit(
        {"path": "new.ipynb", "op": "insert", "source": "z = 3"}, ctx
    ).startswith("OK:")


# -- the container bind address is not LAN mode ------------------------------
#
# The container image has to listen on 0.0.0.0 to be reachable through a
# published port at all. Doing that by setting gateway.lan would have taken the
# anti-DNS-rebinding Host check off with it, for every containerised gateway,
# invisibly. These pin the two apart.


def _gw(tmp_path, monkeypatch, cfg):
    from cagentic import gateway as gateway_module
    from cagentic.agent import Agent
    from cagentic.gateway import Gateway

    class _NullClient:
        def chat(self, _model, _messages, **_kwargs):
            return {"content": ""}

        def chat_stream(self, _model, _messages, **_kwargs):
            yield ""

        def list_models(self):
            return []

    monkeypatch.setattr(gateway_module, "_warm_model_cache", lambda _cfg: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    agent = Agent(_NullClient(), "test-model", tmp_path, stream=True)
    return Gateway(agent, cfg, port=18993)


def test_the_default_gateway_is_loopback_only(tmp_path, monkeypatch):
    monkeypatch.delenv("CAGENTIC_GATEWAY_BIND", raising=False)
    assert _gw(tmp_path, monkeypatch, {}).bind == "127.0.0.1"


def test_lan_mode_still_binds_every_interface(tmp_path, monkeypatch):
    monkeypatch.delenv("CAGENTIC_GATEWAY_BIND", raising=False)
    assert _gw(tmp_path, monkeypatch, {"gateway": {"lan": True}}).bind == "0.0.0.0"


@pytest.mark.parametrize(
    "cfg, env",
    [
        ({"gateway": {"bind": "0.0.0.0"}}, None),
        ({}, "0.0.0.0"),
    ],
)
def test_binding_every_interface_does_not_turn_on_lan_mode(tmp_path, monkeypatch, cfg, env):
    if env is None:
        monkeypatch.delenv("CAGENTIC_GATEWAY_BIND", raising=False)
    else:
        monkeypatch.setenv("CAGENTIC_GATEWAY_BIND", env)

    gw = _gw(tmp_path, monkeypatch, cfg)

    assert gw.bind == "0.0.0.0"
    # lan is what relaxes the Host check and starts mDNS advertising; a bind
    # override must touch neither.
    assert gw.lan is False
