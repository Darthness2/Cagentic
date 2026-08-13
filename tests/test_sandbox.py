"""Shell sandbox regressions.

`run_bash` was a bare subprocess.run: approving one shell command approved
arbitrary access to the machine, with no confinement and full network. These
tests cover the profile construction (pure, runs everywhere) and — on a Mac
with sandbox-exec — that the confinement is real rather than nominal.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cagentic import sandbox
from cagentic.tools import ToolContext, shell_sandbox_settings, t_run_bash


class _Engine:
    """Minimal stand-in — the tool only reads .config off the engine."""

    def __init__(self, config: dict) -> None:
        self.config = config


class TestProfile(unittest.TestCase):
    def test_workspace_is_writable_and_writes_are_denied_by_default(self) -> None:
        profile = sandbox.seatbelt_profile(Path("/tmp/proj"), network=False)
        self.assertIn("(deny file-write*)", profile)
        self.assertIn("(deny network*)", profile)
        # The resolved path is what Seatbelt matches on.
        self.assertIn(str(Path("/tmp/proj").resolve()), profile)

    def test_network_can_be_granted(self) -> None:
        profile = sandbox.seatbelt_profile(Path("/tmp/proj"), network=True)
        self.assertNotIn("(deny network*)", profile)

    def test_quotes_in_a_path_cannot_break_out_of_the_profile(self) -> None:
        profile = sandbox.seatbelt_profile(Path('/tmp/we"ird'), network=False)
        # The quote must be escaped, not left to terminate the string literal.
        self.assertNotIn('"/tmp/we"ird"', profile)
        self.assertIn('\\"', profile)


class TestWrap(unittest.TestCase):
    def test_disabled_passes_the_invocation_through_untouched(self) -> None:
        cmd, shell, note = sandbox.wrap(
            "echo hi", True, workspace=Path("/tmp"), network=False, enabled=False
        )
        self.assertEqual((cmd, shell), ("echo hi", True))
        self.assertIn("off", note)

    def test_missing_backend_is_reported_not_hidden(self) -> None:
        """A sandbox that silently isn't there is worse than none at all."""
        original = sandbox.backend
        sandbox.backend = lambda: "none"
        try:
            cmd, shell, note = sandbox.wrap("echo hi", True, workspace=Path("/tmp"), network=False)
            self.assertEqual((cmd, shell), ("echo hi", True))
            self.assertTrue(note.startswith("NOT sandboxed"), note)
        finally:
            sandbox.backend = original

    def test_shell_string_becomes_an_argv_list_under_the_sandbox(self) -> None:
        original = sandbox.backend
        sandbox.backend = lambda: "seatbelt"
        try:
            cmd, shell, note = sandbox.wrap("echo hi", True, workspace=Path("/tmp"), network=False)
            self.assertFalse(shell, "the sandbox binary must exec the shell, not the reverse")
            self.assertEqual(cmd[0], "sandbox-exec")
            self.assertEqual(cmd[-3:], ["/bin/sh", "-c", "echo hi"])
            self.assertIn("no network", note)
        finally:
            sandbox.backend = original

    def test_bwrap_confines_writes_to_the_workspace(self) -> None:
        original = sandbox.backend
        sandbox.backend = lambda: "bwrap"
        try:
            cmd, shell, _note = sandbox.wrap("ls", True, workspace=Path("/tmp"), network=False)
            self.assertEqual(cmd[0], "bwrap")
            self.assertIn("--ro-bind", cmd)
            self.assertIn("--unshare-net", cmd)
            self.assertFalse(shell)
        finally:
            sandbox.backend = original


class TestSettings(unittest.TestCase):
    def _ctx(self, config: dict) -> ToolContext:
        return ToolContext(root=Path("/tmp"), engine=_Engine(config))

    def test_defaults_are_confined_and_offline(self) -> None:
        enabled, network = shell_sandbox_settings(self._ctx({}), {})
        self.assertTrue(enabled, "the sandbox must be on by default")
        self.assertFalse(network, "network must be denied by default")

    def test_per_call_network_opt_in(self) -> None:
        _enabled, network = shell_sandbox_settings(self._ctx({}), {"network": True})
        self.assertTrue(network)

    def test_config_can_disable_the_sandbox(self) -> None:
        enabled, _network = shell_sandbox_settings(self._ctx({"shell": {"sandbox": "off"}}), {})
        self.assertFalse(enabled)

    def test_config_can_allow_network_globally(self) -> None:
        _enabled, network = shell_sandbox_settings(self._ctx({"shell": {"network": "allow"}}), {})
        self.assertTrue(network)

    def test_malformed_shell_config_falls_back_to_the_safe_defaults(self) -> None:
        enabled, network = shell_sandbox_settings(self._ctx({"shell": "nonsense"}), {})
        self.assertTrue(enabled)
        self.assertFalse(network)


class TestDenialHeuristic(unittest.TestCase):
    def test_recognises_common_offline_failures(self) -> None:
        self.assertTrue(sandbox.looks_like_network_denial("curl: (6) Could not resolve host"))
        self.assertTrue(sandbox.looks_like_network_denial("Network is unreachable"))

    def test_does_not_fire_on_ordinary_failures(self) -> None:
        self.assertFalse(sandbox.looks_like_network_denial("SyntaxError: invalid syntax"))
        self.assertFalse(sandbox.looks_like_network_denial("test failed: 3 assertions"))


@unittest.skipUnless(
    sys.platform == "darwin" and shutil.which("sandbox-exec"),
    "needs macOS sandbox-exec",
)
class TestRealSeatbelt(unittest.TestCase):
    """The confinement has to be real, not just present in the argv."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ctx = ToolContext(root=self.root, engine=_Engine({}))

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def _run(self, command: str, **extra) -> str:
        # session=False pins these to the one-shot path they were written for.
        # The persistent-session path is confined by the same profile and is
        # covered separately in TestSessionIsSandboxedToo.
        extra.setdefault("session", False)
        return t_run_bash({"command": command, "timeout": 30, **extra}, self.ctx)

    def test_a_normal_command_still_works(self) -> None:
        out = self._run("echo hello-from-sandbox")
        self.assertIn("hello-from-sandbox", out)
        self.assertIn("PASS", out)

    def test_writes_inside_the_workspace_succeed(self) -> None:
        out = self._run("echo written > inside.txt && cat inside.txt")
        self.assertIn("PASS", out)
        self.assertTrue((self.root / "inside.txt").exists())

    def test_writes_outside_the_workspace_are_blocked(self) -> None:
        target = Path.home() / ".cagentic-sandbox-escape-probe"
        try:
            out = self._run(f"echo escaped > {target}")
            self.assertIn("FAIL", out, "the sandbox let a write escape the workspace")
            self.assertFalse(target.exists(), "file was created outside the workspace")
        finally:
            if target.exists():  # pragma: no cover - only on a real escape
                target.unlink()

    def test_reads_outside_the_workspace_still_work(self) -> None:
        """Read access is deliberately broad — the agent has to see the system."""
        out = self._run("cat /etc/hosts")
        self.assertIn("PASS", out)

    def test_temp_dirs_are_writable_on_purpose(self) -> None:
        """Documents the one hole in "workspace only": pip/npm/cargo/pytest all
        write scratch outside the project, and a sandbox that breaks every build
        is one the user switches off. If this ever tightens, the notes in
        sandbox.wrap() and the module docstring have to change with it."""
        probe = Path("/tmp/.cagentic-sandbox-tmp-probe")
        try:
            out = self._run(f"echo scratch > {probe}")
            self.assertIn("PASS", out)
        finally:
            if probe.exists():
                probe.unlink()


@unittest.skipUnless(
    sys.platform == "darwin" and shutil.which("sandbox-exec"),
    "needs macOS sandbox-exec",
)
class TestSessionIsSandboxedToo(unittest.TestCase):
    """The persistent shell is launched *inside* the sandbox, so it must be as
    confined as a one-shot run — a session that escaped would be a silent hole
    in the guarantee run_bash advertises."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ctx = ToolContext(root=self.root, engine=_Engine({}))

    def tearDown(self) -> None:
        from cagentic.shell_session import POOL

        POOL.close_all()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def _run(self, command: str) -> str:
        return t_run_bash({"command": command, "timeout": 30}, self.ctx)

    def test_state_persists_between_calls(self) -> None:
        (self.root / "sub").mkdir()
        self._run("cd sub")
        self.assertIn("/sub", self._run("pwd"))
        self._run("export CAG_TEST=marker")
        self.assertIn("marker", self._run('echo "$CAG_TEST"'))

    def test_writes_outside_the_workspace_are_still_blocked(self) -> None:
        target = Path.home() / ".cagentic-session-escape-probe"
        try:
            out = self._run(f"echo escaped > {target}")
            self.assertIn("FAIL", out, "the session escaped the sandbox")
            self.assertFalse(target.exists())
        finally:
            if target.exists():  # pragma: no cover - only on a real escape
                target.unlink()

    def test_a_failing_command_still_reports_its_exit_code(self) -> None:
        out = self._run("false")
        self.assertIn("exit code 1", out)
        self.assertIn("FAIL", out)

    def test_a_command_that_kills_the_shell_falls_back_instead_of_erroring(self) -> None:
        """`exit` ends the session by definition. The user should still get
        their result, and the next command should work on a fresh shell."""
        out = self._run("exit 3")
        self.assertIn("exit code 3", out)
        self.assertIn("hello", self._run("echo hello"))

    def test_session_false_bypasses_the_session(self) -> None:
        (self.root / "sub").mkdir()
        t_run_bash({"command": "cd sub", "timeout": 30}, self.ctx)
        out = t_run_bash({"command": "pwd", "timeout": 30, "session": False}, self.ctx)
        self.assertNotIn("/sub", out, "session=False must start from the workspace root")


@unittest.skipUnless(
    sys.platform == "darwin" and shutil.which("sandbox-exec"),
    "needs macOS sandbox-exec",
)
class TestRealNetworkDenial(unittest.TestCase):
    def test_network_is_denied_by_default(self) -> None:
        profile = sandbox.seatbelt_profile(Path("/tmp"), network=False)
        proc = subprocess.run(
            [
                "sandbox-exec",
                "-p",
                profile,
                "/bin/sh",
                "-c",
                # A raw socket connect, so the result doesn't depend on curl
                # or on DNS being reachable in the first place.
                "python3 -c \"import socket;socket.create_connection(('1.1.1.1',53),3)\"",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(proc.returncode, 0, "the sandbox allowed an outbound connection")


if __name__ == "__main__":
    unittest.main()
