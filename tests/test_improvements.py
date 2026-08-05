from __future__ import annotations

import json
from pathlib import Path

from cagentic import projects, reminders, sessions
from cagentic.agent import Agent
from cagentic.cli import main, parse_args
from cagentic.completion import script
from cagentic.gateway import Gateway
from cagentic.ollama_client import OllamaClient
from cagentic.storage import database_path
from cagentic.token_count import count_messages


def test_sqlite_migrates_legacy_sessions_and_projects(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    session_dir = tmp_path / "cagentic" / "sessions"
    project_dir = tmp_path / "cagentic" / "projects"
    session_dir.mkdir(parents=True)
    project_dir.mkdir(parents=True)
    (session_dir / "legacy.json").write_text(
        json.dumps({"id": "legacy", "title": "Old", "messages": [], "updated_at": 1})
    )
    (project_dir / "project1.json").write_text(
        json.dumps({"id": "project1", "name": "Old project", "updated_at": 1})
    )
    assert sessions.load("legacy")["title"] == "Old"
    assert projects.load("project1")["name"] == "Old project"
    assert database_path().exists()
    assert (session_dir / "legacy.json").exists()


def test_sqlite_reminder_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    reminder = reminders.add("remember sqlite")
    assert reminders.update(reminder.id, status="done").status == "done"
    assert reminders.delete(reminder.id)


def test_cli_management_flags_parse_and_completion():
    args = parse_args(["--doctor", "--json"])
    assert args.doctor and args.json
    assert "complete" in script("bash")
    assert "#compdef" in script("zsh")
    assert "complete -c cagentic" in script("fish")


def test_module_entrypoint_propagates_exit_status():
    assert "SystemExit(main())" in Path("cagentic/__main__.py").read_text()


def test_cli_sessions_and_search_do_not_require_model(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    saved = sessions.make("test", title="Needle title")
    saved["messages"] = [{"role": "user", "content": "searchable phrase"}]
    sessions.save(saved)
    assert main(["--search", "searchable"]) == 0
    assert saved["id"] in capsys.readouterr().out


def test_token_count_fallback_is_deterministic():
    messages = [{"role": "user", "content": "abcd" * 20}]
    assert count_messages(messages, "ollama:test") == count_messages(messages, "ollama:test")


def test_gateway_assets_are_packaged_and_externalized():
    from importlib.resources import files

    assets = files("cagentic.gateway_assets")
    assert "<!doctype html>" in assets.joinpath("index.html").read_text()
    assert len(assets.joinpath("app.css").read_text()) > 10_000
    assert len(assets.joinpath("app.js").read_text()) > 10_000


def test_gateway_session_actors_isolate_project_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CAGENTIC_WORKSPACE_ROOTS", str(tmp_path / "root"))
    root = tmp_path / "root"
    root.mkdir()
    gw = Gateway(Agent(OllamaClient("http://127.0.0.1:1"), "test", root), {})
    folder = tmp_path / "root" / "actor-folder"
    folder.mkdir()
    folder_id = gw.new_chat(gw.validate_project({"kind": "gatewayFolder", "value": str(folder)}))[
        "id"
    ]
    repo_id = gw.new_chat(gw.validate_project({"kind": "repository", "value": "owner/actor"}))["id"]
    folder_actor = gw._session_actor(folder_id)
    repo_actor = gw._session_actor(repo_id)
    assert folder_actor["engine"].state.workspace == folder
    assert folder_actor["engine"].state.workspace_boundary == folder
    assert repo_actor["engine"].state.default_repository == "owner/actor"
    assert repo_actor["engine"].state.workspace != folder
