"""Guards for the polish pass: one command catalog, one set of helpers.

These are the invariants that quietly rot — a command added to the REPL but not
to the catalog, a glyph map copied for the third time. Cheap to assert,
annoying to notice by hand.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from cagentic import fmt
from cagentic.prompt import ALL_COMMANDS, COMMAND_GROUPS, SLASH_COMMANDS


def _repl_commands() -> set[str]:
    """Every command name cli.repl() actually dispatches on."""
    src = Path("cagentic/cli.py").read_text(encoding="utf-8")
    names = set(re.findall(r'cmd == "([a-z]+)"', src))
    for group in re.findall(r'cmd in \(([^)]*)\)', src):
        names.update(re.findall(r'"([a-z]+)"', group))
    return {f"/{n}" for n in names}


class TestCommandCatalog(unittest.TestCase):
    """The completion popup and /help are generated from one list, and that
    list has to match what the REPL will actually accept."""

    def test_every_catalog_command_is_dispatched(self) -> None:
        missing = sorted(set(ALL_COMMANDS) - _repl_commands())
        self.assertEqual(missing, [], f"offered but not handled: {missing}")

    def test_every_dispatched_command_is_offered(self) -> None:
        missing = sorted(_repl_commands() - set(ALL_COMMANDS))
        self.assertEqual(missing, [], f"handled but never advertised: {missing}")

    def test_no_duplicate_entries(self) -> None:
        names = [name for name, _args, _hint in
                 (e for _s, entries in COMMAND_GROUPS for e in entries)]
        self.assertEqual(len(names), len(set(names)), "a command is listed twice")

    def test_popup_entries_are_well_formed(self) -> None:
        for name, hint in SLASH_COMMANDS:
            self.assertTrue(name.startswith("/"), name)
            self.assertTrue(hint.strip(), f"{name} has no hint")

    def test_help_renders(self) -> None:
        import io
        from contextlib import redirect_stdout

        from cagentic.cli import print_help

        buf = io.StringIO()
        with redirect_stdout(buf):
            print_help()
        out = buf.getvalue()
        for name, _args, _hint in (e for _s, entries in COMMAND_GROUPS for e in entries):
            self.assertIn(name, out)


class TestDurationFormatting(unittest.TestCase):
    def test_ladder(self) -> None:
        self.assertEqual(fmt.fmt_duration(5), "5s")
        self.assertEqual(fmt.fmt_duration(125), "2m")
        self.assertEqual(fmt.fmt_duration(7200), "2h")
        self.assertEqual(fmt.fmt_duration(200000), "2d")

    def test_negative_uses_magnitude(self) -> None:
        # Reminders format overdue times by passing the negated delta; a bare
        # int() truncation would have rendered "-1m" as "0m".
        self.assertEqual(fmt.fmt_duration(-125), "2m")

    def test_fmt_ago_handles_missing_timestamps(self) -> None:
        self.assertEqual(fmt.fmt_ago(0), "?")
        self.assertEqual(fmt.fmt_ago(None), "?")

    def test_status_mark_falls_back(self) -> None:
        self.assertEqual(fmt.status_mark("done"), "✓")
        self.assertEqual(fmt.status_mark("nonsense"), "?")
        self.assertEqual(fmt.status_mark(""), "?")


if __name__ == "__main__":
    unittest.main()
