"""Phase 4b/4c regressions — context pressure and expanding truncated output.

The status bar showed a bare `context ~12,345`: a number with no denominator,
which said nothing about how close auto-compact was, so compaction arrived as a
surprise mid-task. And `_truncate` cut long tool output with no way to see the
rest — the model and the user both got the clipped text, but only the model was
supposed to.
"""

from __future__ import annotations

import unittest

from cagentic import ui


def _segment(ctx: int, limit: int, compact_at: int) -> str:
    bar = ui.StatusBar(ctx_tokens=ctx, ctx_limit=limit, compact_at=compact_at)
    return ui._strip_ansi(bar._context_segment(ctx))


class TestContextPressure(unittest.TestCase):
    W = 200_000
    C = 120_000  # 60% of the window — engine.compact_threshold()

    def test_an_unknown_window_falls_back_to_the_absolute_count(self) -> None:
        """A percentage of an unknown total would be a fabricated number."""
        self.assertEqual(_segment(12_345, 0, 0), "context ~12,345")

    def test_a_known_window_reports_a_percentage(self) -> None:
        self.assertEqual(_segment(40_000, self.W, self.C), "context 20%")

    def test_a_fresh_session_does_not_mention_compaction(self) -> None:
        """Early on the trigger is irrelevant; showing it is just noise."""
        self.assertNotIn("compact", _segment(5_000, self.W, self.C))

    def test_the_trigger_appears_as_it_gets_close(self) -> None:
        self.assertEqual(_segment(100_000, self.W, self.C), "context 50% · compact at 60%")

    def test_crossing_the_threshold_says_so(self) -> None:
        self.assertIn("compacting", _segment(125_000, self.W, self.C))

    def test_a_near_miss_does_not_print_the_same_number_twice(self) -> None:
        """59.5% rounds to 60% against a 60% trigger — "60% · compact at 60%"
        reads like a stuck bar rather than a near miss."""
        seg = _segment(119_000, self.W, self.C)
        self.assertEqual(seg, "context 60% · compact soon")

    def test_the_threshold_is_relative_to_the_window_not_hardcoded(self) -> None:
        """An 8k local model and a 200k cloud model must both read sensibly —
        this is the Phase-1b budget showing through."""
        self.assertEqual(_segment(4_096, 8192, 4915), "context 50% · compact at 60%")

    def test_a_zero_compact_threshold_still_shows_the_percentage(self) -> None:
        self.assertEqual(_segment(50_000, self.W, 0), "context 25%")

    def test_over_full_context_does_not_crash(self) -> None:
        self.assertIn("compacting", _segment(500_000, self.W, self.C))


class TestStatusBarWiring(unittest.TestCase):
    def test_the_bar_defaults_to_no_budget(self) -> None:
        """Every other construction site (tests, sub-agents) must keep working
        without passing the new arguments."""
        bar = ui.StatusBar()
        self.assertEqual(bar._ctx_limit, 0)
        self.assertEqual(bar._compact_at, 0)

    def test_negative_inputs_are_clamped(self) -> None:
        bar = ui.StatusBar(ctx_limit=-5, compact_at=-9)
        self.assertEqual((bar._ctx_limit, bar._compact_at), (0, 0))

    def test_the_turn_feeds_the_live_budget_in(self) -> None:
        """Read per turn, not once — `/model` moves the window mid-session."""
        import inspect

        from cagentic.agent import Agent

        source = inspect.getsource(Agent.turn)
        self.assertIn("ctx_limit=self.engine.context_window()", source)
        self.assertIn("compact_at=self.engine.compact_threshold()", source)

    def test_the_reservation_is_untouched_by_the_new_arguments(self) -> None:
        """The Windows invisible-reply bug came from a reservation mismatch;
        adding constructor arguments must not disturb it."""
        self.assertEqual(ui.StatusBar(ctx_limit=1000)._reserve, 1)
        self.assertEqual(ui.StatusBar(extra_reserved_rows=1, ctx_limit=1000)._reserve, 2)


class TestExpandTruncatedOutput(unittest.TestCase):
    def setUp(self) -> None:
        from cagentic.tools import _last_truncated

        _last_truncated.clear()

    def test_nothing_truncated_means_nothing_to_expand(self) -> None:
        from cagentic.tools import last_truncated

        self.assertIsNone(last_truncated())

    def test_short_output_is_not_captured(self) -> None:
        from cagentic.tools import _truncate, last_truncated

        self.assertEqual(_truncate("hi", 100), "hi")
        self.assertIsNone(last_truncated())

    def test_the_full_text_survives_head_truncation(self) -> None:
        from cagentic.tools import _truncate, last_truncated

        original = "".join(f"line {i}\n" for i in range(2000))
        clipped = _truncate(original, 200)
        self.assertLess(len(clipped), len(original))
        self.assertEqual(last_truncated()["text"], original)

    def test_the_full_text_survives_middle_truncation(self) -> None:
        """`_truncate_ends` drops the middle, which is exactly the part Ctrl-O
        exists to recover."""
        from cagentic.tools import _truncate_ends, last_truncated

        original = "HEAD" + "m" * 5000 + "TAIL"
        _truncate_ends(original, 200)
        self.assertEqual(last_truncated()["text"], original)

    def test_the_marker_tells_the_user_the_key(self) -> None:
        """An invisible feature is not a feature."""
        from cagentic.tools import _truncate, _truncate_ends

        self.assertIn("Ctrl-O", _truncate("x" * 5000, 100))
        self.assertIn("Ctrl-O", _truncate_ends("x" * 5000, 100))

    def test_only_the_most_recent_is_kept(self) -> None:
        """Holding every truncated result would grow without bound."""
        from cagentic.tools import _truncate, last_truncated

        _truncate("a" * 3000, 100)
        _truncate("b" * 4000, 100)
        record = last_truncated()
        self.assertEqual(record["total"], 4000)
        self.assertTrue(record["text"].startswith("b"))

    def test_an_enormous_result_is_capped_and_says_so(self) -> None:
        from cagentic import tools as tools_mod

        huge = "z" * (tools_mod._EXPAND_CAP + 5000)
        tools_mod._truncate(huge, 100)
        record = tools_mod.last_truncated()
        self.assertTrue(record["clipped"])
        self.assertEqual(len(record["text"]), tools_mod._EXPAND_CAP)
        self.assertEqual(record["total"], len(huge))

    def test_the_returned_record_is_a_copy(self) -> None:
        """A caller mutating it must not corrupt the stored capture."""
        from cagentic.tools import _truncate, last_truncated

        _truncate("q" * 3000, 100)
        first = last_truncated()
        first["text"] = "tampered"
        self.assertNotEqual(last_truncated()["text"], "tampered")

    def test_the_key_is_bound_at_the_prompt(self) -> None:
        import inspect

        from cagentic import prompt as prompt_mod

        source = inspect.getsource(prompt_mod)
        self.assertIn('@bindings.add("c-o")', source)
        self.assertIn("run_in_terminal", source)
        self.assertIn("last_truncated", source)


if __name__ == "__main__":
    unittest.main()
