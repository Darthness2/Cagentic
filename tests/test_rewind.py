"""Phase 3a regressions — turn checkpoints (/rewind) and type-ahead wiring.

`/undo` only ever reverted one edit. A turn typically writes several files, so
undoing it meant running /undo N times and counting — and nothing put the
*conversation* back, so the model still believed it had made the changes.

The type-ahead module existed but was dead: it referenced two `ui` helpers that
don't exist (`ui.input_prefix`, `ui._trunc`), so its first repaint would have
raised AttributeError, and `StatusBar` had no way to reserve the extra row its
echo line needs.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cagentic.cli import (
    _revert_edits,
    _truncate_to_turn,
    _turn_summary,
    _user_message_indices,
)
from cagentic.tools import ToolContext, _read_text_robust, t_edit_file, t_write_file
from cagentic.state import AppState


class _FakeAgent:
    def __init__(self, messages):
        self.messages = list(messages)

    def load_messages(self, messages):
        self.messages = list(messages)


class TestUserMessageIndices(unittest.TestCase):
    def test_tool_results_are_not_turns(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "", "tool_calls": [{}]},
            {"role": "tool", "content": "tool output"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "second"},
        ]
        self.assertEqual(_user_message_indices(msgs), [1, 5])

    def test_background_injections_are_not_turns(self) -> None:
        """They're appended with role=user but the user never typed them;
        counting them would shift every /rewind number the user was shown."""
        msgs = [
            {"role": "user", "content": "real turn"},
            {"role": "user", "content": "[background] task 3 finished (done): x"},
            {"role": "user", "content": "another real turn"},
        ]
        self.assertEqual(_user_message_indices(msgs), [0, 2])


class TestTruncateToTurn(unittest.TestCase):
    def test_drops_from_the_target_turn_onward(self) -> None:
        agent = _FakeAgent(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "two"},
                {"role": "assistant", "content": "a2"},
            ]
        )
        dropped = _truncate_to_turn(agent, 2)
        self.assertEqual(dropped, 2)
        self.assertEqual([m["content"] for m in agent.messages], ["sys", "one", "a1"])

    def test_rewinding_to_turn_one_leaves_only_the_system_prompt(self) -> None:
        agent = _FakeAgent(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "a1"},
            ]
        )
        _truncate_to_turn(agent, 1)
        self.assertEqual([m["role"] for m in agent.messages], ["system"])

    def test_out_of_range_is_a_no_op(self) -> None:
        agent = _FakeAgent([{"role": "user", "content": "one"}])
        self.assertEqual(_truncate_to_turn(agent, 9), 0)
        self.assertEqual(_truncate_to_turn(agent, 0), 0)
        self.assertEqual(len(agent.messages), 1)


class TestRevertEdits(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = AppState(workspace=self.root, home=self.root, yolo=True)
        self.ctx = ToolContext(root=self.root, state=self.state, yolo=True)

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_reverts_an_edit_back_to_its_before_text(self) -> None:
        (self.root / "a.py").write_text("one\ntwo\n", encoding="utf-8")
        t_edit_file({"path": "a.py", "old_string": "two", "new_string": "TWO"}, self.ctx)
        reverted, problems = _revert_edits(list(reversed(self.state.edit_history)))
        self.assertEqual((reverted, problems), (1, []))
        self.assertEqual(_read_text_robust(self.root / "a.py"), "one\ntwo\n")

    def test_a_created_file_is_removed(self) -> None:
        t_write_file({"path": "new.txt", "content": "hi\n"}, self.ctx)
        _revert_edits(list(reversed(self.state.edit_history)))
        self.assertFalse((self.root / "new.txt").exists())

    def test_two_edits_to_one_file_unwind_in_reverse(self) -> None:
        """Applying them oldest-first would restore the first edit's 'before'
        and then immediately overwrite it with the second's."""
        (self.root / "a.py").write_text("v0\n", encoding="utf-8")
        t_edit_file({"path": "a.py", "old_string": "v0", "new_string": "v1"}, self.ctx)
        t_edit_file({"path": "a.py", "old_string": "v1", "new_string": "v2"}, self.ctx)
        reverted, problems = _revert_edits(list(reversed(self.state.edit_history)))
        self.assertEqual((reverted, problems), (2, []))
        self.assertEqual(_read_text_robust(self.root / "a.py"), "v0\n")

    def test_refuses_a_file_the_user_changed_afterwards(self) -> None:
        """Reverting would silently discard the user's own work."""
        (self.root / "a.py").write_text("one\n", encoding="utf-8")
        t_edit_file({"path": "a.py", "old_string": "one", "new_string": "ONE"}, self.ctx)
        (self.root / "a.py").write_text("hand-edited by me\n", encoding="utf-8")
        reverted, problems = _revert_edits(list(reversed(self.state.edit_history)))
        self.assertEqual(reverted, 0)
        self.assertIn("changed after Cagentic's edit", problems[0])
        self.assertEqual(_read_text_robust(self.root / "a.py"), "hand-edited by me\n")

    def test_reports_a_vanished_file_instead_of_recreating_it(self) -> None:
        (self.root / "a.py").write_text("one\n", encoding="utf-8")
        t_edit_file({"path": "a.py", "old_string": "one", "new_string": "ONE"}, self.ctx)
        (self.root / "a.py").unlink()
        reverted, problems = _revert_edits(list(reversed(self.state.edit_history)))
        self.assertEqual(reverted, 0)
        self.assertIn("no longer exists", problems[0])

    def test_crlf_survives_a_revert(self) -> None:
        with (self.root / "c.txt").open("w", encoding="utf-8", newline="") as fh:
            fh.write("a\r\nb\r\n")
        t_edit_file({"path": "c.txt", "old_string": "b", "new_string": "B"}, self.ctx)
        _revert_edits(list(reversed(self.state.edit_history)))
        self.assertEqual(_read_text_robust(self.root / "c.txt"), "a\r\nb\r\n")


class TestTurnStamping(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = AppState(workspace=self.root, home=self.root, yolo=True)
        self.ctx = ToolContext(root=self.root, state=self.state, yolo=True)

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_edits_carry_the_current_turn(self) -> None:
        self.state.update(turn_index=1)
        t_write_file({"path": "a.txt", "content": "1\n"}, self.ctx)
        self.state.update(turn_index=2)
        t_write_file({"path": "b.txt", "content": "2\n"}, self.ctx)
        self.assertEqual([e["turn"] for e in self.state.edit_history], [1, 2])

    def test_summary_counts_edits_per_turn(self) -> None:
        agent = _FakeAgent(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "add a file"},
                {"role": "user", "content": "and another\n\n--- @x.txt ---\nnoise"},
            ]
        )
        history = [{"turn": 1}, {"turn": 1}, {"turn": 2}]
        summary = _turn_summary(agent, history)
        self.assertEqual([(n, e) for n, _p, e in summary], [(1, 2), (2, 1)])
        # The attachment block is stripped from the preview.
        self.assertEqual(summary[1][1], "and another")


class TestTypeAheadIsUsable(unittest.TestCase):
    """It was dead code. The defect that kept it that way was the `ui` helpers
    below — the first repaint would have died with AttributeError."""

    def test_can_run_stays_a_property(self) -> None:
        """Every guard in the module is written `if not self.can_run`. Turning
        it back into a method would make all of them silently always-false."""
        from cagentic.typeahead import TypeAhead

        self.assertIsInstance(TypeAhead.can_run, property)

    def test_it_only_references_helpers_that_exist(self) -> None:
        """It called ui.input_prefix() and ui._trunc(), neither of which is
        real — the first paint would have raised AttributeError."""
        from cagentic import ui

        self.assertTrue(hasattr(ui, "prompt_prefix"))
        self.assertTrue(hasattr(ui, "truncate"))
        source = Path(ui.__file__).with_name("typeahead.py").read_text(encoding="utf-8")
        self.assertNotIn("ui.input_prefix", source)
        self.assertNotIn("ui._trunc(", source)

    def test_disabled_backend_makes_every_entry_point_a_no_op(self) -> None:
        from cagentic.typeahead import TypeAhead

        ta = TypeAhead()
        ta._backend = "none"
        self.assertFalse(ta.can_run)
        self.assertIsNone(ta.make_interrupt_check())
        ta.start_after_bar(None)  # must not raise
        ta.stop()
        self.assertIsNone(ta.take_pending())
        self.assertIsNone(ta.consume_interrupt())

    def test_status_bar_can_reserve_the_echo_row(self) -> None:
        """The echo row must sit outside the scroll region, or streamed output
        scrolls straight over what the user is typing."""
        from cagentic import ui

        self.assertEqual(ui.StatusBar()._reserve, 1)
        self.assertEqual(ui.StatusBar(extra_reserved_rows=1)._reserve, 2)
        seq = ui._reserve_bottom_row_seq(24, region_active=False, reserve=2)
        self.assertIn("\033[1;22r", seq)


class TestAgentPendingInput(unittest.TestCase):
    def test_turn_exposes_pending_input(self) -> None:
        """The REPL reads this to submit a queued message without prompting."""
        from cagentic.agent import Agent

        class _Null:
            pass

        with tempfile.TemporaryDirectory() as d:
            agent = Agent(_Null(), "m", Path(d))
            self.assertTrue(hasattr(agent, "pending_input"))
            self.assertIsNone(agent.pending_input)


if __name__ == "__main__":
    unittest.main()
