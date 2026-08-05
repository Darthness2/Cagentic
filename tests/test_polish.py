"""Guards for the polish pass: one command catalog, one persistence path.

These are the invariants that quietly rot — a command added to the REPL but not
to the catalog, a glyph map copied for the third time, a save path that forgets
to be atomic. Cheap to assert, annoying to notice by hand.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from cagentic import storage
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


class TestAtomicWrite(unittest.TestCase):
    """Every persistence module now shares one write path."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_roundtrip_and_no_temp_left_behind(self) -> None:
        target = self.tmp / "nested" / "thing.json"
        storage.atomic_write_json(target, {"a": 1, "b": ["x"]})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                         {"a": 1, "b": ["x"]})
        strays = [p.name for p in target.parent.iterdir() if p.name != "thing.json"]
        self.assertEqual(strays, [])

    def test_overwrite_is_not_a_partial_read(self) -> None:
        target = self.tmp / "thing.json"
        storage.atomic_write_json(target, {"v": 1})
        storage.atomic_write_json(target, {"v": 2})
        self.assertEqual(storage.read_json(target, None), {"v": 2})

    def test_read_json_tolerates_missing_and_corrupt(self) -> None:
        self.assertEqual(storage.read_json(self.tmp / "nope.json", "fallback"),
                         "fallback")
        broken = self.tmp / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        self.assertEqual(storage.read_json(broken, "fallback"), "fallback")

    @unittest.skipUnless(hasattr(os, "fchmod"), "POSIX permissions only")
    def test_private_write_is_not_world_readable(self) -> None:
        import stat

        target = self.tmp / "secret.json"
        storage.atomic_write_json(target, {"token": "sk-xxx"}, private=True)
        mode = stat.S_IMODE(target.stat().st_mode)
        self.assertEqual(mode & (stat.S_IRGRP | stat.S_IROTH), 0)


class TestDurationFormatting(unittest.TestCase):
    def test_ladder(self) -> None:
        self.assertEqual(storage.fmt_duration(5), "5s")
        self.assertEqual(storage.fmt_duration(125), "2m")
        self.assertEqual(storage.fmt_duration(7200), "2h")
        self.assertEqual(storage.fmt_duration(200000), "2d")

    def test_negative_uses_magnitude(self) -> None:
        # Reminders format overdue times by passing the negated delta; a bare
        # int() truncation would have rendered "-1m" as "0m".
        self.assertEqual(storage.fmt_duration(-125), "2m")

    def test_fmt_ago_handles_missing_timestamps(self) -> None:
        self.assertEqual(storage.fmt_ago(0), "?")
        self.assertEqual(storage.fmt_ago(None), "?")

    def test_status_mark_falls_back(self) -> None:
        self.assertEqual(storage.status_mark("done"), "✓")
        self.assertEqual(storage.status_mark("nonsense"), "?")
        self.assertEqual(storage.status_mark(""), "?")


if __name__ == "__main__":
    unittest.main()
