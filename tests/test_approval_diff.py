"""Diff-at-approval-time regressions.

The approval prompt used to say only "Edit file · path: replace 1
occurrence(s)" — the user was asked to approve a change they could not see.
These tests pin two things: that a preview is produced for each mutating file
tool, and (more importantly) that the preview matches what the tool actually
writes. A preview that disagrees with the write is worse than no preview.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cagentic.permissions import describe_change
from cagentic.state import AppState
from cagentic.tools import (
    ToolContext,
    _read_text_robust,
    preview_change,
    t_edit_file,
    t_write_file,
)


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = AppState(workspace=self.root, home=self.root, yolo=True)
        self.ctx = ToolContext(root=self.root, state=self.state, yolo=True)

    def tearDown(self) -> None:
        # SQLite/Windows can hold handles; teardown must tolerate it.
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def write(self, name: str, body: str) -> Path:
        p = self.root / name
        with p.open("w", encoding="utf-8", newline="") as f:
            f.write(body)
        return p


class TestPreviewMatchesTheWrite(_Workspace):
    def test_edit_preview_equals_the_applied_result(self) -> None:
        self.write("a.py", "one\ntwo\nthree\n")
        args = {"path": "a.py", "old_string": "two", "new_string": "TWO"}

        planned = preview_change("edit_file", args, self.root)
        self.assertIsNotNone(planned)
        _path, before, after = planned

        result = t_edit_file(dict(args), self.ctx)
        self.assertTrue(result.startswith("OK:"), result)
        on_disk = _read_text_robust(self.root / "a.py")

        self.assertEqual(before, "one\ntwo\nthree\n")
        self.assertEqual(after, on_disk)

    def test_preview_preserves_crlf_exactly_as_the_tool_does(self) -> None:
        """The EOL-restoration path is the easiest place for a preview to lie."""
        self.write("crlf.txt", "alpha\r\nbeta\r\ngamma\r\n")
        args = {"path": "crlf.txt", "old_string": "beta", "new_string": "BETA"}

        _p, _before, after = preview_change("edit_file", args, self.root)
        t_edit_file(dict(args), self.ctx)
        on_disk = _read_text_robust(self.root / "crlf.txt")

        self.assertIn("\r\n", after)
        self.assertEqual(after, on_disk)

    def test_write_preview_equals_the_applied_result(self) -> None:
        self.write("b.txt", "old body\n")
        args = {"path": "b.txt", "content": "new body\n"}

        _p, before, after = preview_change("write_file", args, self.root)
        t_write_file(dict(args), self.ctx)
        on_disk = _read_text_robust(self.root / "b.txt")

        self.assertEqual(before, "old body\n")
        self.assertEqual(after, on_disk)

    def test_new_file_previews_against_empty(self) -> None:
        planned = preview_change("write_file", {"path": "new.txt", "content": "hi\n"}, self.root)
        self.assertIsNotNone(planned)
        _p, before, after = planned
        self.assertEqual(before, "")
        self.assertEqual(after, "hi\n")

    def test_multi_edit_preview_applies_edits_in_sequence(self) -> None:
        self.write("m.txt", "a\nb\nc\n")
        planned = preview_change(
            "multi_edit",
            {
                "path": "m.txt",
                "edits": [
                    {"old_string": "a", "new_string": "A"},
                    {"old_string": "c", "new_string": "C"},
                ],
            },
            self.root,
        )
        self.assertIsNotNone(planned)
        self.assertEqual(planned[2], "A\nb\nC\n")

    def test_replace_lines_preview(self) -> None:
        self.write("r.txt", "1\n2\n3\n4\n")
        planned = preview_change(
            "replace_lines",
            {"path": "r.txt", "start_line": 2, "end_line": 3, "new_content": "X"},
            self.root,
        )
        self.assertIsNotNone(planned)
        self.assertEqual(planned[2], "1\nX\n4\n")


class TestPreviewFailsQuietly(_Workspace):
    """A preview must never break or mislead the approval prompt."""

    def test_unmatched_edit_previews_nothing(self) -> None:
        self.write("a.py", "one\n")
        planned = preview_change(
            "edit_file",
            {"path": "a.py", "old_string": "nowhere", "new_string": "x"},
            self.root,
        )
        self.assertIsNone(planned, "an edit that would fail must not show a diff")

    def test_missing_file_previews_nothing(self) -> None:
        self.assertIsNone(
            preview_change(
                "edit_file", {"path": "gone.py", "old_string": "a", "new_string": "b"}, self.root
            )
        )

    def test_path_escape_previews_nothing(self) -> None:
        self.assertIsNone(
            preview_change("write_file", {"path": "../../etc/passwd", "content": "x"}, self.root)
        )

    def test_non_file_tool_previews_nothing(self) -> None:
        self.assertIsNone(preview_change("run_bash", {"command": "ls"}, self.root))


class TestDescribeChange(_Workspace):
    def test_renders_a_patch_with_a_stat_header(self) -> None:
        self.write("a.py", "one\ntwo\nthree\n")
        out = describe_change(
            "edit_file",
            {"path": "a.py", "old_string": "two", "new_string": "TWO"},
            self.state,
            colorize=False,
        )
        self.assertIn("a.py", out)
        self.assertIn("+1 -1", out)
        self.assertIn("-two", out)
        self.assertIn("+TWO", out)

    def test_plain_mode_emits_no_ansi(self) -> None:
        self.write("a.py", "one\ntwo\n")
        out = describe_change(
            "edit_file",
            {"path": "a.py", "old_string": "two", "new_string": "TWO"},
            self.state,
            colorize=False,
        )
        self.assertNotIn("\x1b[", out, "ANSI would render literally in the browser")

    def test_no_op_change_is_called_out_rather_than_shown_as_empty(self) -> None:
        self.write("a.py", "same\n")
        out = describe_change(
            "write_file", {"path": "a.py", "content": "same\n"}, self.state, colorize=False
        )
        self.assertIn("no change", out)

    def test_non_diffable_tool_returns_empty(self) -> None:
        self.assertEqual(describe_change("run_bash", {"command": "ls"}, self.state), "")

    def test_unpreviewable_change_returns_empty_not_an_exception(self) -> None:
        self.assertEqual(
            describe_change(
                "edit_file",
                {"path": "missing.py", "old_string": "a", "new_string": "b"},
                self.state,
            ),
            "",
        )


if __name__ == "__main__":
    unittest.main()
