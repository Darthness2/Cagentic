"""Terminal behaviours that were rough enough to matter every session.

Four of them: a single Ctrl-C at the prompt quit with no confirmation; a
mid-turn Ctrl-C on POSIX printed nothing at all, so an interrupt with an empty
type-ahead buffer looked identical to the model going quiet; the terminal
printed raw tool identifiers while the web page printed human phrasing from the
same event; and leaving plan mode was a flag the model flipped for itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cagentic import ui
from cagentic.agent import _tool_heading
from cagentic.permissions import PLAN_MODE_EXTRAS, can_use_tool
from cagentic.state import AppState
from cagentic.tool_labels import label_pair
from cagentic.tools import ToolContext, t_exit_plan_mode

# ------------------------------------------------------------ tool labels --


def test_a_single_run_uses_the_human_label():
    assert _tool_heading("read_file", label_pair("read_file"), 1) == "Read file"


def test_a_collapsed_run_uses_the_plural_with_its_count():
    # "Read 5 files", not "Read file  x 5" — the plural form carries its own
    # {n} slot precisely so the collapsed line reads like English.
    assert _tool_heading("read_file", label_pair("read_file"), 5) == "Read 5 files"


def test_an_unlabelled_tool_falls_back_to_its_identifier():
    assert _tool_heading("mystery_tool", None, 1) == "mystery_tool"
    assert _tool_heading("mystery_tool", None, 3) == "mystery_tool  × 3"


def test_a_malformed_plural_does_not_raise():
    # A label table entry someone edited badly must not take down rendering.
    assert _tool_heading("x", ("One", "Broken {nope}"), 4) == "One  × 4"


def test_every_default_tool_has_a_label():
    """The table is the single source of wording for three front ends, so a
    tool added without an entry should be noticed here rather than in a
    screenshot."""
    from cagentic.tools import DEFAULT_GROUPS, all_tool_schemas

    missing = [
        s["function"]["name"]
        for s in all_tool_schemas(DEFAULT_GROUPS, compact=False)
        if not label_pair(s["function"]["name"])[0]
    ]
    assert not missing, f"no human label for: {missing}"


# ------------------------------------------------------- plan-mode gating --


def planning_state(**kw) -> AppState:
    return AppState(workspace=Path.cwd(), home=Path.home(), plan_mode=True, **kw)


def test_leaving_plan_mode_asks_rather_than_assuming():
    approved, reason = can_use_tool(
        "exit_plan_mode", {"plan": "do the thing"}, planning_state(), lambda *_a: "yes"
    )
    assert approved is True
    assert reason == "user approved"


def test_declining_keeps_plan_mode_on():
    state = planning_state()
    approved, _reason = can_use_tool(
        "exit_plan_mode", {"plan": "do the thing"}, state, lambda *_a: "no"
    )
    assert approved is False
    assert state.plan_mode is True, "a refused exit must not have already happened"


def test_exit_plan_mode_is_no_longer_waved_through():
    assert "exit_plan_mode" not in PLAN_MODE_EXTRAS
    assert "enter_plan_mode" in PLAN_MODE_EXTRAS


def test_plan_mode_still_blocks_everything_else():
    approved, reason = can_use_tool(
        "write_file", {"path": "x"}, planning_state(), lambda *_a: "yes"
    )
    assert approved is False
    assert "plan mode" in reason


def test_the_tool_requires_an_actual_plan(tmp_path):
    state = AppState(workspace=tmp_path, home=tmp_path, plan_mode=True)
    ctx = ToolContext(root=tmp_path, state=state)

    out = t_exit_plan_mode({}, ctx)
    assert out.startswith("ERROR:")
    assert state.plan_mode is True, "a rejected call must not leave plan mode"


def test_a_plan_turns_it_off(tmp_path):
    state = AppState(workspace=tmp_path, home=tmp_path, plan_mode=True)
    ctx = ToolContext(root=tmp_path, state=state)

    assert t_exit_plan_mode({"plan": "1. do x\n2. do y"}, ctx).startswith("OK:")
    assert state.plan_mode is False


# ---------------------------------------------------- syntax highlighting --


@pytest.fixture()
def colour(monkeypatch):
    """Force colour on: highlighting is deliberately skipped on a plain stream,
    and pytest's stdout is not a tty."""
    monkeypatch.setenv("CAGENTIC_COLOR", "always")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(ui, "_PYGMENTS_MISSING", False)
    ui._LEXER_CACHE.clear()


def test_highlighting_is_optional_and_degrades_to_plain(colour, monkeypatch):
    """Without pygments the block renders exactly as it always did."""
    monkeypatch.setattr(ui, "_PYGMENTS_MISSING", True)
    out = ui.render_markdown("```python\nx = 1\n```")
    assert "x = 1" in out
    assert "│" in out


def test_a_plain_stream_is_never_highlighted(monkeypatch):
    """NO_COLOR and friends must win — escape codes in a redirected log are
    exactly what that setting exists to prevent."""
    monkeypatch.setenv("NO_COLOR", "1")
    assert ui._highlight_code("x = 1", "python") is None


def test_an_unknown_fence_language_is_not_an_error(colour):
    assert ui._highlight_code("whatever", "not-a-real-language-xyz") is None


def test_a_block_with_no_language_stays_plain(colour):
    assert ui._highlight_code("x = 1", "") is None


def test_highlighting_preserves_every_line(colour):
    pytest.importorskip("pygments")
    body = "def f(x):\n    return x + 1\n\nprint(f(2))"
    lines = ui._highlight_code(body, "python")
    assert lines is not None
    assert len(lines) == len(body.splitlines())


def test_the_rail_survives_highlighting(colour):
    pytest.importorskip("pygments")
    out = ui.render_markdown("```python\nx = 1\ny = 2\n```")
    assert out.count("│") >= 2, "every code line keeps its left rail"


# ------------------------------------------------------------ the interrupt --


def test_the_quit_confirmation_window_is_short_but_real():
    from cagentic.cli import _QUIT_CONFIRM_SECONDS

    # Long enough to be a deliberate second press, short enough that a stray
    # Ctrl-C much earlier in the session cannot quit you.
    assert 0.5 <= _QUIT_CONFIRM_SECONDS <= 5.0


def test_ctrl_d_still_exits_immediately():
    """Only Ctrl-C is confirmed. Ctrl-D means end of input and means it."""
    import inspect

    from cagentic import cli

    source = inspect.getsource(cli.repl)
    assert "except EOFError:" in source
    assert "press Ctrl-C again" in source


def test_a_posix_interrupt_says_something():
    import inspect

    from cagentic import agent

    source = inspect.getsource(agent.Agent.turn)
    # Both the POSIX poll and the Windows signal path acknowledge the stop.
    assert source.count("turn interrupted") == 2
