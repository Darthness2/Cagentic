"""What a sub-agent shows, what it may touch, and when several may run at once.

Three things it did not do. It ran silently — whatever it read, ran or wrote was
invisible, and the only evidence was a paragraph of prose that might have been
made up. It got the parent's whole toolset regardless of the job. And every call
was serial, so three independent lookups cost three round trips in sequence.

The last two are the same question: a child restricted to groups that cannot
change anything is no riskier to fan out than three greps, while an unrestricted
one inherits the parent's write access and has to stay in line.

(Inheritance of the parent's *restrictions* is covered in
test_subagent_inheritance.py — that is a safety property, this is behaviour.)
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from cagentic.coding_tools import _requested_groups
from cagentic.engine import QueryEngine, StreamingToolExecutor, _call_is_concurrent_safe
from cagentic.permissions import auto_deny_resolver
from cagentic.state import AppState
from cagentic.subagent import _client_for, subagent_is_read_only


class SlowClient:
    """A model that reads one file, then answers. No shared session."""

    def __init__(self, delay: float = 0.3):
        self.delay = delay

    def chat(self, model, messages, **_kw):
        time.sleep(self.delay)
        if not any(m.get("role") == "tool" for m in messages):
            return {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "read_file", "arguments": {"path": "f0.txt"}}}
                ],
            }
        return {"content": "done reading"}

    def chat_stream_assembled(self, *a, **k):
        return self.chat(*a, **k)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text(f"content {i}\n")
    return tmp_path


@pytest.fixture()
def executor(workspace):
    state = AppState(workspace=workspace, home=workspace, yolo=True)
    parent = QueryEngine(client=SlowClient(), state=state, model="test", config={})
    return StreamingToolExecutor(state=state, resolver=auto_deny_resolver, engine=parent)


def results(events):
    return [e for e in events if e.kind == "tool_result"]


# ------------------------------------------------------- what it shows --


def test_the_answer_carries_a_trace_of_what_it_did(executor):
    events = list(executor.execute([("agent_call", {"prompt": "look"}, "tool")]))
    body = results(events)[0].data["result"]
    assert "[sub-agent answer]" in body
    assert "done reading" in body
    assert "[what it did]" in body
    assert "Read file" in body, "the trace uses the human label, like every other renderer"


def test_a_child_that_used_no_tools_gets_no_trace_section(workspace):
    class Chatty:
        def chat(self, *_a, **_k):
            return {"content": "just an opinion"}

        chat_stream_assembled = chat

    state = AppState(workspace=workspace, home=workspace, yolo=True)
    parent = QueryEngine(client=Chatty(), state=state, model="test", config={})
    ex = StreamingToolExecutor(state=state, resolver=auto_deny_resolver, engine=parent)

    body = results(list(ex.execute([("agent_call", {"prompt": "think"}, "tool")])))[0].data[
        "result"
    ]
    assert "just an opinion" in body
    assert "[what it did]" not in body


# ------------------------------------------------- what it may touch --


@pytest.mark.parametrize(
    "raw,expected",
    [
        (["files", "web"], {"files", "web"}),
        ("files web", {"files", "web"}),
        ("files,web", {"files", "web"}),
        (["worktree"], {"workspace"}),  # renamed group still resolves
        ([], None),
        (None, None),
    ],
)
def test_the_tools_argument_is_read_forgivingly(raw, expected):
    assert _requested_groups({"tools": raw} if raw is not None else {}) == expected


@pytest.mark.parametrize("raw", [["not_a_group"], ["read-only"], ["file"], ["files", "typo"]])
def test_an_unrecognised_group_fails_closed(raw):
    """This used to drop unknown names and return None for "nothing left" —
    and None means *inherit everything*. So any plausible name a model invented
    silently granted the full toolset instead of narrowing it."""
    from cagentic.coding_tools import UnknownToolGroup

    with pytest.raises(UnknownToolGroup):
        _requested_groups({"tools": raw})


def test_a_restricted_child_only_gets_what_it_asked_for(workspace, monkeypatch):
    seen: dict = {}
    import cagentic.subagent as sub

    real = sub.fork_subagent

    def spy(parent, prompt, **kw):
        seen.update(kw)
        return "ok"

    monkeypatch.setattr(sub, "fork_subagent", spy)
    from cagentic.coding_tools import t_agent_call
    from cagentic.tools import ToolContext

    state = AppState(workspace=workspace, home=workspace)
    parent = QueryEngine(client=SlowClient(), state=state, model="test", config={})
    t_agent_call(
        {"prompt": "x", "tools": ["files"]},
        ToolContext(root=workspace, state=state, engine=parent),
    )
    assert seen["tool_groups"] == {"files"}
    assert real is not spy  # sanity: we patched the module attribute, not the import


# ------------------------------------------------------------ fan-out --


@pytest.mark.parametrize(
    "groups,parallel",
    [
        ({"web"}, True),
        ({"web", "interaction"}, True),
        # files writes, mcp calls arbitrary server tools, system has config_set.
        ({"files"}, False),
        ({"mcp"}, False),
        ({"system"}, False),
        ({"web", "files"}, False),
        ({"files", "shell"}, False),
        ({"git"}, False),
        (None, False),
        (set(), False),
    ],
)
def test_only_a_child_that_cannot_change_anything_runs_in_parallel(groups, parallel):
    assert subagent_is_read_only(groups) is parallel


def test_the_parallel_safe_set_is_derived_not_listed():
    """It was a hand-written literal containing three groups that write. Deriving
    it from CONCURRENT_SAFE means a tool added to a group cannot quietly
    re-introduce that — the group just stops qualifying."""
    from cagentic.permissions import CONCURRENT_SAFE
    from cagentic.subagent import parallel_safe_groups
    from cagentic.tools import TOOL_GROUPS

    for group in parallel_safe_groups():
        writers = [t for t in TOOL_GROUPS[group] if t not in CONCURRENT_SAFE]
        assert not writers, f"{group} is called parallel-safe but contains {writers}"


def test_the_executor_agrees():
    assert _call_is_concurrent_safe(("agent_call", {"tools": ["web"]}, "tool"))
    assert not _call_is_concurrent_safe(("agent_call", {"tools": ["files"]}, "tool"))
    assert not _call_is_concurrent_safe(("agent_call", {"tools": ["shell"]}, "tool"))
    # A malformed restriction is the tool's problem to report; serial is the
    # safe answer here either way.
    assert not _call_is_concurrent_safe(("agent_call", {"tools": ["nonsense"]}, "tool"))
    assert not _call_is_concurrent_safe(("agent_call", {}, "tool"))
    # Unchanged for everything else.
    assert _call_is_concurrent_safe(("read_file", {}, "tool"))
    assert not _call_is_concurrent_safe(("run_bash", {}, "tool"))


def test_read_only_children_actually_overlap(executor):
    """The point of the whole exercise: three independent lookups should not
    cost three round trips in sequence."""
    calls = [("agent_call", {"prompt": f"f{i}", "tools": ["web"]}, "tool") for i in range(3)]
    started = time.time()
    events = list(executor.execute(calls))
    elapsed = time.time() - started

    assert len(results(events)) == 3
    # Two round trips each at 0.3s: ~0.6s overlapped, ~1.8s in sequence.
    assert elapsed < 1.4, f"three read-only sub-agents took {elapsed:.1f}s — not overlapping"


def test_unrestricted_children_stay_in_line(executor):
    calls = [("agent_call", {"prompt": f"f{i}"}, "tool") for i in range(3)]
    started = time.time()
    list(executor.execute(calls))
    elapsed = time.time() - started
    assert elapsed > 1.4, "an unrestricted sub-agent can write; it must not overlap"


def test_every_call_still_gets_exactly_one_result(executor):
    calls = [("agent_call", {"prompt": f"f{i}", "tools": ["web"]}, "tool") for i in range(3)]
    assert len(results(list(executor.execute(calls)))) == 3


# ------------------------------------------------------------- client --


class _Parent:
    def __init__(self, client):
        self.client = client
        self.model = "test"
        self.config: dict = {}
        self.state = AppState(workspace=Path.cwd(), home=Path.home())


def test_an_injected_client_is_reused():
    """A caller that handed QueryEngine a particular client meant it — quietly
    building one from config instead would ignore that."""

    class Plain:
        pass

    client = Plain()
    assert _client_for(_Parent(client), None) is client


def test_a_client_with_a_shared_session_is_not_shared():
    """The cloud clients keep one requests.Session, which is not thread-safe,
    and the background executor already runs several sub-agents at once."""

    class Cloud:
        _session = object()

    client = Cloud()
    assert _client_for(_Parent(client), None) is not client


def test_a_different_model_gets_its_own_client():
    class Plain:
        pass

    client = Plain()
    assert _client_for(_Parent(client), "ollama:something-else") is not client
