"""`ask_user_question` on a front end that is not the terminal.

The tool reads stdin, which belongs to the REPL — on the gateway it used to return an error
telling the model to ask in its reply instead. That works, but it turns a question with defined
options into free text somebody has to parse back out of a sentence. The gateway now puts the
question to whoever is watching and parks the turn until they answer, the same way an approval
prompt does.
"""

from __future__ import annotations

import threading
from pathlib import Path

from cagentic.agent import Agent
from cagentic.gateway import Gateway
from cagentic.ollama_client import OllamaClient
from cagentic.tools import ToolContext, t_ask_user_question


def make_gateway(tmp_path: Path, monkeypatch) -> Gateway:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CAGENTIC_WORKSPACE_ROOTS", str(tmp_path / "root"))
    (tmp_path / "root").mkdir(exist_ok=True)
    agent = Agent(OllamaClient("http://127.0.0.1:1"), "test", tmp_path / "root")
    return Gateway(agent, {"gateway": {"port": 19615}}, port=19615)


def test_the_question_reaches_the_client_and_the_answer_comes_back(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    events: list[tuple[str, dict]] = []
    gw._active_emit = lambda kind, data: events.append((kind, data))

    answer: list[str] = []
    asking = threading.Thread(
        target=lambda: answer.append(gw.ask_user("Which one?", ["Red", "Blue"]))
    )
    asking.start()

    # Wait for the question to be put, then answer it the way the HTTP route would.
    for _ in range(200):
        if events:
            break
        threading.Event().wait(0.01)
    kind, payload = events[0]
    assert kind == "question"
    assert payload["question"] == "Which one?"
    assert payload["options"] == ["Red", "Blue"]

    gw.deliver_answer("Blue", payload["id"])
    asking.join(timeout=5)

    assert answer == ["Blue"]


def test_a_stale_id_does_not_answer_the_live_question(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    events: list[tuple[str, dict]] = []
    gw._active_emit = lambda kind, data: events.append((kind, data))

    answer: list[str] = []
    asking = threading.Thread(target=lambda: answer.append(gw.ask_user("Which one?", ["A"])))
    asking.start()
    for _ in range(200):
        if events:
            break
        threading.Event().wait(0.01)

    # An answer to a question that already went away must not satisfy this one.
    gw.deliver_answer("wrong", "00000000-0000-0000-0000-000000000000")
    assert not answer

    gw.deliver_answer("A", events[0][1]["id"])
    asking.join(timeout=5)
    assert answer == ["A"]


def test_with_nobody_watching_the_model_is_told_rather_than_left_hanging(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path, monkeypatch)
    gw._active_emit = None

    result = gw.ask_user("Which one?", ["A"])

    assert result.startswith("ERROR:")


def test_the_tool_uses_the_asker_when_there_is_no_terminal():
    asked: list[tuple[str, list[str]]] = []

    def asker(question, options):
        asked.append((question, options))
        return "Blue"

    # An engine whose resolver is not the terminal one — i.e. anything but the REPL.
    class _Engine:
        permission_resolver = object()

    ctx = ToolContext(root=Path.cwd(), engine=_Engine(), ask_user=asker)
    result = t_ask_user_question({"question": "Which one?", "options": ["Red", "Blue"]}, ctx)

    assert result == "Blue"
    assert asked == [("Which one?", ["Red", "Blue"])]


def test_without_an_asker_the_tool_still_explains_itself():
    class _Engine:
        permission_resolver = object()

    ctx = ToolContext(root=Path.cwd(), engine=_Engine())
    result = t_ask_user_question({"question": "Which one?", "options": ["Red"]}, ctx)

    # The old behaviour, kept for any front end that cannot prompt at all.
    assert result.startswith("ERROR: no terminal to prompt on")
    assert "Which one?" in result
