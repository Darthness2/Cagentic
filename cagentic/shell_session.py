"""A persistent shell per workspace, so `cd` and exported vars survive.

`run_bash` runs each command in a fresh `subprocess`, which means this does not
work:

    run_bash("cd backend")
    run_bash("npm test")        # still in the workspace root

The model has to remember to prefix every command with the directory, and it
routinely doesn't — it "cd"s once and then acts surprised. A long-lived shell
fixes that: state accumulates the way it does in a real terminal.

Mechanics. The shell is started once (inside the sandbox — see `sandbox.wrap`),
and commands are written to its stdin followed by a sentinel that echoes the
exit status. Output is read until the sentinel appears, which is what delimits
one command's output from the next.

Two things this has to get right or it is worse than no session at all:

  * **Deadlock.** A pipe fills and the shell blocks writing while we block
    reading something else. Stderr is merged into stdout (one stream, one
    reader) and a dedicated thread drains it into a queue, so the shell is
    never blocked on us.
  * **Poisoning.** If a command times out, its output is still arriving and
    would be attributed to the *next* command. A timed-out session is killed
    rather than reused, and the caller falls back to a one-shot run.

POSIX only. Windows shells differ enough (cmd.exe quoting, the WSL launcher
problem documented in `tools._shell_run_invocation`) that a half-working
session would be worse than the honest one-shot fallback.
"""

from __future__ import annotations

import logging
import os
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

_log = logging.getLogger(__name__)

# Marks the end of one command's output. Randomised per session so output that
# happens to contain the literal text can't fake a command boundary.
_SENTINEL_PREFIX = "__cagentic_done_"

# A session that has gone unused this long is closed; an idle shell holding a
# sandbox and a pipe pair forever is just a leak.
IDLE_TIMEOUT = 900.0
# Hard cap on live shells. Each is a real process; a long session that keeps
# switching workspace (the gateway can) would otherwise accumulate them
# without bound. Least-recently-used is evicted.
MAX_SESSIONS = 4


class ShellSession:
    """One long-lived shell. Not thread-safe; callers hold `lock`."""

    def __init__(self, workspace: Path, argv: list[str]) -> None:
        self.workspace = Path(workspace)
        self.argv = list(argv)
        self.nonce = f"{_SENTINEL_PREFIX}{secrets.token_hex(8)}"
        self.lock = threading.Lock()
        self.last_used = time.time()
        self._proc: subprocess.Popen | None = None
        self._out: queue.Queue[str | None] = queue.Queue()
        self._reader: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> bool:
        if self.alive:
            return True
        try:
            self._proc = subprocess.Popen(
                self.argv,
                cwd=str(self.workspace),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # One stream: two pipes with one reader is how these deadlock.
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                # Own process group, so a timeout can kill the whole job tree
                # rather than just the shell that spawned it.
                start_new_session=True,
            )
        except OSError:
            _log.warning("could not start a shell session", exc_info=True)
            self._proc = None
            return False
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()
        return True

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    # -- running ------------------------------------------------------------

    def _drain(self) -> None:
        """Pump the shell's output into the queue so it can never block on us."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self._out.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._out.put(None)  # EOF marker

    def run(self, command: str, timeout: float) -> tuple[int, str, str]:
        """Run `command`. Returns (exit_code, output, error).

        `error` is non-empty when the *session* failed (died, timed out) — as
        opposed to the command merely exiting non-zero, which is normal and
        reported through exit_code.
        """
        if not self.alive or self._proc is None or self._proc.stdin is None:
            return -1, "", "shell session is not running"

        self.last_used = time.time()
        # Drop anything left over from a previous command before issuing the
        # next one, so stale bytes can't be misread as this command's output.
        while True:
            try:
                self._out.get_nowait()
            except queue.Empty:
                break

        marker = self.nonce
        try:
            # `$?` is captured immediately, before printf can overwrite it.
            self._proc.stdin.write(
                f"{command}\n__cag_rc=$?\nprintf '%s%s\\n' '{marker}' \"$__cag_rc\"\n"
            )
            self._proc.stdin.flush()
        except (OSError, ValueError):
            self.close()
            return -1, "", "shell session closed unexpectedly"

        lines: list[str] = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # The command is still running and still producing output that
                # would contaminate the next one. Kill the session; the caller
                # falls back to a one-shot run.
                self.close()
                return -1, "".join(lines), f"timed out after {timeout:.0f}s"
            try:
                line = self._out.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if line is None:
                self.close()
                return -1, "".join(lines), "the shell exited"
            if line.startswith(marker):
                code_text = line[len(marker) :].strip()
                try:
                    return int(code_text), "".join(lines), ""
                except ValueError:
                    return 0, "".join(lines), ""
            lines.append(line)


class SessionPool:
    """One shell per (workspace, sandbox settings). Created on demand."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, ShellSession] = {}

    @staticmethod
    def supported() -> bool:
        # See the module docstring: Windows shells differ enough that a
        # half-working session is worse than the one-shot fallback.
        return os.name != "nt" and sys.platform != "win32"

    def get(self, key: str, workspace: Path, argv: list[str]) -> ShellSession | None:
        if not self.supported():
            return None
        with self._lock:
            self._reap_locked()
            session = self._sessions.get(key)
            if session is not None and session.alive:
                return session
            session = ShellSession(workspace, argv)
            if not session.start():
                return None
            self._sessions[key] = session
            self._evict_locked()
            return session

    def _evict_locked(self) -> None:
        while len(self._sessions) > MAX_SESSIONS:
            oldest = min(self._sessions.items(), key=lambda kv: kv[1].last_used)[0]
            self._sessions.pop(oldest).close()

    def _reap_locked(self) -> None:
        now = time.time()
        for key, session in list(self._sessions.items()):
            if not session.alive or (now - session.last_used) > IDLE_TIMEOUT:
                session.close()
                self._sessions.pop(key, None)

    def close_all(self) -> None:
        with self._lock:
            for session in self._sessions.values():
                session.close()
            self._sessions.clear()


# One pool per process. The gateway and the REPL each get their own because
# they are separate processes; within one process a workspace has one shell.
POOL = SessionPool()


# Shells are real child processes; leaving them behind on exit would strand a
# sandbox-exec per workspace the session ever touched.
import atexit  # noqa: E402

atexit.register(POOL.close_all)
