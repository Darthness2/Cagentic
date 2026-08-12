from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from cagentic import sessions
from cagentic.agent import Agent
from cagentic.gateway import Gateway
from cagentic.ollama_client import OllamaClient


def make_gateway(tmp_path: Path, monkeypatch, port: int) -> Gateway:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CAGENTIC_WORKSPACE_ROOTS", str(tmp_path / "root"))
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    agent = Agent(OllamaClient("http://127.0.0.1:1"), "test", root)
    return Gateway(agent, {"gateway": {"port": port}}, port=port)


def request(gw: Gateway, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{gw.port}{path}",
        data=data,
        headers={"X-Cagentic-Token": gw.token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_workspace_browse_roots_children_sorting_and_files(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch, 18992)
    root = tmp_path / "root"
    (root / "zulu").mkdir()
    (root / "Alpha").mkdir()
    (root / "beta").mkdir()
    (root / "file.txt").write_text("not a folder")
    assert gw.start()
    try:
        status, payload = request(gw, "/api/workspace/browse?path=")
        assert status == 200
        assert payload["folders"] == [{"name": "root", "path": str(root.resolve())}]
        encoded = urllib.parse.quote(str(root), safe="")
        status, payload = request(gw, f"/api/workspace/browse?path={encoded}")
        assert status == 200
        assert [f["name"] for f in payload["folders"]] == ["Alpha", "beta", "zulu"]
        assert "file.txt" not in json.dumps(payload)
    finally:
        gw.stop()


def test_workspace_rejects_outside_traversal_and_symlink_escape(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch, 18993)
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    assert gw.start()
    try:
        for candidate in (outside, root / ".." / "outside", root / "escape"):
            encoded = urllib.parse.quote(str(candidate), safe="")
            status, _ = request(gw, f"/api/workspace/browse?path={encoded}")
            assert status in (400, 403)
    finally:
        gw.stop()


def test_workspace_selection_canonicalizes(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch, 18994)
    child = tmp_path / "root" / "child"
    child.mkdir()
    alias = tmp_path / "root" / "alias"
    alias.symlink_to(child, target_is_directory=True)
    assert gw.start()
    try:
        status, payload = request(gw, "/api/workspace", {"path": str(alias)})
        assert status == 200
        assert payload["workspace"] == str(child.resolve())
    finally:
        gw.stop()


def test_collama_requires_project_but_normal_new_chat_does_not(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch, 18995)
    assert gw.start()
    try:
        status, payload = request(gw, "/api/chats/new", {"client": "collama"})
        assert status == 400 and "project" in payload["error"]
        status, payload = request(gw, "/api/chat", {"client": "collama", "prompt": "hello"})
        assert status == 400 and "project" in payload["error"]
        status, payload = request(gw, "/api/chats/new", {})
        assert status == 200 and payload["current"]["id"]
        req = urllib.request.Request(
            f"http://127.0.0.1:{gw.port}/api/chat",
            data=json.dumps({"message": "hello"}).encode(),
            headers={"X-Cagentic-Token": gw.token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as response:
            assert response.status == 200
    finally:
        gw.stop()


def test_project_persistence_immutability_legacy_assignment_and_bootstrap(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch, 18996)
    folder = tmp_path / "root" / "one"
    folder.mkdir()
    project = {"kind": "gatewayFolder", "value": str(folder)}
    current = gw.new_chat(gw.validate_project(project))
    gw.session["messages"] = [{"role": "user", "content": "hello"}]
    gw.engine.messages = list(gw.session["messages"])
    gw._save_current()
    session_id = current["id"]
    assert gw.load_chat(session_id)["project"] == project
    assert next(c for c in gw.bootstrap()["chats"] if c["id"] == session_id)["project"] == project
    assert (
        next(s for s in gw.list_sessions_compat()["sessions"] if s["id"] == session_id)["project"]
        == project
    )
    gw.rename_chat(session_id, "renamed")
    restarted = make_gateway(tmp_path, monkeypatch, 19001)
    loaded = restarted.load_chat(session_id)
    assert loaded["title"] == "renamed"
    assert loaded["project"] == project
    with pytest.raises(Exception) as exc:
        gw.pin_collama_project({"kind": "repository", "value": "owner/repo"})
    assert getattr(exc.value, "status", None) == 409

    legacy = gw.new_chat()
    gw.pin_collama_project({"kind": "repository", "value": "owner/repo"})
    assert gw.current_chat()["project"]["value"] == "owner/repo"
    assert legacy["id"] == gw.session["id"]


def test_repository_and_folder_sessions_do_not_leak_workspace(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch, 18997)
    root = (tmp_path / "root").resolve()
    folder = root / "folder"
    folder.mkdir()
    folder_id = gw.new_chat(gw.validate_project({"kind": "gatewayFolder", "value": str(folder)}))[
        "id"
    ]
    assert gw.agent.state.workspace == folder
    repo_id = gw.new_chat(gw.validate_project({"kind": "repository", "value": "owner/repo"}))["id"]
    assert gw.agent.state.workspace == root
    assert gw.agent.state.default_repository == "owner/repo"
    gw.load_chat(folder_id)
    assert gw.agent.state.workspace == folder
    assert gw.agent.state.default_repository is None
    gw.load_chat(repo_id)
    assert gw.agent.state.workspace == root
    assert gw.agent.state.default_repository == "owner/repo"


def test_project_change_returns_http_409(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch, 18998)
    gw.new_chat(gw.validate_project({"kind": "repository", "value": "owner/one"}))
    assert gw.start()
    try:
        status, payload = request(
            gw,
            "/api/chat",
            {
                "client": "collama",
                "prompt": "hello",
                "project": {"kind": "repository", "value": "owner/two"},
            },
        )
        assert status == 409
        assert "immutable" in payload["error"]
    finally:
        gw.stop()


def test_missing_folder_project_load_does_not_switch_session(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch, 18999)
    folder = tmp_path / "root" / "temporary"
    folder.mkdir()
    folder_id = gw.new_chat(gw.validate_project({"kind": "gatewayFolder", "value": str(folder)}))[
        "id"
    ]
    current_id = gw.new_chat()["id"]
    folder.rmdir()
    result = gw.load_chat(folder_id)
    assert "cannot load chat project" in result["error"]
    assert gw.session["id"] == current_id


def test_gateway_model_switch_changes_provider_and_persists_full_spec(tmp_path, monkeypatch):
    import cagentic.gateway as gateway_module

    gw = make_gateway(tmp_path, monkeypatch, 19003)
    clients = {"openai": object(), "ollama": object()}
    monkeypatch.setattr(gateway_module, "_build_client", lambda _cfg, provider: clients[provider])
    monkeypatch.setattr(gateway_module._config, "save", lambda _cfg: None)

    assert gw.set_model("openai:gpt-test") == {"model": "openai:gpt-test"}
    assert gw.engine.client is clients["openai"]
    cloud_id = gw.new_chat()["id"]
    assert gw.session["model"] == "openai:gpt-test"
    gw.engine.messages.append({"role": "user", "content": "persist me"})
    gw._save_current()
    gw.new_chat()

    assert gw.set_model("local-test") == {"model": "local-test"}
    assert gw.engine.client is clients["ollama"]
    assert gw.load_chat(cloud_id)["model"] == "openai:gpt-test"
    assert gw.engine.client is clients["openai"]


def test_context_compact_is_hidden_persistent_and_idempotent(tmp_path, monkeypatch):
    from cagentic.services.compact import SUMMARY_MARKER

    gw = make_gateway(tmp_path, monkeypatch, 19004)
    project = {"kind": "repository", "value": "owner/repo"}
    session_id = gw.new_chat(gw.validate_project(project))["id"]
    original = []
    for index in range(10):
        original.extend(
            [
                {"role": "user", "content": f"request-{index} " + "x" * 500},
                {"role": "assistant", "content": f"answer-{index} " + "y" * 500},
            ]
        )
    gw.engine.load_messages(original)
    gw._save_current()
    monkeypatch.setattr(
        gw,
        "_summarize_compaction",
        lambda _older, _model: "Requirements retained; changed files and errors retained.",
    )

    result, status = gw.compact_context(session_id, project, threshold=0.1)
    assert status == 200
    assert result["after"] < result["before"]
    assert result["usage"]["input"] == result["after"]
    assert all(SUMMARY_MARKER not in m.get("content", "") for m in result["messages"])
    assert [m["content"] for m in result["messages"] if m["role"] == "user"] == [
        m["content"] for m in original if m["role"] == "user"
    ][-6:]
    stored = sessions.load(session_id)
    summaries = [
        m
        for m in stored["messages"]
        if m.get("role") == "system" and SUMMARY_MARKER in m.get("content", "")
    ]
    assert len(summaries) == 1

    duplicate, status = gw.compact_context(session_id, project, threshold=0.1)
    assert status == 200
    assert duplicate["before"] == duplicate["after"]
    stored = sessions.load(session_id)
    assert sum(SUMMARY_MARKER in m.get("content", "") for m in stored["messages"]) == 1


def test_context_compact_project_mismatch_and_busy_conflict(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch, 19005)
    project = {"kind": "repository", "value": "owner/repo"}
    session_id = gw.new_chat(gw.validate_project(project))["id"]
    result, status = gw.compact_context(
        session_id, {"kind": "repository", "value": "owner/other"}, 0.9
    )
    assert status == 409
    assert "does not match" in result["error"]
    gw._turn_lock.acquire()
    try:
        result, status = gw.compact_context(session_id, project, 0.9)
    finally:
        gw._turn_lock.release()
    assert status == 409
    assert "busy" in result["error"]


def test_context_compact_http_contract(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch, 19006)
    project = {"kind": "repository", "value": "owner/repo"}
    session_id = gw.new_chat(gw.validate_project(project))["id"]
    assert gw.start()
    try:
        status, payload = request(
            gw,
            "/api/context/compact",
            {
                "client": "collama",
                "id": session_id,
                "threshold": 0.9,
                "project": project,
            },
        )
        assert status == 200
        assert payload["id"] == session_id
        assert payload["project"] == project
        assert payload["usage"]["input"] == payload["after"]
    finally:
        gw.stop()
