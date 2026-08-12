"""Restart the background gateway when its own code changes.

The gateway installed by `--install-service` runs `python -m cagentic --serve`
from wherever the package lives. With an editable install that is the working
tree, so editing the source leaves a long-lived daemon serving whatever it
imported at boot — you change a file, reload the browser, and nothing is
different until you remember to `launchctl kickstart` it.

This watcher polls the package directory and re-executes the process once the
tree settles. `os.execv` rather than "exit and let the supervisor restart us":

  * launchd's ThrottleInterval means an exit costs a 30-second outage,
  * systemd's `Restart=on-failure` would not restart a *clean* exit at all,
  * exec keeps the PID, so the service manager never sees a blip.

Guardrails matter more than speed here, because the thing being restarted is
holding the user's conversation:

  * settle before acting — a `git pull` or an editor's save-all touches many
    files, and restarting on the first one races the rest onto disk,
  * never restart mid-turn — an in-flight reply would be dropped,
  * never restart into code that doesn't compile — that is a crash loop, and
    the supervisor would keep feeding it.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

_log = logging.getLogger(__name__)

# Extensions worth watching: Python, plus the gateway's UI assets, which are
# read once at import (`_asset_text`) and so are just as stale as the code.
WATCH_SUFFIXES = frozenset({".py", ".html", ".css", ".js"})
# Directories that never contain source we load.
SKIP_DIRS = frozenset({"__pycache__", ".git", ".mypy_cache", ".ruff_cache", ".pytest_cache"})

# Seconds the tree must be unchanged before we act. A save-all or a pull
# writes many files over a second or two; acting on the first one restarts
# into a half-written tree.
SETTLE_SECONDS = 2.0
# How often to stat the tree. Cheap (a few hundred stat calls) but not free.
POLL_SECONDS = 1.0
# Refuse to restart more often than this, so a pathological editor that
# rewrites a file every second can't turn into a restart loop.
MIN_RESTART_INTERVAL = 10.0
# How long to keep waiting for an in-flight turn before giving up on this
# round. The change is still on disk, so the next poll picks it up again.
BUSY_GRACE_SECONDS = 120.0


def package_root() -> Path:
    """The directory whose contents this process actually imported."""
    return Path(__file__).resolve().parent


def snapshot(root: Path) -> dict[str, float]:
    """Map watched file → mtime. Missing/unreadable files are simply absent,
    which makes a deletion show up as a change."""
    out: dict[str, float] = {}
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if os.path.splitext(name)[1] not in WATCH_SUFFIXES:
                    continue
                full = os.path.join(dirpath, name)
                try:
                    out[full] = os.stat(full).st_mtime
                except OSError:
                    continue
    except OSError:
        _log.warning("autoreload: could not walk %s", root, exc_info=True)
    return out


def compiles(paths: list[str]) -> tuple[bool, str]:
    """Do the changed Python files parse? Returns (ok, first error).

    Restarting into a SyntaxError gives the supervisor a process that dies on
    import, forever. Checking first turns that into a logged warning and a
    gateway that keeps serving the last good code.

    A path that no longer exists is a *deletion*, not a failure — blocking on
    those would mean a removed module wedged the reloader permanently. A
    half-written file still on disk fails the parse and is caught below.
    """
    for path in paths:
        if not path.endswith(".py") or not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as handle:
                source = handle.read()
        except OSError:
            # On disk but unreadable — mid-write. Not ready yet.
            return False, f"{path}: could not be read"
        try:
            compile(source, path, "exec")
        except SyntaxError as exc:
            return False, f"{path}:{exc.lineno}: {exc.msg}"
        except ValueError as exc:  # e.g. embedded null bytes
            return False, f"{path}: {exc}"
    return True, ""


def restart_argv() -> list[str]:
    """Rebuild the command that started this process.

    `python -m cagentic --serve` and the `cagentic` console script need
    different argv shapes, and getting it wrong means the restart silently
    becomes a different program. `__main__.__spec__` is set only for `-m`,
    which is exactly the distinction.
    """
    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    parent = getattr(spec, "parent", None) if spec is not None else None
    if isinstance(parent, str) and parent:
        return [sys.executable, "-m", parent, *sys.argv[1:]]
    return [sys.executable, *sys.argv]


class GatewayReloader:
    """Polls the package tree and re-executes the process when it changes.

    `is_busy` and `shutdown` are injected rather than importing Gateway, so
    this module stays testable without standing up a server.
    """

    def __init__(
        self,
        *,
        is_busy=None,
        shutdown=None,
        root: Path | None = None,
        poll_seconds: float = POLL_SECONDS,
        settle_seconds: float = SETTLE_SECONDS,
        on_restart=None,
    ) -> None:
        self.root = root or package_root()
        self._is_busy = is_busy or (lambda: False)
        self._shutdown = shutdown or (lambda: None)
        self.poll_seconds = poll_seconds
        self.settle_seconds = settle_seconds
        # Seam for tests: swap in something that records instead of execing.
        self._on_restart = on_restart or self._exec_self
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._baseline = snapshot(self.root)
        self._started_at = time.monotonic()
        self._changed_at: float | None = None
        # Snapshot the settle window is currently armed against; the window
        # restarts whenever the tree differs from it.
        self._pending: dict[str, float] | None = None
        self._busy_since: float | None = None
        # Last compile failure reported, so a file that stays broken
        # doesn't log the same line on every poll.
        self._last_problem: str | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="cagentic-autoreload", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- the check ----------------------------------------------------------

    @staticmethod
    def _diff(baseline: dict[str, float], current: dict[str, float]) -> list[str]:
        """Files added, removed or touched between two snapshots."""
        changed = [path for path, mtime in current.items() if baseline.get(path) != mtime]
        changed.extend(path for path in baseline if path not in current)
        return sorted(changed)

    def changed_files(self) -> list[str]:
        """Files changed since the last accepted snapshot."""
        self._latest = snapshot(self.root)
        return self._diff(self._baseline, self._latest)

    def poll_once(self, now: float | None = None) -> bool:
        """One tick. Returns True if a restart was triggered."""
        now = time.monotonic() if now is None else now
        current = snapshot(self.root)
        self._latest = current
        changed = self._diff(self._baseline, current)

        if not changed:
            self._changed_at = None
            self._pending = None
            return False

        # Wait for the tree to stop *moving*, not merely for time to pass since
        # the first change. A `git pull` or a save-all writes many files over a
        # second or two, so the window has to re-arm whenever anything moves
        # again — otherwise the restart lands in the middle of the write and
        # the process comes back on a half-updated tree.
        armed_at = self._changed_at
        if self._pending is None or armed_at is None or self._diff(self._pending, current):
            self._pending = current
            self._changed_at = now
            _log.info("autoreload: %d file(s) changed; waiting to settle", len(changed))
            return False
        if now - armed_at < self.settle_seconds:
            return False

        ok, problem = compiles(changed)
        if not ok:
            # Don't consume the change: once the file is fixed its mtime moves
            # again, which re-arms the window above and we re-evaluate.
            #
            # Log the problem once, not once per poll. A file stays broken for
            # as long as you are mid-edit, and a warning every second turns one
            # syntax error into thousands of identical log lines.
            if problem != self._last_problem:
                _log.warning("autoreload: not restarting, code does not compile — %s", problem)
                self._last_problem = problem
            return False
        self._last_problem = None

        if now - self._started_at < MIN_RESTART_INTERVAL:
            return False

        if self._is_busy():
            if self._busy_since is None:
                self._busy_since = now
                _log.info("autoreload: change detected but a turn is in flight; waiting")
            elif now - self._busy_since > BUSY_GRACE_SECONDS:
                # Something is wedged. Leave the change pending rather than
                # killing a request; the next poll tries again.
                _log.warning("autoreload: still busy after %.0fs; deferring", BUSY_GRACE_SECONDS)
                self._busy_since = None
            return False
        self._busy_since = None

        # WARNING, not INFO: the default log level is WARNING, and "the
        # background service restarted itself" is precisely the event you
        # need in the log when something looks off later.
        _log.warning(
            "autoreload: restarting for %d changed file(s): %s",
            len(changed),
            ", ".join(os.path.basename(c) for c in changed[:5]),
        )
        self._baseline = current
        self._pending = None
        self._changed_at = None
        self._on_restart(changed)
        return True

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                if self.poll_once():
                    return
            except Exception:
                # A watcher that crashes must not take the gateway with it.
                _log.warning("autoreload: poll failed", exc_info=True)

    # -- the restart --------------------------------------------------------

    def _exec_self(self, changed: list[str]) -> None:
        argv = restart_argv()
        try:
            # Release the listening socket before handing the port to our
            # replacement. Python sockets are close-on-exec (PEP 446), so this
            # is belt-and-braces — but the braces matter when the bind fails.
            self._shutdown()
        except Exception:
            _log.warning("autoreload: shutdown before restart failed", exc_info=True)
        # Flush our own streams: exec does not run atexit handlers, and the
        # service's log file would otherwise lose the last lines.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        logging.shutdown()
        try:
            os.execv(argv[0], argv)
        except OSError:
            # exec failed, so we are still the old process with a stopped
            # server. Exiting non-zero lets launchd/systemd restart us, which
            # is the whole point of running under a supervisor.
            _log.warning("autoreload: exec failed; exiting for the supervisor", exc_info=True)
            os._exit(3)
