"""Deleting the chat that is currently open.

Every way of switching chats — `_new_chat_unlocked`, `_load_chat_unlocked`, and even
`list_chats` — starts by flushing the loaded session to disk. Deleting the open chat
removed its stored copy and then immediately switched away, so the flush wrote it straight
back: the row never disappeared, and the new empty session the switch created showed up
above it. From the phone it looked like delete did nothing except add a chat.
"""

from __future__ import annotations

from pathlib import Path

from cagentic import sessions
from cagentic.agent import Agent
from cagentic.gateway import Gateway
from cagentic.ollama_client import OllamaClient


def make_gateway(tmp_path: Path, monkeypatch) -> Gateway:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CAGENTIC_WORKSPACE_ROOTS", str(tmp_path / "root"))
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    agent = Agent(OllamaClient("http://127.0.0.1:1"), "test", root)
    return Gateway(agent, {"gateway": {"port": 19311}}, port=19311)


def seed(title: str, updated_at: float) -> str:
    """A stored chat with a turn in it — an empty chat is never listed."""
    session = sessions.make("ollama:test")
    session["title"] = title
    session["messages"] = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    session["updated_at"] = updated_at
    sessions.save(session)
    return session["id"]


def titles(payload: dict) -> list[str]:
    return [chat["title"] for chat in payload["chats"]]


def test_deleting_the_open_chat_actually_removes_it(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    older = seed("Older", 100.0)
    newest = seed("Newest", 200.0)

    assert "error" not in gw.load_chat(newest)
    payload = gw.delete_chat(newest)

    assert "Newest" not in titles(payload)
    # And it stays gone once the next request rebuilds the list from storage.
    assert "Newest" not in titles({"chats": gw.list_chats()})
    assert sessions.load(newest) in (None, {})
    assert older in {chat["id"] for chat in payload["chats"]}


def test_deleting_the_open_chat_opens_the_next_one_instead_of_a_new_one(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    seed("Older", 100.0)
    newest = seed("Newest", 200.0)

    gw.load_chat(newest)
    payload = gw.delete_chat(newest)

    # Deleting a chat should not manufacture one: the list is exactly the survivor.
    assert titles(payload) == ["Older"]
    assert payload["current"]["title"] == "Older"


def test_deleting_the_last_chat_leaves_a_fresh_one_open(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    only = seed("Only", 100.0)

    gw.load_chat(only)
    payload = gw.delete_chat(only)

    # Nothing left to open, so a new chat is the right answer — but it is empty, and an
    # empty chat is not listed.
    assert payload["chats"] == []
    assert payload["current"]["id"] != only


def test_deleting_a_chat_that_is_not_open_leaves_the_open_one_alone(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    other = seed("Other", 100.0)
    open_chat = seed("Open", 200.0)

    gw.load_chat(open_chat)
    payload = gw.delete_chat(other)

    assert titles(payload) == ["Open"]
    assert payload["current"]["id"] == open_chat


def test_the_open_chat_is_not_silently_emptied_by_deleting_another(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    other = seed("Other", 100.0)
    open_chat = seed("Open", 200.0)

    gw.load_chat(open_chat)
    gw.delete_chat(other)

    # The detach used to make the delete work must not reach the chat the user is reading.
    stored = sessions.load(open_chat) or {}
    assert [m.get("content") for m in stored.get("messages", [])] == ["hi", "hello"]
