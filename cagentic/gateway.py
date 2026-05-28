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
# A professional AI chat app — clean sans-serif type, generous spacing,
# refined components — dressed in Cagentic's warm-dusk palette.

_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<title>Cagentic</title>
<link rel="stylesheet" href="/app.css" />
</head>
<body>
<div id="app">
  <aside id="sidebar">
    <div class="brand">
      <span class="brand-mark">&#10022;</span>
      <span class="brand-name">Cagentic</span>
    </div>
    <button id="newChat" class="new-chat">
      <svg viewBox="0 0 24 24" width="16" height="16"><path d="M12 5v14M5 12h14"
        fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
      New chat
    </button>
    <div class="chats-label">Recent</div>
    <div id="chatList" class="chat-list"></div>
    <div class="sidebar-foot">
      <button id="openSettings" class="foot-btn">
        <svg viewBox="0 0 24 24" width="16" height="16"><path
          d="M12 15a3 3 0 100-6 3 3 0 000 6z" fill="none" stroke="currentColor"
          stroke-width="2"/><path d="M19.4 15a1.6 1.6 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.6 1.6 0 00-1.8-.3 1.6 1.6 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.6 1.6 0 00-1-1.5 1.6 1.6 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.6 1.6 0 00.3-1.8 1.6 1.6 0 00-1.5-1H3a2 2 0 110-4h.1a1.6 1.6 0 001.5-1 1.6 1.6 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.6 1.6 0 001.8.3H9a1.6 1.6 0 001-1.5V3a2 2 0 114 0v.1a1.6 1.6 0 001 1.5 1.6 1.6 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.6 1.6 0 00-.3 1.8V9a1.6 1.6 0 001.5 1H21a2 2 0 110 4h-.1a1.6 1.6 0 00-1.5 1z"
          fill="none" stroke="currentColor" stroke-width="2"/></svg>
        Settings
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
      <div id="chatTitle">New chat</div>
      <div id="modelChip" class="chip"></div>
    </header>
    <div id="log" class="log"></div>
    <div id="composer" class="composer">
      <div class="composer-inner">
        <div class="composer-box">
          <textarea id="input" rows="1" placeholder="Message Cagentic…"></textarea>
          <button id="send" class="send" title="Send" aria-label="Send">
            <svg viewBox="0 0 24 24" width="18" height="18"><path
              d="M12 19V5M12 5l-6 6M12 5l6 6" fill="none" stroke="currentColor"
              stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
        </div>
        <div class="composer-hint">
          Cagentic runs on your machine — it can browse, manage files, and take notes.
        </div>
      </div>
    </div>
  </main>
</div>
<div id="backdrop" class="backdrop hidden"></div>

<div id="settingsModal" class="modal hidden">
  <div class="modal-card">
    <div class="modal-head">
      <h2>Settings</h2>
      <button id="closeSettings" class="icon-btn" aria-label="Close">
        <svg viewBox="0 0 24 24" width="18" height="18"><path d="M6 6l12 12M18 6L6 18"
          fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
      </button>
    </div>
    <div class="modal-body">
      <div class="field">
        <span class="field-label">Model</span>
        <select id="setModel"></select>
      </div>
      <div class="field">
        <span class="field-label">Your name</span>
        <input id="setName" type="text" placeholder="What should I call you?" />
      </div>
      <div class="field">
        <span class="field-label">Temperature <em id="tempVal">0.4</em></span>
        <input id="setTemp" type="range" min="0" max="1.5" step="0.05" />
      </div>
      <div class="field row">
        <div><span class="field-label">Stream responses</span>
          <span class="field-hint">show replies as they're written</span></div>
        <label class="toggle"><input id="setStream" type="checkbox" /><span></span></label>
      </div>
      <div class="field row">
        <div><span class="field-label">Auto-approve tools</span>
          <span class="field-hint">skip every approval prompt</span></div>
        <label class="toggle"><input id="setYolo" type="checkbox" /><span></span></label>
      </div>
    </div>
    <div class="modal-foot">
      <button id="cancelSettings" class="btn-ghost">Cancel</button>
      <button id="saveSettings" class="btn-primary">Save changes</button>
    </div>
  </div>
</div>

<script src="/app.js"></script>
</body>
</html>
"""

_CSS = """
/* ===== Cagentic — gateway web app ====================================== */
:root {
  --bg:        #161118;
  --bg-side:   #1a141e;
  --surface:   #221b29;
  --surface-2: #2a2233;
  --surface-3: #342a3f;
  --border:    rgba(237,231,242,.07);
  --border-2:  rgba(237,231,242,.13);
  --text:      #ece7f0;
  --text-2:    #ada3b8;
  --text-3:    #7c7388;
  --accent:    #f0a87a;
  --accent-2:  #f6bd95;
  --accent-dim:rgba(240,168,122,.13);
  --orchid:    #c79ccf;
  --gold:      #e6c073;
  --ok:        #8ecf95;
  --err:       #e5928f;
  --grad:      linear-gradient(135deg, #f0a87a, #c79ccf);
  --shadow:    0 18px 48px rgba(0,0,0,.42);
  --sans: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto,
          system-ui, sans-serif;
  --mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  background: var(--bg); color: var(--text);
  font: 15px/1.62 var(--sans);
  -webkit-font-smoothing: antialiased;
}
#app { display: grid; grid-template-columns: 268px 1fr; height: 100vh; height: 100dvh; }
::selection { background: rgba(240,168,122,.26); }

/* ---- sidebar ---------------------------------------------------------- */
#sidebar {
  background: var(--bg-side); border-right: 1px solid var(--border);
  display: flex; flex-direction: column; padding: 16px 12px;
}
.brand { display: flex; align-items: center; gap: 10px; padding: 6px 8px 16px; }
.brand-mark {
  width: 27px; height: 27px; border-radius: 8px; background: var(--grad);
  display: flex; align-items: center; justify-content: center;
  color: #1c1018; font-size: 14px;
}
.brand-name { font-size: 15.5px; font-weight: 650; letter-spacing: -.01em; }
.new-chat {
  display: flex; align-items: center; gap: 9px; width: 100%;
  padding: 10px 13px; cursor: pointer; color: var(--text);
  background: var(--surface); border: 1px solid var(--border-2);
  border-radius: 10px; font: 600 14px var(--sans);
  transition: background .15s, border-color .15s;
}
.new-chat:hover { background: var(--surface-2); border-color: var(--accent); }
.new-chat svg { color: var(--accent); }
.chats-label {
  font-size: 11px; font-weight: 600; letter-spacing: .07em;
  text-transform: uppercase; color: var(--text-3); padding: 20px 9px 8px;
}
.chat-list { flex: 1; overflow-y: auto; margin: 0 -4px; padding: 0 4px; }
.chat-item {
  display: flex; align-items: center; gap: 8px; position: relative;
  padding: 9px 11px; border-radius: 9px; cursor: pointer;
  color: var(--text-2); transition: background .13s, color .13s;
}
.chat-item:hover { background: var(--surface); color: var(--text); }
.chat-item.active { background: var(--surface-2); color: var(--text); }
.chat-item .ci-title {
  flex: 1; min-width: 0; font-size: 13.5px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.chat-item .ci-del {
  opacity: 0; border: 0; background: transparent; color: var(--text-3);
  cursor: pointer; padding: 2px; display: flex; border-radius: 5px;
  transition: opacity .13s, color .13s, background .13s;
}
.chat-item:hover .ci-del { opacity: 1; }
.chat-item .ci-del:hover { color: var(--err); background: var(--surface-3); }
.sidebar-foot { border-top: 1px solid var(--border); padding-top: 8px; margin-top: 8px; }
.foot-btn {
  display: flex; align-items: center; gap: 9px; width: 100%;
  background: transparent; color: var(--text-2); border: 0; cursor: pointer;
  padding: 9px 11px; border-radius: 9px; font: 500 13.5px var(--sans);
  transition: background .13s, color .13s;
}
.foot-btn:hover { background: var(--surface); color: var(--text); }
.foot-meta { font-size: 11px; color: var(--text-3); padding: 8px 11px 2px; }

/* ---- main + topbar ---------------------------------------------------- */
#main { display: flex; flex-direction: column; min-width: 0; background: var(--bg); }
#topbar {
  display: flex; align-items: center; gap: 12px;
  padding: 0 24px; height: 58px; flex-shrink: 0;
  border-bottom: 1px solid var(--border);
}
#chatTitle {
  flex: 1; min-width: 0; font-size: 14px; font-weight: 600; color: var(--text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.chip {
  display: flex; align-items: center; gap: 7px;
  font-size: 12.5px; color: var(--text-2);
  background: var(--surface); border: 1px solid var(--border);
  padding: 5px 11px; border-radius: 999px; white-space: nowrap;
}
.chip::before {
  content: ""; width: 6px; height: 6px; border-radius: 50%;
  background: var(--ok); box-shadow: 0 0 0 3px rgba(142,207,149,.18);
}
.menu-btn {
  display: none; align-items: center; justify-content: center;
  width: 36px; height: 36px; margin-left: -8px; flex-shrink: 0;
  background: transparent; border: 0; border-radius: 8px;
  color: var(--text-2); cursor: pointer;
}
.menu-btn:hover { background: var(--surface); color: var(--text); }

/* ---- message log ------------------------------------------------------ */
.log { flex: 1; overflow-y: auto; }
.thread { max-width: 768px; margin: 0 auto; padding: 30px 28px 14px; }
.row { margin-bottom: 26px; animation: rise .26s ease both; }
@keyframes rise { from { opacity: 0; transform: translateY(7px); } }

/* user message — a bubble on the right */
.row.user { display: flex; justify-content: flex-end; }
.row.user .bubble {
  background: var(--surface-2); border: 1px solid var(--border);
  padding: 11px 16px; border-radius: 16px 16px 5px 16px;
  max-width: 80%; white-space: pre-wrap; word-wrap: break-word;
  line-height: 1.55;
}

/* assistant message — avatar + content */
.row.assistant { display: flex; gap: 14px; }
.avatar {
  width: 30px; height: 30px; border-radius: 9px; background: var(--grad);
  flex-shrink: 0; display: flex; align-items: center; justify-content: center;
  color: #1c1018; font-size: 14px; margin-top: 1px;
}
.row.assistant .body { min-width: 0; flex: 1; padding-top: 3px; }
.body p { margin: 0 0 12px; }
.body p:last-child { margin-bottom: 0; }
.body h2, .body h3 {
  font-size: 16px; font-weight: 650; margin: 20px 0 9px; color: var(--text);
}
.body h2:first-child, .body h3:first-child { margin-top: 0; }
.body ul { margin: 10px 0; padding-left: 22px; }
.body li { margin: 4px 0; }
.body li::marker { color: var(--accent); }
.body a { color: var(--accent); text-decoration: none; }
.body a:hover { text-decoration: underline; }
.body code {
  background: var(--surface-2); color: var(--accent-2);
  padding: 2px 6px; border-radius: 5px; font: 13.5px var(--mono);
}
.body strong { font-weight: 650; }
.cursor::after {
  content: ""; display: inline-block; width: 8px; height: 15px;
  background: var(--accent); border-radius: 2px; margin-left: 2px;
  vertical-align: -2px; animation: blink 1.05s steps(2) infinite;
}
@keyframes blink { 50% { opacity: 0; } }

/* code blocks */
.codeblock {
  margin: 12px 0; border: 1px solid var(--border-2); border-radius: 10px;
  overflow: hidden; background: #18131e;
}
.cb-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 7px 12px; background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.cb-lang { font: 11px var(--mono); color: var(--text-3); letter-spacing: .04em; }
.cb-copy {
  background: transparent; border: 0; color: var(--text-3); cursor: pointer;
  font: 11.5px var(--sans); padding: 2px 6px; border-radius: 5px;
  transition: color .13s, background .13s;
}
.cb-copy:hover { color: var(--text); background: var(--surface-2); }
.codeblock pre { margin: 0; padding: 13px 14px; overflow-x: auto; }
.codeblock code { font: 13px/1.6 var(--mono); color: var(--text); background: none; }

/* thinking indicator */
.thinking { display: flex; gap: 14px; margin-bottom: 26px; }
.thinking .dots { display: flex; gap: 5px; align-items: center; padding-top: 11px; }
.thinking .dots span {
  width: 7px; height: 7px; border-radius: 50%; background: var(--text-3);
  animation: bob 1.25s ease-in-out infinite;
}
.thinking .dots span:nth-child(2) { animation-delay: .16s; }
.thinking .dots span:nth-child(3) { animation-delay: .32s; }
@keyframes bob { 0%,80%,100% { opacity: .3; transform: translateY(0); }
                 40% { opacity: 1; transform: translateY(-4px); } }

/* tool call rows */
.tool {
  display: flex; align-items: center; gap: 9px; flex-wrap: wrap;
  margin: 9px 0; padding: 9px 13px; font-size: 13px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
}
.tool .ticon { color: var(--orchid); display: flex; flex-shrink: 0; }
.tool .tname { color: var(--text); font-weight: 600; }
.tool .tsum { color: var(--text-3); }
.tool .tres { margin-left: auto; font-size: 12.5px; }
.tool.ok .tres { color: var(--ok); }
.tool.bad .tres { color: var(--err); }
.tool.pending .tres { color: var(--text-3); }

/* plan card */
.plan {
  margin: 12px 0; padding: 14px 16px; background: var(--surface);
  border: 1px solid var(--border); border-radius: 11px;
}
.plan .ph {
  display: flex; align-items: center; gap: 7px;
  font-weight: 650; font-size: 13px; color: var(--gold); margin-bottom: 9px;
}
.plan ol { margin: 0; padding-left: 22px; color: var(--text-2); }
.plan li { margin: 5px 0; }
.plan li::marker { color: var(--gold); }

/* notes / errors */
.note { margin: 9px 0; font-size: 13px; color: var(--text-3); }
.note.err { color: var(--err); }

/* permission card */
.perm {
  margin: 13px 0; padding: 15px 17px; background: var(--surface-2);
  border: 1px solid var(--border-2); border-left: 3px solid var(--accent);
  border-radius: 11px;
}
.perm .pq { font-size: 14px; margin-bottom: 13px; }
.perm .pq code {
  background: var(--bg); color: var(--accent-2);
  padding: 2px 7px; border-radius: 5px; font: 13px var(--mono);
}
.perm .pbtns { display: flex; gap: 9px; flex-wrap: wrap; }
.perm button {
  border: 1px solid transparent; border-radius: 9px; padding: 8px 16px;
  cursor: pointer; font: 600 13px var(--sans); transition: filter .13s, background .13s;
}
.perm .yes { background: var(--accent); color: #241408; }
.perm .yes:hover { filter: brightness(1.08); }
.perm .always { background: var(--surface-3); color: var(--gold); border-color: var(--border-2); }
.perm .always:hover { background: var(--surface); }
.perm .no { background: transparent; color: var(--text-2); border-color: var(--border-2); }
.perm .no:hover { background: var(--surface); }
.perm .decided { color: var(--text-3); font-size: 13px; }

/* ---- empty state ------------------------------------------------------ */
.empty {
  min-height: 100%; display: flex; flex-direction: column;
  align-items: center; justify-content: center; padding: 40px 24px;
}
.empty .e-mark {
  width: 52px; height: 52px; border-radius: 15px; background: var(--grad);
  display: flex; align-items: center; justify-content: center;
  color: #1c1018; font-size: 24px; margin-bottom: 22px;
}
.empty .e-greet { font-size: 27px; font-weight: 680; letter-spacing: -.02em; }
.empty .e-sub { font-size: 15px; color: var(--text-3); margin-top: 6px; }
.cards {
  display: grid; grid-template-columns: 1fr 1fr; gap: 11px;
  margin-top: 30px; width: 100%; max-width: 540px;
}
.card {
  text-align: left; padding: 15px 16px; cursor: pointer;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 13px; transition: border-color .15s, background .15s, transform .15s;
}
.card:hover { border-color: var(--border-2); background: var(--surface-2);
  transform: translateY(-2px); }
.card .c-title { font-weight: 600; font-size: 14px; }
.card .c-sub { font-size: 12.5px; color: var(--text-3); margin-top: 3px; }

/* ---- composer --------------------------------------------------------- */
.composer { flex-shrink: 0; padding: 10px 28px 18px; }
.composer-inner { max-width: 768px; margin: 0 auto; }
.composer-box {
  display: flex; align-items: flex-end; gap: 9px;
  background: var(--surface); border: 1px solid var(--border-2);
  border-radius: 17px; padding: 9px 9px 9px 17px;
  transition: border-color .15s, box-shadow .15s;
}
.composer-box:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-dim);
}
.composer-box textarea {
  flex: 1; resize: none; background: transparent; border: 0; outline: 0;
  color: var(--text); font: 15px/1.55 var(--sans); padding: 8px 0;
  max-height: 200px;
}
.composer-box textarea::placeholder { color: var(--text-3); }
.send {
  flex-shrink: 0; width: 36px; height: 36px; border-radius: 11px;
  border: 0; cursor: pointer; background: var(--accent); color: #241408;
  display: flex; align-items: center; justify-content: center;
  transition: filter .13s, opacity .13s;
}
.send:hover { filter: brightness(1.08); }
.send:disabled { opacity: .35; cursor: default; }
.composer-hint {
  text-align: center; font-size: 11.5px; color: var(--text-3); margin-top: 10px;
}

/* ---- settings modal --------------------------------------------------- */
.modal {
  position: fixed; inset: 0; background: rgba(10,7,13,.66);
  display: flex; align-items: center; justify-content: center; z-index: 60;
  animation: fade .15s ease;
}
@keyframes fade { from { opacity: 0; } }
.modal.hidden { display: none; }
.modal-card {
  background: var(--bg-side); border: 1px solid var(--border-2);
  border-radius: 16px; width: 460px; max-width: calc(100vw - 36px);
  box-shadow: var(--shadow);
}
.modal-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 17px 22px; border-bottom: 1px solid var(--border);
}
.modal-head h2 { margin: 0; font-size: 16px; font-weight: 650; }
.icon-btn {
  background: transparent; border: 0; color: var(--text-3); cursor: pointer;
  padding: 4px; display: flex; border-radius: 7px;
}
.icon-btn:hover { color: var(--text); background: var(--surface); }
.modal-body { padding: 20px 22px; display: flex; flex-direction: column; gap: 19px; }
.field { display: flex; flex-direction: column; gap: 8px; }
.field.row { flex-direction: row; align-items: center; justify-content: space-between; }
.field-label { font-size: 13.5px; font-weight: 600; }
.field-label em { color: var(--text-3); font-style: normal; font-weight: 400; margin-left: 6px; }
.field-hint { display: block; font-size: 12px; color: var(--text-3); margin-top: 2px; }
.field select, .field input[type=text] {
  background: var(--surface); border: 1px solid var(--border-2);
  border-radius: 9px; color: var(--text); padding: 10px 12px; font: 14px var(--sans);
}
.field select:focus, .field input[type=text]:focus {
  outline: 0; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim);
}
.field input[type=range] { accent-color: var(--accent); }
/* toggle switch */
.toggle { position: relative; width: 42px; height: 24px; flex-shrink: 0; }
.toggle input { position: absolute; opacity: 0; }
.toggle span {
  position: absolute; inset: 0; cursor: pointer; border-radius: 999px;
  background: var(--surface-3); transition: background .15s;
}
.toggle span::after {
  content: ""; position: absolute; width: 18px; height: 18px; border-radius: 50%;
  background: #ece7f0; top: 3px; left: 3px; transition: transform .16s;
}
.toggle input:checked + span { background: var(--accent); }
.toggle input:checked + span::after { transform: translateX(18px); }
.modal-foot {
  padding: 15px 22px; border-top: 1px solid var(--border);
  display: flex; justify-content: flex-end; gap: 9px;
}
.btn-primary {
  background: var(--accent); color: #241408; border: 0; border-radius: 9px;
  padding: 9px 18px; font: 600 13.5px var(--sans); cursor: pointer;
  transition: filter .13s;
}
.btn-primary:hover { filter: brightness(1.08); }
.btn-ghost {
  background: transparent; color: var(--text-2); border: 1px solid var(--border-2);
  border-radius: 9px; padding: 9px 16px; font: 600 13.5px var(--sans); cursor: pointer;
}
.btn-ghost:hover { background: var(--surface); color: var(--text); }

/* ---- scrollbars + backdrop ------------------------------------------- */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: var(--surface-3); border-radius: 6px;
  border: 2px solid transparent; background-clip: padding-box; }
::-webkit-scrollbar-thumb:hover { background: #423650; background-clip: padding-box; }
::-webkit-scrollbar-track { background: transparent; }
.backdrop { display: none; }

/* ---- responsive ------------------------------------------------------- */
@media (max-width: 820px) {
  #app { grid-template-columns: 1fr; }
  #sidebar {
    position: fixed; top: 0; bottom: 0; left: 0; width: 286px; max-width: 86vw;
    z-index: 70; transform: translateX(-100%); transition: transform .22s ease;
    padding-top: max(16px, env(safe-area-inset-top));
  }
  #sidebar.open { transform: translateX(0); box-shadow: var(--shadow); }
  .backdrop {
    display: block; position: fixed; inset: 0;
    background: rgba(10,7,13,.6); z-index: 65;
  }
  .backdrop.hidden { display: none; }
  .menu-btn { display: flex; }
  #topbar { padding: 0 14px; }
  .thread { padding: 22px 16px 12px; }
  .composer { padding: 8px 14px max(14px, env(safe-area-inset-bottom)); }
  .composer-hint { display: none; }
  .cards { grid-template-columns: 1fr; }
  .row.user .bubble { max-width: 88%; }
  .chat-item .ci-del { opacity: 1; }
  .composer-box textarea,
  .field select, .field input[type=text] { font-size: 16px; }
  .modal { align-items: flex-end; }
  .modal-card { width: 100%; max-width: 100%; border-radius: 18px 18px 0 0;
    padding-bottom: env(safe-area-inset-bottom); }
}
"""

_JS = r"""
const $ = (s) => document.querySelector(s);
const log = $('#log'), input = $('#input'), sendBtn = $('#send');
let state = { chats: [], currentId: null, settings: {}, busy: false };
const SPARK = '✦';

// ---- helpers --------------------------------------------------------------
function esc(s) {
  return (s || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}
function md(src) {
  const blocks = [];
  let s = (src || '').replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    blocks.push(
      '<div class="codeblock"><div class="cb-head"><span class="cb-lang">' +
      (lang || 'text') + '</span><button class="cb-copy">Copy</button></div>' +
      '<pre><code>' + esc(code.replace(/\n$/, '')) + '</code></pre></div>');
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
  s = s.replace(/<p>(<(?:ul|h3|div))/g, '$1').replace(/(<\/(?:ul|h3|div)>)<\/p>/g, '$1');
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
function avatarHTML() { return '<div class="avatar">' + SPARK + '</div>'; }

// ---- rendering ------------------------------------------------------------
function showEmpty() {
  clearLog();
  const name = state.settings.user_name;
  const h = new Date().getHours();
  const greet = h < 5 ? 'Working late' : h < 12 ? 'Good morning'
    : h < 18 ? 'Good afternoon' : 'Good evening';
  const e = document.createElement('div');
  e.className = 'empty';
  e.innerHTML =
    '<div class="e-mark">' + SPARK + '</div>' +
    '<div class="e-greet">' + greet + (name ? ', ' + esc(name) : '') + '</div>' +
    '<div class="e-sub">How can I help you today?</div>' +
    '<div class="cards"></div>';
  const cards = [
    ['Look something up', 'Search the web and summarize'],
    ['Check my reminders', 'See what\'s on your list'],
    ['Take a note', 'Remember something for later'],
    ['Read my screen', 'Summarize the page in my browser'],
  ];
  const grid = e.querySelector('.cards');
  cards.forEach(([title, sub]) => {
    const c = document.createElement('div');
    c.className = 'card';
    c.innerHTML = '<div class="c-title">' + title + '</div>' +
      '<div class="c-sub">' + sub + '</div>';
    c.onclick = () => { input.value = title; input.focus(); autoGrow(); };
    grid.appendChild(c);
  });
  log.appendChild(e);
}

function addUser(text) {
  const r = document.createElement('div');
  r.className = 'row user';
  r.innerHTML = '<div class="bubble">' + esc(text) + '</div>';
  thread().appendChild(r); scrollDown();
}
function addAssistant(html, tools) {
  const r = document.createElement('div');
  r.className = 'row assistant';
  r.innerHTML = avatarHTML() + '<div class="body">' + (html || '') + '</div>';
  thread().appendChild(r);
  (tools || []).forEach(t => addToolRow({ name: t }, true));
  scrollDown();
  return r.querySelector('.body');
}
function addToolRow(t, done) {
  const row = document.createElement('div');
  row.className = 'tool' + (done ? '' : ' pending');
  row.innerHTML =
    '<span class="ticon"><svg viewBox="0 0 24 24" width="14" height="14">' +
    '<path d="M4 7h16M4 12h16M4 17h10" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round"/></svg></span>' +
    '<span class="tname">' + esc(t.name || '') + '</span>' +
    (t.summary ? '<span class="tsum">' + esc(t.summary) + '</span>' : '') +
    (done ? '' : '<span class="tres">running…</span>');
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
  box.innerHTML = '<div class="pq">Cagentic wants to run <code>' + esc(d.tool) +
    '</code>' + (d.summary ? ' &mdash; ' + esc(d.summary) : '') + '</div>';
  const btns = document.createElement('div'); btns.className = 'pbtns';
  const answer = (a, past) => {
    box.innerHTML = '<div class="pq"><code>' + esc(d.tool) + '</code></div>' +
      '<div class="decided">&rarr; ' + past + '</div>';
    fetch('/api/permission', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer: a })
    });
  };
  [['yes', 'Approve', 'approved'], ['always', 'Always allow', 'always allowed'],
   ['no', 'Deny', 'denied']].forEach(([a, lbl, past]) => {
    const b = document.createElement('button');
    b.className = a; b.textContent = lbl;
    b.onclick = () => answer(a, past);
    btns.appendChild(b);
  });
  box.appendChild(btns);
  thread().appendChild(box); scrollDown();
}

// ---- live turn ------------------------------------------------------------
let live = { body: null, raw: '', toolRow: null, thinking: null };

function showThinking() {
  const t = document.createElement('div');
  t.className = 'thinking';
  t.innerHTML = avatarHTML() + '<div class="dots"><span></span><span></span><span></span></div>';
  thread().appendChild(t); scrollDown();
  live.thinking = t;
}
function clearThinking() {
  if (live.thinking) { live.thinking.remove(); live.thinking = null; }
}

function handle(ev) {
  const k = ev.kind, d = ev.data || {};
  if (k !== 'user') clearThinking();
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
    p.innerHTML = '<div class="ph">' +
      '<svg viewBox="0 0 24 24" width="14" height="14"><path d="M9 11l3 3 8-8M4 12l3 3" ' +
      'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
      'stroke-linejoin="round"/></svg> Plan</div><ol>' +
      (d.steps || []).map(s => '<li>' + esc(s) + '</li>').join('') + '</ol>';
    thread().appendChild(p); live.body = null; scrollDown();
  } else if (k === 'tool_call') {
    live.body = null;
    live.toolRow = addToolRow({ name: d.name, summary: d.summary }, false);
  } else if (k === 'tool_result') {
    if (live.toolRow) {
      live.toolRow.classList.remove('pending');
      live.toolRow.classList.add(d.ok ? 'ok' : 'bad');
      const res = live.toolRow.querySelector('.tres') || document.createElement('span');
      res.className = 'tres';
      res.textContent = (d.ok ? '✓ ' : '✗ ') + (d.first_line || '').slice(0, 80);
      if (!res.parentNode) live.toolRow.appendChild(res);
      live.toolRow = null;
    }
  } else if (k === 'permission') {
    live.body = null; showPermission(d);
  } else if (k === 'info' || k === 'warn') {
    addNote(d.text, false); live.body = null;
  } else if (k === 'error') {
    addNote(d.text || 'Something went wrong.', true); live.body = null;
  } else if (k === 'done') {
    if (live.body) live.body.classList.remove('cursor');
    live.body = null;
  } else if (k === 'end') {
    finishTurn();
  }
  scrollDown();
}

// ---- code-block copy (event delegation) -----------------------------------
log.addEventListener('click', (e) => {
  const btn = e.target.closest('.cb-copy');
  if (!btn) return;
  const code = btn.closest('.codeblock').querySelector('pre code');
  navigator.clipboard.writeText(code.textContent || '').then(() => {
    btn.textContent = 'Copied';
    setTimeout(() => { btn.textContent = 'Copy'; }, 1400);
  });
});

// ---- sidebar --------------------------------------------------------------
function renderChats() {
  const list = $('#chatList');
  list.innerHTML = '';
  if (!state.chats.length) {
    list.innerHTML = '<div class="foot-meta">No chats yet</div>';
  }
  const x = '<svg viewBox="0 0 24 24" width="14" height="14"><path d="M6 6l12 12M18 6L6 18" ' +
    'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
  state.chats.forEach(c => {
    const item = document.createElement('div');
    item.className = 'chat-item' + (c.id === state.currentId ? ' active' : '');
    item.innerHTML = '<span class="ci-title">' + esc(c.title) + '</span>' +
      '<button class="ci-del" title="Delete">' + x + '</button>';
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
  $('#chatTitle').textContent = cur.title || 'New chat';
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
  $('#footMeta').textContent = 'Cagentic v' + b.version;
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
  $('#chatTitle').textContent = b.current.title || 'New chat';
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
  live = { body: null, raw: '', toolRow: null, thinking: null };
  showThinking();
  let res;
  try {
    res = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text })
    });
  } catch (e) { clearThinking(); addNote('Connection failed.', true); finishTurn(); return; }
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
  clearThinking();
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
function closeSettings() { $('#settingsModal').classList.add('hidden'); }
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
  closeSettings();
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
$('#closeSettings').onclick = closeSettings;
$('#cancelSettings').onclick = closeSettings;
$('#saveSettings').onclick = saveSettings;
$('#menuBtn').onclick = openDrawer;
$('#backdrop').onclick = closeDrawer;
$('#setTemp').addEventListener('input', (e) =>
  $('#tempVal').textContent = (+e.target.value).toFixed(2));
$('#settingsModal').addEventListener('click', (e) => {
  if (e.target.id === 'settingsModal') closeSettings();
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (!$('#settingsModal').classList.contains('hidden')) closeSettings();
  else closeDrawer();
});

boot();
"""
