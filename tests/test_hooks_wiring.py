"""Lifecycle hooks, from config.json to the agent loop.

`hooks.py` was written in full — six events, the exit-code contract, a
`describe()` for a `/hooks` command — and then never called from anywhere. It
had zero call sites in the package. These tests cover the wiring rather than the
module: that each event fires from the right place, that a deny actually stops
the thing it is denying, and that a broken hook cannot take the agent down with
it.

The exit-code contract under test:

    0   allow, stdout offered to the model as context
    1   allow, stdout offered as a warning
    2   deny  (stderr shown; the call or prompt is blocked)
    anything else / timeout / crash -> allow, because a hook must never be the
    reason the agent stops working.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from cagentic import hooks
from cagentic.engine import StreamingToolExecutor
from cagentic.permissions import auto_deny_resolver
from cagentic.state import AppState


def script(tmp_path: Path, name: str, body: str) -> str:
    """A hook handler, written as a python script so it runs on any platform."""
    path = tmp_path / f"{name}.py"
    path.write_text(body)
    return f'"{sys.executable}" "{path}"'


ALLOW = "import sys; sys.stdin.read(); sys.exit(0)"
ALLOW_WITH_CONTEXT = "import sys; sys.stdin.read(); print('extra context'); sys.exit(0)"
WARN = "import sys; sys.stdin.read(); print('a warning'); sys.exit(1)"
DENY = "import sys; sys.stdin.read(); print('not allowed', file=sys.stderr); sys.exit(2)"
CRASH = "import sys; raise SystemExit(99)"
EXPLODE = "import sys; sys.exit(int('boom'))"


class _Engine:
    _read_cache: dict = {}

    def __init__(self, cfg):
        self.config = cfg


def executor(tmp_path: Path, cfg: dict, **state_kw) -> StreamingToolExecutor:
    state = AppState(workspace=tmp_path, home=tmp_path, yolo=True, **state_kw)
    return StreamingToolExecutor(state=state, resolver=auto_deny_resolver, engine=_Engine(cfg))


def run(ex: StreamingToolExecutor, call) -> list:
    return list(ex._run_one(call))


def results(events) -> list:
    return [e for e in events if e.kind == "tool_result"]


@pytest.fixture()
def sample(tmp_path: Path) -> Path:
    p = tmp_path / "a.txt"
    p.write_text("hello\n")
    return p


# ------------------------------------------------------------- PreToolUse --


def test_a_denying_hook_blocks_the_call(tmp_path, sample):
    cfg = {"hooks": {"PreToolUse": [{"command": script(tmp_path, "deny", DENY)}]}}
    events = run(executor(tmp_path, cfg), ("read_file", {"path": "a.txt"}, "tool"))

    denied = [e for e in events if e.kind == "tool_denied"]
    assert denied, "a blocked call should say so"
    assert "not allowed" in denied[0].data["reason"]

    (result,) = results(events)
    assert result.data["ok"] is False
    assert "blocked by hook" in result.data["result"]


def test_a_blocked_call_still_produces_exactly_one_result(tmp_path, sample):
    """Both providers 400 on an unanswered tool call, so a block is not an
    excuse to skip the result — see _record_unrun_call for the same duty."""
    cfg = {"hooks": {"PreToolUse": [{"command": script(tmp_path, "deny", DENY)}]}}
    events = run(executor(tmp_path, cfg), ("read_file", {"path": "a.txt"}, "tool"))
    assert len(results(events)) == 1


def test_a_denying_hook_beats_yolo(tmp_path, sample):
    """The executor runs in yolo here. A deterministic tripwire that an
    approval mode could switch off would not be worth configuring."""
    cfg = {"hooks": {"PreToolUse": [{"command": script(tmp_path, "deny", DENY)}]}}
    ex = executor(tmp_path, cfg)
    assert ex.state.yolo is True
    assert results(run(ex, ("read_file", {"path": "a.txt"}, "tool")))[0].data["ok"] is False


def test_an_allowing_hook_leaves_the_call_alone(tmp_path, sample):
    cfg = {"hooks": {"PreToolUse": [{"command": script(tmp_path, "ok", ALLOW)}]}}
    (result,) = results(run(executor(tmp_path, cfg), ("read_file", {"path": "a.txt"}, "tool")))
    assert result.data["ok"] is True
    assert "hello" in result.data["result"]


def test_hook_stdout_reaches_the_model(tmp_path, sample):
    cfg = {"hooks": {"PreToolUse": [{"command": script(tmp_path, "ctx", ALLOW_WITH_CONTEXT)}]}}
    (result,) = results(run(executor(tmp_path, cfg), ("read_file", {"path": "a.txt"}, "tool")))
    assert result.data["ok"] is True
    assert "extra context" in result.data["result"]


def test_a_warning_hook_allows_but_annotates(tmp_path, sample):
    cfg = {"hooks": {"PreToolUse": [{"command": script(tmp_path, "warn", WARN)}]}}
    (result,) = results(run(executor(tmp_path, cfg), ("read_file", {"path": "a.txt"}, "tool")))
    assert result.data["ok"] is True
    assert "hook warning" in result.data["result"]
    assert "a warning" in result.data["result"]


@pytest.mark.parametrize("body", [CRASH, EXPLODE])
def test_a_broken_hook_never_breaks_the_agent(tmp_path, sample, body):
    cfg = {"hooks": {"PreToolUse": [{"command": script(tmp_path, "bad", body)}]}}
    (result,) = results(run(executor(tmp_path, cfg), ("read_file", {"path": "a.txt"}, "tool")))
    assert result.data["ok"] is True, "a failing hook must fail open"


def test_a_missing_command_fails_open(tmp_path, sample):
    cfg = {"hooks": {"PreToolUse": [{"command": "definitely-not-a-real-command-xyz"}]}}
    (result,) = results(run(executor(tmp_path, cfg), ("read_file", {"path": "a.txt"}, "tool")))
    assert result.data["ok"] is True


def test_a_hanging_hook_times_out_and_allows(tmp_path, sample):
    slow = script(tmp_path, "slow", "import sys, time; sys.stdin.read(); time.sleep(30)")
    cfg = {"hooks": {"PreToolUse": [{"command": slow, "timeout": 0.5}]}}
    (result,) = results(run(executor(tmp_path, cfg), ("read_file", {"path": "a.txt"}, "tool")))
    assert result.data["ok"] is True


def test_no_hooks_configured_changes_nothing(tmp_path, sample):
    (result,) = results(run(executor(tmp_path, {}), ("read_file", {"path": "a.txt"}, "tool")))
    assert result.data["ok"] is True
    assert "hook" not in result.data["result"]


# ------------------------------------------------------------ PostToolUse --


def test_post_tool_use_receives_the_result(tmp_path, sample):
    log = tmp_path / "audit.log"
    body = f"import sys, json, pathlib; pathlib.Path({str(log)!r}).write_text(sys.stdin.read())"
    cfg = {"hooks": {"PostToolUse": [{"command": script(tmp_path, "audit", body)}]}}
    run(executor(tmp_path, cfg), ("read_file", {"path": "a.txt"}, "tool"))

    import json

    payload = json.loads(log.read_text())
    assert payload["tool"] == "read_file"
    assert "hello" in payload["result"]


def test_post_tool_use_cannot_block(tmp_path, sample):
    """Only the gating events gate; a non-zero exit here is informational."""
    cfg = {"hooks": {"PostToolUse": [{"command": script(tmp_path, "deny", DENY)}]}}
    (result,) = results(run(executor(tmp_path, cfg), ("read_file", {"path": "a.txt"}, "tool")))
    assert result.data["ok"] is True


# ------------------------------------------------- the contract, directly --


def test_non_gating_events_always_allow(tmp_path):
    cfg = {"hooks": {"Stop": [{"command": script(tmp_path, "deny", DENY)}]}}
    assert hooks.decision("Stop", {}, cfg) == ("allow", "")


def test_deny_wins_over_warn_and_allow(tmp_path):
    cfg = {
        "hooks": {
            "PreToolUse": [
                {"command": script(tmp_path, "ok", ALLOW_WITH_CONTEXT)},
                {"command": script(tmp_path, "warn", WARN)},
                {"command": script(tmp_path, "deny", DENY)},
            ]
        }
    }
    verdict, message = hooks.decision("PreToolUse", {}, cfg)
    assert verdict == "deny"
    assert "not allowed" in message


def test_every_declared_event_is_wired_somewhere():
    """Each name in EVENTS should be fired by the package, not just declared.

    Notification is the exception: it is reserved and has no natural call site
    yet, so it is listed here to be explicit rather than quietly forgotten.
    """
    import pathlib

    pkg = pathlib.Path(__file__).resolve().parents[1] / "cagentic"
    sources = "\n".join(
        p.read_text(encoding="utf-8") for p in pkg.rglob("*.py") if p.name != "hooks.py"
    )
    unwired = {e for e in hooks.EVENTS if f'"{e}"' not in sources}
    assert unwired == {"Notification"}, f"unexpectedly unwired: {unwired}"


def test_hooks_is_a_declared_config_key():
    from cagentic.config import _DEFAULTS

    assert "hooks" in _DEFAULTS


def test_the_slash_command_exists():
    from cagentic.prompt import ALL_COMMANDS

    assert "/hooks" in ALL_COMMANDS


@pytest.mark.skipif(os.name == "nt", reason="shell quoting differs on Windows")
def test_describe_counts_handlers_per_event(tmp_path):
    cfg = {"hooks": {"PreToolUse": [{"command": "true"}, {"command": "true"}]}}
    counts = dict(hooks.describe(cfg))
    assert counts["PreToolUse"] == 2
    assert counts["Stop"] == 0
