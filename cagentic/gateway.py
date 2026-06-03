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

  <!-- HEADER -->
  <header class="hud-header">
    <div class="hdr-left" id="hdrLeft">
      <div class="jarvis-wordmark">
        <span class="jl">J</span><span class="jd">.</span><span class="jl">A</span><span class="jd">.</span><span class="jl">R</span><span class="jd">.</span><span class="jl">V</span><span class="jd">.</span><span class="jl">I</span><span class="jd">.</span><span class="jl">S</span><span class="jd">.</span>
        <span class="j-full">JUST A RATHER VERY INTELLIGENT SYSTEM</span>
      </div>
      <div class="j-objective">OBJECTIVE &mdash; <span id="objectiveText">AWAITING DIRECTIVE</span></div>
      <div class="j-badges">
        <span class="badge b-on">&#9679; ONLINE</span>
        <span class="badge b-sec">&#9679; SECURE</span>
        <span class="badge b-enc">&#9679; ENCRYPTED</span>
        <span class="badge b-model" id="modelBadge">&#9679; ---</span>
      </div>
    </div>
    <div class="hdr-center">
      <button class="hud-btn" id="logsBtn">[ MISSION LOGS ]</button>
      <button class="hud-btn" id="newMissionBtn">[ + NEW MISSION ]</button>
      <button class="hud-btn" id="configBtn">[ CONFIG ]</button>
    </div>
    <div class="hdr-right">
      <div class="j-clock" id="jClock">00:00:00.00</div>
      <div class="j-date" id="jDate">---</div>
      <div class="j-meta">SESSION &mdash; <span id="jSession">--------</span></div>
      <div class="j-meta">LAT 34.0194&deg;N &bull; LON 118.4912&deg;W</div>
      <div class="j-meta">ALT 312FT &bull; BEARING 007&deg;</div>
    </div>
  </header>

  <!-- BODY -->
  <div class="hud-body">

    <!-- LEFT COLUMN -->
    <div class="hud-col left-col">
      <div class="panel">
        <div class="panel-hdr">&#123; SYSTEM VITALS &#125;</div>
        <div class="vitals">
          <div class="vital-row"><span class="vk">NEURAL CORE</span><div class="vbar"><div class="vfill" id="v0"></div></div><span class="vv" id="vv0">54.7%</span></div>
          <div class="vital-row"><span class="vk">MEMORY</span><div class="vbar"><div class="vfill" id="v1"></div></div><span class="vv" id="vv1">40.1%</span></div>
          <div class="vital-row"><span class="vk">LATENCY</span><div class="vbar"><div class="vfill vfill-warn" id="v2"></div></div><span class="vv" id="vv2">12.7ms</span></div>
          <div class="vital-row"><span class="vk">SIGNAL</span><div class="vbar"><div class="vfill" id="v3"></div></div><span class="vv" id="vv3">90.2%</span></div>
          <div class="vital-row"><span class="vk">THERMAL</span><div class="vbar"><div class="vfill vfill-warm" id="v4"></div></div><span class="vv" id="vv4">39.1&deg;C</span></div>
          <div class="vital-row"><span class="vk">THROUGHPUT</span><div class="vbar"><div class="vfill" id="v5"></div></div><span class="vv" id="vv5">5.07Gb/s</span></div>
        </div>
      </div>
      <div class="panel tele-panel">
        <div class="panel-hdr">&#123; TELEMETRY &#125;</div>
        <div class="tele-log" id="teleLog"></div>
      </div>
    </div>

    <!-- CENTER COLUMN -->
    <div class="hud-col center-col">
      <div class="center-orb" id="centerOrb">
        <canvas id="orbCanvas"></canvas>
        <div class="orb-rings">
          <div class="ring r1"></div>
          <div class="ring r2"></div>
          <div class="ring r3"></div>
        </div>
      </div>
      <div id="log" class="chat-log"></div>
      <div class="cmd-area">
        <div class="cmd-box" id="cmdBox">
          <span class="cmd-prompt">&gt;_</span>
          <textarea id="input" rows="1" placeholder="ENTER COMMAND&#8230;"></textarea>
          <button id="send" class="exec-btn">EXECUTE</button>
        </div>
        <div class="cmd-hint">CAGENTIC NEURAL INTERFACE v<span id="versionSpan">--</span> &bull; END-TO-END ENCRYPTED &bull; LOCALHOST ONLY</div>
      </div>
    </div>

    <!-- RIGHT COLUMN -->
    <div class="hud-col right-col">
      <div class="panel">
        <div class="panel-hdr">&#123; PROXIMITY &#125;</div>
        <div class="radar-wrap"><canvas id="radarCanvas" width="140" height="140"></canvas></div>
        <div class="radar-label">PROX 0&ndash;999</div>
      </div>
      <div class="panel">
        <div class="panel-hdr">&#123; AUDIO I/O &#125;</div>
        <canvas id="audioCanvas" height="50"></canvas>
        <div class="audio-labels"><span>AUDIO &bull; CODEC</span><span id="audioCodec">74 &middot; 86</span></div>
      </div>
      <div class="panel diag-panel">
        <div class="panel-hdr">&#123; DIAGNOSTICS &#125;</div>
        <div class="diag-log" id="diagLog"></div>
      </div>
    </div>

  </div>

  <!-- FOOTER -->
  <footer class="hud-footer">
    <div class="foot-left">
      <span class="fstat ok">BIOMETRIC LINK &mdash; STABLE</span>
      <span class="fstat ok">VOICE PRINT &mdash; VERIFIED</span>
      <span class="fstat">SIPHONING LEADS &mdash; 339,355</span>
      <span class="fstat">OBJECTIVE LOCK &mdash; #88480</span>
    </div>
    <div class="foot-right">
      <span class="fstat">UPLINK 5.2Mb/s</span>
      <span class="fstat">DOWNLINK 800Mb/s</span>
      <span class="fstat">PACKET LOSS 0.00</span>
      <span class="fstat">NODES 4/4 SYNC</span>
    </div>
  </footer>

</div>

<!-- Sessions sliding drawer -->
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
        <span class="field-label">TEMPERATURE <em id="tempVal">0.40</em></span>
        <input id="setTemp" type="range" min="0" max="1.5" step="0.05" />
      </div>
      <div class="field row">
        <div><span class="field-label">STREAM RESPONSES</span></div>
        <label class="toggle"><input id="setStream" type="checkbox" /><span></span></label>
      </div>
      <div class="field row">
        <div><span class="field-label">AUTO-APPROVE TOOLS</span></div>
        <label class="toggle"><input id="setYolo" type="checkbox" /><span></span></label>
      </div>
    </div>
    <div class="modal-foot">
      <button id="cancelSettings" class="btn-ghost">CANCEL</button>
      <button id="saveSettings" class="btn-primary">SAVE CONFIG</button>
    </div>
  </div>
</div>

<script src="/app.js"></script>
</body>
</html>
"""

_CSS = """
/* ===== J.A.R.V.I.S. — Neural Interface ================================= */
:root {
  --bg:        #020d1a;
  --panel-bg:  rgba(0,15,35,.82);
  --cyan:      #00d4ff;
  --cyan-dim:  rgba(0,212,255,.1);
  --cyan-glow: rgba(0,212,255,.4);
  --text:      #8de3ff;
  --text-2:    #5ba8c8;
  --text-dim:  #2a5a72;
  --ok:        #00ff88;
  --warn:      #ffaa00;
  --hot:       #ff5533;
  --border:    rgba(0,212,255,.22);
  --border-2:  rgba(0,212,255,.45);
  --grid:      rgba(0,212,255,.035);
  --mono: "Courier New", Consolas, monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; overflow: hidden; }
body {
  background: var(--bg); color: var(--text);
  font-family: var(--mono); font-size: 11px;
  background-image:
    linear-gradient(var(--grid) 1px, transparent 1px),
    linear-gradient(90deg, var(--grid) 1px, transparent 1px);
  background-size: 40px 40px;
}
.scanlines {
  position: fixed; inset: 0; pointer-events: none; z-index: 999;
  background: repeating-linear-gradient(
    0deg, transparent, transparent 2px, rgba(0,0,0,.04) 2px, rgba(0,0,0,.04) 4px);
}
.vignette {
  position: fixed; inset: 0; pointer-events: none; z-index: 998;
  background: radial-gradient(ellipse at center, transparent 55%, rgba(0,5,18,.75) 100%);
}
#app { display: flex; flex-direction: column; height: 100vh; height: 100dvh; }
::selection { background: rgba(0,212,255,.2); }

/* ---- HEADER ------------------------------------------------------------ */
.hud-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 14px 5px; border-bottom: 1px solid var(--border);
  background: rgba(0,8,22,.7); flex-shrink: 0; gap: 12px;
}
.jarvis-wordmark {
  display: flex; align-items: baseline; gap: 0; letter-spacing: .25em;
}
.jl { font-size: 17px; color: #fff; text-shadow: 0 0 12px var(--cyan); }
.jd { font-size: 17px; color: var(--cyan); }
.j-full {
  font-size: 8.5px; color: var(--text-2); letter-spacing: .12em;
  margin-left: 12px; align-self: flex-end; padding-bottom: 2px;
  text-transform: uppercase;
}
.j-objective {
  font-size: 9px; color: var(--text-2); letter-spacing: .07em;
  margin-top: 3px; text-transform: uppercase;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 340px;
}
.j-badges { display: flex; gap: 6px; margin-top: 4px; flex-wrap: wrap; }
.badge {
  font-size: 8.5px; padding: 2px 7px; border: 1px solid;
  letter-spacing: .1em; text-transform: uppercase;
}
.b-on  { color: var(--ok);  border-color: rgba(0,255,136,.4);  background: rgba(0,255,136,.05); }
.b-sec { color: var(--cyan); border-color: var(--border); background: var(--cyan-dim); }
.b-enc { color: var(--cyan); border-color: var(--border); background: var(--cyan-dim); }
.b-model{ color: var(--warn); border-color: rgba(255,170,0,.4); background: rgba(255,170,0,.05); }
.hdr-center { display: flex; gap: 8px; flex-shrink: 0; }
.hud-btn {
  background: var(--cyan-dim); border: 1px solid var(--border);
  color: var(--cyan); font: 9px var(--mono); cursor: pointer;
  padding: 5px 10px; letter-spacing: .12em; text-transform: uppercase;
  transition: background .15s, border-color .15s;
  white-space: nowrap;
}
.hud-btn:hover { background: rgba(0,212,255,.2); border-color: var(--border-2); }
.hdr-right { text-align: right; flex-shrink: 0; }
.j-clock {
  font-size: 20px; color: #fff; letter-spacing: .1em;
  text-shadow: 0 0 16px var(--cyan-glow); font-family: var(--mono);
}
.j-date  { font-size: 9px; color: var(--text-2); letter-spacing: .1em; margin-top: 2px; }
.j-meta  { font-size: 8.5px; color: var(--text-dim); letter-spacing: .06em; margin-top: 1px; }

/* ---- BODY -------------------------------------------------------------- */
.hud-body {
  flex: 1; display: grid;
  grid-template-columns: 196px 1fr 196px;
  min-height: 0; overflow: hidden;
}

/* ---- PANELS ------------------------------------------------------------ */
.panel {
  border: 1px solid var(--border); background: var(--panel-bg);
  margin: 5px; padding: 7px; position: relative; overflow: hidden;
}
.panel::before {
  content: ''; position: absolute; top: -1px; left: 12px; right: 12px; height: 1px;
  background: linear-gradient(90deg, transparent, var(--cyan), transparent);
}
.panel-hdr {
  font-size: 9px; color: var(--cyan); letter-spacing: .15em;
  text-transform: uppercase; margin-bottom: 7px;
  text-shadow: 0 0 8px var(--cyan-glow);
}

/* ---- LEFT COLUMN ------------------------------------------------------- */
.left-col  { display: flex; flex-direction: column; overflow: hidden; }
.vitals    { display: flex; flex-direction: column; gap: 5px; }
.vital-row { display: flex; align-items: center; gap: 4px; }
.vk { width: 68px; font-size: 8.5px; color: var(--text-2); letter-spacing: .05em; flex-shrink: 0; }
.vbar {
  flex: 1; height: 5px; background: rgba(0,212,255,.06);
  border: 1px solid rgba(0,212,255,.15); overflow: hidden;
}
.vfill {
  height: 100%; background: var(--cyan);
  box-shadow: 0 0 5px var(--cyan-glow); transition: width .9s ease;
}
.vfill-warn { background: var(--warn); box-shadow: 0 0 5px rgba(255,170,0,.5); }
.vfill-warm { background: var(--hot);  box-shadow: 0 0 5px rgba(255,85,51,.5); }
.vv { width: 46px; text-align: right; font-size: 8.5px; color: var(--text); flex-shrink: 0; }
.tele-panel { flex: 1; overflow: hidden; }
.tele-log {
  height: calc(100% - 22px); overflow-y: auto;
  font-size: 8px; line-height: 1.65; color: var(--text-2);
}
.telog-entry { padding: 1px 0; border-bottom: 1px solid rgba(0,212,255,.04); }
.telog-entry.ok { color: var(--ok); }

/* ---- CENTER COLUMN ----------------------------------------------------- */
.center-col {
  display: flex; flex-direction: column; min-height: 0;
  border-left: 1px solid var(--border); border-right: 1px solid var(--border);
}
.center-orb {
  position: relative; flex-shrink: 0; height: 240px;
  display: flex; align-items: center; justify-content: center;
  overflow: hidden;
}
#orbCanvas { position: absolute; inset: 0; width: 100%; height: 100%; }
.orb-rings { position: absolute; pointer-events: none; top: 50%; left: 50%; }
.ring { position: absolute; border-radius: 50%; border: 1px solid rgba(0,212,255,.18); }
.r1 {
  width: 260px; height: 260px; margin: -130px 0 0 -130px;
  animation: spin1 22s linear infinite;
}
.r2 {
  width: 200px; height: 200px; margin: -100px 0 0 -100px;
  border-style: dashed; border-color: rgba(0,212,255,.25);
  animation: spin2 15s linear infinite;
}
.r3 {
  width: 150px; height: 150px; margin: -75px 0 0 -75px;
  border-color: rgba(0,212,255,.35);
  animation: spin1 11s linear infinite;
}
@keyframes spin1 { to { transform: rotate(360deg); } }
@keyframes spin2 { to { transform: rotate(-360deg); } }

.chat-log { flex: 1; overflow-y: auto; padding: 8px 14px; min-height: 0; }
.j-thread { /* container */ }

.j-empty {
  height: 100%; display: flex; flex-direction: column; align-items: center;
  justify-content: center; color: var(--text-dim);
  font-size: 9.5px; letter-spacing: .2em; text-align: center; gap: 6px;
}

/* messages */
.msg-row { margin: 8px 0; animation: fadeIn .25s ease; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } }
.msg-row.user { display: flex; justify-content: flex-end; }
.msg-row.user .bubble {
  background: rgba(0,80,160,.25); border: 1px solid rgba(0,150,255,.3);
  padding: 6px 11px; max-width: 82%; font-size: 11px; color: #c0e8ff;
  line-height: 1.5; letter-spacing: .02em;
}
.msg-row.user .bubble::before { content: "> "; color: var(--cyan); }
.msg-row.assistant { display: flex; gap: 9px; }
.j-avatar {
  width: 22px; height: 22px; flex-shrink: 0;
  border: 1px solid var(--cyan); display: flex; align-items: center;
  justify-content: center; color: var(--cyan); font-size: 10px;
  box-shadow: 0 0 8px var(--cyan-glow); background: rgba(0,212,255,.05);
}
.msg-body {
  flex: 1; font-size: 11px; color: var(--text); line-height: 1.6;
  letter-spacing: .02em; min-width: 0;
}
.msg-body p { margin: 0 0 7px; }
.msg-body p:last-child { margin: 0; }
.msg-body h3 { font-size: 12px; color: #fff; margin: 10px 0 4px; }
.msg-body code { color: var(--cyan); background: rgba(0,212,255,.07); padding: 1px 4px; font-size: 10.5px; }
.msg-body strong { color: #fff; }
.msg-body a { color: var(--cyan); text-decoration: none; }
.msg-body ul { padding-left: 16px; margin: 5px 0; }
.msg-body li::marker { color: var(--cyan); }
.cursor::after {
  content: '█'; color: var(--cyan); animation: blink .9s steps(2) infinite; font-size: 11px;
}
@keyframes blink { 50% { opacity: 0; } }

/* code blocks */
.codeblock { margin: 7px 0; border: 1px solid var(--border); background: rgba(0,5,15,.9); }
.cb-head {
  display: flex; justify-content: space-between; padding: 4px 8px;
  background: rgba(0,212,255,.05); border-bottom: 1px solid var(--border);
}
.cb-lang { font-size: 8.5px; color: var(--cyan); letter-spacing: .1em; text-transform: uppercase; }
.cb-copy {
  background: transparent; border: 0; color: var(--text-2); cursor: pointer;
  font: 8.5px var(--mono); padding: 0 4px; letter-spacing: .1em;
}
.cb-copy:hover { color: var(--cyan); }
.codeblock pre { margin: 0; padding: 7px 9px; overflow-x: auto; }
.codeblock code { font: 10.5px/1.55 var(--mono); color: #a8d8f0; background: none; }

/* tool rows */
.tool-row {
  display: flex; align-items: center; gap: 7px; padding: 5px 7px;
  margin: 4px 0; font-size: 9.5px; border: 1px solid var(--border);
  background: rgba(0,20,40,.6); letter-spacing: .04em;
}
.tool-row .tname { color: #fff; }
.tool-row .tsum { color: var(--text-2); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tool-row .tres { margin-left: auto; }
.tool-row.ok  .tres { color: var(--ok); }
.tool-row.bad .tres { color: var(--hot); }
.tool-row.pending .tres { color: var(--text-dim); animation: pulse 1s ease infinite; }
@keyframes pulse { 50% { opacity: .3; } }

/* thinking */
.thinking-row {
  display: flex; align-items: center; gap: 8px; padding: 4px 0;
  font-size: 9px; color: var(--text-dim); letter-spacing: .12em;
}
.thinking-dots { display: flex; gap: 4px; }
.thinking-dots span {
  width: 5px; height: 5px; background: var(--cyan); border-radius: 50%;
  animation: bob .9s ease-in-out infinite;
}
.thinking-dots span:nth-child(2) { animation-delay: .15s; }
.thinking-dots span:nth-child(3) { animation-delay: .30s; }
@keyframes bob { 0%,100%{opacity:.2;transform:translateY(0)} 50%{opacity:1;transform:translateY(-3px)} }

/* plan */
.plan-box { margin: 7px 0; padding: 9px 11px; border: 1px solid rgba(255,170,0,.3); background: rgba(255,170,0,.03); }
.plan-box .ph { color: var(--warn); font-size: 9px; letter-spacing: .1em; margin-bottom: 5px; }
.plan-box ol { padding-left: 14px; color: var(--text-2); }
.plan-box li { margin: 2px 0; font-size: 10px; }

/* note / error */
.note-row { font-size: 9.5px; color: var(--text-dim); padding: 2px 0; }
.note-row.err { color: var(--hot); }

/* permission */
.perm-box { margin: 7px 0; padding: 9px 11px; border: 1px solid rgba(255,170,0,.4); border-left: 2px solid var(--warn); background: rgba(255,170,0,.03); }
.perm-box .pq { font-size: 10px; color: var(--text); margin-bottom: 7px; }
.perm-box code { color: var(--warn); background: rgba(255,170,0,.07); padding: 1px 4px; }
.perm-btns { display: flex; gap: 6px; }
.perm-btns button {
  border: 1px solid; padding: 5px 9px; cursor: pointer;
  font: 8.5px var(--mono); letter-spacing: .1em; text-transform: uppercase;
}
.perm-btns .yes    { background: rgba(0,255,136,.08);  color: var(--ok);  border-color: rgba(0,255,136,.4); }
.perm-btns .yes:hover { background: rgba(0,255,136,.18); }
.perm-btns .always { background: rgba(255,170,0,.08);  color: var(--warn); border-color: rgba(255,170,0,.4); }
.perm-btns .no     { background: transparent; color: var(--text-2); border-color: var(--border); }
.perm-decided      { font-size: 9px; color: var(--text-dim); }

/* ---- CMD AREA ---------------------------------------------------------- */
.cmd-area { flex-shrink: 0; padding: 6px 10px 7px; border-top: 1px solid var(--border); }
.cmd-box {
  display: flex; align-items: flex-end; gap: 7px;
  border: 1px solid var(--border-2); padding: 6px 9px;
  background: rgba(0,20,40,.75);
  box-shadow: 0 0 18px rgba(0,212,255,.08), inset 0 0 20px rgba(0,0,0,.4);
}
.cmd-box:focus-within {
  border-color: var(--cyan);
  box-shadow: 0 0 22px rgba(0,212,255,.22), inset 0 0 20px rgba(0,0,0,.4);
}
.cmd-prompt { color: var(--cyan); font-size: 13px; flex-shrink: 0; padding-bottom: 1px; }
.cmd-box textarea {
  flex: 1; background: transparent; border: 0; outline: 0;
  color: #c8f0ff; font: 11.5px/1.5 var(--mono); resize: none; max-height: 110px;
  letter-spacing: .03em;
}
.cmd-box textarea::placeholder { color: var(--text-dim); }
.exec-btn {
  flex-shrink: 0; padding: 4px 12px; border: 1px solid var(--cyan);
  background: rgba(0,212,255,.07); color: var(--cyan); font: 9px var(--mono);
  cursor: pointer; letter-spacing: .14em; text-transform: uppercase;
  transition: background .15s;
}
.exec-btn:hover { background: rgba(0,212,255,.2); }
.exec-btn:disabled { opacity: .3; cursor: default; }
.cmd-hint {
  text-align: center; font-size: 8px; color: var(--text-dim);
  letter-spacing: .08em; margin-top: 3px; text-transform: uppercase;
}

/* ---- RIGHT COLUMN ------------------------------------------------------ */
.right-col { display: flex; flex-direction: column; overflow: hidden; }
.radar-wrap { display: flex; justify-content: center; margin: 3px 0; }
#radarCanvas { display: block; }
.radar-label { font-size: 8px; color: var(--text-dim); text-align: center; }
#audioCanvas { display: block; width: 100%; }
.audio-labels { display: flex; justify-content: space-between; font-size: 8px; color: var(--text-dim); margin-top: 2px; }
.diag-panel { flex: 1; overflow: hidden; }
.diag-log { height: calc(100% - 22px); overflow-y: auto; font-size: 8px; line-height: 1.65; color: var(--text-2); }
.dlog-entry { padding: 1px 0; border-bottom: 1px solid rgba(0,212,255,.04); }
.dlog-entry.ok { color: var(--ok); }

/* ---- FOOTER ------------------------------------------------------------ */
.hud-footer {
  display: flex; justify-content: space-between; padding: 3px 14px;
  border-top: 1px solid var(--border); background: rgba(0,8,22,.6);
  flex-shrink: 0; font-size: 8px; letter-spacing: .07em;
}
.foot-left, .foot-right { display: flex; gap: 14px; align-items: center; }
.fstat { color: var(--text-2); }
.fstat.ok { color: var(--ok); }

/* ---- SESSIONS DRAWER --------------------------------------------------- */
.sessions-panel {
  position: fixed; top: 0; left: 0; bottom: 0; width: 256px;
  background: rgba(2,8,22,.97); border-right: 1px solid var(--border-2);
  z-index: 100; padding: 10px; display: flex; flex-direction: column;
  transform: translateX(-100%); transition: transform .2s ease;
}
.sessions-panel.open { transform: translateX(0); box-shadow: 0 0 40px rgba(0,212,255,.12); }
.sessions-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 0 10px; border-bottom: 1px solid var(--border); margin-bottom: 8px;
}
.chat-list-j { flex: 1; overflow-y: auto; }
.chat-item-j {
  display: flex; align-items: center; padding: 6px 7px;
  cursor: pointer; color: var(--text-2); border-bottom: 1px solid rgba(0,212,255,.06);
  font-size: 9.5px; letter-spacing: .05em; gap: 5px;
}
.chat-item-j:hover { background: rgba(0,212,255,.05); color: var(--text); }
.chat-item-j.active { color: var(--cyan); background: rgba(0,212,255,.07); }
.chat-item-j .ci-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ci-del-j { background: transparent; border: 0; color: var(--text-dim); cursor: pointer; font-size: 13px; padding: 0 3px; }
.ci-del-j:hover { color: var(--hot); }
.icon-btn { background: transparent; border: 0; color: var(--text-dim); cursor: pointer; font: 12px var(--mono); padding: 3px 5px; }
.icon-btn:hover { color: var(--cyan); }

/* ---- SETTINGS MODAL ---------------------------------------------------- */
.modal {
  position: fixed; inset: 0; background: rgba(0,4,14,.8);
  display: flex; align-items: center; justify-content: center; z-index: 200;
}
.modal.hidden { display: none; }
.modal-card {
  background: #020d1a; border: 1px solid var(--border-2);
  width: 430px; max-width: calc(100vw - 28px);
  box-shadow: 0 0 50px rgba(0,212,255,.15);
}
.modal-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 11px 15px; border-bottom: 1px solid var(--border);
}
.modal-body { padding: 14px 15px; display: flex; flex-direction: column; gap: 13px; }
.field { display: flex; flex-direction: column; gap: 5px; }
.field.row { flex-direction: row; align-items: center; justify-content: space-between; }
.field-label { font-size: 8.5px; color: var(--text-2); letter-spacing: .1em; text-transform: uppercase; }
.field-label em { color: var(--text-dim); font-style: normal; }
.field select, .field input[type=text] {
  background: rgba(0,20,40,.9); border: 1px solid var(--border);
  color: var(--text); padding: 7px 9px; font: 10.5px var(--mono); letter-spacing: .04em;
}
.field select:focus, .field input[type=text]:focus { outline: 0; border-color: var(--cyan); }
.field input[type=range] { accent-color: var(--cyan); width: 100%; }
.toggle { position: relative; width: 36px; height: 19px; flex-shrink: 0; }
.toggle input { position: absolute; opacity: 0; }
.toggle span {
  position: absolute; inset: 0; cursor: pointer;
  background: rgba(0,212,255,.08); border: 1px solid var(--border); transition: background .15s;
}
.toggle span::after {
  content: ''; position: absolute; width: 13px; height: 13px;
  background: var(--text-dim); top: 2px; left: 2px; transition: transform .15s;
}
.toggle input:checked + span { background: rgba(0,212,255,.22); border-color: var(--cyan); }
.toggle input:checked + span::after { transform: translateX(17px); background: var(--cyan); }
.modal-foot {
  padding: 11px 15px; border-top: 1px solid var(--border);
  display: flex; justify-content: flex-end; gap: 7px;
}
.btn-primary {
  background: rgba(0,212,255,.12); color: var(--cyan); border: 1px solid var(--cyan);
  padding: 6px 14px; font: 9px var(--mono); cursor: pointer; letter-spacing: .12em; text-transform: uppercase;
}
.btn-primary:hover { background: rgba(0,212,255,.26); }
.btn-ghost {
  background: transparent; color: var(--text-2); border: 1px solid var(--border);
  padding: 6px 12px; font: 9px var(--mono); cursor: pointer; letter-spacing: .1em; text-transform: uppercase;
}
.btn-ghost:hover { border-color: var(--text-dim); }

/* ---- BACKDROP + SCROLLBARS --------------------------------------------- */
.backdrop { position: fixed; inset: 0; z-index: 99; background: rgba(0,4,14,.55); }
.backdrop.hidden { display: none; }
::-webkit-scrollbar { width: 3px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(0,212,255,.2); }
::-webkit-scrollbar-thumb:hover { background: rgba(0,212,255,.4); }
"""

_JS = r"""
// ============================================================
// J.A.R.V.I.S. — Neural Interface JavaScript
// ============================================================
const $ = s => document.querySelector(s);
const log = $('#log'), input = $('#input'), sendBtn = $('#send');
let state = { chats: [], currentId: null, settings: {}, busy: false };

// ---- CLOCK -------------------------------------------------------------------
function updateClock() {
  const n = new Date();
  const pad = v => String(v).padStart(2,'0');
  $('#jClock').textContent =
    pad(n.getHours())+':'+pad(n.getMinutes())+':'+pad(n.getSeconds())+'.'+
    String(Math.floor(n.getMilliseconds()/10)).padStart(2,'0');
  const days   = ['SUN','MON','TUE','WED','THU','FRI','SAT'];
  const months = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
  $('#jDate').textContent = days[n.getDay()]+' '+n.getDate()+' '+months[n.getMonth()]+' '+n.getFullYear();
}
setInterval(updateClock, 50); updateClock();

// ---- VITALS ------------------------------------------------------------------
const VITALS = [
  { id:0, base:54.7, range:4, unit:'%',     pctOf:100 },
  { id:1, base:40.1, range:6, unit:'%',     pctOf:100 },
  { id:2, base:12.7, range:2, unit:'ms',    pctOf:50  },
  { id:3, base:90.2, range:2, unit:'%',     pctOf:100 },
  { id:4, base:39.1, range:1, unit:'°C', pctOf:80 },
  { id:5, base:5.07, range:0.5, unit:'Gb/s', pctOf:10, dec:2 },
];
function tickVitals() {
  VITALS.forEach(v => {
    const val = v.base + (Math.random()-.5)*v.range;
    const pct = Math.min(100, Math.max(2, (val/v.pctOf)*100));
    const fill  = document.getElementById('v'+v.id);
    const label = document.getElementById('vv'+v.id);
    if (fill)  fill.style.width = pct.toFixed(1)+'%';
    if (label) label.textContent = (v.dec ? val.toFixed(v.dec) : val.toFixed(1)) + v.unit;
  });
}
tickVitals();
setInterval(tickVitals, 2200);

// ---- TELEMETRY / DIAG --------------------------------------------------------
const TELE_POOL = [
  'attention.head norm applied','sensor.poll quantized',
  'gpu.thermal sync complete','tokenizer.run buffer clear',
  'mlsync.decode quantized','embedding.cache task acquired',
  'vector.query done','allocator.buffer clean',
  'net.handshake ok','memory.result ok',
  'GRU vector.query quantized','context.load throttle 0.06',
  'audio.stream norm applied','mlsync ok',
];
function addTeleEntry(text, ok) {
  const el = $('#teleLog'); if (!el) return;
  const n = new Date();
  const pad = v => String(v).padStart(2,'0');
  const ts = pad(n.getHours())+':'+pad(n.getMinutes())+':'+pad(n.getSeconds());
  const d = document.createElement('div');
  d.className = 'telog-entry'+(ok?' ok':'');
  d.textContent = ts+' '+(ok?'OK':'SYS')+' '+text;
  el.insertBefore(d, el.firstChild);
  while (el.children.length > 22) el.removeChild(el.lastChild);
}
function addDiagEntry(text, ok) {
  const el = $('#diagLog'); if (!el) return;
  const n = new Date();
  const pad = v => String(v).padStart(2,'0');
  const ts = pad(n.getHours())+':'+pad(n.getMinutes())+':'+pad(n.getSeconds());
  const d = document.createElement('div');
  d.className = 'dlog-entry'+(ok?' ok':'');
  d.textContent = ts+' '+(ok?'OK':'SYS')+' '+text;
  el.insertBefore(d, el.firstChild);
  while (el.children.length > 18) el.removeChild(el.lastChild);
}
// Seed initial log entries
const DIAG_SEED = [
  ['gpu.thermal sync complete', true],['GRU vector.query quantized',false],
  ['vector.query done',false],['tokenizer.run buffer clear',true],
  ['tokenizer.cache quantized',false],['allocator.buffer clean',true],
  ['net.handshake latency low',false],['memory.result ok',true],
];
DIAG_SEED.slice().reverse().forEach(([t,ok]) => addDiagEntry(t,ok));
const TELE_SEED = [
  ['gpu.thermal.sync completed',false],['attention.head[2] norm applied',true],
  ['attention.head[1] norm applied',true],['sensor.poll quantized',true],
  ['tokens - 250k taken',true],['audio.stream norm applied',false],
  ['context.load throttle 0.06',true],['mlsync ok',true],
  ['mlsync.decode quantized',false],['sensor.poll sync complete',true],
  ['embedding.cache task acquired',false],
];
TELE_SEED.slice().reverse().forEach(([t,ok]) => addTeleEntry(t,ok));
setInterval(() => {
  const t = TELE_POOL[Math.floor(Math.random()*TELE_POOL.length)];
  addTeleEntry(t, Math.random()>.28);
}, 2800 + Math.random()*1800);

// ---- ORB CANVAS --------------------------------------------------------------
(function() {
  const canvas = $('#orbCanvas'); if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let W, H, cx, cy, particles = [], t = 0;

  function resize() {
    const p = canvas.parentElement;
    W = canvas.width  = p.clientWidth  || 400;
    H = canvas.height = p.clientHeight || 240;
    cx = W/2; cy = H/2;
  }

  function makeParticle() {
    const theta = Math.random()*Math.PI*2;
    const phi   = Math.random()*Math.PI;
    const r     = 40 + Math.random()*35;
    return {
      x: cx + r*Math.sin(phi)*Math.cos(theta),
      y: cy + r*Math.sin(phi)*Math.sin(theta)*0.42,
      z: Math.cos(phi),
      vx: (Math.random()-.5)*0.28,
      vy: (Math.random()-.5)*0.28,
      life: Math.random(),
      decay: 0.008 + Math.random()*0.018,
      size: 0.6 + Math.random()*1.8,
      alpha: 0.35 + Math.random()*0.65,
    };
  }

  function resetParticle(p) {
    const theta = Math.random()*Math.PI*2;
    const phi   = Math.random()*Math.PI;
    const r     = 38 + Math.random()*38;
    p.x=cx+r*Math.sin(phi)*Math.cos(theta);
    p.y=cy+r*Math.sin(phi)*Math.sin(theta)*0.42;
    p.z=Math.cos(phi); p.life=1;
  }

  function initParticles() {
    particles = [];
    for (let i=0;i<180;i++) { const p=makeParticle(); particles.push(p); }
  }

  const ORBITALS = [
    {r:96, s:0.7,  sz:3, ph:0},
    {r:96, s:0.7,  sz:3, ph:Math.PI},
    {r:78, s:-1.1, sz:2, ph:Math.PI/2},
    {r:112,s:0.5,  sz:2, ph:Math.PI/3},
    {r:78, s:-1.1, sz:2, ph:Math.PI*1.5},
  ];

  function draw() {
    ctx.clearRect(0,0,W,H);
    t += 0.012;

    // outer glow halos
    for (let r=110;r>=15;r-=16) {
      const g = ctx.createRadialGradient(cx,cy,r*0.45,cx,cy,r);
      g.addColorStop(0, 'rgba(0,180,255,'+(0.025+(110-r)*0.0008)+')');
      g.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.beginPath(); ctx.arc(cx,cy,r,0,Math.PI*2);
      ctx.fillStyle=g; ctx.fill();
    }

    // main glow
    const mg = ctx.createRadialGradient(cx,cy,0,cx,cy,58);
    mg.addColorStop(0,  'rgba(180,235,255,0.85)');
    mg.addColorStop(0.25,'rgba(0,180,255,0.55)');
    mg.addColorStop(0.6, 'rgba(0,80,200,0.25)');
    mg.addColorStop(1,   'rgba(0,0,0,0)');
    ctx.beginPath(); ctx.arc(cx,cy,58,0,Math.PI*2);
    ctx.fillStyle=mg; ctx.fill();

    // bright inner core
    const ic = ctx.createRadialGradient(cx,cy,0,cx,cy,18);
    ic.addColorStop(0,'rgba(255,255,255,1)');
    ic.addColorStop(0.55,'rgba(140,220,255,0.7)');
    ic.addColorStop(1,'rgba(0,140,255,0)');
    ctx.beginPath(); ctx.arc(cx,cy,18,0,Math.PI*2);
    ctx.fillStyle=ic; ctx.fill();

    // particles
    particles.forEach(p => {
      p.x += p.vx; p.y += p.vy; p.life -= p.decay;
      if (p.life<=0) resetParticle(p);
      const a = Math.max(0,p.life)*p.alpha;
      const br = 0.5+p.z*0.5;
      ctx.beginPath(); ctx.arc(p.x,p.y,p.size,0,Math.PI*2);
      ctx.fillStyle='rgba('+Math.round(80+br*175)+','+Math.round(175+br*80)+',255,'+a+')';
      ctx.fill();
    });

    // orbiting satellites
    ORBITALS.forEach(o => {
      const a = t*o.s+o.ph;
      const ox = cx+o.r*Math.cos(a);
      const oy = cy+o.r*0.38*Math.sin(a);
      ctx.beginPath(); ctx.arc(ox,oy,o.sz,0,Math.PI*2);
      ctx.fillStyle='rgba(0,220,255,0.95)';
      ctx.shadowColor='#00d4ff'; ctx.shadowBlur=10;
      ctx.fill(); ctx.shadowBlur=0;
    });

    requestAnimationFrame(draw);
  }

  window.addEventListener('resize', () => { resize(); initParticles(); });
  resize(); initParticles(); draw();
})();

// ---- RADAR -------------------------------------------------------------------
(function() {
  const canvas = $('#radarCanvas'); if (!canvas) return;
  canvas.width=140; canvas.height=140;
  const ctx=canvas.getContext('2d'), cx=70, cy=70, r=60;
  let angle=0;
  const blips=[
    {a:0.82,d:0.42,fade:1}, {a:2.15,d:0.68,fade:0.6},
    {a:3.82,d:0.31,fade:0.85},{a:5.20,d:0.58,fade:0.45},
  ];

  function drawRadar() {
    ctx.clearRect(0,0,140,140);
    ctx.strokeStyle='rgba(0,212,255,0.13)'; ctx.lineWidth=1;
    for(let i=1;i<=4;i++){
      ctx.beginPath(); ctx.arc(cx,cy,r*i/4,0,Math.PI*2); ctx.stroke();
    }
    ctx.strokeStyle='rgba(0,212,255,0.08)';
    ctx.beginPath();ctx.moveTo(cx-r,cy);ctx.lineTo(cx+r,cy);ctx.stroke();
    ctx.beginPath();ctx.moveTo(cx,cy-r);ctx.lineTo(cx,cy+r);ctx.stroke();

    // sweep trail (draw several fading sectors)
    for(let i=0;i<20;i++){
      const a0=angle-0.03*i, a1=angle-0.03*(i+1);
      ctx.beginPath();
      ctx.moveTo(cx,cy);
      ctx.arc(cx,cy,r,a0,a1,false);
      ctx.closePath();
      ctx.fillStyle='rgba(0,255,136,'+(0.18*(1-i/20))+')';
      ctx.fill();
    }
    // sweep line
    ctx.beginPath(); ctx.moveTo(cx,cy);
    ctx.lineTo(cx+r*Math.cos(angle), cy+r*Math.sin(angle));
    ctx.strokeStyle='rgba(0,255,136,0.9)'; ctx.lineWidth=1.5; ctx.stroke();

    // blips
    blips.forEach(b=>{
      let da=((angle-b.a)%(Math.PI*2)+Math.PI*2)%(Math.PI*2);
      if(da>Math.PI) da=Math.PI*2-da;
      const fade=b.fade*Math.max(0,1-da*0.6);
      if(fade<0.04) return;
      ctx.beginPath();
      ctx.arc(cx+r*b.d*Math.cos(b.a), cy+r*b.d*Math.sin(b.a), 3, 0, Math.PI*2);
      ctx.fillStyle='rgba(0,255,136,'+fade+')';
      ctx.shadowColor='#00ff88'; ctx.shadowBlur=fade>0.4?7:0;
      ctx.fill(); ctx.shadowBlur=0;
    });
    angle+=0.028;
    requestAnimationFrame(drawRadar);
  }
  drawRadar();
})();

// ---- AUDIO WAVE --------------------------------------------------------------
(function() {
  const canvas = $('#audioCanvas'); if (!canvas) return;
  const ctx=canvas.getContext('2d');
  let off=0;
  function drawAudio(){
    const W=canvas.offsetWidth||180, H=50;
    if(canvas.width!==W) canvas.width=W;
    canvas.height=H;
    ctx.clearRect(0,0,W,H);
    ctx.beginPath();
    ctx.strokeStyle='rgba(0,212,255,0.75)'; ctx.lineWidth=1.4;
    for(let x=0;x<=W;x++){
      const y=H/2
        +Math.sin((x+off)*0.07)*10
        +Math.sin((x+off)*0.14+1)*5
        +Math.sin((x+off)*0.035+2.2)*14;
      x===0?ctx.moveTo(x,y):ctx.lineTo(x,y);
    }
    ctx.stroke();
    off+=2;
    requestAnimationFrame(drawAudio);
  }
  drawAudio();
})();

// ---- HELPERS -----------------------------------------------------------------
function esc(s){
  return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}
function md(src){
  const blocks=[];
  let s=(src||'').replace(/```(\w*)\n?([\s\S]*?)```/g,(m,lang,code)=>{
    blocks.push(
      '<div class="codeblock"><div class="cb-head"><span class="cb-lang">'+(lang||'text')+'</span>'+
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

// ---- RENDERING ---------------------------------------------------------------
function showEmpty(){
  clearLog();
  const e=document.createElement('div'); e.className='j-empty';
  e.innerHTML='<div>NEURAL INTERFACE READY</div><div style="font-size:8.5px;margin-top:4px">ENTER COMMAND TO INITIALIZE SEQUENCE</div>';
  log.appendChild(e);
}
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
  const row=document.createElement('div');
  row.className='tool-row'+(done?'':' pending');
  row.innerHTML=
    '<span style="color:var(--cyan)">&#9889;</span>'+
    '<span class="tname">'+esc(t.name||'')+'</span>'+
    (t.summary?'<span class="tsum">'+esc(t.summary)+'</span>':'')+
    (done?'':'<span class="tres">EXECUTING&#8230;</span>');
  getThread().appendChild(row); scrollDown();
  addDiagEntry((t.name||'?')+' invoked', false);
  return row;
}
function addNote(text, isErr){
  const n=document.createElement('div');
  n.className='note-row'+(isErr?' err':'');
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
  [['yes','APPROVE','approved'],['always','ALWAYS ALLOW','always allowed'],['no','DENY','denied']].forEach(([a,lbl,past])=>{
    const b=document.createElement('button');
    b.className=a; b.textContent=lbl;
    b.onclick=()=>answer(a,past);
    btns.appendChild(b);
  });
  box.appendChild(btns);
  getThread().appendChild(box); scrollDown();
}

// ---- LIVE TURN ---------------------------------------------------------------
let live={body:null,raw:'',toolRow:null,thinking:null};

function showThinking(){
  const t=document.createElement('div'); t.className='thinking-row';
  t.innerHTML=avatarHTML()+
    '<span style="letter-spacing:.14em;font-size:8.5px">PROCESSING</span>'+
    '<div class="thinking-dots"><span></span><span></span><span></span></div>';
  getThread().appendChild(t); scrollDown();
  live.thinking=t;
}
function clearThinking(){
  if(live.thinking){live.thinking.remove();live.thinking=null;}
}
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
      res.textContent=(d.ok?'✓ ':'✗ ')+(d.first_line||'').slice(0,80);
      if(!res.parentNode) live.toolRow.appendChild(res);
      live.toolRow=null;
      addDiagEntry('tool '+(d.ok?'success':'failed'), d.ok);
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

// code-block copy (event delegation)
log.addEventListener('click',e=>{
  const btn=e.target.closest('.cb-copy'); if(!btn) return;
  const code=btn.closest('.codeblock').querySelector('pre code');
  navigator.clipboard.writeText(code.textContent||'').then(()=>{
    btn.textContent='COPIED'; setTimeout(()=>{btn.textContent='COPY';},1400);
  });
});

// ---- SESSIONS ----------------------------------------------------------------
function renderChats(){
  const list=$('#chatList'); list.innerHTML='';
  if(!state.chats.length){
    list.innerHTML='<div style="color:var(--text-dim);font-size:9px;padding:8px">NO MISSION LOGS</div>';
    return;
  }
  state.chats.forEach(c=>{
    const item=document.createElement('div');
    item.className='chat-item-j'+(c.id===state.currentId?' active':'');
    item.innerHTML=
      '<span style="color:var(--cyan);font-size:9px">&#9658;</span>'+
      '<span class="ci-title">'+esc(c.title)+'</span>'+
      '<button class="ci-del-j" title="Delete">&times;</button>';
    item.querySelector('.ci-title').onclick=()=>loadChat(c.id);
    item.querySelector('.ci-del-j').onclick=e=>{
      e.stopPropagation();
      if(confirm('DELETE MISSION LOG "'+c.title+'"?')) deleteChat(c.id);
    };
    list.appendChild(item);
  });
}
function setCurrent(cur){
  state.currentId=cur.id;
  const sid=$('#jSession');
  if(sid) sid.textContent=(cur.id||'--------').slice(0,8).toUpperCase();
  const obj=$('#objectiveText');
  if(obj) obj.textContent=(cur.title||'AWAITING DIRECTIVE').toUpperCase();
  clearLog();
  if(!cur.messages||!cur.messages.length){showEmpty();return;}
  cur.messages.forEach(m=>{
    if(m.role==='user') addUser(m.content);
    else addAssistant(md(m.content),m.tools);
  });
  scrollDown();
}

// ---- NETWORK -----------------------------------------------------------------
async function api(path,body){
  const r=await fetch(path,{
    method:body?'POST':'GET',
    headers:{'Content-Type':'application/json'},
    body:body?JSON.stringify(body):undefined,
  });
  return r.json();
}
async function boot(){
  const b=await api('/api/bootstrap');
  state.chats=b.chats; state.settings=b.settings;
  const mb=$('#modelBadge');
  if(mb) mb.textContent='● '+(b.model||'').toUpperCase();
  const vs=$('#versionSpan');
  if(vs) vs.textContent=b.version||'--';
  renderChats(); setCurrent(b.current);
}
async function newChat(){
  const r=await api('/api/chats/new',{});
  state.chats=r.chats; renderChats(); setCurrent(r.current);
  closeSessions(); input.focus();
}
async function loadChat(id){
  const r=await api('/api/chats/load',{id});
  state.chats=r.chats; renderChats(); setCurrent(r.current);
  closeSessions();
}
async function deleteChat(id){
  const r=await api('/api/chats/delete',{id});
  state.chats=r.chats; renderChats(); setCurrent(r.current);
}
async function refreshChats(){
  const b=await api('/api/bootstrap');
  state.chats=b.chats;
  const obj=$('#objectiveText');
  if(obj) obj.textContent=(b.current.title||'AWAITING DIRECTIVE').toUpperCase();
  renderChats();
}

// ---- SESSIONS DRAWER ---------------------------------------------------------
function openSessions(){
  $('#sessionsPanel').classList.add('open');
  $('#backdrop').classList.remove('hidden');
}
function closeSessions(){
  $('#sessionsPanel').classList.remove('open');
  $('#backdrop').classList.add('hidden');
}

// ---- SEND --------------------------------------------------------------------
function finishTurn(){
  state.busy=false; sendBtn.disabled=false;
  input.disabled=false; input.focus(); refreshChats();
}
async function send(text){
  if(state.busy) return;
  state.busy=true; sendBtn.disabled=true;
  if(log.querySelector('.j-empty')) clearLog();
  addUser(text);
  live={body:null,raw:'',toolRow:null,thinking:null};
  showThinking();
  addTeleEntry('command received — processing',false);
  let res;
  try{
    res=await fetch('/api/chat',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:text}),
    });
  } catch(e){clearThinking();addNote('CONNECTION FAILURE',true);finishTurn();return;}
  const reader=res.body.getReader(), dec=new TextDecoder();
  let buf='';
  while(true){
    let chunk;
    try{chunk=await reader.read();}catch(e){break;}
    if(chunk.done) break;
    buf+=dec.decode(chunk.value,{stream:true});
    let i;
    while((i=buf.indexOf('\n\n'))>=0){
      const line=buf.slice(0,i); buf=buf.slice(i+2);
      if(line.startsWith('data: ')){
        try{handle(JSON.parse(line.slice(6)));}catch(e){}
      }
    }
  }
  clearThinking();
  if(state.busy) finishTurn();
}

// ---- SETTINGS ----------------------------------------------------------------
function openSettings(){
  closeSessions();
  const s=state.settings;
  const sel=$('#setModel'); sel.innerHTML='';
  (s.models&&s.models.length?s.models:[s.model]).forEach(m=>{
    const o=document.createElement('option');
    o.value=m; o.textContent=m; if(m===s.model) o.selected=true;
    sel.appendChild(o);
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
  const body={
    model:$('#setModel').value,
    user_name:$('#setName').value,
    temperature:parseFloat($('#setTemp').value),
    stream:$('#setStream').checked,
    yolo:$('#setYolo').checked,
  };
  state.settings=await api('/api/settings',body);
  const mb=$('#modelBadge');
  if(mb) mb.textContent='● '+(state.settings.model||'').toUpperCase();
  closeSettings();
}

// ---- COMPOSER ----------------------------------------------------------------
function autoGrow(){
  input.style.height='auto';
  input.style.height=Math.min(input.scrollHeight,110)+'px';
}
function submit(){
  const text=input.value.trim();
  if(!text||state.busy) return;
  input.value=''; autoGrow(); send(text);
}
input.addEventListener('input',autoGrow);
input.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();submit();}
});
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
$('#settingsModal').addEventListener('click',e=>{if(e.target.id==='settingsModal') closeSettings();});
document.addEventListener('keydown',e=>{
  if(e.key!=='Escape') return;
  if(!$('#settingsModal').classList.contains('hidden')) closeSettings();
  else closeSessions();
});

boot();
"""
