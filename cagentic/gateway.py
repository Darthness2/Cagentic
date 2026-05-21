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
    <div class="brand"><span class="spark">&#10022;</span> Cagentic</div>
    <button id="newChat" class="new-chat">
      <span class="plus">+</span> New chat
    </button>
    <div class="chats-label">Chats</div>
    <div id="chatList" class="chat-list"></div>
    <div class="sidebar-foot">
      <button id="openSettings" class="foot-btn">
        <span class="gear">&#9881;</span> Settings
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
      <div class="composer-box">
        <textarea id="input" rows="1"
          placeholder="Message Cagentic…"></textarea>
        <button id="send" class="send" title="Send" aria-label="Send">
          <svg viewBox="0 0 24 24" width="18" height="18"><path
            d="M12 19V5M12 5l-6 6M12 5l6 6" fill="none" stroke="currentColor"
            stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
      </div>
      <div class="composer-hint">
        Cagentic runs locally &mdash; it can browse, manage files, take notes &amp; reminders.
      </div>
    </div>
  </main>
</div>
<div id="backdrop" class="backdrop hidden"></div>

<div id="settingsModal" class="modal hidden">
  <div class="modal-card">
    <div class="modal-head">
      <h2>Settings</h2>
      <button id="closeSettings" class="icon-btn">&times;</button>
    </div>
    <div class="modal-body">
      <label class="field">
        <span class="field-label">Model</span>
        <select id="setModel"></select>
      </label>
      <label class="field">
        <span class="field-label">Your name</span>
        <input id="setName" type="text" placeholder="What should I call you?" />
      </label>
      <label class="field">
        <span class="field-label">Temperature
          <em id="tempVal">0.4</em></span>
        <input id="setTemp" type="range" min="0" max="1.5" step="0.05" />
      </label>
      <label class="field row">
        <span class="field-label">Stream responses</span>
        <input id="setStream" type="checkbox" class="switch" />
      </label>
      <label class="field row">
        <span class="field-label">Auto-approve tools
          <em>skip approval prompts</em></span>
        <input id="setYolo" type="checkbox" class="switch" />
      </label>
    </div>
    <div class="modal-foot">
      <button id="saveSettings" class="primary">Save</button>
    </div>
  </div>
</div>

<script src="/app.js"></script>
</body>
</html>
"""

_CSS = """
:root {
  --bg: #141118;
  --sidebar: #100d14;
  --surface: #1d1925;
  --surface-2: #261f31;
  --border: rgba(255,255,255,.07);
  --border-strong: rgba(255,255,255,.13);
  --text: #ece8f0;
  --dim: #9b91a8;
  --faint: #6b6377;
  --accent: #e3a978;
  --accent-soft: rgba(227,169,120,.13);
  --mauve: #bb9dd2;
  --ok: #8ecf9b;
  --err: #e79090;
  --radius: 12px;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 15px/1.62 -apple-system, "Inter", "Segoe UI", system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
}
/* 100dvh tracks the *visible* viewport — so the composer stays put when
   a mobile keyboard opens, unlike 100vh which mobile browsers lie about. */
#app {
  display: grid; grid-template-columns: 268px 1fr;
  height: 100vh; height: 100dvh;
}

/* ---- sidebar ---- */
#sidebar {
  background: var(--sidebar);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
  padding: 16px 12px;
}
.brand {
  font-size: 16px; font-weight: 650; letter-spacing: .04em;
  padding: 6px 8px 16px;
}
.brand .spark { color: var(--accent); margin-right: 4px; }
.new-chat {
  display: flex; align-items: center; gap: 8px;
  width: 100%; padding: 10px 12px; cursor: pointer;
  background: var(--accent-soft); color: var(--text);
  border: 1px solid var(--border-strong); border-radius: 10px;
  font: 600 14px inherit; transition: background .14s, border-color .14s;
}
.new-chat:hover { background: rgba(227,169,120,.2); border-color: var(--accent); }
.new-chat .plus { color: var(--accent); font-size: 17px; line-height: 1; }
.chats-label {
  font-size: 11px; letter-spacing: .1em; text-transform: uppercase;
  color: var(--faint); padding: 18px 8px 6px;
}
.chat-list { flex: 1; overflow-y: auto; margin: 0 -4px; padding: 0 4px; }
.chat-item {
  display: flex; align-items: center; gap: 8px;
  padding: 9px 10px; border-radius: 9px; cursor: pointer;
  color: var(--dim); transition: background .12s;
  position: relative;
}
.chat-item:hover { background: var(--surface); color: var(--text); }
.chat-item.active { background: var(--surface-2); color: var(--text); }
.chat-item.active::before {
  content: ""; position: absolute; left: 0; top: 9px; bottom: 9px;
  width: 2.5px; border-radius: 2px; background: var(--accent);
}
.chat-item .ci-title {
  flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  font-size: 13.5px;
}
.chat-item .ci-del {
  opacity: 0; border: 0; background: transparent; color: var(--faint);
  cursor: pointer; font-size: 15px; padding: 0 2px; line-height: 1;
  transition: opacity .12s, color .12s;
}
.chat-item:hover .ci-del { opacity: 1; }
.chat-item .ci-del:hover { color: var(--err); }
.sidebar-foot { border-top: 1px solid var(--border); padding-top: 10px; margin-top: 8px; }
.foot-btn {
  display: flex; align-items: center; gap: 8px; width: 100%;
  background: transparent; color: var(--dim); border: 0; cursor: pointer;
  padding: 8px 10px; border-radius: 9px; font: 500 13.5px inherit;
  transition: background .12s, color .12s;
}
.foot-btn:hover { background: var(--surface); color: var(--text); }
.foot-meta { font-size: 11px; color: var(--faint); padding: 6px 10px 0; }

/* ---- main ---- */
#main { display: flex; flex-direction: column; min-width: 0; }
#topbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 28px; border-bottom: 1px solid var(--border);
}
#chatTitle {
  flex: 1; min-width: 0;
  font-size: 14.5px; font-weight: 600;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
/* hamburger — hidden on desktop, shown when the sidebar becomes a drawer */
.menu-btn {
  display: none; align-items: center; justify-content: center;
  width: 38px; height: 38px; margin-left: -8px; flex-shrink: 0;
  background: transparent; border: 0; border-radius: 9px;
  color: var(--dim); cursor: pointer;
}
.menu-btn:hover { background: var(--surface); color: var(--text); }
/* drawer backdrop — only ever visible on small screens */
.backdrop { display: none; }
.chip {
  font-size: 12px; color: var(--dim);
  background: var(--surface); border: 1px solid var(--border);
  padding: 4px 10px; border-radius: 999px; white-space: nowrap;
}
.log { flex: 1; overflow-y: auto; padding: 28px 28px 8px; }
.thread { max-width: 740px; margin: 0 auto; }

/* ---- messages ---- */
.row { margin-bottom: 22px; display: flex; }
.row.user { justify-content: flex-end; }
.bubble {
  background: var(--accent-soft); border: 1px solid rgba(227,169,120,.22);
  padding: 10px 15px; border-radius: 14px 14px 4px 14px;
  max-width: 80%; white-space: pre-wrap; word-wrap: break-word;
}
.assistant { display: block; }
.assistant .who {
  display: flex; align-items: center; gap: 7px;
  font-size: 12px; font-weight: 600; color: var(--dim); margin-bottom: 7px;
}
.assistant .who .spark { color: var(--accent); font-size: 13px; }
.assistant .body { color: var(--text); }
.assistant .body p { margin: 0 0 10px; }
.assistant .body p:last-child { margin-bottom: 0; }
.assistant .body h2, .assistant .body h3 {
  font-size: 15px; margin: 16px 0 8px; color: var(--text);
}
.assistant .body ul { margin: 8px 0; padding-left: 20px; }
.assistant .body li { margin: 3px 0; }
.assistant .body a { color: var(--accent); text-decoration: none; }
.assistant .body a:hover { text-decoration: underline; }
.assistant .body code {
  background: var(--surface-2); padding: 1.5px 5px; border-radius: 5px;
  font: 13px ui-monospace, "SF Mono", Menlo, monospace; color: #ecc79b;
}
.assistant .body pre {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 9px; padding: 12px 14px; overflow-x: auto; margin: 10px 0;
}
.assistant .body pre code { background: none; padding: 0; color: var(--text); }

/* tool + status rows */
.tool {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-size: 12.5px; color: var(--dim); margin: 7px 0;
  padding: 6px 11px; background: var(--surface);
  border: 1px solid var(--border); border-radius: 9px;
}
.tool .arrow { color: var(--mauve); }
.tool .tname { color: var(--mauve); font-weight: 600; }
.tool .tsum { color: var(--faint); }
.tool .tres { margin-left: auto; }
.tool.ok .tres { color: var(--ok); }
.tool.bad .tres { color: var(--err); }
.tool.pending .tres::after { content: "…"; color: var(--faint); }
.note {
  font-size: 12.5px; color: var(--faint); font-style: italic;
  margin: 7px 0;
}
.note.err { color: var(--err); font-style: normal; }
.plan {
  background: var(--surface); border: 1px solid var(--border);
  border-left: 2.5px solid var(--accent); border-radius: 9px;
  padding: 11px 14px; margin: 10px 0; font-size: 13.5px;
}
.plan .ph { color: var(--accent); font-weight: 600; margin-bottom: 5px; }
.plan ol { margin: 0; padding-left: 20px; color: var(--dim); }

/* permission card */
.perm {
  background: var(--surface-2); border: 1px solid var(--accent);
  border-radius: 11px; padding: 14px 16px; margin: 12px 0;
}
.perm .pq { margin-bottom: 11px; font-size: 13.5px; }
.perm .pq code {
  background: var(--bg); padding: 2px 7px; border-radius: 5px;
  color: var(--accent); font: 13px ui-monospace, Menlo, monospace;
}
.perm .pbtns { display: flex; gap: 8px; }
.perm button {
  border: 0; border-radius: 8px; padding: 7px 15px; cursor: pointer;
  font: 600 13px inherit; transition: filter .12s;
}
.perm button:hover { filter: brightness(1.12); }
.perm .yes { background: var(--ok); color: #16241a; }
.perm .always { background: var(--accent); color: #241a10; }
.perm .no { background: var(--surface); color: var(--dim);
  border: 1px solid var(--border-strong); }
.perm .decided { color: var(--dim); font-size: 12.5px; }

/* empty state */
.empty {
  height: 100%; display: flex; flex-direction: column;
  align-items: center; justify-content: center; text-align: center;
  color: var(--dim);
}
.empty .big { font-size: 26px; font-weight: 600; color: var(--text); margin-bottom: 8px; }
.empty .sub { font-size: 14px; margin-bottom: 26px; }
.suggestions { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center;
  max-width: 560px; }
.suggestion {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 10px 14px; cursor: pointer; font-size: 13px;
  color: var(--dim); transition: border-color .14s, color .14s, background .14s;
}
.suggestion:hover { border-color: var(--accent); color: var(--text);
  background: var(--surface-2); }

/* composer */
.composer { padding: 8px 28px 20px; }
.composer-box {
  max-width: 740px; margin: 0 auto;
  display: flex; align-items: flex-end; gap: 8px;
  background: var(--surface); border: 1px solid var(--border-strong);
  border-radius: 16px; padding: 8px 8px 8px 16px;
  transition: border-color .14s;
}
.composer-box:focus-within { border-color: var(--accent); }
.composer-box textarea {
  flex: 1; resize: none; background: transparent; border: 0; outline: 0;
  color: var(--text); font: inherit; padding: 7px 0; max-height: 200px;
  line-height: 1.5;
}
.composer-box textarea::placeholder { color: var(--faint); }
.send {
  flex-shrink: 0; width: 36px; height: 36px; border-radius: 10px;
  border: 0; cursor: pointer; background: var(--accent); color: #1b130b;
  display: flex; align-items: center; justify-content: center;
  transition: filter .12s, opacity .12s;
}
.send:hover { filter: brightness(1.1); }
.send:disabled { opacity: .4; cursor: default; }
.composer-hint {
  max-width: 740px; margin: 9px auto 0; text-align: center;
  font-size: 11.5px; color: var(--faint);
}

/* modal */
.modal {
  position: fixed; inset: 0; background: rgba(0,0,0,.55);
  display: flex; align-items: center; justify-content: center; z-index: 20;
}
.modal.hidden { display: none; }
.modal-card {
  background: var(--bg); border: 1px solid var(--border-strong);
  border-radius: 14px; width: 440px; max-width: calc(100vw - 40px);
  box-shadow: 0 24px 60px rgba(0,0,0,.5);
}
.modal-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 20px; border-bottom: 1px solid var(--border);
}
.modal-head h2 { margin: 0; font-size: 16px; }
.icon-btn {
  background: transparent; border: 0; color: var(--dim); cursor: pointer;
  font-size: 22px; line-height: 1; padding: 0 4px;
}
.icon-btn:hover { color: var(--text); }
.modal-body { padding: 18px 20px; display: flex; flex-direction: column; gap: 16px; }
.field { display: flex; flex-direction: column; gap: 7px; }
.field.row { flex-direction: row; align-items: center; justify-content: space-between; }
.field-label { font-size: 13px; font-weight: 600; }
.field-label em { color: var(--faint); font-weight: 400; font-style: normal;
  margin-left: 6px; font-size: 12px; }
.field select, .field input[type=text] {
  background: var(--surface); border: 1px solid var(--border-strong);
  border-radius: 8px; color: var(--text); padding: 9px 11px; font: inherit;
}
.field select:focus, .field input[type=text]:focus { outline: 0; border-color: var(--accent); }
.field input[type=range] { accent-color: var(--accent); }
.switch { width: 38px; height: 21px; accent-color: var(--accent); cursor: pointer; }
.modal-foot {
  padding: 14px 20px; border-top: 1px solid var(--border);
  display: flex; justify-content: flex-end;
}
.primary {
  background: var(--accent); color: #1b130b; border: 0; border-radius: 9px;
  padding: 9px 20px; font: 600 14px inherit; cursor: pointer;
}
.primary:hover { filter: brightness(1.1); }

/* scrollbars */
::-webkit-scrollbar { width: 9px; height: 9px; }
::-webkit-scrollbar-thumb { background: var(--surface-2); border-radius: 6px; }
::-webkit-scrollbar-thumb:hover { background: #322a40; }
::-webkit-scrollbar-track { background: transparent; }

.cursor::after {
  content: "▌"; color: var(--accent); animation: blink 1s steps(2) infinite;
}
@keyframes blink { 50% { opacity: 0; } }

/* ---- phones & small tablets ---- */
@media (max-width: 760px) {
  /* single column — the sidebar lifts out into a slide-over drawer */
  #app { grid-template-columns: 1fr; }
  #sidebar {
    position: fixed; top: 0; bottom: 0; left: 0; width: 286px; max-width: 86vw;
    z-index: 30; transform: translateX(-100%);
    transition: transform .22s ease;
    padding-top: max(16px, env(safe-area-inset-top));
  }
  #sidebar.open { transform: translateX(0); box-shadow: 0 0 40px rgba(0,0,0,.6); }
  .backdrop {
    display: block; position: fixed; inset: 0;
    background: rgba(0,0,0,.55); z-index: 25;
  }
  .backdrop.hidden { display: none; }
  .menu-btn { display: flex; }

  #topbar { padding: 12px 14px; gap: 6px; }
  .chip { max-width: 42vw; overflow: hidden; text-overflow: ellipsis; }
  .log { padding: 18px 14px 6px; }
  .composer {
    padding: 8px 14px max(14px, env(safe-area-inset-bottom));
  }
  .composer-hint { display: none; }
  .bubble { max-width: 88%; }

  /* always-visible delete (no hover on touch) + bigger tap targets */
  .chat-item .ci-del { opacity: 1; padding: 4px 6px; }
  .chat-item { padding: 11px 10px; }
  .suggestion { padding: 12px 14px; }

  /* 16px form text stops iOS from auto-zooming when a field is focused */
  .composer-box textarea,
  .field select,
  .field input[type=text] { font-size: 16px; }

  .modal { align-items: flex-end; }
  .modal-card {
    width: 100%; max-width: 100%;
    border-radius: 16px 16px 0 0;
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
function ago(ts) {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return 'just now';
  if (d < 3600) return Math.floor(d / 60) + 'm ago';
  if (d < 86400) return Math.floor(d / 3600) + 'h ago';
  return Math.floor(d / 86400) + 'd ago';
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

// ---- rendering ------------------------------------------------------------
function clearLog() { log.innerHTML = ''; }

function showEmpty() {
  clearLog();
  const name = state.settings.user_name;
  const hour = new Date().getHours();
  const greet = hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
  const e = document.createElement('div');
  e.className = 'empty';
  e.innerHTML =
    '<div class="big">' + greet + (name ? ', ' + esc(name) : '') + '</div>' +
    '<div class="sub">How can I help you today?</div>' +
    '<div class="suggestions"></div>';
  const sg = e.querySelector('.suggestions');
  ['Summarize what\'s on my screen', 'What are my reminders?',
   'Take a note for me', 'Search the web for something'].forEach(t => {
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
  r.innerHTML = '<div class="bubble">' + esc(text) + '</div>';
  thread().appendChild(r); scrollDown();
}
function addAssistant(html, tools) {
  const r = document.createElement('div');
  r.className = 'row assistant';
  r.innerHTML = '<div class="who"><span class="spark">&#10022;</span> Cagentic</div>' +
    '<div class="body">' + (html || '') + '</div>';
  thread().appendChild(r);
  (tools || []).forEach(t => addToolRow(t, true));
  scrollDown();
  return r.querySelector('.body');
}

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
    p.innerHTML = '<div class="ph">&#10047; Plan</div><ol>' +
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
      res.textContent = (d.ok ? '✓ ' : '✗ ') +
        (d.first_line || '').slice(0, 90);
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
  box.innerHTML = '<div class="pq">Cagentic wants to run <code>' + esc(d.tool) +
    '</code>' + (d.summary ? ' &mdash; ' + esc(d.summary) : '') + '</div>';
  const btns = document.createElement('div'); btns.className = 'pbtns';
  const answer = (a, label) => {
    box.innerHTML = '<div class="pq">' + esc(d.tool) +
      '</div><div class="decided">&rarr; ' + label + '</div>';
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

// ---- sidebar --------------------------------------------------------------
function renderChats() {
  const list = $('#chatList');
  list.innerHTML = '';
  if (!state.chats.length) {
    list.innerHTML = '<div class="foot-meta">No chats yet</div>';
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
  closeDrawer();
  input.focus();
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
  $('#chatTitle').textContent = (b.current.title || 'New chat');
  renderChats();
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
// ---- drawer (mobile) ------------------------------------------------------
function openDrawer() {
  $('#sidebar').classList.add('open');
  $('#backdrop').classList.remove('hidden');
}
function closeDrawer() {
  $('#sidebar').classList.remove('open');
  $('#backdrop').classList.add('hidden');
}

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
}

// ---- composer -------------------------------------------------------------
function autoGrow() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 200) + 'px';
}
input.addEventListener('input', autoGrow);
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    submit();
  }
});
function submit() {
  const text = input.value.trim();
  if (!text || state.busy) return;
  input.value = ''; autoGrow();
  send(text);
}
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
