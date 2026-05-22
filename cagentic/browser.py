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

Nothing is reachable beyond localhost, and every mutating browser action
still goes through Cagentic's normal approval prompt before it's queued.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# How long /next is held open waiting for a command before returning empty.
_LONGPOLL_SECONDS = 25
# The extension counts as "connected" if it has polled within this window.
_CONNECTED_WINDOW = 45


def _version() -> str:
    try:
        return __import__("cagentic").__version__
    except Exception:
        return "0.1.0"


class BrowserBridge:
    """Localhost command channel to the Chrome extension."""

    def __init__(self, port: int = 8765) -> None:
        self.port = port
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
            except Exception:
                pass
            self._server = None

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

    def _bridge(self) -> BrowserBridge:
        return self.server.bridge  # type: ignore[attr-defined]

    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/next":
            cmd = self._bridge()._take_command()
            self._send_json({"command": cmd})
        elif path == "/ping":
            self._bridge()._heartbeat()
            self._send_json({"ok": True, "service": "cagentic-browser-bridge"})
        elif path == "/status":
            self._bridge()._heartbeat()
            self._send_json(self._bridge().status())
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/result":
            self._send_json({"error": "not found"}, status=404)
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
