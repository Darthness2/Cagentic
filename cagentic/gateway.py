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
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
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
            except (BrokenPipeError, ConnectionResetError):
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
# The web UI deliberately mirrors the terminal CLI: warm-dusk palette, a
# monospace face throughout, the ✦ wordmark, and the same markers the REPL
# uses (✦ assistant, ↳ tool, ❀ plan, · note, ▲ warn, ✗ error).

_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<title>cagentic</title>
<link rel="stylesheet" href="/app.css" />
</head>
<body>
<div id="app">
  <aside id="sidebar">
    <div class="brand"><span class="spark">&#10022;</span><span class="word">cagentic</span></div>
    <button id="newChat" class="new-chat"><span class="plus">+</span> new chat</button>
    <div class="chats-label">chats</div>
    <div id="chatList" class="chat-list"></div>
    <div class="sidebar-foot">
      <button id="openSettings" class="foot-btn">
        <span class="gear">&#9881;</span> settings
      </button>
      <div id="footMeta" class="foot-meta"></div>
    </div>
  </aside>
  <main id="main">
    <header id="topbar">
      <button id="menuBtn" class="menu-btn" aria-label="Open chats">
        <svg viewBox="0 0 24 24" width="20" height="20"><path
          d="M3 6h18M3 12h18M3 18h18" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round"/></svg>
      </button>
      <div id="chatTitle">new chat</div>
      <div id="modelChip" class="chip"></div>
    </header>
    <div id="log" class="log"></div>
    <div id="composer" class="composer">
      <div class="composer-box">
        <span class="prompt-mark">&#10022;</span>
        <textarea id="input" rows="1" placeholder="message cagentic…"></textarea>
        <button id="send" class="send" title="Send" aria-label="Send">
          <svg viewBox="0 0 24 24" width="17" height="17"><path
            d="M12 19V5M12 5l-6 6M12 5l6 6" fill="none" stroke="currentColor"
            stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
      </div>
      <div class="composer-hint">
        runs entirely on your machine &mdash; browse, files, notes, reminders
      </div>
    </div>
  </main>
</div>
<div id="backdrop" class="backdrop hidden"></div>

<div id="settingsModal" class="modal hidden">
  <div class="modal-card">
    <div class="modal-head">
      <h2><span class="spark">&#10022;</span> settings</h2>
      <button id="closeSettings" class="icon-btn">&times;</button>
    </div>
    <div class="modal-body">
      <label class="field">
        <span class="field-label">model</span>
        <select id="setModel"></select>
      </label>
      <label class="field">
        <span class="field-label">your name</span>
        <input id="setName" type="text" placeholder="what should I call you?" />
      </label>
      <label class="field">
        <span class="field-label">temperature <em id="tempVal">0.4</em></span>
        <input id="setTemp" type="range" min="0" max="1.5" step="0.05" />
      </label>
      <label class="field row">
        <span class="field-label">stream responses</span>
        <input id="setStream" type="checkbox" class="switch" />
      </label>
      <label class="field row">
        <span class="field-label">auto-approve tools
          <em>skip approval prompts</em></span>
        <input id="setYolo" type="checkbox" class="switch" />
      </label>
    </div>
    <div class="modal-foot">
      <button id="saveSettings" class="primary">save</button>
    </div>
  </div>
</div>

<script src="/app.js"></script>
</body>
</html>
"""

_CSS = """
/* ===== cagentic gateway — terminal-styled web app ======================= */
:root {
  /* warm dusk — the CLI palette, tuned for screens */
  --bg:        #16121b;
  --bg-2:      #120f17;
  --panel:     #1f1926;
  --raised:    #2a2233;
  --border:    #362c42;
  --border-2:  #473a55;
  --text:      #e9e1ec;
  --muted:     #9a8fa6;
  --soft:      #6b6076;
  --dusk:      #c79ec7;   /* orchid  — greetings, info */
  --glow:      #ffb892;   /* peach   — the spark, prompt, you */
  --plum:      #a384a3;   /* plum    — tool marker, structure */
  --gold:      #e3bd6e;   /* gold    — plans, code, accents */
  --ok:        #93c58f;
  --warn:      #f0b266;
  --err:       #e29696;
  --mono: "JetBrains Mono","SF Mono","Cascadia Code","Fira Code",
          ui-monospace,"Roboto Mono",Menlo,Consolas,monospace;
  --radius: 11px;
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font: 14px/1.66 var(--mono);
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
#app {
  display: grid; grid-template-columns: 264px 1fr;
  height: 100vh; height: 100dvh;
}
::selection { background: rgba(255,184,146,.26); }

/* ---- sidebar ---------------------------------------------------------- */
#sidebar {
  background: var(--bg-2);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
  padding: 16px 12px;
}
.brand {
  display: flex; align-items: center; gap: 9px;
  padding: 6px 8px 18px;
}
.brand .spark { color: var(--gold); font-size: 16px; }
.brand .word {
  color: var(--glow); font-weight: 600;
  letter-spacing: .26em; font-size: 14.5px;
}
.new-chat {
  display: flex; align-items: center; gap: 9px;
  width: 100%; padding: 10px 12px; cursor: pointer;
  background: var(--panel); color: var(--text);
  border: 1px solid var(--border); border-radius: 9px;
  font: inherit; transition: border-color .14s, background .14s;
}
.new-chat:hover { border-color: var(--border-2); background: var(--raised); }
.new-chat .plus { color: var(--gold); font-size: 15px; }
.chats-label {
  font-size: 11px; letter-spacing: .14em; color: var(--soft);
  padding: 20px 8px 8px;
}
.chat-list { flex: 1; overflow-y: auto; margin: 0 -4px; padding: 0 4px; }
.chat-item {
  display: flex; align-items: center; gap: 8px; position: relative;
  padding: 8px 10px; border-radius: 8px; cursor: pointer;
  color: var(--muted); transition: background .12s, color .12s;
}
.chat-item:hover { background: var(--panel); color: var(--text); }
.chat-item.active { background: var(--raised); color: var(--text); }
.chat-item.active::before {
  content: ""; position: absolute; left: 0; top: 8px; bottom: 8px;
  width: 2px; border-radius: 2px; background: var(--glow);
}
.chat-item .ci-title {
  flex: 1; min-width: 0; font-size: 13px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.chat-item .ci-del {
  opacity: 0; border: 0; background: transparent; color: var(--soft);
  cursor: pointer; font-size: 15px; line-height: 1; padding: 0 3px;
  transition: opacity .12s, color .12s;
}
.chat-item:hover .ci-del { opacity: 1; }
.chat-item .ci-del:hover { color: var(--err); }
.sidebar-foot { border-top: 1px solid var(--border); padding-top: 10px; margin-top: 8px; }
.foot-btn {
  display: flex; align-items: center; gap: 9px; width: 100%;
  background: transparent; color: var(--muted); border: 0; cursor: pointer;
  padding: 8px 10px; border-radius: 8px; font: inherit;
  transition: background .12s, color .12s;
}
.foot-btn:hover { background: var(--panel); color: var(--text); }
.foot-meta { font-size: 11px; color: var(--soft); padding: 7px 10px 0; letter-spacing: .04em; }

/* ---- main ------------------------------------------------------------- */
#main { display: flex; flex-direction: column; min-width: 0; background: var(--bg); }
#topbar {
  display: flex; align-items: center; gap: 12px;
  padding: 13px 24px; border-bottom: 1px solid var(--border);
}
#chatTitle {
  flex: 1; min-width: 0; font-size: 13px; color: var(--muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.chip {
  font-size: 12px; color: var(--muted);
  background: var(--panel); border: 1px solid var(--border);
  padding: 4px 11px; border-radius: 999px; white-space: nowrap;
}
.chip::before {
  content: "●"; color: var(--ok); font-size: 8px;
  vertical-align: 2px; margin-right: 7px;
}
.menu-btn {
  display: none; align-items: center; justify-content: center;
  width: 36px; height: 36px; margin-left: -6px; flex-shrink: 0;
  background: transparent; border: 0; border-radius: 8px;
  color: var(--muted); cursor: pointer;
}
.menu-btn:hover { background: var(--panel); color: var(--text); }

.log { flex: 1; overflow-y: auto; padding: 26px 24px 10px; }
.thread { max-width: 720px; margin: 0 auto; }

/* ---- messages --------------------------------------------------------- */
.row { margin-bottom: 20px; }

.row.user {
  display: flex; gap: 10px;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 11px 14px;
}
.row.user .umark { color: var(--glow); flex-shrink: 0; }
.row.user .utext { white-space: pre-wrap; word-wrap: break-word; }

.row.assistant .who {
  display: flex; align-items: center; gap: 8px;
  font-size: 11px; letter-spacing: .16em; color: var(--soft);
  margin-bottom: 9px;
}
.row.assistant .who .spark { color: var(--glow); font-size: 13px; letter-spacing: 0; }
.row.assistant .body { color: var(--text); }
.body p { margin: 0 0 11px; }
.body p:last-child { margin-bottom: 0; }
.body h2, .body h3 {
  font-size: 14px; font-weight: 600; color: var(--glow);
  margin: 17px 0 9px; letter-spacing: .01em;
}
.body ul { margin: 9px 0; padding-left: 4px; list-style: none; }
.body li { margin: 4px 0; padding-left: 18px; position: relative; }
.body li::before {
  content: "–"; color: var(--dusk); position: absolute; left: 2px;
}
.body a { color: var(--glow); text-decoration: none; border-bottom: 1px solid var(--border-2); }
.body a:hover { border-color: var(--glow); }
.body code {
  background: var(--raised); color: var(--gold);
  padding: 1.5px 5px; border-radius: 5px; font-size: 13px;
}
.body pre {
  background: var(--panel); border: 1px solid var(--border);
  border-left: 2.5px solid var(--plum);
  border-radius: 8px; padding: 12px 14px; overflow-x: auto; margin: 11px 0;
}
.body pre code { background: none; padding: 0; color: var(--text); }
.cursor::after {
  content: "▌"; color: var(--glow); margin-left: 1px;
  animation: blink 1.1s steps(2) infinite;
}
@keyframes blink { 50% { opacity: 0; } }

/* tool rows — '↳ name  summary    ✓ result' */
.tool {
  display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;
  font-size: 12.5px; margin: 8px 0; padding: 7px 12px;
  background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
}
.tool .arrow { color: var(--plum); }
.tool .tname { color: var(--dusk); }
.tool .tsum { color: var(--soft); }
.tool .tres { margin-left: auto; }
.tool.ok .tres { color: var(--ok); }
.tool.bad .tres { color: var(--err); }
.tool.pending .tres::after { content: "running…"; color: var(--soft); }

.note { font-size: 12.5px; margin: 8px 0; color: var(--dusk); }
.note::before { content: "· "; }
.note.err { color: var(--err); }
.note.err::before { content: "✗ "; }

/* plan panel — '❀ here's my plan' */
.plan {
  background: var(--panel); border: 1px solid var(--border);
  border-left: 2.5px solid var(--gold); border-radius: 8px;
  padding: 12px 15px; margin: 11px 0;
}
.plan .ph { color: var(--gold); font-size: 12.5px; margin-bottom: 7px; letter-spacing: .03em; }
.plan ol { margin: 0; padding-left: 22px; color: var(--muted); font-size: 13px; }
.plan li { margin: 3px 0; }
.plan li::marker { color: var(--gold); }

/* permission card */
.perm {
  background: var(--raised); border: 1px solid var(--border-2);
  border-left: 2.5px solid var(--glow);
  border-radius: var(--radius); padding: 14px 16px; margin: 12px 0;
}
.perm .pq { margin-bottom: 12px; font-size: 13px; color: var(--text); }
.perm .pq code {
  background: var(--bg); color: var(--glow);
  padding: 2px 7px; border-radius: 5px;
}
.perm .pbtns { display: flex; gap: 8px; flex-wrap: wrap; }
.perm button {
  border: 1px solid transparent; border-radius: 8px;
  padding: 7px 15px; cursor: pointer; font: 600 12.5px var(--mono);
  transition: filter .12s;
}
.perm button:hover { filter: brightness(1.13); }
.perm .yes { background: var(--ok); color: #15240f; }
.perm .always { background: var(--gold); color: #281e08; }
.perm .no { background: transparent; color: var(--muted); border-color: var(--border-2); }
.perm .decided { color: var(--soft); font-size: 12.5px; }

/* ---- empty state — the CLI banner, on the web ------------------------- */
.empty {
  min-height: 100%; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 26px; padding: 30px 0;
}
.banner {
  background: var(--panel); border: 1px solid var(--border-2);
  border-radius: 16px; padding: 30px 36px; width: 420px; max-width: 88vw;
}
.banner .b-mark { color: var(--gold); font-size: 22px; }
.banner .b-word {
  color: var(--glow); font-weight: 600; font-size: 21px;
  letter-spacing: .34em; margin-top: 12px;
}
.banner .b-tag { color: var(--muted); font-size: 12.5px; margin-top: 8px; }
.banner .b-greet {
  color: var(--dusk); font-size: 13.5px; margin-top: 20px;
  padding-top: 16px; border-top: 1px solid var(--border);
}
.suggestions {
  display: flex; flex-wrap: wrap; gap: 9px;
  justify-content: center; max-width: 540px;
}
.suggestion {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 9px; padding: 9px 13px; cursor: pointer;
  font-size: 12.5px; color: var(--muted);
  transition: border-color .14s, color .14s, background .14s;
}
.suggestion::before { content: "› "; color: var(--plum); }
.suggestion:hover { border-color: var(--border-2); color: var(--text); background: var(--raised); }

/* ---- composer --------------------------------------------------------- */
.composer { padding: 8px 24px 18px; }
.composer-box {
  max-width: 720px; margin: 0 auto;
  display: flex; align-items: flex-end; gap: 10px;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 13px; padding: 9px 9px 9px 14px;
  transition: border-color .14s;
}
.composer-box:focus-within { border-color: var(--border-2); }
.prompt-mark { color: var(--glow); padding: 6px 0; user-select: none; }
.composer-box textarea {
  flex: 1; resize: none; background: transparent; border: 0; outline: 0;
  color: var(--text); font: inherit; padding: 6px 0; max-height: 200px;
  line-height: 1.6;
}
.composer-box textarea::placeholder { color: var(--soft); }
.send {
  flex-shrink: 0; width: 34px; height: 34px; border-radius: 9px;
  border: 0; cursor: pointer; background: var(--glow); color: #2a1808;
  display: flex; align-items: center; justify-content: center;
  transition: filter .12s, opacity .12s;
}
.send:hover { filter: brightness(1.08); }
.send:disabled { opacity: .4; cursor: default; }
.composer-hint {
  max-width: 720px; margin: 9px auto 0; text-align: center;
  font-size: 11px; color: var(--soft); letter-spacing: .03em;
}

/* ---- settings modal --------------------------------------------------- */
.modal {
  position: fixed; inset: 0; background: rgba(8,6,11,.66);
  display: flex; align-items: center; justify-content: center; z-index: 40;
}
.modal.hidden { display: none; }
.modal-card {
  background: var(--bg); border: 1px solid var(--border-2);
  border-radius: 14px; width: 430px; max-width: calc(100vw - 36px);
  box-shadow: 0 28px 70px rgba(0,0,0,.55);
}
.modal-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 15px 20px; border-bottom: 1px solid var(--border);
}
.modal-head h2 { margin: 0; font-size: 14px; font-weight: 600; color: var(--text); }
.modal-head .spark { color: var(--gold); }
.icon-btn {
  background: transparent; border: 0; color: var(--muted); cursor: pointer;
  font-size: 22px; line-height: 1; padding: 0 4px;
}
.icon-btn:hover { color: var(--text); }
.modal-body { padding: 18px 20px; display: flex; flex-direction: column; gap: 17px; }
.field { display: flex; flex-direction: column; gap: 7px; }
.field.row { flex-direction: row; align-items: center; justify-content: space-between; }
.field-label { font-size: 12.5px; color: var(--text); }
.field-label em {
  color: var(--soft); font-style: normal; margin-left: 7px; font-size: 11.5px;
}
.field select, .field input[type=text] {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text); padding: 9px 11px; font: inherit;
}
.field select:focus, .field input[type=text]:focus {
  outline: 0; border-color: var(--border-2);
}
.field input[type=range] { accent-color: var(--gold); }
.switch { width: 36px; height: 20px; accent-color: var(--glow); cursor: pointer; }
.modal-foot {
  padding: 14px 20px; border-top: 1px solid var(--border);
  display: flex; justify-content: flex-end;
}
.primary {
  background: var(--glow); color: #2a1808; border: 0; border-radius: 9px;
  padding: 9px 22px; font: 600 13px var(--mono); cursor: pointer;
  transition: filter .12s;
}
.primary:hover { filter: brightness(1.08); }

/* ---- scrollbars ------------------------------------------------------- */
::-webkit-scrollbar { width: 9px; height: 9px; }
::-webkit-scrollbar-thumb { background: var(--raised); border-radius: 6px; }
::-webkit-scrollbar-thumb:hover { background: var(--border-2); }
::-webkit-scrollbar-track { background: transparent; }

/* drawer backdrop — only visible on small screens */
.backdrop { display: none; }

/* ---- phones & small tablets ------------------------------------------ */
@media (max-width: 760px) {
  #app { grid-template-columns: 1fr; }
  #sidebar {
    position: fixed; top: 0; bottom: 0; left: 0; width: 286px; max-width: 86vw;
    z-index: 50; transform: translateX(-100%); transition: transform .22s ease;
    padding-top: max(16px, env(safe-area-inset-top));
  }
  #sidebar.open { transform: translateX(0); box-shadow: 0 0 44px rgba(0,0,0,.65); }
  .backdrop {
    display: block; position: fixed; inset: 0;
    background: rgba(8,6,11,.6); z-index: 45;
  }
  .backdrop.hidden { display: none; }
  .menu-btn { display: flex; }
  #topbar { padding: 12px 14px; }
  .chip { max-width: 42vw; overflow: hidden; text-overflow: ellipsis; }
  .log { padding: 18px 14px 6px; }
  .composer { padding: 8px 14px max(14px, env(safe-area-inset-bottom)); }
  .composer-hint { display: none; }
  .chat-item .ci-del { opacity: 1; }
  .chat-item { padding: 11px 10px; }
  .composer-box textarea,
  .field select, .field input[type=text] { font-size: 16px; }
  .modal { align-items: flex-end; }
  .modal-card {
    width: 100%; max-width: 100%; border-radius: 14px 14px 0 0;
    padding-bottom: env(safe-area-inset-bottom);
  }
}
"""

_JS = r"""
const $ = (s) => document.querySelector(s);
const log = $('#log'), input = $('#input'), sendBtn = $('#send');
let state = { chats: [], currentId: null, settings: {}, busy: false };

// ---- helpers --------------------------------------------------------------
function esc(s) {
  return (s || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}
function md(src) {
  const blocks = [];
  let s = (src || '').replace(/```(\w*)\n?([\s\S]*?)```/g, (m, l, c) => {
    blocks.push('<pre><code>' + esc(c.replace(/\n$/, '')) + '</code></pre>');
    return '\x00B' + (blocks.length - 1) + '\x00';
  });
  s = esc(s);
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  s = s.replace(/^\s*#{1,6}\s+(.+)$/gm, '<h3>$1</h3>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, '$1<em>$2</em>');
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/(?:^|\n)((?:\s*[-*]\s+.+(?:\n|$))+)/g, (m, b) =>
    '\n<ul>' + b.trim().split('\n').map(x =>
      '<li>' + x.replace(/^\s*[-*]\s+/, '') + '</li>').join('') + '</ul>');
  s = s.split(/\n{2,}/).map(p => p.trim() ? '<p>' + p + '</p>' : '').join('');
  s = s.replace(/\n/g, '<br>');
  s = s.replace(/<p>(<(?:ul|h3|pre))/g, '$1').replace(/(<\/(?:ul|h3|pre)>)<\/p>/g, '$1');
  s = s.replace(/\x00B(\d+)\x00/g, (m, i) => blocks[+i]);
  return s;
}
function scrollDown() { log.scrollTop = log.scrollHeight; }
function thread() {
  let t = log.querySelector('.thread');
  if (!t) { t = document.createElement('div'); t.className = 'thread'; log.appendChild(t); }
  return t;
}
function clearLog() { log.innerHTML = ''; }

// ---- rendering ------------------------------------------------------------
function showEmpty() {
  clearLog();
  const name = state.settings.user_name;
  const h = new Date().getHours();
  const greet = h < 5 ? "you're up late" : h < 12 ? 'good morning'
    : h < 18 ? 'good afternoon' : h < 22 ? 'good evening' : 'winding down';
  const e = document.createElement('div');
  e.className = 'empty';
  e.innerHTML =
    '<div class="banner">' +
      '<div class="b-mark">&#10022;</div>' +
      '<div class="b-word">cagentic</div>' +
      '<div class="b-tag">your local personal assistant</div>' +
      '<div class="b-greet">' + greet + (name ? ', ' + esc(name) : '') + '.</div>' +
    '</div>' +
    '<div class="suggestions"></div>';
  const sg = e.querySelector('.suggestions');
  ["what's on my screen right now?", 'show me my reminders',
   'take a note about something', 'search the web for me'].forEach(t => {
    const c = document.createElement('div');
    c.className = 'suggestion'; c.textContent = t;
    c.onclick = () => { input.value = t; input.focus(); autoGrow(); };
    sg.appendChild(c);
  });
  log.appendChild(e);
}

function addUser(text) {
  const r = document.createElement('div');
  r.className = 'row user';
  r.innerHTML = '<span class="umark">&#10022;</span>' +
    '<div class="utext">' + esc(text) + '</div>';
  thread().appendChild(r); scrollDown();
}
function addAssistant(html, tools) {
  const r = document.createElement('div');
  r.className = 'row assistant';
  r.innerHTML = '<div class="who"><span class="spark">&#10022;</span>cagentic</div>' +
    '<div class="body">' + (html || '') + '</div>';
  thread().appendChild(r);
  (tools || []).forEach(t => addToolRow({ name: t }, true));
  scrollDown();
  return r.querySelector('.body');
}
function addToolRow(t, done) {
  const row = document.createElement('div');
  row.className = 'tool' + (done ? '' : ' pending');
  row.innerHTML = '<span class="arrow">&#8627;</span>' +
    '<span class="tname">' + esc(t.name || '') + '</span>' +
    (t.summary ? '<span class="tsum">' + esc(t.summary) + '</span>' : '');
  thread().appendChild(row); scrollDown();
  return row;
}
function addNote(text, isErr) {
  const n = document.createElement('div');
  n.className = 'note' + (isErr ? ' err' : '');
  n.textContent = text || '';
  thread().appendChild(n); scrollDown();
}
function showPermission(d) {
  const box = document.createElement('div');
  box.className = 'perm';
  box.innerHTML = '<div class="pq">cagentic wants to run <code>' + esc(d.tool) +
    '</code>' + (d.summary ? ' &mdash; ' + esc(d.summary) : '') + '</div>';
  const btns = document.createElement('div'); btns.className = 'pbtns';
  const answer = (a, past) => {
    box.innerHTML = '<div class="pq"><code>' + esc(d.tool) +
      '</code></div><div class="decided">&rarr; ' + past + '</div>';
    fetch('/api/permission', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer: a })
    });
  };
  [['yes', 'approve', 'approved'], ['always', 'always allow', 'always allowed'],
   ['no', 'deny', 'denied']].forEach(([a, lbl, past]) => {
    const b = document.createElement('button');
    b.className = a; b.textContent = lbl;
    b.onclick = () => answer(a, past);
    btns.appendChild(b);
  });
  box.appendChild(btns);
  thread().appendChild(box); scrollDown();
}

// ---- live turn ------------------------------------------------------------
let live = { body: null, raw: '', toolRow: null };

function handle(ev) {
  const k = ev.kind, d = ev.data || {};
  if (k === 'delta') {
    if (!live.body) { live.body = addAssistant(''); live.raw = ''; }
    live.raw += d.text || '';
    live.body.innerHTML = md(live.raw);
    live.body.classList.add('cursor');
    scrollDown();
  } else if (k === 'assistant') {
    if (!live.body && (d.text || '').trim()) {
      live.body = addAssistant(md(d.text)); live.raw = d.text;
    }
    if (live.body) live.body.classList.remove('cursor');
  } else if (k === 'plan') {
    const p = document.createElement('div');
    p.className = 'plan';
    p.innerHTML = '<div class="ph">&#10047; here\'s my plan</div><ol>' +
      (d.steps || []).map(s => '<li>' + esc(s) + '</li>').join('') + '</ol>';
    thread().appendChild(p); live.body = null; scrollDown();
  } else if (k === 'tool_call') {
    live.body = null;
    live.toolRow = addToolRow({ name: d.name, summary: d.summary }, false);
  } else if (k === 'tool_result') {
    if (live.toolRow) {
      live.toolRow.classList.remove('pending');
      live.toolRow.classList.add(d.ok ? 'ok' : 'bad');
      const res = document.createElement('span');
      res.className = 'tres';
      res.textContent = (d.ok ? '✓ ' : '✗ ') + (d.first_line || '').slice(0, 90);
      live.toolRow.appendChild(res);
      live.toolRow = null;
    }
  } else if (k === 'permission') {
    live.body = null; showPermission(d);
  } else if (k === 'info' || k === 'warn') {
    addNote(d.text, false); live.body = null;
  } else if (k === 'error') {
    addNote(d.text || 'something went wrong', true); live.body = null;
  } else if (k === 'done') {
    if (live.body) live.body.classList.remove('cursor');
    live.body = null;
  } else if (k === 'end') {
    finishTurn();
  }
  scrollDown();
}

// ---- sidebar --------------------------------------------------------------
function renderChats() {
  const list = $('#chatList');
  list.innerHTML = '';
  if (!state.chats.length) {
    list.innerHTML = '<div class="foot-meta">no chats yet</div>';
  }
  state.chats.forEach(c => {
    const item = document.createElement('div');
    item.className = 'chat-item' + (c.id === state.currentId ? ' active' : '');
    item.innerHTML = '<span class="ci-title">' + esc(c.title) + '</span>' +
      '<button class="ci-del" title="Delete">&times;</button>';
    item.querySelector('.ci-title').onclick = () => loadChat(c.id);
    item.querySelector('.ci-del').onclick = (e) => {
      e.stopPropagation();
      if (confirm('Delete "' + c.title + '"?')) deleteChat(c.id);
    };
    list.appendChild(item);
  });
}
function setCurrent(cur) {
  state.currentId = cur.id;
  $('#chatTitle').textContent = cur.title || 'new chat';
  clearLog();
  if (!cur.messages || !cur.messages.length) { showEmpty(); return; }
  cur.messages.forEach(m => {
    if (m.role === 'user') addUser(m.content);
    else addAssistant(md(m.content), m.tools);
  });
  scrollDown();
}

// ---- network --------------------------------------------------------------
async function api(path, body) {
  const r = await fetch(path, {
    method: body ? 'POST' : 'GET',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined
  });
  return r.json();
}
async function boot() {
  const b = await api('/api/bootstrap');
  state.chats = b.chats; state.settings = b.settings;
  $('#modelChip').textContent = b.model;
  $('#footMeta').textContent = 'v' + b.version + ' · local';
  renderChats();
  setCurrent(b.current);
}
async function newChat() {
  const r = await api('/api/chats/new', {});
  state.chats = r.chats; renderChats(); setCurrent(r.current);
  closeDrawer(); input.focus();
}
async function loadChat(id) {
  const r = await api('/api/chats/load', { id });
  state.chats = r.chats; renderChats(); setCurrent(r.current);
  closeDrawer();
}
async function deleteChat(id) {
  const r = await api('/api/chats/delete', { id });
  state.chats = r.chats; renderChats(); setCurrent(r.current);
}
async function refreshChats() {
  const b = await api('/api/bootstrap');
  state.chats = b.chats;
  $('#chatTitle').textContent = b.current.title || 'new chat';
  renderChats();
}

// ---- drawer (mobile) ------------------------------------------------------
function openDrawer() {
  $('#sidebar').classList.add('open');
  $('#backdrop').classList.remove('hidden');
}
function closeDrawer() {
  $('#sidebar').classList.remove('open');
  $('#backdrop').classList.add('hidden');
}

// ---- sending --------------------------------------------------------------
function finishTurn() {
  state.busy = false;
  sendBtn.disabled = false;
  input.disabled = false;
  input.focus();
  refreshChats();
}
async function send(text) {
  if (state.busy) return;
  state.busy = true;
  sendBtn.disabled = true;
  if (log.querySelector('.empty')) clearLog();
  addUser(text);
  live = { body: null, raw: '', toolRow: null };
  let res;
  try {
    res = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text })
    });
  } catch (e) { addNote('connection failed', true); finishTurn(); return; }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  while (true) {
    let chunk;
    try { chunk = await reader.read(); } catch (e) { break; }
    if (chunk.done) break;
    buf += dec.decode(chunk.value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const line = buf.slice(0, i); buf = buf.slice(i + 2);
      if (line.startsWith('data: ')) {
        try { handle(JSON.parse(line.slice(6))); } catch (e) {}
      }
    }
  }
  if (state.busy) finishTurn();
}

// ---- settings -------------------------------------------------------------
function openSettings() {
  closeDrawer();
  const s = state.settings;
  const sel = $('#setModel');
  sel.innerHTML = '';
  (s.models && s.models.length ? s.models : [s.model]).forEach(m => {
    const o = document.createElement('option');
    o.value = m; o.textContent = m; if (m === s.model) o.selected = true;
    sel.appendChild(o);
  });
  $('#setName').value = s.user_name || '';
  $('#setTemp').value = s.temperature;
  $('#tempVal').textContent = (+s.temperature).toFixed(2);
  $('#setStream').checked = !!s.stream;
  $('#setYolo').checked = !!s.yolo;
  $('#settingsModal').classList.remove('hidden');
}
async function saveSettings() {
  const body = {
    model: $('#setModel').value,
    user_name: $('#setName').value,
    temperature: parseFloat($('#setTemp').value),
    stream: $('#setStream').checked,
    yolo: $('#setYolo').checked
  };
  state.settings = await api('/api/settings', body);
  $('#modelChip').textContent = state.settings.model;
  $('#settingsModal').classList.add('hidden');
  if (log.querySelector('.empty')) showEmpty();
}

// ---- composer + wiring ----------------------------------------------------
function autoGrow() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 200) + 'px';
}
function submit() {
  const text = input.value.trim();
  if (!text || state.busy) return;
  input.value = ''; autoGrow();
  send(text);
}
input.addEventListener('input', autoGrow);
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
});
sendBtn.onclick = submit;
$('#newChat').onclick = newChat;
$('#openSettings').onclick = openSettings;
$('#closeSettings').onclick = () => $('#settingsModal').classList.add('hidden');
$('#saveSettings').onclick = saveSettings;
$('#menuBtn').onclick = openDrawer;
$('#backdrop').onclick = closeDrawer;
$('#setTemp').addEventListener('input', (e) =>
  $('#tempVal').textContent = (+e.target.value).toFixed(2));
$('#settingsModal').addEventListener('click', (e) => {
  if (e.target.id === 'settingsModal') e.target.classList.add('hidden');
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (!$('#settingsModal').classList.contains('hidden'))
    $('#settingsModal').classList.add('hidden');
  else closeDrawer();
});

boot();
"""
