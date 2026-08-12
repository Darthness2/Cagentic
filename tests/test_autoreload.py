"""Auto-reload regressions for the background gateway service.

The daemon installed by `--install-service` runs until the machine reboots, so
without this it serves whatever source it imported at boot — you edit a file
and the background gateway silently keeps running yesterday's code.

The risk being managed is the opposite of "reload fast": the thing restarting
is holding a live conversation, and a supervisor will happily restart a
crash-looping process forever. So most of what's pinned here is the *refusals*.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from cagentic import autoreload
from cagentic.autoreload import GatewayReloader, compiles, restart_argv, snapshot


class _Tree(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.write("mod.py", "x = 1\n")
        self.write("gateway_assets/app.css", "body { color: red }\n")
        self.restarts: list[list[str]] = []

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def touch(self, rel: str, text: str | None = None) -> None:
        p = self.root / rel
        if text is not None:
            p.write_text(text, encoding="utf-8")
        # Push the mtime forward: two writes inside one filesystem timestamp
        # tick look identical to a stat-based watcher.
        future = time.time() + 10
        os.utime(p, (future, future))

    def reloader(self, **kw) -> GatewayReloader:
        kw.setdefault("root", self.root)
        kw.setdefault("settle_seconds", 2.0)
        kw.setdefault("on_restart", lambda changed: self.restarts.append(changed))
        r = GatewayReloader(**kw)
        # Skip the start-up grace period unless a test is exercising it.
        r._started_at = -1000.0
        return r


class TestChangeDetection(_Tree):
    def test_a_quiet_tree_never_restarts(self) -> None:
        r = self.reloader()
        self.assertFalse(r.poll_once(now=100.0))
        self.assertFalse(r.poll_once(now=200.0))
        self.assertEqual(self.restarts, [])

    def test_edit_then_settle_restarts_once(self) -> None:
        r = self.reloader()
        self.touch("mod.py", "x = 2\n")
        self.assertFalse(r.poll_once(now=100.0), "first sight only arms the timer")
        self.assertFalse(r.poll_once(now=101.0), "still inside the settle window")
        self.assertTrue(r.poll_once(now=103.0))
        self.assertEqual(len(self.restarts), 1)
        self.assertIn(str(self.root / "mod.py"), self.restarts[0])

    def test_a_burst_of_writes_extends_the_window_instead_of_restarting_midway(self) -> None:
        """A `git pull` or save-all writes many files over a second or two;
        restarting on the first one races the rest onto disk."""
        r = self.reloader()
        for i, at in enumerate([100.0, 101.0, 102.0, 103.0]):
            self.touch("mod.py", f"x = {i}\n")
            self.assertFalse(r.poll_once(now=at), f"restarted mid-burst at {at}")
        self.assertEqual(self.restarts, [])
        self.assertTrue(r.poll_once(now=106.0))
        self.assertEqual(len(self.restarts), 1)

    def test_assets_count_as_code(self) -> None:
        """gateway_assets are read once at import, so they go just as stale."""
        r = self.reloader()
        self.touch("gateway_assets/app.css", "body { color: blue }\n")
        r.poll_once(now=100.0)
        self.assertTrue(r.poll_once(now=103.0))

    def test_new_and_deleted_files_are_both_changes(self) -> None:
        r = self.reloader()
        self.write("added.py", "y = 1\n")
        r.poll_once(now=100.0)
        self.assertTrue(r.poll_once(now=103.0))

        r2 = self.reloader()
        (self.root / "added.py").unlink()
        r2.poll_once(now=200.0)
        self.assertTrue(r2.poll_once(now=203.0))

    def test_pycache_and_unwatched_suffixes_are_ignored(self) -> None:
        r = self.reloader()
        self.write("__pycache__/mod.cpython-312.pyc", "junk")
        self.write("notes.txt", "not code")
        self.write(".git/HEAD", "ref: refs/heads/main")
        self.assertFalse(r.poll_once(now=100.0))
        self.assertFalse(r.poll_once(now=103.0))
        self.assertEqual(self.restarts, [])

    def test_baseline_advances_so_one_edit_restarts_once(self) -> None:
        r = self.reloader()
        self.touch("mod.py", "x = 2\n")
        r.poll_once(now=100.0)
        self.assertTrue(r.poll_once(now=103.0))
        # Nothing further changed, so no second restart.
        self.assertFalse(r.poll_once(now=110.0))
        self.assertEqual(len(self.restarts), 1)


class TestRefusals(_Tree):
    def test_broken_code_does_not_restart(self) -> None:
        """Restarting into a SyntaxError hands the supervisor a process that
        dies on import — forever."""
        r = self.reloader()
        self.touch("mod.py", "def oops(:\n")
        r.poll_once(now=100.0)
        self.assertFalse(r.poll_once(now=103.0))
        self.assertEqual(self.restarts, [])

    def test_a_persistently_broken_file_logs_once_not_once_per_poll(self) -> None:
        """A file stays broken for as long as you're mid-edit; a warning every
        second would turn one syntax error into thousands of log lines."""
        r = self.reloader()
        self.touch("mod.py", "def oops(:\n")
        with self.assertLogs("cagentic.autoreload", level="WARNING") as caught:
            r.poll_once(now=100.0)
            for at in (103.0, 104.0, 105.0, 106.0):
                self.assertFalse(r.poll_once(now=at))
        compile_warnings = [m for m in caught.output if "does not compile" in m]
        self.assertEqual(len(compile_warnings), 1, compile_warnings)

    def test_a_fixed_file_restarts_on_the_next_round(self) -> None:
        r = self.reloader()
        self.touch("mod.py", "def oops(:\n")
        r.poll_once(now=100.0)
        self.assertFalse(r.poll_once(now=103.0))
        # Developer fixes it; the mtime moves again.
        self.touch("mod.py", "def fine():\n    return 1\n")
        r.poll_once(now=110.0)
        self.assertTrue(r.poll_once(now=113.0))

    def test_a_turn_in_flight_blocks_the_restart(self) -> None:
        busy = {"v": True}
        r = self.reloader(is_busy=lambda: busy["v"])
        self.touch("mod.py", "x = 3\n")
        r.poll_once(now=100.0)
        self.assertFalse(r.poll_once(now=103.0), "must not drop a live reply")
        self.assertEqual(self.restarts, [])
        busy["v"] = False
        self.assertTrue(r.poll_once(now=104.0))

    def test_a_fresh_process_does_not_immediately_restart(self) -> None:
        """Guards against a boot loop when the tree is mid-write at start-up."""
        r = self.reloader()
        r._started_at = 100.0
        self.touch("mod.py", "x = 4\n")
        r.poll_once(now=100.5)
        self.assertFalse(r.poll_once(now=103.0))
        self.assertTrue(r.poll_once(now=100.0 + autoreload.MIN_RESTART_INTERVAL + 1))

    def test_an_unreadable_root_does_not_raise(self) -> None:
        r = self.reloader(root=self.root / "does-not-exist")
        self.assertFalse(r.poll_once(now=100.0))


class TestCompiles(unittest.TestCase):
    def test_reports_the_offending_file_and_line(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.py"
            bad.write_text("def f(:\n", encoding="utf-8")
            ok, problem = compiles([str(bad)])
            self.assertFalse(ok)
            self.assertIn("bad.py", problem)

    def test_non_python_files_are_not_parsed_as_python(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            css = Path(d) / "app.css"
            css.write_text("body { color: red }", encoding="utf-8")
            self.assertEqual(compiles([str(css)]), (True, ""))

    def test_a_deleted_file_does_not_block_the_restart(self) -> None:
        """Deleting a module is a legitimate change. Treating "can't read it"
        as "not ready" would wedge the reloader on it forever."""
        ok, problem = compiles(["/nope/gone.py"])
        self.assertTrue(ok, problem)


class TestRestartArgv(unittest.TestCase):
    """Getting this wrong means the restart silently becomes a different
    program — the service comes back as something that isn't the gateway."""

    def setUp(self) -> None:
        import sys

        self._argv = list(sys.argv)
        self._main = sys.modules.get("__main__")
        self.sys = sys

    def tearDown(self) -> None:
        self.sys.argv = self._argv
        if self._main is not None:
            self.sys.modules["__main__"] = self._main

    def test_module_invocation_is_rebuilt_as_dash_m(self) -> None:
        """`python -m cagentic --serve` is exactly what the service runs."""

        class FakeSpec:
            parent = "cagentic"

        class FakeMain:
            __spec__ = FakeSpec()

        self.sys.modules["__main__"] = FakeMain()
        self.sys.argv = ["/somewhere/cagentic/__main__.py", "--serve", "--port", "8700"]
        self.assertEqual(
            restart_argv(),
            [self.sys.executable, "-m", "cagentic", "--serve", "--port", "8700"],
        )

    def test_console_script_invocation_reuses_argv0(self) -> None:
        class FakeMain:
            __spec__ = None

        self.sys.modules["__main__"] = FakeMain()
        self.sys.argv = ["/usr/local/bin/cagentic", "--serve"]
        self.assertEqual(
            restart_argv(), [self.sys.executable, "/usr/local/bin/cagentic", "--serve"]
        )


class TestSnapshot(_Tree):
    def test_walks_nested_packages(self) -> None:
        self.write("services/compact.py", "z = 1\n")
        found = snapshot(self.root)
        self.assertIn(str(self.root / "services" / "compact.py"), found)
        self.assertIn(str(self.root / "gateway_assets" / "app.css"), found)


class TestGatewayBusySignal(unittest.TestCase):
    """is_busy is the seam the reloader relies on; if it stops reflecting a
    live turn, restarts start eating replies."""

    def test_reports_a_held_turn_lock(self) -> None:
        import tempfile as tf

        from cagentic import config
        from cagentic.agent import Agent
        from cagentic.gateway import Gateway
        from cagentic.ollama_client import OllamaClient

        with tf.TemporaryDirectory() as cfgdir, tf.TemporaryDirectory() as root:
            os.environ["XDG_CONFIG_HOME"] = cfgdir
            cfg = config.load()
            agent = Agent(
                client=OllamaClient(host="http://localhost:11434"),
                model="test",
                root=Path(root),
                config=cfg,
            )
            gw = Gateway(agent, cfg, port=0)
            self.assertFalse(gw.is_busy())
            gw._turn_lock.acquire()
            try:
                self.assertTrue(gw.is_busy())
            finally:
                gw._turn_lock.release()
            self.assertFalse(gw.is_busy())


if __name__ == "__main__":
    unittest.main()
