"""Type-ahead regressions: a half-typed line must survive the turn ending.

Two bugs, both reported live:

  * You type while the model streams; the model finishes before you press
    Enter; your text is gone. ``partial_buffer``/``reset_partial`` existed for
    exactly this and were called by nothing — the reader thread stopped and the
    buffer died with it.
  * There was no visible insertion point on the echo row, so mid-turn typing
    had no cursor to type "at".
"""

from __future__ import annotations

import unittest

from cagentic import ui
from cagentic.typeahead import _CURSOR, TypeAhead, _visible_tail


class _Ta(TypeAhead):
    """A TypeAhead that reports itself runnable and never paints, so the buffer
    logic can be exercised off a tty."""

    @property
    def can_run(self) -> bool:
        return True

    def _paint(self) -> None:
        pass

    def _clear_row(self) -> None:
        pass


def _paint_with(*, buf: str = "", pending: str | None = None) -> list[str]:
    """Capture what `_paint` emits. stdout isn't a tty under pytest, so the
    tty guard has to be stubbed or the method returns without painting."""
    import sys

    painted: list[str] = []
    ta = TypeAhead()
    ta._active = True
    ta._buf = buf
    ta._pending = pending
    original_write, original_stdout = ui.sync_write, sys.stdout

    class _Tty:
        def __getattr__(self, name):
            return getattr(original_stdout, name)

        def isatty(self):
            return True

    ui.sync_write = painted.append
    sys.stdout = _Tty()
    try:
        ta._paint()
    finally:
        ui.sync_write = original_write
        sys.stdout = original_stdout
    return painted


class TestPartialSurvivesTheTurn(unittest.TestCase):
    def setUp(self) -> None:
        self.ta = _Ta()
        self.ta._active = True

    def test_typed_text_with_no_enter_is_retrievable(self) -> None:
        for ch in "how does th":
            self.ta._handle_char(ch)
        self.assertEqual(self.ta.partial_buffer(), "how does th")

    def test_stopping_the_reader_does_not_discard_it(self) -> None:
        """`stop()` runs in Agent.turn's finally, before the buffer is read."""
        for ch in "half a thought":
            self.ta._handle_char(ch)
        self.ta.stop()
        self.assertEqual(self.ta.partial_buffer(), "half a thought")

    def test_reset_clears_it_so_it_is_offered_once(self) -> None:
        self.ta._handle_char("x")
        self.ta.partial_buffer()
        self.ta.reset_partial()
        self.assertEqual(self.ta.partial_buffer(), "")

    def test_an_entered_line_queues_instead_of_carrying(self) -> None:
        """Enter means send, not carry — it must not do both."""
        for ch in "send me\r":
            self.ta._handle_char(ch)
        self.assertEqual(self.ta.take_pending(), "send me")
        self.assertEqual(self.ta.partial_buffer(), "")

    def test_text_typed_after_enter_still_carries(self) -> None:
        for ch in "first\rsecond bit":
            self.ta._handle_char(ch)
        self.assertEqual(self.ta.take_pending(), "first")
        self.assertEqual(self.ta.partial_buffer(), "second bit")

    def test_ctrl_c_sends_the_text_rather_than_carrying_it(self) -> None:
        """Otherwise the same sentence is both sent and re-offered."""
        for ch in "urgent\x03":
            self.ta._handle_char(ch)
        self.assertEqual(self.ta.consume_interrupt(), "urgent")
        self.assertEqual(self.ta.partial_buffer(), "")

    def test_backspace_edits_the_carried_text(self) -> None:
        for ch in "abc\x7f\x7f":
            self.ta._handle_char(ch)
        self.assertEqual(self.ta.partial_buffer(), "a")


class TestAgentExposesThePartial(unittest.TestCase):
    def test_turn_reads_the_buffer_and_resets_it(self) -> None:
        """The wiring is the whole fix — the methods already existed."""
        import inspect

        from cagentic.agent import Agent

        source = inspect.getsource(Agent.turn)
        self.assertIn("partial_buffer()", source)
        self.assertIn("reset_partial()", source)

    def test_the_attribute_is_declared_on_the_agent(self) -> None:
        import inspect

        from cagentic import agent as agent_mod

        self.assertIn("self.pending_partial", inspect.getsource(agent_mod))


class _ScriptedPrompt:
    """Feeds the REPL a fixed script and records every pre-fill it was offered."""

    status_note = None
    backend = "test"

    def __init__(self, *lines: str) -> None:
        self._lines = iter(lines)
        self.defaults: list[str] = []

    def set_workspace_provider(self, provider) -> None:
        pass

    def set_context_provider(self, provider) -> None:
        pass

    def ask(self, _prefix: str, default: str = "") -> str:
        self.defaults.append(default)
        return next(self._lines)


class TestReplCarriesItToThePrompt(unittest.TestCase):
    """`Agent` only exposes the partial; the REPL is what puts it back on
    screen. Driven through `cli.repl` rather than by reading its source, so
    this fails if the wiring works but the behaviour doesn't."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from cagentic import cli
        from cagentic.agent import Agent

        self.cli = cli
        self._tmp = tempfile.TemporaryDirectory()
        self._cfg = tempfile.TemporaryDirectory()
        import os

        os.environ["XDG_CONFIG_HOME"] = self._cfg.name

        class _NullClient:
            pass

        self.agent = Agent(_NullClient(), "test-model", Path(self._tmp.name))
        self.turns: list[str] = []
        self._original_prompt = cli.Prompt

    def tearDown(self) -> None:
        self.cli.Prompt = self._original_prompt
        for d in (self._tmp, self._cfg):
            try:
                d.cleanup()
            except (OSError, PermissionError):
                pass

    def _run(self, script, partials) -> _ScriptedPrompt:
        """Run the REPL over `script`; `partials` is the half-typed line each
        turn leaves behind, consumed in order."""
        left = iter(partials)

        def fake_turn(line, typeahead=None):
            self.turns.append(line)
            self.agent.pending_input = None
            self.agent.pending_partial = next(left, "")
            return ""

        self.agent.turn = fake_turn  # type: ignore[method-assign]
        scripted = _ScriptedPrompt(*script)
        self.cli.Prompt = lambda: scripted
        self.assertEqual(self.cli.repl(self.agent, {}), 0)
        return scripted

    def test_a_half_typed_line_comes_back_at_the_next_prompt(self) -> None:
        """The reported bug: it used to vanish with the reader thread."""
        scripted = self._run(["hello", "/quit"], ["how does th"])
        self.assertEqual(scripted.defaults, ["", "how does th"])

    def test_it_is_offered_only_once(self) -> None:
        """A carry that never clears re-inserts itself at every later prompt."""
        scripted = self._run(["hello", "ok", "/quit"], ["carry me", ""])
        self.assertEqual(scripted.defaults, ["", "carry me", ""])

    def test_nothing_typed_means_no_pre_fill(self) -> None:
        scripted = self._run(["hello", "/quit"], [""])
        self.assertEqual(scripted.defaults, ["", ""])

    def test_it_survives_a_type_ahead_queued_turn(self) -> None:
        """Enter-queued text runs a turn without prompting, and that turn resets
        the agent's own attribute — so the REPL has to hold the carry itself."""
        queued = iter([("queued msg", "leftover"), (None, "")])

        def fake_turn(line, typeahead=None):
            self.turns.append(line)
            nxt, partial = next(queued, (None, ""))
            self.agent.pending_input = nxt
            self.agent.pending_partial = partial
            return ""

        self.agent.turn = fake_turn  # type: ignore[method-assign]
        scripted = _ScriptedPrompt("hello", "/quit")
        self.cli.Prompt = lambda: scripted
        self.assertEqual(self.cli.repl(self.agent, {}), 0)
        # The queued message ran without a prompt, and the leftover text still
        # arrived at the next real one.
        self.assertEqual(self.turns, ["hello", "queued msg"])
        self.assertEqual(scripted.defaults, ["", "leftover"])


class TestPromptPreFill(unittest.TestCase):
    def test_ask_accepts_a_default(self) -> None:
        import inspect

        from cagentic.prompt import Prompt

        self.assertIn("default", inspect.signature(Prompt.ask).parameters)

    def test_the_default_reaches_prompt_toolkit(self) -> None:
        from cagentic.prompt import Prompt

        seen: dict = {}

        class _Pt:
            def prompt(self, text, **kw):
                seen.update(kw)
                return "typed"

        p = Prompt.__new__(Prompt)
        p._pt = _Pt()
        self.assertEqual(p.ask("> ", default="carried text"), "typed")
        self.assertEqual(seen.get("default"), "carried text")

    def test_no_default_is_an_empty_pre_fill_not_none(self) -> None:
        """prompt_toolkit rejects None for `default`."""
        from cagentic.prompt import Prompt

        seen: dict = {}

        class _Pt:
            def prompt(self, text, **kw):
                seen.update(kw)
                return ""

        p = Prompt.__new__(Prompt)
        p._pt = _Pt()
        p.ask("> ")
        self.assertEqual(seen.get("default"), "")


class TestVisibleCursor(unittest.TestCase):
    def test_the_echo_row_draws_a_cursor(self) -> None:
        """The hardware cursor can't be parked there — it has to stay in the
        scroll region where streamed output is written."""
        painted = _paint_with(buf="hello")
        self.assertTrue(painted, "nothing painted")
        self.assertIn(_CURSOR, painted[0])
        self.assertIn("hello", painted[0])

    def test_the_cursor_restore_is_still_emitted(self) -> None:
        """Dropping the DECRC would leave the real cursor on the pinned row and
        the next streamed token would overwrite the echo line."""
        painted = _paint_with(buf="x")
        self.assertTrue(painted[0].endswith("\0338"))
        self.assertTrue(painted[0].startswith("\0337"))


class TestVisibleTail(unittest.TestCase):
    """A long line must show its END — that's where you're typing. `ui.truncate`
    keeps the head, which is right for labels and wrong here."""

    def test_short_text_is_untouched(self) -> None:
        self.assertEqual(_visible_tail("hello", 20), "hello")

    def test_a_long_line_keeps_its_tail(self) -> None:
        out = _visible_tail("abcdefghijklmnop", 6)
        self.assertTrue(out.endswith("nop"), out)
        self.assertTrue(out.startswith("…"), out)

    def test_the_result_fits_the_width(self) -> None:
        for width in range(1, 30):
            self.assertLessEqual(ui._vlen(_visible_tail("x" * 100, width)), width, width)

    def test_wide_characters_are_measured_in_columns(self) -> None:
        """Counting characters instead of columns would overflow the row and
        push the status bar off screen."""
        self.assertLessEqual(ui._vlen(_visible_tail("漢字" * 20, 10)), 10)

    def test_a_zero_width_budget_yields_nothing(self) -> None:
        self.assertEqual(_visible_tail("abc", 0), "")

    def test_a_pasted_paragraph_does_not_scan_the_whole_string(self) -> None:
        """The trim loop is per-column; slicing to a bound first keeps it from
        going quadratic on a big paste."""
        out = _visible_tail("y" * 200_000, 40)
        self.assertLessEqual(ui._vlen(out), 40)
        self.assertTrue(out.startswith("…"))


if __name__ == "__main__":
    unittest.main()
