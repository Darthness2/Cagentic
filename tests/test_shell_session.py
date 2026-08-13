"""Phase 3c regressions — the persistent shell.

Each `run_bash` used to be a fresh process, so `cd backend` then `npm test` ran
the tests in the wrong directory. A long-lived shell fixes that, but only if it
handles its own failure modes: a session that deadlocks, or that attributes one
command's output to the next, is worse than no session at all.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from cagentic.shell_session import IDLE_TIMEOUT, MAX_SESSIONS, SessionPool, ShellSession
from cagentic.tools import _truncate_ends

POSIX_ONLY = unittest.skipUnless(SessionPool.supported(), "sessions are POSIX-only")


class TestTruncateEnds(unittest.TestCase):
    """A failing build prints thousands of lines and then the one line that
    says what broke. Head-only truncation throws that away."""

    def test_short_output_is_untouched(self) -> None:
        self.assertEqual(_truncate_ends("hello", 100), "hello")

    def test_both_ends_survive(self) -> None:
        text = "START" + ("x" * 5000) + "THE-ACTUAL-ERROR"
        out = _truncate_ends(text, 400)
        self.assertTrue(out.startswith("START"))
        self.assertTrue(out.endswith("THE-ACTUAL-ERROR"))
        self.assertIn("omitted from the middle", out)

    def test_the_result_stays_near_the_budget(self) -> None:
        out = _truncate_ends("y" * 10000, 500)
        self.assertLess(len(out), 500 + 120)


@POSIX_ONLY
class TestSession(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.session = ShellSession(self.root, ["/bin/sh"])
        self.assertTrue(self.session.start())

    def tearDown(self) -> None:
        self.session.close()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_state_persists_across_commands(self) -> None:
        (self.root / "sub").mkdir()
        self.assertEqual(self.session.run("cd sub", 10)[0], 0)
        code, out, err = self.session.run("pwd", 10)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("sub", out)

    def test_exported_variables_persist(self) -> None:
        self.session.run("export GREETING=hello", 10)
        _code, out, _err = self.session.run('echo "$GREETING world"', 10)
        self.assertIn("hello world", out)

    def test_exit_codes_are_reported(self) -> None:
        self.assertEqual(self.session.run("true", 10)[0], 0)
        self.assertEqual(self.session.run("false", 10)[0], 1)
        self.assertEqual(self.session.run("sh -c 'exit 7'", 10)[0], 7)

    def test_stderr_is_captured_too(self) -> None:
        _code, out, _err = self.session.run("echo oops >&2", 10)
        self.assertIn("oops", out)

    def test_one_commands_output_never_leaks_into_the_next(self) -> None:
        """The whole point of the sentinel protocol."""
        _c, first, _e = self.session.run("echo AAA", 10)
        _c, second, _e = self.session.run("echo BBB", 10)
        self.assertIn("AAA", first)
        self.assertNotIn("AAA", second)
        self.assertIn("BBB", second)

    def test_output_that_looks_like_the_sentinel_cannot_fake_a_boundary(self) -> None:
        """The nonce is randomised per session precisely for this."""
        _code, out, _err = self.session.run("echo __cagentic_done_0", 10)
        self.assertIn("__cagentic_done_0", out)
        # The session is still usable, i.e. we didn't mistake that for the end.
        self.assertEqual(self.session.run("echo still-here", 10)[0], 0)

    def test_large_output_does_not_deadlock(self) -> None:
        """A full pipe with a single blocked reader is the classic hang."""
        code, out, err = self.session.run("for i in $(seq 1 4000); do echo line-$i; done", 30)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("line-4000", out)

    def test_a_timeout_kills_the_session_rather_than_poisoning_it(self) -> None:
        """Leaving it alive would attribute the slow command's late output to
        whatever ran next."""
        code, _out, err = self.session.run("sleep 5", timeout=0.5)
        self.assertEqual(code, -1)
        self.assertIn("timed out", err)
        self.assertFalse(self.session.alive)

    def test_a_shell_that_exits_is_reported_not_hung(self) -> None:
        code, _out, err = self.session.run("exit 4", 10)
        self.assertEqual(code, -1)
        self.assertIn("exited", err)
        self.assertFalse(self.session.alive)

    def test_running_on_a_dead_session_errors_instead_of_blocking(self) -> None:
        self.session.close()
        code, _out, err = self.session.run("echo hi", 10)
        self.assertEqual(code, -1)
        self.assertIn("not running", err)


@POSIX_ONLY
class TestPool(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.pool = SessionPool()

    def tearDown(self) -> None:
        self.pool.close_all()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_the_same_key_reuses_one_shell(self) -> None:
        a = self.pool.get("k", self.root, ["/bin/sh"])
        b = self.pool.get("k", self.root, ["/bin/sh"])
        self.assertIs(a, b)

    def test_different_keys_get_different_shells(self) -> None:
        a = self.pool.get("one", self.root, ["/bin/sh"])
        b = self.pool.get("two", self.root, ["/bin/sh"])
        self.assertIsNot(a, b)

    def test_live_shells_are_capped(self) -> None:
        """Each is a real process; a gateway that keeps switching workspace
        would otherwise accumulate them without bound."""
        for i in range(MAX_SESSIONS + 3):
            self.pool.get(f"key-{i}", self.root, ["/bin/sh"])
        self.assertLessEqual(len(self.pool._sessions), MAX_SESSIONS)

    def test_a_dead_shell_is_replaced_not_returned(self) -> None:
        first = self.pool.get("k", self.root, ["/bin/sh"])
        first.close()
        second = self.pool.get("k", self.root, ["/bin/sh"])
        self.assertIsNot(first, second)
        self.assertTrue(second.alive)

    def test_close_all_leaves_nothing_running(self) -> None:
        sessions = [self.pool.get(f"k{i}", self.root, ["/bin/sh"]) for i in range(3)]
        self.pool.close_all()
        self.assertFalse(any(s.alive for s in sessions if s))

    def test_idle_timeout_is_a_sane_bound(self) -> None:
        self.assertGreater(IDLE_TIMEOUT, 60)


class TestWindowsFallsBack(unittest.TestCase):
    def test_sessions_are_disabled_off_posix(self) -> None:
        """cmd.exe quoting and the WSL launcher problem make a half-working
        session worse than the honest one-shot path."""
        import cagentic.shell_session as mod

        original = mod.os.name
        try:
            mod.os.name = "nt"
            self.assertFalse(SessionPool.supported())
        finally:
            mod.os.name = original
        # And on this platform it reflects reality.
        self.assertEqual(SessionPool.supported(), sys.platform != "win32" and original != "nt")


if __name__ == "__main__":
    unittest.main()
