"""Bridge between Cagentic and the companion Chrome extension.

Cagentic runs a tiny HTTP server bound to localhost. The Chrome extension
(in the `extension/` directory) long-polls it for commands, runs them in
the browser via Chrome's own APIs, and posts the results back. This lets
the assistant see and act on whatever the user has open — read a page,
open tabs, click links, fill forms.

Wire protocol (JSON, server bound to 127.0.0.1 only):

    GET  /next     extension long-polls; receives {"command": {...}|null}
    POST /result   extension posts {"id", "ok", "result"}
    GET  /ping     health check + connection heartbeat
    GET  /status   live status for the extension popup (model, activity, …)

Security model: even though the server only binds to 127.0.0.1, any web page
the user visits can issue requests to localhost and any other local process can
talk to it. So the bridge is *not* trusted to its bind address alone:

  * A shared secret token is generated on start() and written to
    ~/.config/cagentic/browser_token (0600). The user copies it into the
    extension popup, and the extension sends it (X-Cagentic-Token header, or a
    ?token= query param for GETs) on every request. /next, /result and /status
    require a valid token; mismatches get 403.
  * The Host header must be localhost / 127.0.0.1 (anti DNS-rebinding — stops a
    page on attacker.com that resolves to 127.0.0.1 from driving the bridge).
  * There is no permissive CORS header, so a cross-origin page's fetch can't
    read the responses even if it somehow learned the token.

Every mutating browser action still goes through Cagentic's normal approval
prompt before it's queued.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_log = logging.getLogger(__name__)

# How long /next is held open waiting for a command before returning empty.
_LONGPOLL_SECONDS = 25
# The extension counts as "connected" if it has polled within this window.
_CONNECTED_WINDOW = 45

# Hosts we accept in the Host header (anti DNS-rebinding). Port is stripped
# before comparison.
_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})


def _token_path() -> str:
    """Path to the persisted shared-secret token file."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "cagentic", "browser_token")


def _version() -> str:
    try:
        return __import__("cagentic").__version__
    except Exception:
        return "0.1.0"


class BrowserBridge:
    """Localhost command channel to the Chrome extension."""

    def __init__(self, port: int = 8765) -> None:
        self.port = port
        # Shared secret required on /next, /result and /status. Generated lazily
        # in start(); the user copies it from the token file into the extension.
        self.token: str = ""
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._queue: list[dict] = []         # commands awaiting the extension
        self._results: dict[int, dict] = {}  # command id -> result
        self._next_id = 1
        self._last_poll = 0.0                # monotonic time of last extension poll
        self.error: str | None = None        # set if start() failed
        # Live status surfaced to the extension popup.
        self.model: str | None = None        # the loaded Ollama model
        self.activity: str = "idle"          # what the assistant is doing
        self.activity_at: float = time.time()
        self._recent: list[dict] = []        # last browser actions, newest first

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> bool:
        """Start the localhost HTTP server. Returns False (and sets .error)
        if the port can't be bound — typically another Cagentic is running."""
        if self._server is not None:
            return True
        self.token = self._load_or_create_token()
        try:
            server = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        except OSError as e:
            self.error = f"could not bind 127.0.0.1:{self.port} ({e})"
            return False
        server.bridge = self            # type: ignore[attr-defined]
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except OSError:
                _log.warning("error shutting down browser bridge", exc_info=True)
            self._server = None

    # -- shared-secret token ------------------------------------------------

    def _load_or_create_token(self) -> str:
        """Return the persisted token, generating + persisting one if absent.

        Reuses an existing token (so a restart doesn't force the user to re-paste
        it into the extension) but regenerates if the file is unreadable/empty.
        Written with 0600 perms; the token value is never logged.
        """
        path = _token_path()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                existing = fh.read().strip()
            if existing:
                return existing
        except FileNotFoundError:
            pass
        except OSError:
            _log.warning("could not read browser token file", exc_info=True)

        token = secrets.token_urlsafe(32)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Create with 0600 from the start (umask-safe via O_CREAT|0o600).
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, token.encode("utf-8"))
            finally:
                os.close(fd)
            try:
                os.chmod(path, 0o600)
            except OSError:
                _log.warning("could not chmod browser token file", exc_info=True)
        except OSError:
            # Couldn't persist; still enforce auth this session with an
            # in-memory token (the user just won't be able to read it from disk).
            _log.warning("could not persist browser token file", exc_info=True)
        return token

    def verify_token(self, presented: str | None) -> bool:
        """Constant-time comparison of a presented token against ours."""
        if not self.token or not presented:
            return False
        return hmac.compare_digest(self.token, presented)

    @property
    def running(self) -> bool:
        return self._server is not None

    def is_connected(self) -> bool:
        """True if the extension has polled recently enough to be considered live."""
        return self.running and (time.monotonic() - self._last_poll) < _CONNECTED_WINDOW

    # -- live status (for the extension popup) ------------------------------

    def set_status(self, *, model: str | None = None, activity: str | None = None) -> None:
        """Update what the popup shows — the loaded model and current activity."""
        with self._lock:
            if model is not None:
                self.model = model
            if activity is not None and activity != self.activity:
                self.activity = activity
                self.activity_at = time.time()

    def _record(self, action: str, summary: str, ok: bool) -> None:
        with self._lock:
            self._recent.insert(0, {
                "action": action, "summary": summary, "ok": ok, "ts": time.time(),
            })
            del self._recent[8:]

    def status(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "service": "cagentic-browser-bridge",
                "version": _version(),
                "model": self.model,
                "activity": self.activity,
                "activity_at": self.activity_at,
                "recent": list(self._recent),
            }

    # -- agent side ---------------------------------------------------------

    def send(self, action: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        """Queue a command for the extension and block until its result
        comes back. Returns {"ok": bool, "result"|"error": ...}."""
        params = params or {}
        summary = _command_summary(action, params)
        if self._server is None:
            return {"ok": False, "error": "browser bridge is not running"}
        with self._cv:
            cmd_id = self._next_id
            self._next_id += 1
            self._queue.append({"id": cmd_id, "action": action, "params": params})
            self._cv.notify_all()
            deadline = time.monotonic() + timeout
            while cmd_id not in self._results:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Give up: drop the command so the extension doesn't run it late.
                    self._queue = [c for c in self._queue if c["id"] != cmd_id]
                    self._record(action, summary, False)
                    if not self.is_connected():
                        return {"ok": False, "error": (
                            "the Cagentic Chrome extension isn't connected — "
                            "install or enable it (run /browser for setup steps)"
                        )}
                    return {"ok": False, "error": f"browser command '{action}' timed out"}
                self._cv.wait(remaining)
            result = self._results.pop(cmd_id)
        self._record(action, summary, bool(result.get("ok")))
        return result

    # -- extension side (called by the HTTP handler) ------------------------

    def _take_command(self) -> dict | None:
        with self._cv:
            self._last_poll = time.monotonic()
            deadline = time.monotonic() + _LONGPOLL_SECONDS
            while not self._queue:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)
            return self._queue.pop(0)

    def _deliver_result(self, cmd_id: int, ok: bool, result) -> None:
        with self._cv:
            self._results[cmd_id] = {"ok": ok, "result": result}
            self._cv.notify_all()

    def _heartbeat(self) -> None:
        with self._lock:
            self._last_poll = time.monotonic()


def _command_summary(action: str, params: dict) -> str:
    """A short human label for a browser command, for the recent-actions list."""
    if action in ("open", "navigate"):
        return str(params.get("url", ""))
    if action == "click":
        return str(params.get("selector") or params.get("text") or "")
    if action == "fill":
        return str(params.get("selector", ""))
    if action == "eval":
        code = str(params.get("code", ""))
        return code if len(code) < 44 else code[:41] + "…"
    if action == "read":
        tid = params.get("tab_id")
        return f"tab {tid}" if tid else "active tab"
    if action == "close":
        return f"tab {params.get('tab_id', '')}"
    return ""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Silence the default per-request stderr logging.
    def log_message(self, *args) -> None:  # noqa: D102
        pass

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # Windows often aborts connections when the browser navigates away
            # or cancels a request.  Silently ignore rather than printing a traceback.
            self.close_connection = True

    def _bridge(self) -> BrowserBridge:
        return self.server.bridge  # type: ignore[attr-defined]

    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            # No Access-Control-Allow-Origin: the extension's service worker
            # fetches this as a same-origin/no-CORS-needed request, so we never
            # opt cross-origin web pages into reading these responses.
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # The Chrome extension closed its long-poll mid-response — common
            # on Windows (ConnectionAbortedError / WinError 10053) when the
            # SW restarts, the popup opens/closes, or the tab reloads.
            # Nothing actionable; the next poll will reconnect.
            _log.debug("client closed connection mid-response", exc_info=True)

    def _host_ok(self) -> bool:
        """Reject requests whose Host header isn't localhost (anti DNS-rebinding)."""
        host = (self.headers.get("Host") or "").strip()
        # Strip the port (but keep IPv6 brackets intact, e.g. "[::1]:8765").
        if host.startswith("["):
            hostname = host.split("]")[0] + "]"
        else:
            hostname = host.split(":")[0]
        return hostname.lower() in _ALLOWED_HOSTS

    def _presented_token(self) -> str | None:
        """Token from the X-Cagentic-Token header or a ?token= query param."""
        tok = self.headers.get("X-Cagentic-Token")
        if tok:
            return tok
        _, _, query = self.path.partition("?")
        for part in query.split("&"):
            key, _, val = part.partition("=")
            if key == "token" and val:
                from urllib.parse import unquote
                return unquote(val)
        return None

    def _authed(self) -> bool:
        """Enforce Host check + shared-secret token; emits the failure response."""
        if not self._host_ok():
            self._send_json({"error": "forbidden host"}, status=403)
            return False
        if not self._bridge().verify_token(self._presented_token()):
            # Never log the token value, only the rejection.
            _log.warning("rejected bridge request with missing/invalid token")
            self._send_json({"error": "forbidden"}, status=403)
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/ping":
            # Health check only: no secrets, no activity — left unauthenticated
            # so the extension/CLI can detect the bridge before pairing.
            if not self._host_ok():
                self._send_json({"error": "forbidden host"}, status=403)
                return
            self._bridge()._heartbeat()
            self._send_json({"ok": True, "service": "cagentic-browser-bridge"})
            return
        if path in ("/next", "/status"):
            if not self._authed():
                return
            if path == "/next":
                cmd = self._bridge()._take_command()
                self._send_json({"command": cmd})
            else:  # /status — gated so we don't leak model/activity/history
                self._bridge()._heartbeat()
                self._send_json(self._bridge().status())
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/result":
            self._send_json({"error": "not found"}, status=404)
            return
        if not self._authed():
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": "bad json"}, status=400)
            return
        self._bridge()._deliver_result(
            int(data.get("id", 0)),
            bool(data.get("ok", True)),
            data.get("result"),
        )
        self._send_json({"ok": True})
