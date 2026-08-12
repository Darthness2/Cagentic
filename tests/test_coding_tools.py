"""Tests for the coding-agent tools absorbed from Collama, plus the
gateway's Collama-compat surface."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from cagentic.coding_tools import (
    t_brief,
    t_check_syntax,
    t_enter_worktree,
    t_exit_worktree,
    t_multi_edit,
    t_notebook_edit,
    t_task_create,
    t_task_delete,
    t_task_update,
)
from cagentic.state import AppState
from cagentic.tasks import TaskGraph
from cagentic.teams import TeamRegistry
from cagentic.tools import (
    DEFAULT_GROUPS,
    TOOL_GROUPS,
    ToolContext,
    _all_tools,
    all_tool_schemas,
    dispatch,
    t_task_get,
    t_task_list,
    t_tool_search,
)


@pytest.fixture()
def ctx(tmp_path):
    state = AppState(workspace=tmp_path, home=tmp_path)
    return ToolContext(
        root=tmp_path, yolo=True, state=state, tasks=TaskGraph(root=tmp_path / "tasks")
    )


# ---------------------------------------------------------------- registry --


def test_every_grouped_tool_is_dispatchable():
    tools = _all_tools()
    for group, names in TOOL_GROUPS.items():
        for name in names:
            assert name in tools, f"{group}/{name} has no implementation"


def test_every_grouped_tool_has_a_schema():
    names = {s["function"]["name"] for s in all_tool_schemas(set(TOOL_GROUPS), compact=False)}
    for group, gnames in TOOL_GROUPS.items():
        for name in gnames:
            assert name in names, f"{group}/{name} has no schema"


def test_tool_search_includes_split_registries(ctx):
    assert "notebook_edit" in t_tool_search({"query": "notebook"}, ctx)
    assert "gh_list_pulls" in t_tool_search({"query": "pull requests"}, ctx)


def test_collama_coding_groups_are_default_enabled():
    assert {"coding", "worktree", "subagent"} <= DEFAULT_GROUPS
    default_names = {s["function"]["name"] for s in all_tool_schemas()}
    for name in (
        "check_syntax",
        "multi_edit",
        "notebook_edit",
        "enter_worktree",
        "exit_worktree",
        "agent_call",
        "task_create",
        "task_update",
        "task_delete",
        "brief",
    ):
        assert name in default_names


# ------------------------------------------------------------ check_syntax --


def test_check_syntax_passes_valid_python(ctx, tmp_path):
    (tmp_path / "good.py").write_text("x = 1\n")
    out = t_check_syntax({"path": "good.py"}, ctx)
    assert out.startswith("syntax check: all passed")


def test_check_syntax_fails_broken_python(ctx, tmp_path):
    (tmp_path / "bad.py").write_text("def f(:\n")
    out = t_check_syntax({"path": "bad.py"}, ctx)
    assert "FAILED" in out and "SyntaxError" in out


def test_check_syntax_inline_json(ctx):
    assert t_check_syntax({"content": '{"a": 1}', "language": "json"}, ctx).startswith("PASS")
    assert t_check_syntax({"content": "{oops}", "language": "json"}, ctx).startswith("FAIL")


# -------------------------------------------------------------- multi_edit --


def test_multi_edit_applies_in_order(ctx, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("one\ntwo\nthree\n")
    out = t_multi_edit(
        {
            "path": "f.txt",
            "edits": [
                {"old_string": "one", "new_string": "1"},
                {"old_string": "two", "new_string": "2"},
            ],
        },
        ctx,
    )
    assert out.startswith("OK: applied 2 edits")
    assert p.read_text() == "1\n2\nthree\n"


def test_multi_edit_is_atomic_on_failure(ctx, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("one\ntwo\n")
    out = t_multi_edit(
        {
            "path": "f.txt",
            "edits": [
                {"old_string": "one", "new_string": "1"},
                {"old_string": "MISSING", "new_string": "x"},
            ],
        },
        ctx,
    )
    assert out.startswith("ERROR: edit #2")
    assert p.read_text() == "one\ntwo\n"  # nothing written


# ----------------------------------------------------------- notebook_edit --


def test_notebook_edit_roundtrip(ctx, tmp_path):
    out = t_notebook_edit({"path": "nb.ipynb", "op": "insert", "source": "print(1)"}, ctx)
    assert out.startswith("OK: insert")
    nb = json.loads((tmp_path / "nb.ipynb").read_text())
    assert nb["cells"][0]["source"] == "print(1)"
    got = t_notebook_edit({"path": "nb.ipynb", "op": "get", "cell_index": 0}, ctx)
    assert "print(1)" in got


def test_notebook_replace_preserves_cell_type_by_default(ctx, tmp_path):
    t_notebook_edit(
        {
            "path": "nb.ipynb",
            "op": "insert",
            "cell_type": "markdown",
            "source": "before",
        },
        ctx,
    )
    result = t_notebook_edit(
        {"path": "nb.ipynb", "op": "replace", "cell_index": 0, "source": "after"},
        ctx,
    )
    assert result.startswith("OK: replace")
    notebook = json.loads((tmp_path / "nb.ipynb").read_text())
    assert notebook["cells"][0]["cell_type"] == "markdown"


# ---------------------------------------------------------------- worktree --


def test_worktree_push_pop(ctx, tmp_path):
    out = t_enter_worktree({"path": "sub", "create": True}, ctx)
    assert out.startswith("OK: entered")
    assert ctx.root == (tmp_path / "sub").resolve()
    out = t_exit_worktree({}, ctx)
    assert out.startswith("OK: exited")
    assert ctx.root == tmp_path


def test_exit_worktree_on_empty_stack(ctx):
    assert t_exit_worktree({}, ctx).startswith("ERROR")


# -------------------------------------------------------------- task graph --


def test_task_graph_crud_via_tools(ctx):
    out = t_task_create({"title": "port everything"}, ctx)
    assert out.startswith("OK: created")
    tid = out.split()[2]
    assert "port everything" in t_task_get({"id": tid}, ctx)
    assert "port everything" in t_task_list({}, ctx)
    assert t_task_update({"id": tid, "status": "done"}, ctx).startswith("OK")
    assert t_task_delete({"id": tid}, ctx) == "OK: deleted"


def test_brief_set_get_delete(ctx):
    assert t_brief({"name": "s", "content": "hello"}, ctx).startswith("OK")
    assert t_brief({"op": "get", "name": "s"}, ctx) == "hello"
    assert t_brief({"op": "delete", "name": "s"}, ctx).startswith("OK")
    assert "(no brief" in t_brief({"op": "get", "name": "s"}, ctx)


# -------------------------------------------------------------------- teams --


def test_team_registry_roundtrip(tmp_path):
    reg = TeamRegistry(root=tmp_path / "teams")
    reg.create_team("alpha")
    tm = reg.add_teammate("alpha", "scout", role="researcher", skills=["search"])
    assert reg.get_teammate("alpha", "scout").id == tm.id
    assert reg.deliver("alpha", "scout", sender="lead", content="hi") is not None
    assert len(reg.get_teammate("alpha", tm.id).mailbox) == 1
    assert reg.delete_teammate("alpha", "scout")
    assert reg.delete_team("alpha")


def test_teams_tools_via_dispatch(ctx, tmp_path):
    ctx.teams = TeamRegistry(root=tmp_path / "teams")
    assert dispatch("team_create", {"name": "beta"}, ctx).startswith("OK")
    assert dispatch("teammate_create", {"team": "beta", "name": "dev"}, ctx).startswith("OK")
    assert "beta/dev" in dispatch("teammate_list", {}, ctx)
    assert dispatch(
        "send_message", {"team": "beta", "to": "dev", "content": "build it"}, ctx
    ).startswith("OK")
    assert "build it" in dispatch("inbox", {"team": "beta", "name": "dev"}, ctx)


# ------------------------------------------------------------------- effort --


def test_effort_section_in_system_prompt(tmp_path):
    from cagentic.engine import fetch_system_prompt_parts

    state = AppState(workspace=tmp_path, home=tmp_path)
    state.effort = "high"
    assert "EFFORT LEVEL: HIGH" in fetch_system_prompt_parts(state)
    state.effort = "low"
    assert "EFFORT LEVEL: LOW" in fetch_system_prompt_parts(state)


# ---------------------------------------------------- gateway compat surface --


def _make_gateway(tmp_path, port):
    from cagentic.agent import Agent
    from cagentic.gateway import Gateway
    from cagentic.ollama_client import OllamaClient

    agent = Agent(OllamaClient("http://localhost:11434"), "testmodel", tmp_path)
    return Gateway(agent, {"gateway": {"port": port}}, port=port)


def test_gateway_collama_compat_endpoints(tmp_path, monkeypatch):
    import urllib.request

    from cagentic import config as _config

    # /api/effort persists to config — don't let the test touch the real file.
    monkeypatch.setattr(_config, "save", lambda cfg: None)
    gw = _make_gateway(tmp_path, 18991)
    assert gw.start(), gw.error
    try:

        def req(path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            r = urllib.request.Request(
                f"http://127.0.0.1:18991{path}",
                data=data,
                headers={"X-Cagentic-Token": gw.token, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(r) as resp:
                return json.loads(resp.read())

        b = req("/api/bootstrap")
        for key in ("workspace", "effort", "yolo", "models", "model"):
            assert key in b
        assert req("/api/effort", {"effort": "high"}) == {"ok": True, "effort": "high"}
        assert gw.agent.state.effort == "high"
        sub = tmp_path / "proj"
        sub.mkdir()
        assert req("/api/workspace", {"path": str(sub)})["ok"] is True
        assert "sessions" in req("/api/sessions")
        assert req("/api/session/new", {"title": "t"})["ok"] is True
    finally:
        gw.stop()
