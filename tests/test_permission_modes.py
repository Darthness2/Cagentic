"""Named approval modes.

Two of the three already existed without a name: "restricted" is what the gate has always done
(reading silent, everything else prompts) and "unrestricted" is `yolo`. Only "ask for everything"
is new — it suppresses the read-only allowance so a turn cannot quietly open a dozen files.

What matters most here is what a mode must *not* be able to do: `dry_run`, deny rules and plan
mode all sit above it, so choosing "unrestricted" on a phone cannot disarm a standing safety rule
on the computer.
"""

from __future__ import annotations

from pathlib import Path

from cagentic.permissions import (
    CHAT_MODES,
    DEFAULT_CHAT_MODE,
    can_use_tool,
    current_mode,
    mode_flags,
    normalize_mode,
)
from cagentic.state import AppState


def state_in(mode: str, **extra) -> AppState:
    state = AppState(workspace=Path.cwd(), home=Path.home(), **extra)
    for field, value in mode_flags(mode).items():
        setattr(state, field, value)
    return state


def always_deny(name, args, state):  # a prompt that is never approved
    return "no"


def test_the_default_is_restricted():
    assert DEFAULT_CHAT_MODE == "restricted"
    assert set(CHAT_MODES) == {"restricted", "ask_all", "unrestricted"}


def test_restricted_reads_silently_and_asks_for_the_rest():
    state = state_in("restricted")

    allowed, reason = can_use_tool("read_file", {"path": "a.txt"}, state, always_deny)
    assert allowed and reason == "read-only tool"

    # A write reaches the resolver, which declines.
    allowed, _ = can_use_tool("write_file", {"path": "a.txt"}, state, always_deny)
    assert not allowed


def test_ask_all_prompts_for_reading_too():
    state = state_in("ask_all")

    allowed, _ = can_use_tool("read_file", {"path": "a.txt"}, state, always_deny)
    assert not allowed, "ask-all must send read-only tools to the resolver"

    approved = []

    def approve(name, args, state):
        approved.append(name)
        return "yes"

    allowed, _ = can_use_tool("list_dir", {"path": "."}, state, approve)
    assert allowed and approved == ["list_dir"]


def test_unrestricted_asks_for_nothing():
    state = state_in("unrestricted")

    for tool in ("read_file", "write_file", "run_bash"):
        allowed, reason = can_use_tool(tool, {"path": "a", "command": "ls"}, state, always_deny)
        assert allowed, f"{tool} should not prompt when unrestricted"


def test_a_deny_rule_still_wins_under_unrestricted():
    # The point of the ordering: a phone cannot switch off a standing rule.
    state = state_in("unrestricted")
    state.permission_rules = {"deny": ["run_bash(rm*)"]}

    allowed, reason = can_use_tool("run_bash", {"command": "rm -rf /"}, state, always_deny)

    assert not allowed
    assert "denied by rule" in reason


def test_dry_run_still_wins_under_unrestricted():
    state = state_in("unrestricted", dry_run=True)

    allowed, reason = can_use_tool("write_file", {"path": "a.txt"}, state, always_deny)

    assert not allowed
    assert "dry run" in reason


def test_plan_mode_still_wins_under_unrestricted():
    state = state_in("unrestricted", plan_mode=True)

    allowed, reason = can_use_tool("write_file", {"path": "a.txt"}, state, always_deny)

    assert not allowed
    assert "plan mode" in reason


def test_ask_all_still_defers_to_the_permission_cache():
    # Answering "always" once should not have to be answered again every turn.
    state = state_in("ask_all")
    state.permissions["read_file"] = "always"

    allowed, reason = can_use_tool("read_file", {"path": "a.txt"}, state, always_deny)

    assert allowed and "cache" in reason


def test_modes_round_trip_through_state():
    for name in CHAT_MODES:
        assert current_mode(state_in(name)) == name


def test_an_unknown_mode_falls_back_to_the_safe_one():
    for junk in ("", None, "YOLO", "nonsense"):
        assert normalize_mode(junk) == "restricted"
    # Spelling variants a client might send.
    assert normalize_mode("Ask All") == "ask_all"
    assert normalize_mode("ask-all") == "ask_all"
