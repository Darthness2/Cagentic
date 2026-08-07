"""Regression tests for a round of correctness fixes.

Each test names the behaviour that was wrong before, so a future change that
reintroduces it fails here rather than in front of a user.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from cagentic.engine import LOOP_THRESHOLD, QueryEngine
from cagentic.openai_client import OpenAIClient
from cagentic.state import AppState
from cagentic.tools import (
    ToolContext,
    _as_tag_list,
    _restore_eol,
    t_edit_file,
    t_read_file,
    t_replace_lines,
    t_write_file,
)


class _NullClient:
    """Stands in for an LLM client — the engine only stores it here."""

    def chat(self, *a, **k):  # pragma: no cover - never reached
        raise AssertionError("no network in tests")


class _PrivateConfigDir(unittest.TestCase):
    """Point XDG_CONFIG_HOME at a temp dir so tests never touch real data."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.addCleanup(self._restore_xdg)
        self.addCleanup(self._cleanup_tmp)

    def _cleanup_tmp(self) -> None:
        """Remove the temp dir, tolerating files the app still holds open.

        The SQLite store keeps a connection to state.sqlite3 for the life of the
        process, and Windows refuses to unlink an open file — a leftover temp
        directory must not turn a passing test into an error.
        """
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def _restore_xdg(self) -> None:
        if self._old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._old_xdg


def _tool_call_msg(name: str) -> dict:
    """The assistant message the engine appends when the model calls a tool."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": name, "arguments": {}}}],
    }


def _pairing_is_intact(messages: list[dict]) -> bool:
    """Every assistant tool_call has a tool message answering it.

    This is what OpenAI and Anthropic both require; an unanswered call makes the
    NEXT request fail with a 400, so the conversation is broken from then on.
    """
    normalized = OpenAIClient(api_key="test")._normalize(messages)
    calls = sum(len(m.get("tool_calls") or []) for m in normalized)
    results = sum(1 for m in normalized if m.get("role") == "tool")
    return calls == results


class TestDroppedToolCallsStayAnswered(_PrivateConfigDir):
    """A tool call the engine declines to run still needs a result.

    The assistant message already announced the call. Skipping it silently left
    the message list malformed for the cloud providers.
    """

    def _engine(self) -> QueryEngine:
        state = AppState(workspace=self.tmp, home=self.tmp)
        return QueryEngine(client=_NullClient(), state=state, model="test-model")

    def test_loop_steered_call_still_gets_a_result(self) -> None:
        eng = self._engine()
        events = []
        # task_list is read-only and inert; repeating it across turns is what
        # trips the call-side loop guard.
        for _ in range(LOOP_THRESHOLD):
            eng.messages.append(_tool_call_msg("task_list"))
            events = list(eng._execute_and_record([("task_list", {}, "tool")]))

        self.assertTrue(
            any(e.kind == "warn" and "loop detected" in e.data.get("text", "") for e in events),
            "expected the last repeat to be steered",
        )
        tool_msgs = [m for m in eng.messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), LOOP_THRESHOLD)
        self.assertTrue(_pairing_is_intact(eng.messages))

    def test_abort_answers_the_calls_it_never_ran(self) -> None:
        eng = self._engine()
        # note_search is read-only, inert against the temp config dir, and NOT
        # in the engine's loop-exempt set, so a repeated result counts.
        args = {"query": "nothing-matches-this"}
        result = "(no notes match 'nothing-matches-this')"
        # Pre-load the result-loop history so the first result of this batch
        # crosses the hard-abort threshold.
        eng._recent_results = [("note_search", result)] * (LOOP_THRESHOLD * 2 - 1)

        eng.messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "note_search", "arguments": args}},
                    {"function": {"name": "note_search", "arguments": args}},
                ],
            }
        )
        calls = [("note_search", args, "tool"), ("note_search", args, "tool")]
        list(eng._execute_and_record(calls))

        self.assertTrue(eng._abort_turn, "expected the repeated result to abort")
        tool_msgs = [m for m in eng.messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 2, "the abandoned call needs a result too")
        self.assertTrue(_pairing_is_intact(eng.messages))


class TestReadFileRangeCache(_PrivateConfigDir):
    """Re-reading a DIFFERENT slice of a file must not come back '[CACHED]'."""

    def test_second_range_is_served(self) -> None:
        target = self.tmp / "long.txt"
        target.write_text("\n".join(f"line {i}" for i in range(1, 101)))
        ctx = ToolContext(
            root=self.tmp, read_cache={}, state=AppState(workspace=self.tmp, home=self.tmp)
        )

        first = t_read_file({"path": str(target), "start_line": 1, "end_line": 10}, ctx)
        self.assertIn("line 1", first)

        second = t_read_file({"path": str(target), "start_line": 90}, ctx)
        self.assertNotIn("CACHED", second)
        self.assertIn("line 95", second)

        # The identical range IS still suppressed — that's the point of the cache.
        again = t_read_file({"path": str(target), "start_line": 90}, ctx)
        self.assertIn("CACHED", again)

    def test_editing_invalidates_every_cached_range(self) -> None:
        target = self.tmp / "edit-me.txt"
        target.write_text("alpha\nbravo\ncharlie\n")
        ctx = ToolContext(
            root=self.tmp, read_cache={}, state=AppState(workspace=self.tmp, home=self.tmp)
        )

        t_read_file({"path": str(target), "start_line": 1, "end_line": 2}, ctx)
        t_edit_file({"path": str(target), "old_string": "bravo", "new_string": "BRAVO"}, ctx)
        after = t_read_file({"path": str(target), "start_line": 1, "end_line": 2}, ctx)
        self.assertNotIn("CACHED", after)
        self.assertIn("BRAVO", after)


class TestLineEndingsSurviveEdits(_PrivateConfigDir):
    """A one-line edit must not rewrite every line ending in the file."""

    def test_crlf_file_stays_crlf_through_the_recovery_path(self) -> None:
        target = self.tmp / "crlf.txt"
        target.write_bytes(b"alpha\r\nbravo\r\ncharlie\r\n")
        ctx = ToolContext(root=self.tmp, state=AppState(workspace=self.tmp, home=self.tmp))

        # old_string spans a line break with a bare LF, so the exact-match path
        # misses and the EOL-normalizing recovery path handles it.
        result = t_edit_file(
            {"path": str(target), "old_string": "alpha\nbravo", "new_string": "alpha\nBRAVO"},
            ctx,
        )
        self.assertTrue(result.startswith("OK:"), result)
        raw = target.read_bytes()
        self.assertEqual(raw, b"alpha\r\nBRAVO\r\ncharlie\r\n")

    def test_lf_file_stays_lf(self) -> None:
        target = self.tmp / "lf.txt"
        target.write_bytes(b"alpha\nbravo\ncharlie\n")
        ctx = ToolContext(root=self.tmp, state=AppState(workspace=self.tmp, home=self.tmp))

        t_edit_file(
            {"path": str(target), "old_string": "alpha\nbravo", "new_string": "alpha\nBRAVO"},
            ctx,
        )
        self.assertEqual(target.read_bytes(), b"alpha\nBRAVO\ncharlie\n")

    def test_replace_lines_keeps_crlf(self) -> None:
        target = self.tmp / "crlf-lines.txt"
        target.write_bytes(b"one\r\ntwo\r\nthree\r\n")
        ctx = ToolContext(root=self.tmp, state=AppState(workspace=self.tmp, home=self.tmp))

        t_replace_lines(
            {"path": str(target), "start_line": 2, "end_line": 2, "new_content": "TWO"},
            ctx,
        )
        self.assertEqual(target.read_bytes(), b"one\r\nTWO\r\nthree\r\n")

    def test_write_file_keeps_an_existing_files_crlf(self) -> None:
        target = self.tmp / "crlf-write.txt"
        target.write_bytes(b"one\r\ntwo\r\n")
        ctx = ToolContext(root=self.tmp, state=AppState(workspace=self.tmp, home=self.tmp))

        t_write_file({"path": str(target), "content": "uno\ndos\n"}, ctx)
        self.assertEqual(target.read_bytes(), b"uno\r\ndos\r\n")

    def test_restore_eol_leaves_lf_files_alone(self) -> None:
        self.assertEqual(_restore_eol("a\nb\n", "a\nB\n"), "a\nB\n")


class TestReplaceLinesDeletion(_PrivateConfigDir):
    """Empty new_content means 'delete the range', not 'leave a blank line'."""

    def test_empty_content_removes_the_lines(self) -> None:
        target = self.tmp / "trim.txt"
        target.write_text("keep1\ndrop\nkeep2\n")
        ctx = ToolContext(root=self.tmp, state=AppState(workspace=self.tmp, home=self.tmp))

        out = t_replace_lines(
            {"path": str(target), "start_line": 2, "end_line": 2, "new_content": ""}, ctx
        )
        self.assertTrue(out.startswith("OK:"), out)
        self.assertEqual(target.read_text(), "keep1\nkeep2\n")


class TestReminderIdMatching(_PrivateConfigDir):
    """An ambiguous id prefix must not act on an arbitrary — or every — match."""

    def test_ambiguous_prefix_deletes_nothing(self) -> None:
        from cagentic import reminders

        a = reminders.add("call the vet")
        b = reminders.add("renew passport")
        # Every id starts with "r", so this prefix matches both.
        self.assertFalse(reminders.delete("r"))
        remaining = {r.id for r in reminders.list_all()}
        self.assertEqual(remaining, {a.id, b.id})

    def test_exact_id_still_deletes_one(self) -> None:
        from cagentic import reminders

        a = reminders.add("call the vet")
        b = reminders.add("renew passport")
        self.assertTrue(reminders.delete(a.id))
        self.assertEqual({r.id for r in reminders.list_all()}, {b.id})

    def test_ambiguous_prefix_updates_nothing(self) -> None:
        from cagentic import reminders

        reminders.add("one")
        reminders.add("two")
        self.assertIsNone(reminders.update("r", status="done"))
        self.assertEqual(len(reminders.list_all()), 2)


class TestCompactionKeepsToolResults(unittest.TestCase):
    """Compaction must not delete a tool result just because it repeats.

    A same-turn fan-out of identical calls is normal (the engine allows it), so
    identical consecutive tool results are normal too. Dropping one left its
    tool_call unanswered and the next cloud request failed with a 400.
    """

    def test_identical_consecutive_tool_results_are_kept(self) -> None:
        from cagentic.services.compact import snip_compact

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "search twice"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "grep", "arguments": {}}},
                    {"function": {"name": "grep", "arguments": {}}},
                ],
            },
            {"role": "tool", "name": "grep", "content": "(no matches)"},
            {"role": "tool", "name": "grep", "content": "(no matches)"},
        ]
        snip_compact(messages)
        self.assertEqual(sum(1 for m in messages if m["role"] == "tool"), 2)
        self.assertTrue(_pairing_is_intact(messages))

    def test_duplicate_user_frames_are_still_dropped(self) -> None:
        from cagentic.services.compact import snip_compact

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "hello"},
        ]
        self.assertEqual(snip_compact(messages), 1)
        self.assertEqual(len(messages), 1)


class TestBrowserBridgeTimeout(unittest.TestCase):
    """A browser command that times out must return, not wedge the process.

    send() held the condition's lock and then called _record(), which took the
    same (non-reentrant) lock again — so the first timed-out browser call hung
    the REPL forever. Any browser tool used while the Chrome extension is not
    connected hit this.
    """

    def test_timeout_returns_instead_of_deadlocking(self) -> None:
        import threading

        from cagentic.browser import BrowserBridge

        bridge = BrowserBridge(port=0)
        bridge._server = object()  # pretend we're listening; nothing will poll

        box: dict = {}

        def run() -> None:
            box["result"] = bridge.send("read", {}, timeout=0.3)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(10)

        self.assertFalse(worker.is_alive(), "send() deadlocked on its own lock")
        self.assertFalse(box["result"]["ok"])
        self.assertEqual(len(bridge._recent), 1)

    def test_late_result_for_an_abandoned_command_is_dropped(self) -> None:
        from cagentic.browser import BrowserBridge

        bridge = BrowserBridge(port=0)
        bridge._server = object()
        bridge.send("read", {}, timeout=0.05)  # times out, abandons id 1
        bridge._deliver_result(1, True, {"text": "late"})
        self.assertEqual(bridge._results, {})


class TestTagCoercion(unittest.TestCase):
    """A string where the schema asked for an array must not become letters."""

    def test_string_tags(self) -> None:
        self.assertEqual(_as_tag_list("work"), ["work"])
        self.assertEqual(_as_tag_list("work, home"), ["work", "home"])

    def test_list_tags(self) -> None:
        self.assertEqual(_as_tag_list(["work", " home "]), ["work", "home"])

    def test_empty(self) -> None:
        self.assertEqual(_as_tag_list(None), [])
        self.assertEqual(_as_tag_list([]), [])


if __name__ == "__main__":
    unittest.main()
