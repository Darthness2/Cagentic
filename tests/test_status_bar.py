"""StatusBar must never strand the cursor at the home position.

The bottom status bar reserves the last terminal row with DECSTBM
(`ESC [ top;bottom r`). Per the VT spec that escape — and its reset
(`ESC [ r`) — homes the cursor to (1,1). If a scroll-region change isn't
bracketed by a cursor save (DECSC, `ESC 7`) / restore (DECRC, `ESC 8`),
the cursor is left at the top of the screen and the model's streamed
answer writes right over the banner and previous output.

Bracketing is necessary but not sufficient, because DECRC faithfully restores
a cursor that was already on the last row — the row the bar just reserved.
Below the scroll region LF stops scrolling, so the turn writes every line onto
the bar row and the next paint frame erases it: the answer never appears. That
is the REPL's steady state once the screen has scrolled full, and it recurs
whenever the terminal shrinks mid-turn and the bar re-reserves.

These tests assert the structural invariants directly: *every* scroll-region
escape the StatusBar emits lies between a save and a later restore; every
bottom-row reservation is preceded by the guard that frees a row inside the
region; and no cursor save is ever nested inside another.
"""

from __future__ import annotations

import io
import os
import re
import sys
import time
import unittest
from unittest import mock

from cagentic import ui

_REGION_RX = re.compile(r"\x1b\[[0-9;]*r")  # DECSTBM set/reset
_RESERVE_RX = re.compile(r"\x1b\[1;(\d+)r")  # DECSTBM reserving the bottom row
# IND, CUU, DECSC — the make-room guard that must immediately precede every
# bottom-row reservation.
_MAKE_ROOM = "\x1bD\x1b[A\x1b7"


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:  # StatusBar no-ops unless stdout is a tty
        return True


def _region_escapes_are_bracketed(s: str) -> bool:
    """True iff every scroll-region escape sits inside an open DECSC/DECRC
    pair — i.e. the cursor is always saved before and restored after any
    region change, so it can never be stranded at home."""
    depth = 0
    i = 0
    n = len(s)
    while i < n:
        two = s[i : i + 2]
        if two == "\x1b7":  # DECSC — save
            depth += 1
            i += 2
            continue
        if two == "\x1b8":  # DECRC — restore
            depth = max(0, depth - 1)
            i += 2
            continue
        m = _REGION_RX.match(s, i)
        if m:
            if depth <= 0:
                return False  # region change with no save in effect
            i = m.end()
            continue
        i += 1
    return True


def _max_save_depth(s: str) -> int:
    """Deepest DECSC nesting reached. Terminals keep a SINGLE saved-cursor
    slot, so a save inside a save silently discards the outer position and
    the matching restore puts the cursor somewhere it never was."""
    depth = 0
    deepest = 0
    i = 0
    while i < len(s):
        two = s[i : i + 2]
        if two == "\x1b7":
            depth += 1
            deepest = max(deepest, depth)
            i += 2
        elif two == "\x1b8":
            depth = max(0, depth - 1)
            i += 2
        else:
            i += 1
    return deepest


class StatusBarCursorSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._real_stdout = sys.stdout
        self._fake = _FakeTTY()
        sys.stdout = self._fake
        # Force the bar on regardless of the environment running the tests.
        self._prev_env = {
            name: os.environ.pop(name, None)
            for name in (
                "CAGENTIC_STATUS_BAR",
                "COLLAMA_STATUS_BAR",
                "CAGENTIC_MOTION",
                "CAGENTIC_CURSOR_CONTROL",
                "TERM",
            )
        }
        # The lifecycle assertions exercise real cursor painting. CI commonly
        # exports TERM=dumb, which the production UI now correctly treats as
        # incapable even when this test swaps in a fake TTY.
        os.environ["TERM"] = "xterm-256color"

    def tearDown(self) -> None:
        sys.stdout = self._real_stdout
        for name, value in self._prev_env.items():
            if value is not None:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)

    def _run_lifecycle(self) -> str:
        bar = ui.StatusBar(ctx_tokens=1234)
        bar.start()
        # Let the background painter run a few frames (incl. the resize path
        # the first frame may exercise).
        time.sleep(0.3)
        bar.stop()
        return self._fake.getvalue()

    def test_region_changes_are_always_bracketed(self) -> None:
        out = self._run_lifecycle()
        self.assertIn("\x1b[", out, "status bar produced no output")
        self.assertTrue(
            _REGION_RX.search(out),
            "status bar never set a scroll region — test would be vacuous",
        )
        self.assertTrue(
            _region_escapes_are_bracketed(out),
            "a scroll-region escape was emitted outside a cursor save/restore "
            "bracket — the cursor can be stranded at home and the model will "
            "write over the screen",
        )

    def test_cursor_is_restored_last(self) -> None:
        # The final cursor-affecting escape must be a restore, never a bare
        # region reset — otherwise the next prompt is drawn at the top.
        out = self._run_lifecycle()
        tail = out.rsplit("\x1b8", 1)[-1]
        self.assertFalse(
            _REGION_RX.search(tail),
            "scroll region was changed AFTER the last cursor restore — the "
            "cursor ends up homed and output overwrites the screen",
        )

    def _assert_every_reservation_makes_room(self, out: str, expected: int) -> None:
        """Every bottom-row reservation must be immediately preceded by the
        make-room guard, and there must be `expected` of them.

        Bracketing DECSTBM in DECSC/DECRC keeps the cursor off the home
        position, but it faithfully restores a cursor that was on the LAST
        row — the row just reserved. That is the REPL's steady state once the
        screen has scrolled full: prompt_toolkit leaves the cursor on the
        bottom row, so the turn's entire output lands below the scroll region,
        where LF no longer scrolls, and each 200 ms paint frame erases it. The
        answer never appears.

        So a row must be freed *before* the save: IND (ESC D) scrolls only if
        we are already at the bottom, CUU (ESC [ A) steps back up without ever
        scrolling. Order matters — both must precede the DECSC whose position
        DECRC will restore.
        """
        reservations = list(_RESERVE_RX.finditer(out))
        self.assertEqual(
            len(reservations),
            expected,
            f"expected {expected} bottom-row reservation(s), saw {len(reservations)}",
        )
        for m in reservations:
            self.assertTrue(
                out[: m.start()].endswith(_MAKE_ROOM),
                f"the reservation at offset {m.start()} ({m.group(0)!r}) is not "
                "immediately preceded by IND + CUU + DECSC — the cursor can be "
                "saved on the row being reserved, and the turn's output is then "
                "written onto the bar row and erased by the next paint frame",
            )

    def test_a_row_is_freed_inside_the_region_before_it_is_reserved(self) -> None:
        out = self._run_lifecycle()
        self._assert_every_reservation_makes_room(out, expected=1)

    def test_resize_mid_turn_re_reserves_with_the_same_guard(self) -> None:
        """A terminal that shrinks mid-turn hits the same defect.

        The bar re-reserves against the new height from the paint thread, and
        the terminal has just clamped the cursor into the shorter screen — so
        it can be sitting on exactly the row about to be reserved. Without the
        guard the rest of the answer is painted over, same as before.
        """
        current = [os.terminal_size((100, 24))]

        def fake_size(fallback=(80, 24)):
            return current[0]

        with mock.patch.object(ui.shutil, "get_terminal_size", fake_size):
            bar = ui.StatusBar(ctx_tokens=1234)
            bar.start()
            time.sleep(0.3)
            current[0] = os.terminal_size((100, 12))  # user drags the window shorter
            time.sleep(0.5)
            bar.stop()
        out = self._fake.getvalue()

        # start() reserved row 24, the resize re-reserved row 12.
        self._assert_every_reservation_makes_room(out, expected=2)
        self.assertEqual(
            [m.group(1) for m in _RESERVE_RX.finditer(out)],
            ["23", "11"],
            "the bar did not re-reserve the bottom row against the new height",
        )

    def test_resize_prelude_resets_the_region_before_making_room(self) -> None:
        """IND scrolls *within* the active scroll region. Re-reserving with a
        stale region still set would scroll the region's contents and drag the
        cursor off the line being streamed, so the region must be reset to the
        full screen (bracketed — that escape homes too) before the guard."""
        seq = ui._reserve_bottom_row_seq(12, region_active=True)
        self.assertTrue(
            seq.startswith("\x1b7\x1b[r\x1b8" + _MAKE_ROOM),
            f"resize reservation does not reset the region before making room: {seq!r}",
        )
        # start() has no region yet; resetting there would be pointless noise.
        self.assertTrue(ui._reserve_bottom_row_seq(24, region_active=False).startswith(_MAKE_ROOM))

    def test_cursor_saves_are_never_nested(self) -> None:
        """DECSC/DECRC is one slot per screen buffer. The resize
        re-reservation carries its own save/restore pairs now, so it has to be
        emitted beside the paint frame's bracket rather than inside it — fold
        it back in and the inner save overwrites the outer position, leaving
        the paint's restore to drop the cursor somewhere it never was."""
        current = [os.terminal_size((100, 24))]

        def fake_size(fallback=(80, 24)):
            return current[0]

        with mock.patch.object(ui.shutil, "get_terminal_size", fake_size):
            bar = ui.StatusBar(ctx_tokens=1234)
            bar.start()
            time.sleep(0.3)
            current[0] = os.terminal_size((100, 12))
            time.sleep(0.5)
            bar.stop()
        self.assertEqual(
            _max_save_depth(self._fake.getvalue()),
            1,
            "a DECSC was emitted while another was still open — the saved "
            "cursor position is lost and DECRC restores the wrong row",
        )

    def test_cagentic_environment_switch_disables_the_bar(self) -> None:
        os.environ["CAGENTIC_STATUS_BAR"] = "off"
        bar = ui.StatusBar(ctx_tokens=1234)
        bar.start()
        bar.stop()
        self.assertEqual(self._fake.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
