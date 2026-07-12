from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

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
        headers={"X-Cagentic-Link": gw.token, "Content-Type": "application/json"},
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
        status, payload = request(
            gw, "/api/chat", {"client": "collama", "prompt": "hello"}
        )
        assert status == 400 and "project" in payload["error"]
        status, payload = request(gw, "/api/chats/new", {})
        assert status == 200 and payload["current"]["id"]
        req = urllib.request.Request(
            f"http://127.0.0.1:{gw.port}/api/chat",
            data=json.dumps({"message": "hello"}).encode(),
            headers={"X-Cagentic-Link": gw.token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as response:
            assert response.status == 200
    finally:
        gw.stop()


def test_project_persistence_immutability_legacy_assignment_and_bootstrap(
    tmp_path, monkeypatch
):
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
    assert (
        next(c for c in gw.bootstrap()["chats"] if c["id"] == session_id)["project"]
        == project
    )
    assert (
        next(s for s in gw.list_sessions_compat()["sessions"] if s["id"] == session_id)[
            "project"
        ]
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
    folder_id = gw.new_chat(
        gw.validate_project({"kind": "gatewayFolder", "value": str(folder)})
    )["id"]
    assert gw.agent.state.workspace == folder
    repo_id = gw.new_chat(
        gw.validate_project({"kind": "repository", "value": "owner/repo"})
    )["id"]
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
