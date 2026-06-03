"""/gateway — a local web app for Cagentic.

Starts an HTTP server (default port 8700) that serves a polished chat UI
and runs the full agent behind it: the same tools, notes, reminders, MCP
servers, browser control — everything the terminal REPL can do.

The app has a sidebar of saved chats, a settings panel, and streams each
turn token-by-token. Tools that need approval surface an Approve / Deny
prompt right in the page. Bound to localhost only.
"""
from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as _config
from . import sessions


class _ClientGone(Exception):
    """Raised when the browser hangs up mid-stream."""


_THINK_RX = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_PLAN_RX = re.compile(r"<plan>.*?</plan>", re.DOTALL | re.IGNORECASE)
_STEP_RX = re.compile(r"<step\s+\d+(?:\s*/\s*\d+)?\s*>", re.IGNORECASE)


def _clean(text: str) -> str:
    text = _THINK_RX.sub("", text)
    text = _PLAN_RX.sub("", text)
    text = _STEP_RX.sub("", text)
    return text.strip()


class Gateway:
    def __init__(self, agent, config: dict, port: int = 8700) -> None:
        self.agent = agent
        self.config = config
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.error: str | None = None

        self._turn_lock = threading.Lock()
        self._active_emit = None

        self._perm_cv = threading.Condition()
        self._perm_answer: str | None = None

        # The gateway's own engine — a separate conversation, but the SAME
        # shared state (notes, reminders, browser bridge, MCP, workspace).
        from .engine import QueryEngine
        self.engine = QueryEngine(
            client=agent.client,
            state=agent.state,
            model=agent.model,
            temperature=agent.engine.temperature,
            config=config,
            permission_resolver=self._resolve,
            stream=True,
        )
        # The current chat is a session record (shared store with the REPL).
        self.session = sessions.make(agent.model)
        self.engine.session_id = self.session["id"]

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> bool:
        if self._server is not None:
            return True
        try:
            server = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        except OSError as e:
            self.error = f"could not bind 127.0.0.1:{self.port} ({e})"
            return False
        server.gateway = self            # type: ignore[attr-defined]
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

    def url(self) -> str:
        return f"http://localhost:{self.port}"

    # -- permission bridge --------------------------------------------------

    def _resolve(self, name: str, args: dict, state) -> str:
        emit = self._active_emit
        if emit is None:
            return "no"
        from .engine import _summarize_args
        with self._perm_cv:
            self._perm_answer = None
        emit("permission", {"tool": name, "summary": _summarize_args(name, args)})
        with self._perm_cv:
            deadline = time.monotonic() + 300
            while self._perm_answer is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "no"
                self._perm_cv.wait(remaining)
            return self._perm_answer

    def deliver_permission(self, answer: str) -> None:
        with self._perm_cv:
            self._perm_answer = answer if answer in ("yes", "no", "always", "never") else "no"
            self._perm_cv.notify_all()

    # -- chats --------------------------------------------------------------

    def _save_current(self) -> None:
        msgs = [m for m in self.engine.messages if m.get("role") != "system"]
        if not msgs:
            return  # don't persist empty chats
        self.session["model"] = self.agent.model
        self.session["messages"] = msgs
        sessions.save(self.session)

    def list_chats(self) -> list[dict]:
        self._save_current()
        out = []
        for s in sessions.list_all():
            out.append({
                "id": s["id"],
                "title": s["title"] if s["title"] not in (None, "", "untitled") else "New chat",
                "updated_at": s["updated_at"],
                "turns": s["turns"],
            })
        return out

    def render_messages(self, messages: list[dict]) -> list[dict]:
        """Turn stored messages into display items for the web UI."""
        out: list[dict] = []
        for m in messages:
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if role == "user":
                if content.startswith((
                    "Tool result for ", "[background] ", "STOP. ", "You called ",
                )):
                    continue
                if content:
                    out.append({"role": "user", "content": content})
            elif role == "assistant":
                tools = [
                    (tc.get("function") or {}).get("name", "?")
                    for tc in (m.get("tool_calls") or [])
                ]
                cleaned = _clean(content)
                if cleaned or tools:
                    out.append({"role": "assistant", "content": cleaned, "tools": tools})
        return out

    def current_chat(self) -> dict:
        return {
            "id": self.session["id"],
            "title": self.session.get("title") or "New chat",
            "messages": self.render_messages(
                [m for m in self.engine.messages if m.get("role") != "system"]
            ),
        }

    def new_chat(self) -> dict:
        self._save_current()
        self.session = sessions.make(self.agent.model)
        self.engine.reset()
        self.engine.session_id = self.session["id"]
        return self.current_chat()

    def load_chat(self, chat_id: str) -> dict:
        self._save_current()
        data = sessions.load(chat_id)
        if not data:
            return {"error": f"chat {chat_id} not found"}
        self.session = data
        self.agent.model = data.get("model") or self.agent.model
        self.engine.model = self.agent.model
        self.engine.load_messages(data.get("messages", []))
        self.engine.session_id = self.session["id"]
        return self.current_chat()

    def delete_chat(self, chat_id: str) -> dict:
        sessions.delete(chat_id)
        if chat_id == self.session.get("id"):
            self.new_chat()
        return {"chats": self.list_chats(), "current": self.current_chat()}

    def rename_chat(self, chat_id: str, title: str) -> dict:
        title = (title or "").strip()
        if chat_id == self.session.get("id"):
            self.session["title"] = title or "New chat"
            self._save_current()
        else:
            data = sessions.load(chat_id)
            if data:
                data["title"] = title or "New chat"
                sessions.save(data)
        return {"chats": self.list_chats()}

    # -- settings -----------------------------------------------------------

    def get_settings(self) -> dict:
        try:
            models = self.agent.client.list_models()
        except Exception:
            models = []
        return {
            "model": self.agent.model,
            "models": models,
            "temperature": self.engine.temperature,
            "user_name": self.agent.state.user_name or "",
            "stream": self.engine.stream,
            "yolo": self.agent.state.yolo,
        }

    def update_settings(self, data: dict) -> dict:
        cfg = self.config
        if data.get("model"):
            self.agent.model = data["model"]
            self.engine.model = data["model"]
            cfg["model"] = data["model"]
        if "temperature" in data:
            try:
                t = max(0.0, min(2.0, float(data["temperature"])))
                self.engine.temperature = t
                cfg["temperature"] = t
            except (TypeError, ValueError):
                pass
        if "user_name" in data:
            name = (data.get("user_name") or "").strip() or None
            self.agent.state.update(user_name=name)
            self.engine.refresh_system_prompt()
            cfg["user_name"] = name
        if "stream" in data:
            self.engine.stream = bool(data["stream"])
            _config.set_value(cfg, "ollama.stream", self.engine.stream)
        if "yolo" in data:
            self.agent.state.update(yolo=bool(data["yolo"]))
            cfg["yolo"] = bool(data["yolo"])
        try:
            _config.save(cfg)
        except Exception:
            pass
        return self.get_settings()

    def bootstrap(self) -> dict:
        return {
            "version": __import__("cagentic").__version__,
            "user_name": self.agent.state.user_name,
            "model": self.agent.model,
            "chats": self.list_chats(),
            "current": self.current_chat(),
            "settings": self.get_settings(),
        }

    # -- a chat turn --------------------------------------------------------

    def run_turn(self, message: str, emit) -> None:
        if not self._turn_lock.acquire(blocking=False):
            emit("error", {"text": "Cagentic is still working on the previous message."})
            return
        self._active_emit = emit
        self.engine.model = self.agent.model
        try:
            for ev in self.engine.submit_message(message):
                emit(ev.kind, ev.data)
        except _ClientGone:
            raise
        except Exception as e:
            emit("error", {"text": f"{type(e).__name__}: {e}"})
        finally:
            self._active_emit = None
            try:
                self._save_current()
            except Exception:
                pass
            self._turn_lock.release()


# ---------------------------------------------------------------- handler ---

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def _gw(self) -> Gateway:
        return self.server.gateway  # type: ignore[attr-defined]

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # Client closed mid-response. Includes Windows' ConnectionAbortedError.
            pass

    def _json(self, obj, status: int = 200) -> None:
        self._send(json.dumps(obj).encode("utf-8"), "application/json", status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/":
            self._send(_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/app.css":
            self._send(_CSS.encode("utf-8"), "text/css; charset=utf-8")
        elif path == "/app.js":
            self._send(_JS.encode("utf-8"), "application/javascript; charset=utf-8")
        elif path == "/api/bootstrap":
            self._json(self._gw().bootstrap())
        elif path == "/api/settings":
            self._json(self._gw().get_settings())
        else:
            self._send(b"not found", "text/plain", status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        gw = self._gw()
        if path == "/api/chat":
            self._stream_chat(str(self._body().get("message", "")).strip())
            return
        if path == "/api/permission":
            gw.deliver_permission(str(self._body().get("answer", "no")))
            self._json({"ok": True})
            return
        if path == "/api/chats/new":
            self._json({"current": gw.new_chat(), "chats": gw.list_chats()})
            return
        if path == "/api/chats/load":
            cur = gw.load_chat(str(self._body().get("id", "")))
            self._json({"current": cur, "chats": gw.list_chats()})
            return
        if path == "/api/chats/delete":
            self._json(gw.delete_chat(str(self._body().get("id", ""))))
            return
        if path == "/api/chats/rename":
            b = self._body()
            self._json(gw.rename_chat(str(b.get("id", "")), str(b.get("title", ""))))
            return
        if path == "/api/settings":
            self._json(gw.update_settings(self._body()))
            return
        self._send(b"not found", "text/plain", status=404)

    def _stream_chat(self, message: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(kind: str, data: dict) -> None:
            payload = json.dumps({"kind": kind, "data": data})
            try:
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
            except OSError:
                # Any socket-write failure (BrokenPipe, ConnectionReset,
                # ConnectionAborted on Windows, etc.) means the client hung up.
                raise _ClientGone()

        if not message:
            try:
                emit("error", {"text": "empty message"})
            except _ClientGone:
                pass
            return
        try:
            self._gw().run_turn(message, emit)
            emit("end", {})
        except _ClientGone:
            return


# ---------------------------------------------------------------- the page --
# J.A.R.V.I.S. — Just A Rather Very Intelligent System
# Full holographic HUD interface for Cagentic.

_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>J.A.R.V.I.S.</title>
<link rel="stylesheet" href="/app.css" />
</head>
<body>
<div id="app">
  <div class="scanlines"></div>
  <div class="vignette"></div>

  <header class="hud-header">
    <div class="hdr-left">
      <span class="jl">J</span><span class="jd">.</span><span class="jl">A</span><span class="jd">.</span><span class="jl">R</span><span class="jd">.</span><span class="jl">V</span><span class="jd">.</span><span class="jl">I</span><span class="jd">.</span><span class="jl">S</span><span class="jd">.</span>
      <span class="j-sub">JUST A RATHER VERY INTELLIGENT SYSTEM</span>
    </div>
    <div class="hdr-center">
      <span class="badge b-on">&#9679; ONLINE</span>
      <span class="badge b-enc">&#9679; ENCRYPTED</span>
      <span class="badge b-model" id="modelBadge">&#9679; ---</span>
    </div>
    <div class="hdr-right">
      <div class="j-clock" id="jClock">00:00:00</div>
      <div class="j-date"  id="jDate">---</div>
    </div>
  </header>

  <div class="nav-bar">
    <button class="nav-btn" id="logsBtn">[ MISSION LOGS ]</button>
    <button class="nav-btn" id="newMissionBtn">[ + NEW MISSION ]</button>
    <div class="nav-divider"></div>
    <span class="nav-meta">SESSION <span id="jSession">--------</span></span>
    <div class="nav-divider"></div>
    <button class="nav-btn" id="configBtn">[ CONFIG ]</button>
  </div>

  <div class="main-area">
    <div class="orb-zone">
      <canvas id="orbCanvas"></canvas>
      <div class="orb-rings">
        <div class="ring r1"></div>
        <div class="ring r2"></div>
        <div class="ring r3"></div>
        <div class="ring r4"></div>
      </div>
      <div class="orb-label" id="orbLabel">AWAITING DIRECTIVE</div>
    </div>
    <div id="log" class="chat-log"></div>
  </div>

  <div class="cmd-area">
    <div class="cmd-box" id="cmdBox">
      <span class="cmd-prompt">&gt;_</span>
      <textarea id="input" rows="1" placeholder="ENTER COMMAND&#8230;"></textarea>
      <button id="send" class="exec-btn">EXECUTE</button>
    </div>
    <div class="cmd-footer">
      <span>CAGENTIC v<span id="versionSpan">--</span></span>
      <span>END-TO-END ENCRYPTED &bull; LOCALHOST ONLY</span>
      <span id="busyLabel" class="busy-label hidden">&#9679; PROCESSING</span>
    </div>
  </div>
</div>

<!-- Sessions drawer -->
<div id="sessionsPanel" class="sessions-panel">
  <div class="sessions-head">
    <span class="panel-hdr">&#123; MISSION LOGS &#125;</span>
    <button id="closeSessionsBtn" class="icon-btn">&#10005;</button>
  </div>
  <div id="chatList" class="chat-list-j"></div>
</div>

<div id="backdrop" class="backdrop hidden"></div>

<!-- Settings modal -->
<div id="settingsModal" class="modal hidden">
  <div class="modal-card">
    <div class="modal-head">
      <span class="panel-hdr">&#123; SYSTEM CONFIGURATION &#125;</span>
      <button id="closeSettings" class="icon-btn">&#10005;</button>
    </div>
    <div class="modal-body">
      <div class="field">
        <span class="field-label">MODEL</span>
        <select id="setModel"></select>
      </div>
      <div class="field">
        <span class="field-label">OPERATOR NAME</span>
        <input id="setName" type="text" placeholder="IDENTIFY YOURSELF" />
      </div>
      <div class="field">
        <span class="field-label">TEMPERATURE &nbsp;<em id="tempVal">0.40</em></span>
        <input id="setTemp" type="range" min="0" max="1.5" step="0.05" />
      </div>
      <div class="field row">
        <span class="field-label">STREAM RESPONSES</span>
        <label class="toggle"><input id="setStream" type="checkbox" /><span></span></label>
      </div>
      <div class="field row">
        <span class="field-label">AUTO-APPROVE TOOLS</span>
        <label class="toggle"><input id="setYolo" type="checkbox" /><span></span></label>
      </div>
    </div>
    <div class="modal-foot">
      <button id="cancelSettings" class="btn-ghost">CANCEL</button>
      <button id="saveSettings"   class="btn-primary">SAVE CONFIG</button>
    </div>
  </div>
</div>

<script src="/app.js"></script>
</body>
</html>
"""

_CSS = """
/* ===== J.A.R.V.I.S. — Neural Interface ===================================== */
:root {
  --bg:       #030e1c;
  --cyan:     #00d4ff;
  --cyan-dim: rgba(0,212,255,.1);
  --cyan-glow:rgba(0,212,255,.35);
  --text:     #90e0ff;
  --text-2:   #5599bb;
  --text-dim: #224466;
  --ok:       #00ff99;
  --warn:     #ffaa00;
  --hot:      #ff4422;
  --border:   rgba(0,212,255,.2);
  --border-h: rgba(0,212,255,.5);
  --panel-bg: rgba(0,14,32,.88);
  --grid:     rgba(0,212,255,.03);
  --mono: "Courier New", Consolas, monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; overflow: hidden; }
body {
  background: var(--bg); color: var(--text);
  font-family: var(--mono); font-size: 12px;
  background-image:
    linear-gradient(var(--grid) 1px, transparent 1px),
    linear-gradient(90deg, var(--grid) 1px, transparent 1px);
  background-size: 44px 44px;
}
::selection { background: rgba(0,212,255,.22); }

/* scanlines / vignette */
.scanlines {
  position: fixed; inset: 0; pointer-events: none; z-index: 9999;
  background: repeating-linear-gradient(
    0deg, transparent, transparent 2px, rgba(0,0,0,.045) 2px, rgba(0,0,0,.045) 4px);
}
.vignette {
  position: fixed; inset: 0; pointer-events: none; z-index: 9998;
  background: radial-gradient(ellipse at center, transparent 50%, rgba(0,5,20,.8) 100%);
}

#app { display: flex; flex-direction: column; height: 100vh; height: 100dvh; }

/* ---- HEADER ---------------------------------------------------------------- */
.hud-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 20px 7px; border-bottom: 1px solid var(--border);
  background: rgba(0,8,20,.75); flex-shrink: 0; gap: 16px;
}
.hdr-left { display: flex; align-items: baseline; gap: 0; flex-shrink: 0; }
.jl { font-size: 20px; color: #fff; text-shadow: 0 0 16px var(--cyan); letter-spacing: .18em; }
.jd { font-size: 20px; color: var(--cyan); letter-spacing: .18em; }
.j-sub {
  font-size: 8px; color: var(--text-2); letter-spacing: .14em;
  margin-left: 18px; align-self: flex-end; padding-bottom: 3px; text-transform: uppercase;
}
.hdr-center { display: flex; align-items: center; gap: 10px; flex: 1; justify-content: center; }
.badge {
  font-size: 9px; padding: 3px 9px; border: 1px solid;
  letter-spacing: .1em; text-transform: uppercase; white-space: nowrap;
}
.b-on  { color: var(--ok);  border-color: rgba(0,255,153,.35); background: rgba(0,255,153,.05); }
.b-enc { color: var(--cyan); border-color: var(--border); background: var(--cyan-dim); }
.b-model{ color: var(--warn); border-color: rgba(255,170,0,.35); background: rgba(255,170,0,.05); }
.hdr-right { text-align: right; flex-shrink: 0; }
.j-clock {
  font-size: 22px; color: #fff; letter-spacing: .12em;
  text-shadow: 0 0 18px var(--cyan-glow);
}
.j-date { font-size: 9px; color: var(--text-2); letter-spacing: .1em; margin-top: 2px; }

/* ---- NAV BAR --------------------------------------------------------------- */
.nav-bar {
  display: flex; align-items: center; gap: 10px; padding: 5px 20px;
  border-bottom: 1px solid var(--border); background: rgba(0,6,16,.6);
  flex-shrink: 0;
}
.nav-btn {
  background: var(--cyan-dim); border: 1px solid var(--border);
  color: var(--cyan); font: 9px var(--mono); cursor: pointer;
  padding: 4px 11px; letter-spacing: .12em; text-transform: uppercase;
  transition: background .15s, border-color .15s;
}
.nav-btn:hover { background: rgba(0,212,255,.22); border-color: var(--border-h); }
.nav-divider { width: 1px; height: 16px; background: var(--border); }
.nav-meta { font-size: 9px; color: var(--text-dim); letter-spacing: .08em; white-space: nowrap; }

/* ---- MAIN AREA ------------------------------------------------------------- */
.main-area { flex: 1; display: flex; flex-direction: column; min-height: 0; overflow: hidden; }

/* ---- ORB ZONE -------------------------------------------------------------- */
.orb-zone {
  position: relative; flex-shrink: 0; height: 310px;
  display: flex; align-items: center; justify-content: center;
  background: radial-gradient(ellipse 70% 80% at 50% 55%,
    rgba(0,60,120,.35) 0%, rgba(0,20,50,.15) 60%, transparent 100%);
  border-bottom: 1px solid var(--border);
  overflow: hidden;
}
#orbCanvas { position: absolute; inset: 0; width: 100%; height: 100%; }
.orb-rings { position: absolute; top: 50%; left: 50%; pointer-events: none; }
.ring { position: absolute; border-radius: 50%; border: 1px solid; }
.r1 { width: 320px; height: 320px; margin: -160px 0 0 -160px; border-color: rgba(0,212,255,.1);  animation: spin1 28s linear infinite; }
.r2 { width: 250px; height: 250px; margin: -125px 0 0 -125px; border-color: rgba(0,212,255,.18); border-style: dashed; animation: spin2 18s linear infinite; }
.r3 { width: 185px; height: 185px; margin: -92px  0 0 -92px;  border-color: rgba(0,212,255,.28); animation: spin1 13s linear infinite; }
.r4 { width: 120px; height: 120px; margin: -60px  0 0 -60px;  border-color: rgba(0,212,255,.42); animation: spin2 8s  linear infinite; }
@keyframes spin1 { to { transform: rotate(360deg);  } }
@keyframes spin2 { to { transform: rotate(-360deg); } }
.orb-label {
  position: absolute; bottom: 12px; left: 50%; transform: translateX(-50%);
  font-size: 9px; color: var(--text-dim); letter-spacing: .18em;
  text-transform: uppercase; white-space: nowrap; pointer-events: none;
}

/* ---- CHAT LOG -------------------------------------------------------------- */
.chat-log { flex: 1; overflow-y: auto; padding: 16px 0; min-height: 0; }
.j-thread { max-width: 800px; margin: 0 auto; padding: 0 24px; }

/* empty state */
.j-empty {
  max-width: 800px; margin: 0 auto; padding: 24px 24px 0;
}
.j-empty-title {
  font-size: 11px; color: var(--text-2); letter-spacing: .2em;
  text-transform: uppercase; margin-bottom: 20px; text-align: center;
}
.quick-cards {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
}
.qcard {
  padding: 14px 16px; border: 1px solid var(--border);
  background: rgba(0,212,255,.03); cursor: pointer;
  transition: background .15s, border-color .15s;
  text-align: left;
}
.qcard:hover { background: rgba(0,212,255,.08); border-color: var(--border-h); }
.qcard-icon { font-size: 18px; margin-bottom: 7px; display: block; }
.qcard-title { font-size: 11px; color: #c8eeff; letter-spacing: .05em; display: block; margin-bottom: 3px; }
.qcard-sub   { font-size: 9px;  color: var(--text-2); letter-spacing: .04em; line-height: 1.5; display: block; }

/* messages */
.msg-row { margin: 10px 0; animation: fadeIn .25s ease; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } }
.msg-row.user { display: flex; justify-content: flex-end; }
.msg-row.user .bubble {
  background: rgba(0,80,160,.28); border: 1px solid rgba(0,160,255,.3);
  padding: 9px 14px; max-width: 78%; font-size: 12px; color: #c5eaff;
  line-height: 1.55; letter-spacing: .02em;
}
.msg-row.user .bubble::before { content: "> "; color: var(--cyan); }
.msg-row.assistant { display: flex; gap: 12px; align-items: flex-start; }
.j-avatar {
  width: 26px; height: 26px; flex-shrink: 0; margin-top: 1px;
  border: 1px solid var(--cyan); display: flex; align-items: center;
  justify-content: center; color: var(--cyan); font-size: 11px;
  box-shadow: 0 0 10px var(--cyan-glow); background: rgba(0,212,255,.05);
}
.msg-body {
  flex: 1; min-width: 0; font-size: 12px; color: var(--text); line-height: 1.65;
}
.msg-body p { margin: 0 0 9px; }
.msg-body p:last-child { margin: 0; }
.msg-body h3 { font-size: 13px; color: #fff; margin: 12px 0 5px; }
.msg-body code { color: var(--cyan); background: rgba(0,212,255,.07); padding: 1px 5px; font-size: 11px; }
.msg-body strong { color: #fff; }
.msg-body a { color: var(--cyan); text-decoration: none; }
.msg-body a:hover { text-decoration: underline; }
.msg-body ul { padding-left: 18px; margin: 6px 0; }
.msg-body li::marker { color: var(--cyan); }
.cursor::after { content: '█'; color: var(--cyan); animation: blink .9s steps(2) infinite; }
@keyframes blink { 50% { opacity: 0; } }

/* code blocks */
.codeblock { margin: 9px 0; border: 1px solid var(--border); background: rgba(0,5,18,.95); }
.cb-head {
  display: flex; justify-content: space-between; padding: 5px 10px;
  background: rgba(0,212,255,.05); border-bottom: 1px solid var(--border);
}
.cb-lang { font-size: 9px; color: var(--cyan); letter-spacing: .1em; text-transform: uppercase; }
.cb-copy { background: transparent; border: 0; color: var(--text-2); cursor: pointer; font: 9px var(--mono); letter-spacing: .1em; }
.cb-copy:hover { color: var(--cyan); }
.codeblock pre { margin: 0; padding: 10px 12px; overflow-x: auto; }
.codeblock code { font: 11.5px/1.6 var(--mono); color: #aadcf5; background: none; }

/* tool rows */
.tool-row {
  display: flex; align-items: center; gap: 8px; padding: 6px 9px;
  margin: 5px 0; font-size: 10px; border: 1px solid var(--border);
  background: rgba(0,18,38,.7); letter-spacing: .04em;
}
.tool-row .tname { color: #c8eeff; }
.tool-row .tsum  { color: var(--text-2); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tool-row .tres  { margin-left: auto; }
.tool-row.ok  .tres { color: var(--ok); }
.tool-row.bad .tres { color: var(--hot); }
.tool-row.pending .tres { color: var(--text-dim); animation: pulse 1s ease infinite; }
@keyframes pulse { 50% { opacity: .3; } }

/* thinking */
.thinking-row {
  display: flex; align-items: center; gap: 10px; padding: 5px 0;
  font-size: 10px; color: var(--text-dim); letter-spacing: .14em;
}
.thinking-dots { display: flex; gap: 5px; }
.thinking-dots span {
  width: 6px; height: 6px; background: var(--cyan); border-radius: 50%;
  animation: bob 1s ease-in-out infinite;
}
.thinking-dots span:nth-child(2) { animation-delay: .18s; }
.thinking-dots span:nth-child(3) { animation-delay: .36s; }
@keyframes bob { 0%,100%{opacity:.15;transform:translateY(0)} 50%{opacity:1;transform:translateY(-4px)} }

/* plan */
.plan-box { margin: 9px 0; padding: 11px 14px; border: 1px solid rgba(255,170,0,.3); background: rgba(255,170,0,.03); }
.plan-box .ph { color: var(--warn); font-size: 10px; letter-spacing: .1em; margin-bottom: 7px; }
.plan-box ol { padding-left: 16px; color: var(--text-2); font-size: 11px; }
.plan-box li { margin: 3px 0; }

/* note / error */
.note-row { font-size: 10px; color: var(--text-dim); padding: 3px 0; }
.note-row.err { color: var(--hot); }

/* permission */
.perm-box {
  margin: 9px 0; padding: 11px 14px;
  border: 1px solid rgba(255,170,0,.4); border-left: 2px solid var(--warn);
  background: rgba(255,170,0,.03);
}
.perm-box .pq { font-size: 11px; color: var(--text); margin-bottom: 9px; }
.perm-box code { color: var(--warn); background: rgba(255,170,0,.08); padding: 1px 5px; }
.perm-btns { display: flex; gap: 8px; }
.perm-btns button {
  border: 1px solid; padding: 6px 12px; cursor: pointer;
  font: 9px var(--mono); letter-spacing: .1em; text-transform: uppercase;
}
.perm-btns .yes    { background: rgba(0,255,153,.07);  color: var(--ok);  border-color: rgba(0,255,153,.4); }
.perm-btns .yes:hover { background: rgba(0,255,153,.18); }
.perm-btns .always { background: rgba(255,170,0,.07);  color: var(--warn); border-color: rgba(255,170,0,.4); }
.perm-btns .no     { background: transparent; color: var(--text-2); border-color: var(--border); }
.perm-decided      { font-size: 10px; color: var(--text-dim); }

/* ---- CMD AREA -------------------------------------------------------------- */
.cmd-area {
  flex-shrink: 0; padding: 10px 20px 12px;
  border-top: 1px solid var(--border); background: rgba(0,6,16,.7);
}
.cmd-box {
  display: flex; align-items: flex-end; gap: 10px;
  border: 1px solid var(--border-h); padding: 9px 12px;
  background: rgba(0,20,45,.8);
  box-shadow: 0 0 30px rgba(0,212,255,.07), inset 0 0 25px rgba(0,0,0,.5);
  max-width: 900px; margin: 0 auto;
}
.cmd-box:focus-within {
  border-color: var(--cyan);
  box-shadow: 0 0 40px rgba(0,212,255,.18), inset 0 0 25px rgba(0,0,0,.5);
}
.cmd-prompt { color: var(--cyan); font-size: 15px; flex-shrink: 0; padding-bottom: 1px; }
.cmd-box textarea {
  flex: 1; background: transparent; border: 0; outline: 0;
  color: #d8f4ff; font: 13px/1.55 var(--mono); resize: none; max-height: 130px;
  letter-spacing: .03em;
}
.cmd-box textarea::placeholder { color: var(--text-dim); }
.exec-btn {
  flex-shrink: 0; padding: 7px 18px; border: 1px solid var(--cyan);
  background: rgba(0,212,255,.1); color: var(--cyan); font: 10px var(--mono);
  cursor: pointer; letter-spacing: .16em; text-transform: uppercase;
  transition: background .15s;
}
.exec-btn:hover    { background: rgba(0,212,255,.24); }
.exec-btn:disabled { opacity: .28; cursor: default; }
.cmd-footer {
  display: flex; justify-content: space-between; align-items: center;
  max-width: 900px; margin: 5px auto 0;
  font-size: 9px; color: var(--text-dim); letter-spacing: .08em;
}
.busy-label { color: var(--ok); animation: pulse 1.2s ease infinite; }
.busy-label.hidden { display: none; }

/* ---- SESSIONS DRAWER ------------------------------------------------------- */
.sessions-panel {
  position: fixed; top: 0; left: 0; bottom: 0; width: 270px;
  background: rgba(2,10,24,.97); border-right: 1px solid var(--border-h);
  z-index: 200; padding: 14px; display: flex; flex-direction: column;
  transform: translateX(-100%); transition: transform .22s ease;
}
.sessions-panel.open { transform: translateX(0); box-shadow: 0 0 50px rgba(0,212,255,.12); }
.sessions-head {
  display: flex; align-items: center; justify-content: space-between;
  padding-bottom: 10px; border-bottom: 1px solid var(--border); margin-bottom: 10px;
}
.panel-hdr { font-size: 9px; color: var(--cyan); letter-spacing: .16em; text-transform: uppercase; }
.icon-btn { background: transparent; border: 0; color: var(--text-dim); cursor: pointer; font: 14px var(--mono); padding: 2px 5px; }
.icon-btn:hover { color: var(--cyan); }
.chat-list-j { flex: 1; overflow-y: auto; }
.chat-item-j {
  display: flex; align-items: center; padding: 8px 8px; cursor: pointer;
  color: var(--text-2); border-bottom: 1px solid rgba(0,212,255,.06);
  font-size: 10px; letter-spacing: .05em; gap: 6px;
}
.chat-item-j:hover  { background: rgba(0,212,255,.05); color: var(--text); }
.chat-item-j.active { background: rgba(0,212,255,.08); color: var(--cyan); }
.chat-item-j .ci-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ci-del-j { background: transparent; border: 0; color: var(--text-dim); cursor: pointer; font-size: 14px; padding: 0 3px; }
.ci-del-j:hover { color: var(--hot); }

/* ---- SETTINGS MODAL -------------------------------------------------------- */
.modal { position: fixed; inset: 0; background: rgba(0,4,14,.82); display: flex; align-items: center; justify-content: center; z-index: 300; }
.modal.hidden { display: none; }
.modal-card {
  background: #030e1c; border: 1px solid var(--border-h);
  width: 440px; max-width: calc(100vw - 28px);
  box-shadow: 0 0 60px rgba(0,212,255,.15);
}
.modal-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 13px 17px; border-bottom: 1px solid var(--border);
}
.modal-body { padding: 16px 17px; display: flex; flex-direction: column; gap: 15px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field.row { flex-direction: row; align-items: center; justify-content: space-between; }
.field-label { font-size: 9px; color: var(--text-2); letter-spacing: .1em; text-transform: uppercase; }
.field-label em { color: var(--text-dim); font-style: normal; }
.field select, .field input[type=text] {
  background: rgba(0,20,45,.9); border: 1px solid var(--border);
  color: var(--text); padding: 8px 11px; font: 11.5px var(--mono); letter-spacing: .04em;
}
.field select:focus, .field input[type=text]:focus { outline: 0; border-color: var(--cyan); }
.field input[type=range] { accent-color: var(--cyan); width: 100%; }
.toggle { position: relative; width: 38px; height: 20px; flex-shrink: 0; }
.toggle input { position: absolute; opacity: 0; }
.toggle span {
  position: absolute; inset: 0; cursor: pointer;
  background: rgba(0,212,255,.07); border: 1px solid var(--border); transition: background .15s;
}
.toggle span::after {
  content: ''; position: absolute; width: 14px; height: 14px;
  background: var(--text-dim); top: 2px; left: 2px; transition: transform .15s;
}
.toggle input:checked + span { background: rgba(0,212,255,.22); border-color: var(--cyan); }
.toggle input:checked + span::after { transform: translateX(18px); background: var(--cyan); }
.modal-foot {
  padding: 12px 17px; border-top: 1px solid var(--border);
  display: flex; justify-content: flex-end; gap: 8px;
}
.btn-primary {
  background: rgba(0,212,255,.14); color: var(--cyan); border: 1px solid var(--cyan);
  padding: 7px 16px; font: 9px var(--mono); cursor: pointer;
  letter-spacing: .14em; text-transform: uppercase;
}
.btn-primary:hover { background: rgba(0,212,255,.28); }
.btn-ghost {
  background: transparent; color: var(--text-2); border: 1px solid var(--border);
  padding: 7px 14px; font: 9px var(--mono); cursor: pointer;
  letter-spacing: .1em; text-transform: uppercase;
}
.btn-ghost:hover { border-color: var(--text-2); }

/* ---- BACKDROP + SCROLLBARS ------------------------------------------------- */
.backdrop { position: fixed; inset: 0; z-index: 150; background: rgba(0,4,14,.6); }
.backdrop.hidden { display: none; }
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(0,212,255,.18); }
::-webkit-scrollbar-thumb:hover { background: rgba(0,212,255,.38); }
"""

_JS = r"""
// J.A.R.V.I.S. — Neural Interface
const $ = s => document.querySelector(s);
const log = $('#log'), input = $('#input'), sendBtn = $('#send');
let state = { chats: [], currentId: null, settings: {}, busy: false };

// ---- CLOCK ------------------------------------------------------------------
function updateClock() {
  const n = new Date(), pad = v => String(v).padStart(2,'0');
  $('#jClock').textContent = pad(n.getHours())+':'+pad(n.getMinutes())+':'+pad(n.getSeconds());
  const days=['SUN','MON','TUE','WED','THU','FRI','SAT'];
  const months=['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
  $('#jDate').textContent = days[n.getDay()]+' '+n.getDate()+' '+months[n.getMonth()]+' '+n.getFullYear();
}
setInterval(updateClock, 1000); updateClock();

// ---- ORB --------------------------------------------------------------------
(function initOrb() {
  const canvas = $('#orbCanvas'); if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let W, H, cx, cy, particles = [], t = 0;

  function resize() {
    const p = canvas.parentElement;
    W = canvas.width  = p.clientWidth  || 600;
    H = canvas.height = p.clientHeight || 310;
    cx = W/2; cy = H/2;
  }

  function mkPart() {
    const theta = Math.random()*Math.PI*2, phi = Math.random()*Math.PI;
    const r = 45 + Math.random()*40;
    return {
      x: cx + r*Math.sin(phi)*Math.cos(theta),
      y: cy + r*Math.sin(phi)*Math.sin(theta)*0.4,
      z: Math.cos(phi),
      vx: (Math.random()-.5)*0.3, vy: (Math.random()-.5)*0.3,
      life: Math.random(), decay: 0.007+Math.random()*0.016,
      size: 0.7+Math.random()*2.2, alpha: 0.4+Math.random()*0.6,
    };
  }
  function resetPart(p) {
    const theta = Math.random()*Math.PI*2, phi = Math.random()*Math.PI;
    const r = 43+Math.random()*42;
    p.x=cx+r*Math.sin(phi)*Math.cos(theta);
    p.y=cy+r*Math.sin(phi)*Math.sin(theta)*0.4;
    p.z=Math.cos(phi); p.life=1;
  }
  function initParts() { particles=[]; for(let i=0;i<220;i++) particles.push(mkPart()); }

  const ORBS=[
    {r:105,s:0.65, sz:3.5,ph:0},       {r:105,s:0.65, sz:3.5,ph:Math.PI},
    {r:82, s:-1.05,sz:2.5,ph:Math.PI/2},{r:125,s:0.45, sz:2,  ph:Math.PI/3},
    {r:82, s:-1.05,sz:2.5,ph:Math.PI*1.5},
  ];

  function draw() {
    ctx.clearRect(0,0,W,H); t+=0.011;

    // halos
    for(let r=120;r>=12;r-=18) {
      const g=ctx.createRadialGradient(cx,cy,r*0.4,cx,cy,r);
      g.addColorStop(0,`rgba(0,180,255,${0.022+(120-r)*0.0006})`);
      g.addColorStop(1,'rgba(0,0,0,0)');
      ctx.beginPath(); ctx.arc(cx,cy,r,0,Math.PI*2);
      ctx.fillStyle=g; ctx.fill();
    }
    // main glow
    const mg=ctx.createRadialGradient(cx,cy,0,cx,cy,65);
    mg.addColorStop(0,'rgba(190,240,255,0.88)');
    mg.addColorStop(0.22,'rgba(0,190,255,0.58)');
    mg.addColorStop(0.6,'rgba(0,80,200,0.22)');
    mg.addColorStop(1,'rgba(0,0,0,0)');
    ctx.beginPath(); ctx.arc(cx,cy,65,0,Math.PI*2); ctx.fillStyle=mg; ctx.fill();
    // inner core
    const ic=ctx.createRadialGradient(cx,cy,0,cx,cy,20);
    ic.addColorStop(0,'rgba(255,255,255,1)');
    ic.addColorStop(0.5,'rgba(150,225,255,0.75)');
    ic.addColorStop(1,'rgba(0,150,255,0)');
    ctx.beginPath(); ctx.arc(cx,cy,20,0,Math.PI*2); ctx.fillStyle=ic; ctx.fill();
    // particles
    particles.forEach(p=>{
      p.x+=p.vx; p.y+=p.vy; p.life-=p.decay;
      if(p.life<=0) resetPart(p);
      const a=Math.max(0,p.life)*p.alpha, br=0.5+p.z*0.5;
      ctx.beginPath(); ctx.arc(p.x,p.y,p.size,0,Math.PI*2);
      ctx.fillStyle=`rgba(${Math.round(80+br*175)},${Math.round(175+br*80)},255,${a})`;
      ctx.fill();
    });
    // orbital dots
    ORBS.forEach(o=>{
      const a=t*o.s+o.ph;
      const ox=cx+o.r*Math.cos(a), oy=cy+o.r*0.38*Math.sin(a);
      ctx.beginPath(); ctx.arc(ox,oy,o.sz,0,Math.PI*2);
      ctx.fillStyle='rgba(0,220,255,0.9)';
      ctx.shadowColor='#00d4ff'; ctx.shadowBlur=12; ctx.fill(); ctx.shadowBlur=0;
    });
    requestAnimationFrame(draw);
  }
  window.addEventListener('resize',()=>{resize();initParts();});
  resize(); initParts(); draw();
})();

// ---- HELPERS ----------------------------------------------------------------
function esc(s){ return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function md(src) {
  const blocks=[];
  let s=(src||'').replace(/```(\w*)\n?([\s\S]*?)```/g,(m,lang,code)=>{
    blocks.push('<div class="codeblock"><div class="cb-head"><span class="cb-lang">'+(lang||'text')+'</span>'+
      '<button class="cb-copy">COPY</button></div><pre><code>'+esc(code.replace(/\n$/,''))+
      '</code></pre></div>');
    return '\x00B'+(blocks.length-1)+'\x00';
  });
  s=esc(s);
  s=s.replace(/`([^`\n]+)`/g,'<code>$1</code>');
  s=s.replace(/^\s*#{1,6}\s+(.+)$/gm,'<h3>$1</h3>');
  s=s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  s=s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g,'$1<em>$2</em>');
  s=s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  s=s.replace(/(?:^|\n)((?:\s*[-*]\s+.+(?:\n|$))+)/g,(m,b)=>
    '\n<ul>'+b.trim().split('\n').map(x=>'<li>'+x.replace(/^\s*[-*]\s+/,'')+'</li>').join('')+'</ul>');
  s=s.split(/\n{2,}/).map(p=>p.trim()?'<p>'+p+'</p>':'').join('');
  s=s.replace(/\n/g,'<br>');
  s=s.replace(/<p>(<(?:ul|h3|div))/g,'$1').replace(/(<\/(?:ul|h3|div)>)<\/p>/g,'$1');
  s=s.replace(/\x00B(\d+)\x00/g,(m,i)=>blocks[+i]);
  return s;
}
function scrollDown(){ log.scrollTop=log.scrollHeight; }
function getThread(){
  let t=log.querySelector('.j-thread');
  if(!t){t=document.createElement('div');t.className='j-thread';log.appendChild(t);}
  return t;
}
function clearLog(){ log.innerHTML=''; }
function avatarHTML(){ return '<div class="j-avatar">J</div>'; }
function setOrbLabel(text){ const l=$('#orbLabel'); if(l) l.textContent=(text||'AWAITING DIRECTIVE').toUpperCase(); }

// ---- EMPTY STATE (quick-start cards) ----------------------------------------
const QUICK = [
  {icon:'🌐', title:'Search the web',      sub:'Find and summarise anything online',        prompt:'Search the web for '},
  {icon:'📋', title:'Read my screen',       sub:'Summarise what\'s in my browser tab',       prompt:'Read my screen and summarise what you see'},
  {icon:'📝', title:'Take a note',          sub:'Remember something for later',              prompt:'Take a note: '},
  {icon:'🔔', title:'Set a reminder',       sub:'Add something to my reminder list',         prompt:'Add a reminder: '},
  {icon:'📁', title:'Browse files',         sub:'List or read files on your machine',        prompt:'List files in my current directory'},
  {icon:'⚡', title:'Run a command',         sub:'Execute a terminal command',                prompt:'Run this command: '},
];
function showEmpty() {
  clearLog();
  const wrap=document.createElement('div'); wrap.className='j-empty';
  wrap.innerHTML='<div class="j-empty-title">SELECT A MISSION OR ENTER A COMMAND</div>'+
    '<div class="quick-cards">'+
    QUICK.map(q=>
      `<div class="qcard" data-prompt="${esc(q.prompt)}">
        <span class="qcard-icon">${q.icon}</span>
        <span class="qcard-title">${esc(q.title)}</span>
        <span class="qcard-sub">${esc(q.sub)}</span>
      </div>`
    ).join('')+'</div>';
  log.appendChild(wrap);
  wrap.querySelectorAll('.qcard').forEach(c=>{
    c.onclick=()=>{ input.value=c.dataset.prompt; autoGrow(); input.focus(); };
  });
}

// ---- RENDERING --------------------------------------------------------------
function addUser(text){
  const r=document.createElement('div'); r.className='msg-row user';
  r.innerHTML='<div class="bubble">'+esc(text)+'</div>';
  getThread().appendChild(r); scrollDown();
}
function addAssistant(html, tools){
  const r=document.createElement('div'); r.className='msg-row assistant';
  r.innerHTML=avatarHTML()+'<div class="msg-body">'+(html||'')+'</div>';
  getThread().appendChild(r);
  (tools||[]).forEach(t=>addToolRow({name:t},true));
  scrollDown();
  return r.querySelector('.msg-body');
}
function addToolRow(t, done){
  const row=document.createElement('div'); row.className='tool-row'+(done?'':' pending');
  row.innerHTML=
    '<span style="color:var(--cyan);font-size:13px">&#9889;</span>'+
    '<span class="tname">'+esc(t.name||'')+'</span>'+
    (t.summary?'<span class="tsum">'+esc(t.summary)+'</span>':'')+
    (done?'':'<span class="tres">EXECUTING&#8230;</span>');
  getThread().appendChild(row); scrollDown();
  return row;
}
function addNote(text, isErr){
  const n=document.createElement('div'); n.className='note-row'+(isErr?' err':'');
  n.textContent=text||'';
  getThread().appendChild(n); scrollDown();
}
function showPermission(d){
  const box=document.createElement('div'); box.className='perm-box';
  box.innerHTML='<div class="pq">AUTHORIZATION REQUIRED: <code>'+esc(d.tool)+'</code>'+
    (d.summary?' &mdash; '+esc(d.summary):'')+' </div>';
  const btns=document.createElement('div'); btns.className='perm-btns';
  const answer=(a,past)=>{
    box.innerHTML='<div class="pq"><code>'+esc(d.tool)+'</code></div>'+
      '<div class="perm-decided">&#8594; '+past.toUpperCase()+'</div>';
    fetch('/api/permission',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({answer:a})});
  };
  [['yes','APPROVE','approved'],['always','ALWAYS ALLOW','always allowed'],['no','DENY','denied']].forEach(([a,l,p])=>{
    const b=document.createElement('button'); b.className=a; b.textContent=l;
    b.onclick=()=>answer(a,p); btns.appendChild(b);
  });
  box.appendChild(btns); getThread().appendChild(box); scrollDown();
}

// ---- LIVE TURN --------------------------------------------------------------
let live={body:null,raw:'',toolRow:null,thinking:null};
function showThinking(){
  const t=document.createElement('div'); t.className='thinking-row';
  t.innerHTML=avatarHTML()+'<span>PROCESSING</span>'+
    '<div class="thinking-dots"><span></span><span></span><span></span></div>';
  getThread().appendChild(t); scrollDown(); live.thinking=t;
}
function clearThinking(){ if(live.thinking){live.thinking.remove();live.thinking=null;} }
function handle(ev){
  const k=ev.kind, d=ev.data||{};
  if(k!=='user') clearThinking();
  if(k==='delta'){
    if(!live.body){live.body=addAssistant('');live.raw='';}
    live.raw+=d.text||'';
    live.body.innerHTML=md(live.raw);
    live.body.classList.add('cursor');
    scrollDown();
  } else if(k==='assistant'){
    if(!live.body&&(d.text||'').trim()){live.body=addAssistant(md(d.text));live.raw=d.text;}
    if(live.body) live.body.classList.remove('cursor');
  } else if(k==='plan'){
    const p=document.createElement('div'); p.className='plan-box';
    p.innerHTML='<div class="ph">&#9658; EXECUTION PLAN</div><ol>'+
      (d.steps||[]).map(s=>'<li>'+esc(s)+'</li>').join('')+'</ol>';
    getThread().appendChild(p); live.body=null; scrollDown();
  } else if(k==='tool_call'){
    live.body=null;
    live.toolRow=addToolRow({name:d.name,summary:d.summary},false);
  } else if(k==='tool_result'){
    if(live.toolRow){
      live.toolRow.classList.remove('pending');
      live.toolRow.classList.add(d.ok?'ok':'bad');
      const res=live.toolRow.querySelector('.tres')||document.createElement('span');
      res.className='tres';
      res.textContent=(d.ok?'✓ ':'✗ ')+(d.first_line||'').slice(0,90);
      if(!res.parentNode) live.toolRow.appendChild(res);
      live.toolRow=null;
    }
  } else if(k==='permission'){
    live.body=null; showPermission(d);
  } else if(k==='info'||k==='warn'){
    addNote(d.text,false); live.body=null;
  } else if(k==='error'){
    addNote(d.text||'ERROR: SYSTEM FAULT',true); live.body=null;
  } else if(k==='done'){
    if(live.body) live.body.classList.remove('cursor');
    live.body=null;
  } else if(k==='end'){
    finishTurn();
  }
  scrollDown();
}
log.addEventListener('click',e=>{
  const btn=e.target.closest('.cb-copy'); if(!btn) return;
  const code=btn.closest('.codeblock').querySelector('pre code');
  navigator.clipboard.writeText(code.textContent||'').then(()=>{
    btn.textContent='COPIED'; setTimeout(()=>{btn.textContent='COPY';},1400);
  });
});

// ---- SESSIONS ---------------------------------------------------------------
function renderChats(){
  const list=$('#chatList'); list.innerHTML='';
  if(!state.chats.length){
    list.innerHTML='<div style="color:var(--text-dim);font-size:9px;padding:10px 6px">NO MISSION LOGS</div>';
    return;
  }
  state.chats.forEach(c=>{
    const item=document.createElement('div');
    item.className='chat-item-j'+(c.id===state.currentId?' active':'');
    item.innerHTML=
      '<span style="color:var(--cyan);font-size:10px">&#9658;</span>'+
      '<span class="ci-title">'+esc(c.title)+'</span>'+
      '<button class="ci-del-j" title="Delete">&times;</button>';
    item.querySelector('.ci-title').onclick=()=>loadChat(c.id);
    item.querySelector('.ci-del-j').onclick=e=>{
      e.stopPropagation();
      if(confirm('DELETE "'+c.title+'"?')) deleteChat(c.id);
    };
    list.appendChild(item);
  });
}
function setCurrent(cur){
  state.currentId=cur.id;
  const s=$('#jSession'); if(s) s.textContent=(cur.id||'--------').slice(0,8).toUpperCase();
  setOrbLabel(cur.title||'AWAITING DIRECTIVE');
  clearLog();
  if(!cur.messages||!cur.messages.length){showEmpty();return;}
  cur.messages.forEach(m=>{
    if(m.role==='user') addUser(m.content);
    else addAssistant(md(m.content),m.tools);
  });
  scrollDown();
}

// ---- NETWORK ----------------------------------------------------------------
async function api(path,body){
  const r=await fetch(path,{
    method:body?'POST':'GET',headers:{'Content-Type':'application/json'},
    body:body?JSON.stringify(body):undefined,
  }); return r.json();
}
async function boot(){
  const b=await api('/api/bootstrap');
  state.chats=b.chats; state.settings=b.settings;
  const mb=$('#modelBadge'); if(mb) mb.textContent='● '+(b.model||'').toUpperCase();
  const vs=$('#versionSpan'); if(vs) vs.textContent=b.version||'--';
  renderChats(); setCurrent(b.current);
}
async function newChat(){
  const r=await api('/api/chats/new',{});
  state.chats=r.chats; renderChats(); setCurrent(r.current);
  closeSessions(); input.focus();
}
async function loadChat(id){
  const r=await api('/api/chats/load',{id});
  state.chats=r.chats; renderChats(); setCurrent(r.current); closeSessions();
}
async function deleteChat(id){
  const r=await api('/api/chats/delete',{id});
  state.chats=r.chats; renderChats(); setCurrent(r.current);
}
async function refreshChats(){
  const b=await api('/api/bootstrap');
  state.chats=b.chats; renderChats();
  setOrbLabel(b.current.title||'AWAITING DIRECTIVE');
}

// ---- DRAWER / MODAL ---------------------------------------------------------
function openSessions()  { $('#sessionsPanel').classList.add('open');    $('#backdrop').classList.remove('hidden'); }
function closeSessions() { $('#sessionsPanel').classList.remove('open'); $('#backdrop').classList.add('hidden'); }
function openSettings()  {
  closeSessions();
  const s=state.settings, sel=$('#setModel'); sel.innerHTML='';
  (s.models&&s.models.length?s.models:[s.model]).forEach(m=>{
    const o=document.createElement('option');
    o.value=m; o.textContent=m; if(m===s.model) o.selected=true; sel.appendChild(o);
  });
  $('#setName').value=s.user_name||'';
  $('#setTemp').value=s.temperature;
  $('#tempVal').textContent=(+s.temperature).toFixed(2);
  $('#setStream').checked=!!s.stream;
  $('#setYolo').checked=!!s.yolo;
  $('#settingsModal').classList.remove('hidden');
}
function closeSettings(){ $('#settingsModal').classList.add('hidden'); }
async function saveSettings(){
  state.settings=await api('/api/settings',{
    model:$('#setModel').value, user_name:$('#setName').value,
    temperature:parseFloat($('#setTemp').value),
    stream:$('#setStream').checked, yolo:$('#setYolo').checked,
  });
  const mb=$('#modelBadge'); if(mb) mb.textContent='● '+(state.settings.model||'').toUpperCase();
  closeSettings();
}

// ---- SEND -------------------------------------------------------------------
function setBusy(on){
  state.busy=on; sendBtn.disabled=on; input.disabled=on;
  const bl=$('#busyLabel');
  if(bl) bl.classList.toggle('hidden',!on);
}
function finishTurn(){ setBusy(false); input.focus(); refreshChats(); }
async function send(text){
  if(state.busy) return;
  setBusy(true);
  if(log.querySelector('.j-empty')) clearLog();
  addUser(text);
  live={body:null,raw:'',toolRow:null,thinking:null};
  showThinking();
  let res;
  try{
    res=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:text})});
  } catch(e){clearThinking();addNote('CONNECTION FAILURE',true);finishTurn();return;}
  const reader=res.body.getReader(), dec=new TextDecoder(); let buf='';
  while(true){
    let chunk; try{chunk=await reader.read();}catch(e){break;}
    if(chunk.done) break;
    buf+=dec.decode(chunk.value,{stream:true});
    let i;
    while((i=buf.indexOf('\n\n'))>=0){
      const line=buf.slice(0,i); buf=buf.slice(i+2);
      if(line.startsWith('data: ')){ try{handle(JSON.parse(line.slice(6)));}catch(e){} }
    }
  }
  clearThinking(); if(state.busy) finishTurn();
}

// ---- COMPOSER ---------------------------------------------------------------
function autoGrow(){ input.style.height='auto'; input.style.height=Math.min(input.scrollHeight,130)+'px'; }
function submit(){ const t=input.value.trim(); if(!t||state.busy)return; input.value=''; autoGrow(); send(t); }
input.addEventListener('input', autoGrow);
input.addEventListener('keydown', e=>{ if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();submit();} });
sendBtn.onclick=submit;
$('#logsBtn').onclick=openSessions;
$('#newMissionBtn').onclick=newChat;
$('#configBtn').onclick=openSettings;
$('#closeSessionsBtn').onclick=closeSessions;
$('#backdrop').onclick=()=>{closeSessions();closeSettings();};
$('#closeSettings').onclick=closeSettings;
$('#cancelSettings').onclick=closeSettings;
$('#saveSettings').onclick=saveSettings;
$('#setTemp').addEventListener('input',e=>{$('#tempVal').textContent=(+e.target.value).toFixed(2);});
$('#settingsModal').addEventListener('click',e=>{if(e.target.id==='settingsModal')closeSettings();});
document.addEventListener('keydown',e=>{
  if(e.key!=='Escape') return;
  if(!$('#settingsModal').classList.contains('hidden')) closeSettings();
  else closeSessions();
});

boot();
"""
