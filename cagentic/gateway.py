"""/gateway — a local web app for Cagentic.

Starts an HTTP server (default port 8700) that serves a polished chat UI
and runs the full agent behind it: the same tools, notes, reminders, MCP
servers, browser control — everything the terminal REPL can do.

The app has a sidebar of saved chats, a settings panel, and streams each
turn token-by-token. HUD panels appear as draggable floating windows.
Tools that need approval surface an Approve / Deny prompt right in the
page. Bound to localhost only.
"""
from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as _config
from . import sessions
from . import projects
from .providers import build_client as _build_client, parse_model as _parse_model, list_all_models as _all_models


class _ClientGone(Exception):
    """Raised when the browser hangs up mid-stream."""


from .engine import _THINK_RX, _PLAN_RX

_STEP_RX = re.compile(r"<step\s+\d+(?:\s*/\s*\d+)?\s*>", re.IGNORECASE)


def _clean(text: str) -> str:
    text = _THINK_RX.sub("", text)
    text = _PLAN_RX.sub("", text)
    text = _STEP_RX.sub("", text)
    return text.strip()


# Taught to the gateway's engine only — lets the model "summon" panels as
# floating windows by emitting fenced ```hud blocks of JSON. The web
# UI parses these out of the reply, renders them as draggable cards, and
# strips them from the chat text. Purely optional sugar — plain replies still
# work — but it makes the interface feel alive.
_HUD_INSTRUCTIONS = """=== HUD Display ===
You are speaking through a heads-up display. Besides your normal
reply, you MAY render visual panels as floating windows by emitting one or more
fenced code blocks with the language tag `hud`, each containing a single JSON
object. Use them when a visual would help — comparisons, status, search hits,
images, locations, key numbers, charts. Keep prose short when you show a panel.

Panel schemas (pick the type that fits; all fields optional except shown):
  {"panel":"stats","title":"...","items":[{"label":"...","value":"...","accent":"ok|warn|hot"}]}
  {"panel":"metric","title":"...","value":"42","unit":"%","sub":"...","trend":"up|down|flat"}
  {"panel":"list","title":"...","items":["...","..."]}
  {"panel":"table","title":"...","columns":["A","B"],"rows":[["1","2"],["3","4"]]}
  {"panel":"image","title":"...","url":"https://...","caption":"..."}
  {"panel":"web","title":"...","results":[{"title":"...","url":"https://...","snippet":"..."}]}
  {"panel":"alert","level":"info|warn|critical","title":"...","text":"..."}
  {"panel":"progress","title":"...","items":[{"label":"...","pct":75}]}
  {"panel":"map","title":"...","lat":34.05,"lon":-118.24,"label":"..."}
  {"panel":"bar","title":"...","labels":["Jan","Feb"],"values":[42,87],"color":"#f0a87a"}
  {"panel":"line","title":"...","labels":["Mon","Tue"],"datasets":[{"label":"CPU","values":[30,80],"color":"#f0a87a"}]}
  {"panel":"pie","title":"...","labels":["A","B","C"],"values":[40,35,25]}
  {"panel":"clear"}   ← closes all floating windows when you want a fresh display

Rules:
- Emit `hud` blocks ONLY for things worth visualizing. Don't wrap every reply.
- Each block = exactly one JSON object, valid JSON, double quotes.
- Use charts (bar/line/pie) for numeric comparisons, trends, and distributions.
- Emit {"panel":"clear"} before new panels when replacing the previous display.
- After tool calls that return structured data, a matching panel is a nice touch.
- Still write a brief natural-language reply alongside the panels.
- Panels appear as draggable floating windows the user can move around freely.
"""


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
        # Teach this engine (gateway only) to drive the holographic HUD.
        self.engine.system_suffix = _HUD_INSTRUCTIONS
        self.engine.refresh_system_prompt()
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
                "project_id": s.get("project_id", ""),
            })
        return out

    def render_messages(self, messages: list[dict]) -> list[dict]:
        """Turn stored messages into display items for the web UI."""
        out: list[dict] = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                continue
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
            "model": self.agent.model,
            "messages": self.render_messages(self.engine.messages),
        }

    def new_chat(self) -> dict:
        self._save_current()
        self.session = sessions.make(self.agent.model)
        self.engine.project_system_prompt = ""
        self.engine.project_context = ""
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
        # Apply project config if chat belongs to a project
        pid = data.get("project_id", "")
        if pid:
            proj = projects.load(pid)
            if proj:
                self.engine.project_system_prompt = proj.get("system_prompt", "")
                self.engine.project_context = proj.get("context", "")
            else:
                self.engine.project_system_prompt = ""
                self.engine.project_context = ""
        else:
            self.engine.project_system_prompt = ""
            self.engine.project_context = ""
        self.engine.refresh_system_prompt()
        return self.current_chat()

    def delete_chat(self, chat_id: str) -> dict:
        sessions.delete(chat_id)
        # Remove from any project
        for proj in projects.list_all():
            if chat_id in proj.get("chats", []):
                projects.remove_chat(proj["id"], chat_id)
        if chat_id == self.session.get("id"):
            self.new_chat()
        return {"chats": self.list_chats(), "current": self.current_chat(), "projects": self.list_projects()}

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

    # -- projects ------------------------------------------------------------

    def list_projects(self) -> list[dict]:
        return projects.list_all()

    def create_project(self, name: str, color: str | None = None) -> dict:
        proj = projects.create(name, color)
        return {"project": proj, "projects": self.list_projects()}

    def delete_project(self, project_id: str) -> dict:
        projects.delete(project_id)
        return {"projects": self.list_projects()}

    def rename_project(self, project_id: str, name: str) -> dict:
        proj = projects.rename(project_id, name)
        return {"project": proj, "projects": self.list_projects()}

    def add_chat_to_project(self, project_id: str, chat_id: str) -> dict:
        proj = projects.add_chat(project_id, chat_id)
        # Also update the session's project_id
        data = sessions.load(chat_id)
        if data:
            data["project_id"] = project_id
            sessions.save(data)
        # If this is the current chat, apply project config
        if self.session.get("id") == chat_id:
            if proj:
                self.engine.project_system_prompt = proj.get("system_prompt", "")
                self.engine.project_context = proj.get("context", "")
            self.engine.refresh_system_prompt()
        return {"project": proj, "projects": self.list_projects(), "chats": self.list_chats()}

    def remove_chat_from_project(self, project_id: str, chat_id: str) -> dict:
        proj = projects.remove_chat(project_id, chat_id)
        data = sessions.load(chat_id)
        if data and data.get("project_id") == project_id:
            data["project_id"] = ""
            sessions.save(data)
        # If this is the current chat, clear project config
        if self.session.get("id") == chat_id:
            self.engine.project_system_prompt = ""
            self.engine.project_context = ""
            self.engine.refresh_system_prompt()
        return {"project": proj, "projects": self.list_projects(), "chats": self.list_chats()}

    def update_project_config(self, project_id: str, system_prompt: str, context: str) -> dict:
        proj = projects.update_config(project_id, system_prompt, context)
        # If the current chat belongs to this project, refresh the system prompt
        if self.session.get("project_id") == project_id:
            self.engine.project_system_prompt = system_prompt
            self.engine.project_context = context
            self.engine.refresh_system_prompt()
        return {"project": proj, "projects": self.list_projects()}

    # -- settings -----------------------------------------------------------

    def get_settings(self) -> dict:
        # Collect models from all configured providers, flattened to one list.
        all_provider_models = _all_models(self.config)
        models: list[str] = []
        for provider, mlist in all_provider_models.items():
            models.extend(mlist)
        # Current model shown with provider prefix if cloud.
        current = self.agent.model
        return {
            "model": current,
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

    def set_model(self, model: str) -> dict:
        """Switch the active model instantly (used by the header dropdown).

        Accepts plain model names (Ollama) or 'provider:model' strings
        (e.g. 'openai:gpt-4o', 'anthropic:claude-opus-4-8').
        """
        model = (model or "").strip()
        if not model:
            return {"model": self.agent.model}

        provider, model_name = _parse_model(model)
        if provider != "ollama":
            try:
                new_client = _build_client(self.config, provider)
                self.agent.client = new_client
                self.engine.client = new_client
            except RuntimeError as e:
                return {"error": str(e), "model": self.agent.model}

        self.agent.model = model_name
        self.engine.model = model_name
        self.config["model"] = model  # persist provider:model
        try:
            _config.save(self.config)
        except Exception:
            pass
        return {"model": model}

    def bootstrap(self) -> dict:
        return {
            "version": __import__("cagentic").__version__,
            "user_name": self.agent.state.user_name,
            "model": self.agent.model,
            "chats": self.list_chats(),
            "current": self.current_chat(),
            "settings": self.get_settings(),
            "projects": self.list_projects(),
        }

    # -- a chat turn --------------------------------------------------------

    def edit_and_resend(self, index: int, message: str, emit) -> None:
        """Truncate history after the *index*-th user message, replace its text
        with *message*, and re-run the turn from that point."""
        if not self._turn_lock.acquire(blocking=False):
            emit("error", {"text": "Cagentic is still working on the previous message."})
            return
        # Find user messages in the engine's message list
        user_indices = [i for i, m in enumerate(self.engine.messages) if m.get("role") == "user"]
        if index < 0 or index >= len(user_indices):
            self._turn_lock.release()
            emit("error", {"text": f"invalid message index {index}"})
            return
        target = user_indices[index]
        # Truncate to before the target user message so submit_message adds it fresh
        self.engine.messages = self.engine.messages[:target]
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

    def delete_message(self, index: int) -> dict:
        """Delete the *index*-th user message and everything after it."""
        user_indices = [i for i, m in enumerate(self.engine.messages) if m.get("role") == "user"]
        if index < 0 or index >= len(user_indices):
            return {"error": f"invalid message index {index}"}
        target = user_indices[index]
        # Truncate everything from this user message onward
        self.engine.messages = self.engine.messages[:target]
        self._save_current()
        return self.current_chat()

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

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # Windows often aborts connections when the browser navigates away
            # or cancels a request.  Silently ignore rather than printing a traceback.
            self.close_connection = True

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
        elif path == "/api/projects":
            self._json(self._gw().list_projects())
        else:
            self._send(b"not found", "text/plain", status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        gw = self._gw()
        if path == "/api/chat":
            self._stream_chat(str(self._body().get("message", "")).strip())
            return
        if path == "/api/chat/edit":
            b = self._body()
            self._stream_chat_edit(int(b.get("index", 0)), str(b.get("message", "")).strip())
            return
        if path == "/api/chat/delete-msg":
            b = self._body()
            self._json(self._gw().delete_message(int(b.get("index", 0))))
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
        if path == "/api/model":
            self._json(gw.set_model(str(self._body().get("model", ""))))
            return
        if path == "/api/projects/create":
            b = self._body()
            self._json(gw.create_project(str(b.get("name", "")), b.get("color")))
            return
        if path == "/api/projects/delete":
            self._json(gw.delete_project(str(self._body().get("id", ""))))
            return
        if path == "/api/projects/rename":
            b = self._body()
            self._json(gw.rename_project(str(b.get("id", "")), str(b.get("name", ""))))
            return
        if path == "/api/projects/add_chat":
            b = self._body()
            self._json(gw.add_chat_to_project(str(b.get("project_id", "")), str(b.get("chat_id", ""))))
            return
        if path == "/api/projects/remove_chat":
            b = self._body()
            self._json(gw.remove_chat_from_project(str(b.get("project_id", "")), str(b.get("chat_id", ""))))
            return
        if path == "/api/projects/config":
            b = self._body()
            self._json(gw.update_project_config(str(b.get("id", "")), b.get("system_prompt", ""), b.get("context", "")))
            return
        self._send(b"not found", "text/plain", status=404)

    def _begin_sse(self):
        """Set up SSE response headers and return an emit callback."""
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
                raise _ClientGone()
        return emit

    def _stream_chat(self, message: str) -> None:
        emit = self._begin_sse()
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

    def _stream_chat_edit(self, index: int, message: str) -> None:
        emit = self._begin_sse()
        if not message:
            try:
                emit("error", {"text": "empty message"})
            except _ClientGone:
                pass
            return
        try:
            self._gw().edit_and_resend(index, message, emit)
            emit("end", {})
        except _ClientGone:
            return


# ---------------------------------------------------------------- the page --
# Cagentic — AI Assistant
# Full HUD interface for Cagentic.
_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Cagentic</title>
<link rel="stylesheet" href="/app.css" />
</head>
<body>
<div id="app">
  <div class="scanlines"></div>
  <div class="vignette"></div>

  <header class="hud-header">
    <div class="hdr-left">
        <span class="jl">C</span><span class="jd">&middot;</span><span class="jl">A</span><span class="jd">&middot;</span><span class="jl">G</span><span class="jd">&middot;</span><span class="jl">E</span><span class="jd">&middot;</span><span class="jl">N</span><span class="jd">&middot;</span><span class="jl">T</span><span class="jd">&middot;</span><span class="jl">I</span><span class="jd">&middot;</span><span class="jl">C</span>
      <span class="j-sub">COGNITIVE AGENT NETWORK FOR INTELLIGENT COMPUTING</span>
    </div>
    <div class="hdr-right">
      <span class="badge b-on">&#9679; Connected</span>
      <div class="model-switch" id="modelSwitch" title="Switch model">
        <span class="ms-dot">&#9679;</span>
        <span class="ms-name" id="msName">---</span>
        <span class="ms-caret">&#9662;</span>
        <div class="model-menu hidden" id="modelMenu"></div>
      </div>
      <div class="j-clock" id="jClock">00:00:00</div>
      <div class="j-date"  id="jDate">---</div>
    </div>
  </header>

  <div class="nav-bar">
    <button class="nav-btn" id="logsBtn">[ Chats ]</button>
    <button class="nav-btn" id="newMissionBtn">[ + New Chat ]</button>
    <div class="nav-divider"></div>
    <span class="nav-meta">SESSION <span id="jSession">--------</span></span>
    <div class="nav-spacer"></div>
    <button class="nav-btn toggle-btn" id="voiceOutBtn" title="Read replies aloud">[ &#128264; Voice: OFF ]</button>
  
    <div class="nav-divider"></div>
    <button class="nav-btn" id="configBtn">[ CONFIG ]</button>
  </div>

  <div class="main-area">
    <div class="center-stack" id="centerStack">
      <div class="orb-zone" id="orbZone">
        <canvas id="orbCanvas"></canvas>
        <div class="orb-rings">
          <div class="ring r1"></div><div class="ring r2"></div>
          <div class="ring r3"></div><div class="ring r4"></div>
        </div>
        <div class="orb-label" id="orbLabel">New Chat</div>
      </div>
      <div id="log" class="chat-log"></div>
    </div>
  </div>

  <div id="windowLayer"></div>

  <div class="cmd-area">
    <div class="cmd-box" id="cmdBox">
      <span class="cmd-prompt">&gt;_</span>
      <textarea id="input" rows="1" placeholder="Type a message&#8230;"></textarea>
      <button id="micBtn" class="mic-btn" title="Voice input">&#127908;</button>
      <button id="send" class="exec-btn">EXECUTE</button>
    </div>
    <div class="cmd-footer">
      <span>CAGENTIC v<span id="versionSpan">--</span></span>
      <span id="hintText">Enter to send &bull; Shift+Enter for newline &bull; Ctrl+K New Chat</span>
      <span id="busyLabel" class="busy-label hidden">&#9679; Thinking&#8230;</span>
    </div>
  </div>
</div>

<!-- Sessions drawer -->
<div id="sessionsPanel" class="sessions-panel">
  <div class="sessions-head">
    <span class="panel-hdr">&#123; Sessions &#125;</span>
    <button id="closeSessionsBtn" class="icon-btn">&#10005;</button>
  </div>
  <div id="sessionList" class="session-list"></div>
</div>

<div id="backdrop" class="backdrop hidden"></div>

<!-- Settings modal -->
<div id="settingsModal" class="modal hidden">
  <div class="modal-card">
    <div class="modal-head">
      <span class="panel-hdr">&#123; Settings &#125;</span>
      <button id="closeSettings" class="icon-btn">&#10005;</button>
    </div>
    <div class="modal-body">
      <div class="field">
        <span class="field-label">MODEL</span>
        <select id="setModel"></select>
      </div>
      <div class="field">
        <span class="field-label">OPERATOR NAME</span>
        <input id="setName" type="text" placeholder="Your name" />
      </div>
      <div class="field">
        <span class="field-label">TEMPERATURE &nbsp;<em id="tempVal">0.40</em></span>
        <input id="setTemp" type="range" min="0" max="1.5" step="0.05" />
      </div>
      <div class="field">
        <span class="field-label">Voice (TTS)</span>
        <select id="setVoice"></select>
      </div>
      <div class="field row">
        <span class="field-label">Stream responses</span>
        <label class="toggle"><input id="setStream" type="checkbox" /><span></span></label>
      </div>
      <div class="field row">
        <span class="field-label">Auto-approve tools</span>
        <label class="toggle"><input id="setYolo" type="checkbox" /><span></span></label>
      </div>
    </div>
    <div class="modal-foot">
      <button id="cancelSettings" class="btn-ghost">Cancel</button>
      <button id="saveSettings"   class="btn-primary">Save</button>
    </div>
  </div>
</div>
<!-- Confirm modal -->
<div id="confirmModal" class="modal hidden">
  <div class="modal-card sm">
      <span id="confirmTitle" class="modal-title">Confirm</span>
      <button id="confirmClose" class="icon-btn">&#10005;</button>
    </div>
    <div class="modal-body">
      <p id="confirmMsg" class="modal-msg"></p>
    </div>
    <div class="modal-foot">
      <button id="confirmCancel" class="btn-ghost">Cancel</button>
      <button id="confirmOk" class="btn-primary">Delete</button>
    </div>
  </div>
</div>

<!-- Rename modal -->
<div id="renameModal" class="modal hidden">
  <div class="modal-card md">
    <div class="modal-head">
      <span class="modal-title">Rename</span>
      <button id="renameClose" class="icon-btn">&#10005;</button>
    </div>
    <div class="modal-body">
      <input id="renameInput" type="text" class="modal-input" />
    </div>
    <div class="modal-foot">
      <button id="renameCancel" class="btn-ghost">Cancel</button>
      <button id="renameOk" class="btn-primary">Rename</button>
    </div>
  </div>
</div>

<!-- New project modal -->
<div id="newProjectModal" class="modal hidden">
  <div class="modal-card md">
    <div class="modal-head">
      <span class="modal-title">New Project</span>
      <button id="newProjectModalClose" class="icon-btn">&#10005;</button>
    </div>
    <div class="modal-body">
      <input id="newProjectInput" type="text" placeholder="Project name" class="modal-input" />
    </div>
    <div class="modal-foot">
      <button id="newProjectCancel" class="btn-ghost">Cancel</button>
      <button id="newProjectOk" class="btn-primary">Create</button>
    </div>
  </div>
</div>

<!-- Add to project modal -->
<div id="projectModal" class="modal hidden">
  <div class="modal-card md">
    <div class="modal-head">
      <span class="modal-title">Add to Project</span>
      <button id="projectModalClose" class="icon-btn">&#10005;</button>
    </div>
    <div class="modal-body" id="projectModalBody">
    </div>
    <div class="modal-foot">
      <button id="projectModalNewBtn" class="btn-ghost">+ New Project</button>
      <button id="projectModalCancel" class="btn-ghost">Cancel</button>
    </div>
  </div>
</div>

<!-- Project config modal -->
<div id="projConfigModal" class="modal hidden">
  <div class="modal-card lg">
    <div class="modal-head">
      <span class="modal-title">Project Config</span>
      <button id="projConfigClose" class="icon-btn">&#10005;</button>
    </div>
    <div class="modal-body">
      <div class="field">
        <span class="field-label">SYSTEM PROMPT</span>
        <textarea id="projConfigPrompt" rows="5" class="modal-textarea" placeholder="Custom instructions for this project's chats&#10;(appended after the base system prompt)"></textarea>
      </div>
      <div class="field">
        <span class="field-label">CONTEXT / NOTES</span>
        <textarea id="projConfigContext" rows="5" class="modal-textarea" placeholder="Reference material always included in this project's chats&#10;(e.g. coding standards, project background, key contacts)"></textarea>
      </div>
    </div>
    <div class="modal-foot">
      <button id="projConfigCancel" class="btn-ghost">Cancel</button>
      <button id="projConfigSave" class="btn-primary">Save</button>
    </div>
  </div>
</div>

<!-- Context menu (floating) -->
<div id="ctxMenu" class="ctx-menu hidden">
  <div class="ctx-item" data-action="rename">&#9998; Rename</div>
  <div class="ctx-item" data-action="project">&#128193; Add to Project</div>
  <div class="ctx-sep"></div>
  <div class="ctx-item ctx-danger" data-action="delete">&#128465; Delete</div>
</div>

<script src="/app.js"></script>
</body>
</html>
"""

_CSS = """
/* ===== Cagentic ===================================== */
:root {
  --bg:       #161118;
  --accent:   #f0a87a;
  --accent-dim:rgba(240,168,122,.1);
  --accent-glow:rgba(240,168,122,.35);
  --text:     #ece7f0;
  --text-2:   #b0a6ba;
  --text-dim: #7d7388;
  --ok:       #8ecf95;
  --warn:     #e6c073;
  --hot:      #e5928f;
  --border:   rgba(236,231,240,.08);
  --border-h: rgba(236,231,240,.14);
  --panel-bg: rgba(34,27,42,.88);
  --grid:     rgba(240,168,122,.03);
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
::selection { background: rgba(240,168,122,.22); }
.hidden { display: none !important; }

.scanlines {
  position: fixed; inset: 0; pointer-events: none; z-index: 9999;
  background: repeating-linear-gradient(
    0deg, transparent, transparent 2px, rgba(0,0,0,.045) 2px, rgba(0,0,0,.045) 4px);
}
.vignette {
  position: fixed; inset: 0; pointer-events: none; z-index: 9998;
  background: radial-gradient(ellipse at center, transparent 50%, rgba(22,17,24,.8) 100%);
}
#app { display: flex; flex-direction: column; height: 100vh; height: 100dvh; }

/* ---- HEADER ---------------------------------------------------------------- */
.hud-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 20px 7px; border-bottom: 1px solid var(--border);
  background: rgba(22,17,24,.75); flex-shrink: 0; gap: 16px;
}
.hdr-left { display: flex; align-items: baseline; gap: 0; flex-shrink: 0; }
.jl { font-size: 20px; color: #fff; text-shadow: 0 0 16px var(--accent); letter-spacing: .18em; }
.jd { font-size: 20px; color: var(--accent); letter-spacing: .18em; }
.j-sub {
  font-size: 8px; color: var(--text-2); letter-spacing: .14em;
  margin-left: 18px; align-self: flex-end; padding-bottom: 3px; text-transform: uppercase;
}
.badge {
  font-size: 9px; padding: 3px 9px; border: 1px solid;
  letter-spacing: .1em; text-transform: uppercase; white-space: nowrap;
}
.b-on  { color: var(--ok);  border-color: rgba(142,207,149,.35); background: rgba(142,207,149,.05); }

/* model switcher */
.model-switch {
  position: relative; display: flex; align-items: center; gap: 6px;
  font-size: 9px; padding: 3px 10px; cursor: pointer;
  border: 1px solid rgba(255,170,0,.4); background: rgba(255,170,0,.05);
  color: var(--warn); letter-spacing: .1em; text-transform: uppercase;
  transition: background .15s;
}
.model-switch:hover { background: rgba(255,170,0,.14); }
.ms-dot { font-size: 8px; }
.ms-caret { font-size: 8px; opacity: .7; }
.model-menu {
  position: absolute; top: 100%; left: 0; margin-top: 4px; z-index: 400;
  min-width: 200px; max-height: 320px; overflow-y: auto;
  background: #1e1728; border: 1px solid var(--border-h);
  box-shadow: 0 0 30px rgba(240,168,122,.18);
}
.mm-item {
  padding: 8px 11px; font-size: 10px; color: var(--text-2);
  cursor: pointer; letter-spacing: .05em; border-bottom: 1px solid rgba(240,168,122,.06);
  white-space: nowrap; display: flex; align-items: center; gap: 7px;
}
.mm-item:hover  { background: rgba(240,168,122,.08); color: var(--text); }
.mm-item.active { color: var(--accent); }
.mm-item .mm-tick { color: var(--accent); width: 8px; }

.hdr-right { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
.j-clock { font-size: 22px; color: #fff; letter-spacing: .12em; text-shadow: 0 0 18px var(--accent-glow); }
.j-date { font-size: 9px; color: var(--text-2); letter-spacing: .1em; margin-top: 2px; }

/* ---- NAV BAR --------------------------------------------------------------- */
.nav-bar {
  display: flex; align-items: center; gap: 10px; padding: 5px 20px;
  border-bottom: 1px solid var(--border); background: rgba(22,17,24,.6); flex-shrink: 0;
}
.nav-btn {
  background: var(--accent-dim); border: 1px solid var(--border);
  color: var(--accent); font: 9px var(--mono); cursor: pointer;
  padding: 4px 11px; letter-spacing: .12em; text-transform: uppercase;
  transition: background .15s, border-color .15s; white-space: nowrap;
}
.nav-btn:hover { background: rgba(240,168,122,.22); border-color: var(--border-h); }
.nav-btn.active { background: rgba(240,168,122,.28); border-color: var(--accent); color: #fff; }
.nav-divider { width: 1px; height: 16px; background: var(--border); }
.nav-spacer { flex: 1; }
.nav-meta { font-size: 9px; color: var(--text-dim); letter-spacing: .08em; white-space: nowrap; }

/* ---- MAIN AREA ------------------------------------------------------------- */
.main-area { flex: 1; display: flex; min-height: 0; overflow: hidden; }
.center-stack { flex: 1; display: flex; flex-direction: column; min-height: 0; min-width: 0; }

/* ---- ORB ZONE -------------------------------------------------------------- */
.orb-zone {
  position: relative; flex-shrink: 0; height: 300px;
  display: flex; align-items: center; justify-content: center;
  background: radial-gradient(ellipse 70% 80% at 50% 55%,
    rgba(120,60,40,.35) 0%, rgba(40,20,30,.15) 60%, transparent 100%);
  border-bottom: 1px solid var(--border); overflow: hidden;
  transition: height .3s ease;
}
.orb-zone.compact { height: 150px; }
#orbCanvas { position: absolute; inset: 0; width: 100%; height: 100%; }
.orb-rings { position: absolute; top: 50%; left: 50%; pointer-events: none; }
.ring { position: absolute; border-radius: 50%; border: 1px solid; }
.r1 { width: 320px; height: 320px; margin: -160px 0 0 -160px; border-color: rgba(240,168,122,.1);  animation: spin1 28s linear infinite; }
.r2 { width: 250px; height: 250px; margin: -125px 0 0 -125px; border-color: rgba(240,168,122,.18); border-style: dashed; animation: spin2 18s linear infinite; }
.r3 { width: 185px; height: 185px; margin: -92px  0 0 -92px;  border-color: rgba(240,168,122,.28); animation: spin1 13s linear infinite; }
.r4 { width: 120px; height: 120px; margin: -60px  0 0 -60px;  border-color: rgba(240,168,122,.42); animation: spin2 8s  linear infinite; }
@keyframes spin1 { to { transform: rotate(360deg);  } }
@keyframes spin2 { to { transform: rotate(-360deg); } }
.orb-label {
  position: absolute; bottom: 12px; left: 50%; transform: translateX(-50%);
  font-size: 9px; color: var(--text-dim); letter-spacing: .18em;
  text-transform: uppercase; white-space: nowrap; pointer-events: none;
}
.orb-zone.listening .orb-label { color: var(--ok); }
.orb-zone.speaking  .orb-label { color: var(--accent); }

/* ---- CHAT LOG -------------------------------------------------------------- */
.chat-log { flex: 1; overflow-y: auto; padding: 16px 0; min-height: 0; }
.j-thread { max-width: 820px; margin: 0 auto; padding: 0 24px; }
.j-empty { max-width: 820px; margin: 0 auto; padding: 24px 24px 0; }
.j-empty-title {
  font-size: 11px; color: var(--text-2); letter-spacing: .2em;
  text-transform: uppercase; margin-bottom: 20px; text-align: center;
}
.quick-cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.qcard {
  padding: 14px 16px; border: 1px solid var(--border);
  background: rgba(240,168,122,.03); cursor: pointer;
  transition: background .15s, border-color .15s, transform .15s; text-align: left;
}
.qcard:hover { background: rgba(240,168,122,.08); border-color: var(--border-h); transform: translateY(-2px); }
.qcard-icon { font-size: 18px; margin-bottom: 7px; display: block; }
.qcard-title { font-size: 11px; color: #d8c8e0; letter-spacing: .05em; display: block; margin-bottom: 3px; }
.qcard-sub   { font-size: 9px;  color: var(--text-2); letter-spacing: .04em; line-height: 1.5; display: block; }

/* messages */
.msg-row { margin: 10px 0; animation: fadeIn .25s ease; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } }
.msg-row.user { display: flex; flex-direction: column; align-items: flex-end; }
.msg-row.user .bubble {
  background: rgba(142,100,120,.28); border: 1px solid rgba(240,168,122,.3);
  padding: 9px 14px; max-width: 78%; font-size: 12px; color: #d8c8e0;
  line-height: 1.55; letter-spacing: .02em;
}
.msg-row.user .bubble::before { content: "> "; color: var(--accent); }
.msg-actions { display: flex; gap: 6px; padding: 2px 0 0; }
.msg-act-btn {
  background: transparent; border: 0; color: var(--text-dim);
  padding: 0; font: 9px/1.4 var(--mono); letter-spacing: .06em; cursor: pointer;
  transition: color .15s;
}
.msg-act-btn:hover { color: var(--accent); }
.msg-act-btn.del-btn:hover { color: var(--hot); }
.msg-row.user.editing .bubble { background: rgba(142,100,120,.18); }
.edit-area {
  width: 100%; min-height: 40px; background: rgba(22,17,24,.6); border: 1px solid var(--accent);
  color: var(--text); font: 12px/1.55 var(--mono); padding: 6px 8px; resize: vertical;
  outline: none; box-sizing: border-box;
}
.edit-save { color: #8ecf95 !important; border-color: #8ecf95 !important; }
.edit-save:hover { background: rgba(142,207,149,.12) !important; }
.edit-cancel { color: #e5928f !important; border-color: #e5928f !important; }
.edit-cancel:hover { background: rgba(229,146,143,.12) !important; }
.msg-row.assistant { display: flex; gap: 12px; align-items: flex-start; }
.j-avatar {
  width: 26px; height: 26px; flex-shrink: 0; margin-top: 1px;
  border: 1px solid var(--accent); display: flex; align-items: center;
  justify-content: center; color: var(--accent); font-size: 11px;
  box-shadow: 0 0 10px var(--accent-glow); background: rgba(240,168,122,.05);
  font-weight: bold;
}
.msg-body { flex: 1; min-width: 0; font-size: 12px; color: var(--text); line-height: 1.65; }
.msg-body p { margin: 0 0 9px; }
.msg-body p:last-child { margin: 0; }
.msg-body h3 { font-size: 13px; color: #fff; margin: 12px 0 5px; }
.msg-body code { color: var(--accent); background: rgba(240,168,122,.07); padding: 1px 5px; font-size: 11px; }
.msg-body strong { color: #fff; }
.msg-body a { color: var(--accent); text-decoration: none; }
.msg-body a:hover { text-decoration: underline; }
.msg-body ul { padding-left: 18px; margin: 6px 0; }
.msg-body li::marker { color: var(--accent); }
.cursor::after { content: '\2588'; color: var(--accent); animation: blink .9s steps(2) infinite; }
/* code blocks */
.codeblock { margin: 9px 0; border: 1px solid var(--border); background: rgba(22,17,24,.95); border-left: 2px solid var(--accent); }
.cb-head { display: flex; justify-content: space-between; padding: 5px 10px; background: rgba(240,168,122,.05); border-bottom: 1px solid var(--border); }
.cb-lang { font-size: 9px; color: var(--accent); letter-spacing: .1em; text-transform: uppercase; }
.cb-copy { background: transparent; border: 0; color: var(--text-2); cursor: pointer; font: 9px var(--mono); letter-spacing: .1em; transition: color .15s; }
.cb-copy:hover { color: var(--accent); }
.codeblock pre { margin: 0; padding: 10px 12px; overflow-x: auto; }
.codeblock code { font: 11.5px/1.6 var(--mono); color: #c9b8d4; background: none; }

/* tool rows */
.tool-row {
  display: flex; align-items: center; gap: 8px; padding: 7px 12px;
  margin: 6px 0; font-size: 11px; border: 1px solid var(--border);
  background: rgba(34,27,42,.85); letter-spacing: .04em;
  border-left: 2px solid var(--text-dim); transition: border-color .2s;
}
.tool-row .tname { color: #d8c8e0; font-weight: 600; }
.tool-row .tsum  { color: var(--text-2); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--mono); font-size: 10px; }
.tool-row .tres  { margin-left: auto; }
.tool-row.ok  .tres { color: var(--ok); }
.tool-row.ok  { border-left-color: var(--ok); }
.tool-row.bad .tres { color: var(--hot); }
.tool-row.bad { border-left-color: var(--hot); }
.tool-row.pending .tres { color: var(--warn); animation: pulse 1s ease infinite; }
.tool-row.pending { border-color: rgba(255,170,0,.3); border-left-color: var(--warn); background: rgba(255,170,0,.04); }
@keyframes pulse { 50% { opacity: .3; } }
@keyframes pulse { 50% { opacity: .3; } }

/* thinking */
.thinking-row { display: flex; align-items: center; gap: 10px; padding: 5px 0; font-size: 10px; color: var(--text-dim); letter-spacing: .14em; }
.thinking-dots { display: flex; gap: 5px; }
.thinking-dots span { width: 6px; height: 6px; background: var(--accent); border-radius: 50%; animation: bob 1s ease-in-out infinite; }
.thinking-dots span:nth-child(2) { animation-delay: .18s; }
.thinking-dots span:nth-child(3) { animation-delay: .36s; }
.thinking-timer { color: var(--text-dim); font-variant-numeric: tabular-nums; }
@keyframes bob { 0%,100%{opacity:.15;transform:translateY(0)} 50%{opacity:1;transform:translateY(-4px)} }
/* done stats */
.done-stats { padding: 2px 0 4px 38px; font-size: 9px; color: var(--text-dim); letter-spacing: .06em; opacity: .7; }
.done-stats .ds-sep { margin: 0 4px; }

/* plan / note / error / permission */
.plan-box { margin: 9px 0; padding: 11px 14px; border: 1px solid rgba(255,170,0,.3); background: rgba(255,170,0,.03); }
.plan-box .ph { color: var(--warn); font-size: 10px; letter-spacing: .1em; margin-bottom: 7px; }
.plan-box ol { padding-left: 16px; color: var(--text-2); font-size: 11px; }
.plan-box li { margin: 3px 0; }
.note-row { font-size: 10px; color: var(--text-dim); padding: 3px 0; }
.note-row.err { color: var(--hot); }
.perm-box { margin: 9px 0; padding: 11px 14px; border: 1px solid rgba(255,170,0,.4); border-left: 2px solid var(--warn); background: rgba(255,170,0,.03); }
.perm-box .pq { font-size: 11px; color: var(--text); margin-bottom: 9px; }
.perm-box code { color: var(--warn); background: rgba(255,170,0,.08); padding: 1px 5px; }
.perm-btns { display: flex; gap: 8px; }
.perm-btns button { border: 1px solid; padding: 6px 12px; cursor: pointer; font: 9px var(--mono); letter-spacing: .1em; text-transform: uppercase; }
.perm-btns .yes    { background: rgba(142,207,149,.07);  color: var(--ok);  border-color: rgba(142,207,149,.4); }
.perm-btns .yes:hover { background: rgba(142,207,149,.18); }
.perm-btns .always { background: rgba(255,170,0,.07);  color: var(--warn); border-color: rgba(255,170,0,.4); }
.perm-btns .no     { background: transparent; color: var(--text-2); border-color: var(--border); }
.perm-decided      { font-size: 10px; color: var(--text-dim); }

/* ---- FLOATING HUD WINDOWS -------------------------------------------------- */
#windowLayer {
  position: fixed; inset: 0; z-index: 170; pointer-events: none;
}
.hud-window {
  position: absolute; pointer-events: auto;
  min-width: 220px; min-height: 100px;
  background: rgba(22,17,24,.92); border: 1px solid var(--border-h);
  box-shadow: 0 4px 30px rgba(0,0,0,.55), 0 0 20px rgba(240,168,122,.06);
  display: flex; flex-direction: column;
  animation: hudWinIn .3s ease;
  backdrop-filter: blur(6px);
}
.hud-win-resize {
  position: absolute; bottom: 0; right: 0;
  width: 16px; height: 16px;
  cursor: nwse-resize;
  z-index: 2;
}
.hud-win-resize::before {
  content: '';
  position: absolute; bottom: 4px; right: 4px;
  width: 8px; height: 8px;
  border-right: 2px solid var(--text-dim);
  border-bottom: 2px solid var(--text-dim);
  opacity: 0.4;
}
.hud-window.resizing { opacity: .85; box-shadow: 0 8px 40px rgba(0,0,0,.7), 0 0 30px rgba(240,168,122,.12); }
@keyframes hudWinIn { from { opacity: 0; transform: scale(.92) translateY(10px); } }
.hud-win-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 7px 12px; border-bottom: 1px solid var(--border);
  cursor: grab; user-select: none; flex-shrink: 0;
}
.hud-win-head:active { cursor: grabbing; }
.hud-win-title { font-size: 9px; color: var(--accent); letter-spacing: .14em; text-transform: uppercase; text-shadow: 0 0 8px var(--accent-glow); }
.hud-win-close { background: transparent; border: 0; color: var(--text-dim); cursor: pointer; font: 14px var(--mono); padding: 0 4px; line-height: 1; }
.hud-win-close:hover { color: var(--accent); }
.hud-window::before { content: ''; position: absolute; top: -1px; left: 12px; right: 12px; height: 1px; background: linear-gradient(90deg, transparent, var(--accent), transparent); }
.hud-win-body { overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 8px; flex: 1; }
.hud-window.dragging { opacity: .85; box-shadow: 0 8px 40px rgba(0,0,0,.7), 0 0 30px rgba(240,168,122,.12); }

/* viewport panels (rendered by model directives) */
.vpanel {
  position: relative;
}

.vpanel-title { font-size: 9px; color: var(--accent); letter-spacing: .14em; text-transform: uppercase; margin-bottom: 9px; text-shadow: 0 0 8px var(--accent-glow); }
.vp-stat-row { display: flex; justify-content: space-between; align-items: center; padding: 4px 0; border-bottom: 1px solid rgba(240,168,122,.06); font-size: 10px; }
.vp-stat-row .l { color: var(--text-2); letter-spacing: .05em; }
.vp-stat-row .v { color: #fff; }
.vp-stat-row .v.ok { color: var(--ok); } .vp-stat-row .v.warn { color: var(--warn); } .vp-stat-row .v.hot { color: var(--hot); }
.vp-metric { text-align: center; padding: 6px 0; }
.vp-metric .big { font-size: 38px; color: #fff; text-shadow: 0 0 22px var(--accent-glow); line-height: 1; }
.vp-metric .big .unit { font-size: 16px; color: var(--accent); margin-left: 3px; }
.vp-metric .sub { font-size: 9px; color: var(--text-2); margin-top: 6px; letter-spacing: .08em; }
.vp-metric .trend { font-size: 11px; margin-top: 4px; }
.vp-metric .trend.up { color: var(--ok); } .vp-metric .trend.down { color: var(--hot); } .vp-metric .trend.flat { color: var(--text-2); }
.vp-list { list-style: none; }
.vp-list li { font-size: 10px; color: var(--text); padding: 4px 0 4px 14px; position: relative; border-bottom: 1px solid rgba(240,168,122,.05); line-height: 1.5; }
.vp-list li::before { content: '\25B8'; color: var(--accent); position: absolute; left: 0; }
.vp-table { width: 100%; border-collapse: collapse; font-size: 9.5px; }
.vp-table th { color: var(--accent); text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--border); letter-spacing: .05em; text-transform: uppercase; }
.vp-table td { color: var(--text); padding: 4px 6px; border-bottom: 1px solid rgba(240,168,122,.05); }
.vp-image img { width: 100%; border: 1px solid var(--border); display: block; }
.vp-image .cap { font-size: 9px; color: var(--text-2); margin-top: 6px; letter-spacing: .04em; }
.vp-web-item { padding: 7px 0; border-bottom: 1px solid rgba(240,168,122,.06); }
.vp-web-item a { color: var(--accent); font-size: 10px; text-decoration: none; display: block; letter-spacing: .03em; }
.vp-web-item a:hover { text-decoration: underline; }
.vp-web-item .url { font-size: 8.5px; color: var(--ok); margin: 2px 0; word-break: break-all; }
.vp-web-item .snip { font-size: 9px; color: var(--text-2); line-height: 1.5; }
.vp-alert { padding: 10px 12px; border-left: 2px solid; }
.vp-alert.info { border-color: var(--accent); background: rgba(240,168,122,.05); }
.vp-alert.warn { border-color: var(--warn); background: rgba(255,170,0,.05); }
.vp-alert.critical { border-color: var(--hot); background: rgba(255,68,34,.07); }
.vp-alert .at { font-size: 10px; color: #fff; margin-bottom: 4px; letter-spacing: .06em; }
.vp-alert .ax { font-size: 9.5px; color: var(--text-2); line-height: 1.5; }
.vp-prog-row { margin: 7px 0; }
.vp-prog-row .pl { display: flex; justify-content: space-between; font-size: 9px; color: var(--text-2); margin-bottom: 3px; }
.vp-prog-bar { height: 6px; background: rgba(240,168,122,.07); border: 1px solid rgba(240,168,122,.15); }
.vp-prog-fill { height: 100%; background: var(--accent); box-shadow: 0 0 6px var(--accent-glow); transition: width .6s ease; }
.vp-map { position: relative; height: 150px; border: 1px solid var(--border); overflow: hidden; background:
  radial-gradient(circle at 50% 50%, rgba(240,168,122,.08), transparent 70%); }
.vp-map .crosshair { position: absolute; top: 50%; left: 50%; width: 16px; height: 16px; margin: -8px 0 0 -8px; }
.vp-map .crosshair::before, .vp-map .crosshair::after { content: ''; position: absolute; background: var(--ok); box-shadow: 0 0 8px var(--ok); }
.vp-map .crosshair::before { left: 7px; top: 0; width: 2px; height: 16px; }
.vp-map .crosshair::after { top: 7px; left: 0; height: 2px; width: 16px; }
.vp-map .mgrid { position: absolute; inset: 0; background-image:
  linear-gradient(rgba(240,168,122,.08) 1px, transparent 1px),
  linear-gradient(90deg, rgba(240,168,122,.08) 1px, transparent 1px); background-size: 22px 22px; }
.vp-map .mlabel { position: absolute; bottom: 6px; left: 8px; font-size: 8.5px; color: var(--ok); letter-spacing: .06em; }

/* ---- CMD AREA -------------------------------------------------------------- */
.cmd-area { flex-shrink: 0; padding: 10px 20px 12px; border-top: 1px solid var(--border); background: rgba(22,17,24,.7); }
.cmd-box {
  display: flex; align-items: center; gap: 10px;
  border: 1px solid var(--border-h); padding: 9px 12px; background: rgba(34,27,42,.8);
  box-shadow: 0 0 30px rgba(240,168,122,.07), inset 0 0 25px rgba(0,0,0,.5);
  max-width: 1000px; margin: 0 auto;
}
.cmd-box:focus-within { border-color: var(--accent); box-shadow: 0 0 40px rgba(240,168,122,.18), inset 0 0 25px rgba(0,0,0,.5); }
.cmd-box.listening { border-color: var(--ok); box-shadow: 0 0 40px rgba(142,207,149,.25), inset 0 0 25px rgba(0,0,0,.5); }
.cmd-prompt { color: var(--accent); font-size: 15px; flex-shrink: 0; padding-bottom: 1px; }
.cmd-box textarea { flex: 1; background: transparent; border: 0; outline: 0; color: #ece7f0; font: 13px/1.55 var(--mono); resize: none; max-height: 130px; letter-spacing: .03em; }
.cmd-box textarea::placeholder { color: var(--text-dim); }
.mic-btn {
  flex-shrink: 0; width: 28px; height: 24px; border: 1px solid var(--border);
  background: var(--accent-dim); color: var(--accent); font-size: 13px; cursor: pointer;
  transition: background .15s, border-color .15s; line-height: 24px; padding: 0;
}
.mic-btn:hover { background: rgba(240,168,122,.22); }
.mic-btn.listening { border-color: var(--ok); color: var(--ok); background: rgba(142,207,149,.12); animation: micPulse 1.1s ease infinite; }
@keyframes micPulse { 50% { box-shadow: 0 0 14px rgba(142,207,149,.5); } }
.exec-btn {
  flex-shrink: 0; padding: 7px 18px; border: 1px solid var(--accent);
  background: rgba(240,168,122,.1); color: var(--accent); font: 10px var(--mono);
  cursor: pointer; letter-spacing: .16em; text-transform: uppercase; transition: background .15s;
}
.exec-btn:hover    { background: rgba(240,168,122,.24); }
.exec-btn:disabled { opacity: .28; cursor: default; }
.cmd-footer { display: flex; justify-content: space-between; align-items: center; max-width: 1000px; margin: 5px auto 0; font-size: 9px; color: var(--text-dim); letter-spacing: .08em; }
.busy-label { color: var(--ok); animation: pulse 1.2s ease infinite; }

/* ---- SESSIONS DRAWER ------------------------------------------------------- */
.sessions-panel {
  position: fixed; top: 0; left: 0; bottom: 0; width: 270px;
  background: rgba(22,17,24,.97); border-right: 1px solid var(--border-h);
  z-index: 200; padding: 14px; display: flex; flex-direction: column;
  transform: translateX(-100%); transition: transform .22s ease;
}
.sessions-panel.open { transform: translateX(0); box-shadow: 0 0 50px rgba(240,168,122,.12); }
.sessions-head { display: flex; align-items: center; justify-content: space-between; padding-bottom: 10px; border-bottom: 1px solid var(--border); margin-bottom: 10px; }
.panel-hdr { font-size: 9px; color: var(--accent); letter-spacing: .16em; text-transform: uppercase; }
.icon-btn { background: transparent; border: 0; color: var(--text-dim); cursor: pointer; font: 14px var(--mono); padding: 2px 5px; }
.icon-btn:hover { color: var(--accent); }
.session-list { flex: 1; overflow-y: auto; }
.sess-group { border-bottom: 1px solid var(--border); }
.sess-group-head { display: flex; align-items: center; padding: 7px 8px; cursor: pointer; color: var(--text-2); font-size: 10px; letter-spacing: .05em; gap: 6px; user-select: none; }
.sess-group-head:hover { background: rgba(240,168,122,.05); color: var(--text); }
.sess-group-head.active { color: var(--accent); }
.sess-group-head .sg-caret { font-size: 8px; transition: transform .15s; width: 10px; text-align: center; }
.sess-group-head.open .sg-caret { transform: rotate(90deg); }
.sess-group-head .sg-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.sess-group-head .sg-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sess-group-head .sg-count { color: var(--text-dim); font-size: 9px; }
.sess-group-head .sg-menu { background: transparent; border: 0; color: var(--text-dim); cursor: pointer; font-size: 12px; padding: 0 3px; line-height: 1; }
.sess-group-head .sg-menu:hover { color: var(--accent); }
.sess-group-head .sg-add { background: transparent; border: 0; color: var(--text-dim); cursor: pointer; font-size: 13px; padding: 0 3px; line-height: 1; font-weight: bold; }
.sess-group-head .sg-add:hover { color: var(--accent); }
.sess-group-chats { display: none; }
.sess-group-chats.open { display: block; }
.chat-item-j { display: flex; align-items: center; padding: 6px 8px 6px 22px; cursor: pointer; color: var(--text-2); border-bottom: 1px solid rgba(240,168,122,.04); font-size: 10px; letter-spacing: .05em; gap: 6px; transition: background .15s, color .15s; }
.chat-item-j:hover  { background: rgba(240,168,122,.05); color: var(--text); }
.chat-item-j.active { background: rgba(240,168,122,.08); color: var(--accent); }
.chat-item-j .ci-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chat-item-j.no-indent { padding-left: 8px; }
.ci-del-j { background: transparent; border: 0; color: var(--text-dim); cursor: pointer; font-size: 14px; padding: 0 3px; }
.ci-del-j:hover { color: var(--hot); }
.ci-menu-btn { background: transparent; border: 0; color: var(--text-dim); cursor: pointer; font-size: 14px; padding: 0 3px; line-height: 1; }
.ci-menu-btn:hover { color: var(--accent); }

/* ---- CONTEXT MENU ---- */
.ctx-menu { position: fixed; z-index: 400; background: #1e1828; border: 1px solid var(--border-h); box-shadow: 0 4px 20px rgba(0,0,0,.5); min-width: 160px; }
.ctx-menu.hidden { display: none; }
.ctx-item { padding: 8px 14px; font-size: 12px; color: var(--text-2); cursor: pointer; }
.ctx-item:hover { background: rgba(240,168,122,.08); color: var(--text); }
.ctx-item.ctx-danger:hover { color: var(--hot); }
.ctx-sep { border-top: 1px solid var(--border); margin: 4px 0; }

/* ---- PROJECT PICKER MODAL ITEMS ---- */
.proj-item-j { display: flex; align-items: center; padding: 6px 8px; cursor: pointer; color: var(--text-2); font-size: 10px; letter-spacing: .05em; gap: 6px; }
.proj-item-j:hover { background: rgba(240,168,122,.05); color: var(--text); }
.proj-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.proj-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ---- SESSION LIST (projects + chats) ---- */

/* ---- SETTINGS MODAL -------------------------------------------------------- */
.modal { position: fixed; inset: 0; background: rgba(22,17,24,.82); display: flex; align-items: center; justify-content: center; z-index: 300; }
.modal-card { background: #161118; border: 1px solid var(--border-h); width: 440px; max-width: calc(100vw - 28px); box-shadow: 0 0 60px rgba(240,168,122,.15); animation: modalIn .2s ease; }
@keyframes modalIn { from { opacity: 0; transform: scale(.96) translateY(8px); } }
.modal-card.sm { width: 340px; }
.modal-card.md { width: 380px; }
.modal-card.lg { width: 480px; }
.modal-input { width: 100%; background: rgba(34,27,42,.8); border: 1px solid var(--border-h); color: var(--text); padding: 8px 10px; font-size: 13px; font-family: var(--mono); }
.modal-input:focus { outline: 0; border-color: var(--accent); }
.modal-textarea { width: 100%; background: rgba(34,27,42,.9); border: 1px solid var(--border); color: var(--text); padding: 8px 11px; font: 11.5px var(--mono); letter-spacing: .04em; resize: vertical; box-sizing: border-box; }
.modal-textarea:focus { outline: 0; border-color: var(--accent); }
.modal-title { font-size: 13px; color: var(--text); }
.modal-msg { color: var(--text-2); font-size: 13px; margin: 0; }
.empty-hint { color: var(--text-dim); font-size: 9px; padding: 6px 22px; }
.empty-hint.md { font-size: 12px; padding: 8px; }
.tool-icon { font-size: 13px; }
.ci-arrow { color: var(--accent); font-size: 10px; }
.modal-head { display: flex; align-items: center; justify-content: space-between; padding: 13px 17px; border-bottom: 1px solid var(--border); }
.modal-body { padding: 16px 17px; display: flex; flex-direction: column; gap: 15px; max-height: 70vh; overflow-y: auto; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field.row { flex-direction: row; align-items: center; justify-content: space-between; }
.field-label { font-size: 9px; color: var(--text-2); letter-spacing: .1em; text-transform: uppercase; }
.field-label em { color: var(--text-dim); font-style: normal; }
.field select, .field input[type=text] { background: rgba(34,27,42,.9); border: 1px solid var(--border); color: var(--text); padding: 8px 11px; font: 11.5px var(--mono); letter-spacing: .04em; }
.field select:focus, .field input[type=text]:focus { outline: 0; border-color: var(--accent); }
.field input[type=range] { accent-color: var(--accent); width: 100%; }
.toggle { position: relative; width: 38px; height: 20px; flex-shrink: 0; }
.toggle input { position: absolute; opacity: 0; }
.toggle span { position: absolute; inset: 0; cursor: pointer; background: rgba(240,168,122,.07); border: 1px solid var(--border); transition: background .15s; }
.toggle span::after { content: ''; position: absolute; width: 14px; height: 14px; background: var(--text-dim); top: 2px; left: 2px; transition: transform .15s; }
.toggle input:checked + span { background: rgba(240,168,122,.22); border-color: var(--accent); }
.toggle input:checked + span::after { transform: translateX(18px); background: var(--accent); }
.modal-foot { padding: 12px 17px; border-top: 1px solid var(--border); display: flex; justify-content: flex-end; gap: 8px; }
.btn-primary { background: rgba(240,168,122,.14); color: var(--accent); border: 1px solid var(--accent); padding: 7px 16px; font: 9px var(--mono); cursor: pointer; letter-spacing: .14em; text-transform: uppercase; }
.btn-primary:hover { background: rgba(240,168,122,.28); }
.btn-ghost { background: transparent; color: var(--text-2); border: 1px solid var(--border); padding: 7px 14px; font: 9px var(--mono); cursor: pointer; letter-spacing: .1em; text-transform: uppercase; }
.btn-ghost:hover { border-color: var(--text-2); }
/* ---- BACKDROP + SCROLLBARS ------------------------------------------------- */
.backdrop { position: fixed; inset: 0; z-index: 150; background: rgba(22,17,24,.6); backdrop-filter: blur(2px); }
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(240,168,122,.18); border-radius: 2px; }
::-webkit-scrollbar-thumb:hover { background: rgba(240,168,122,.38); }
/* Firefox scrollbar */
* { scrollbar-width: thin; scrollbar-color: rgba(240,168,122,.18) transparent; }
@media (max-width: 900px) {
  .hud-window { max-width: 90vw; }
  .j-sub { display: none; }
  .quick-cards { grid-template-columns: 1fr 1fr; }
}
"""

_JS = r"""
// Cagentic
const $ = s => document.querySelector(s);
const log = $('#log'), input = $('#input'), sendBtn = $('#send');
let state = {
  chats: [], currentId: null, settings: {}, busy: false,
  voiceOut: false, voiceName: '', renderedPanels: new Set(),
  projects: [], activeProjectId: null,
  _openProjects: new Set(), _openUnaffiliated: true, _openProjectsRoot: true,
};

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
    W = canvas.width = p.clientWidth || 600;
    H = canvas.height = p.clientHeight || 300;
    cx = W/2; cy = H/2;
  }
  function mkPart() {
    const th = Math.random()*Math.PI*2, ph = Math.random()*Math.PI, r = 45+Math.random()*40;
    return { x:cx+r*Math.sin(ph)*Math.cos(th), y:cy+r*Math.sin(ph)*Math.sin(th)*0.4, z:Math.cos(ph),
      vx:(Math.random()-.5)*0.3, vy:(Math.random()-.5)*0.3, life:Math.random(),
      decay:0.007+Math.random()*0.016, size:0.7+Math.random()*2.2, alpha:0.4+Math.random()*0.6 };
  }
  function resetPart(p) {
    const th=Math.random()*Math.PI*2, ph=Math.random()*Math.PI, r=43+Math.random()*42;
    p.x=cx+r*Math.sin(ph)*Math.cos(th); p.y=cy+r*Math.sin(ph)*Math.sin(th)*0.4; p.z=Math.cos(ph); p.life=1;
  }
  function initParts(){ particles=[]; for(let i=0;i<220;i++) particles.push(mkPart()); }
  const ORBS=[{r:105,s:0.65,sz:3.5,ph:0},{r:105,s:0.65,sz:3.5,ph:Math.PI},
    {r:82,s:-1.05,sz:2.5,ph:Math.PI/2},{r:125,s:0.45,sz:2,ph:Math.PI/3},{r:82,s:-1.05,sz:2.5,ph:Math.PI*1.5}];
  function draw() {
    ctx.clearRect(0,0,W,H);
    // speed reacts to state: faster when busy/listening/speaking
    const sp = state.busy ? 0.03 : (window.__jSpeak ? 0.022 : 0.011);
    t += sp;
    for(let r=120;r>=12;r-=18) {
      const g=ctx.createRadialGradient(cx,cy,r*0.4,cx,cy,r);
      g.addColorStop(0,`rgba(200,120,60,${0.022+(120-r)*0.0006})`); g.addColorStop(1,'rgba(0,0,0,0)');
      ctx.beginPath(); ctx.arc(cx,cy,r,0,Math.PI*2); ctx.fillStyle=g; ctx.fill();
    }
    const mg=ctx.createRadialGradient(cx,cy,0,cx,cy,65);
    mg.addColorStop(0,'rgba(255,220,190,0.88)'); mg.addColorStop(0.22,'rgba(240,168,122,0.58)');
    mg.addColorStop(0.6,'rgba(160,80,60,0.22)'); mg.addColorStop(1,'rgba(0,0,0,0)');
    ctx.beginPath(); ctx.arc(cx,cy,65,0,Math.PI*2); ctx.fillStyle=mg; ctx.fill();
    const ic=ctx.createRadialGradient(cx,cy,0,cx,cy,20);
    ic.addColorStop(0,'rgba(255,255,255,1)'); ic.addColorStop(0.5,'rgba(255,200,170,0.75)'); ic.addColorStop(1,'rgba(240,168,122,0)');
    ctx.beginPath(); ctx.arc(cx,cy,20,0,Math.PI*2); ctx.fillStyle=ic; ctx.fill();
    particles.forEach(p=>{
      p.x+=p.vx; p.y+=p.vy; p.life-=p.decay; if(p.life<=0) resetPart(p);
      const a=Math.max(0,p.life)*p.alpha, br=0.5+p.z*0.5;
      ctx.beginPath(); ctx.arc(p.x,p.y,p.size,0,Math.PI*2);
      ctx.fillStyle=`rgba(${Math.round(200+br*55)},${Math.round(120+br*48)},${Math.round(60+br*62)},${a})`; ctx.fill();
    });
    ORBS.forEach(o=>{
      const a=t*o.s+o.ph, ox=cx+o.r*Math.cos(a), oy=cy+o.r*0.38*Math.sin(a);
      ctx.beginPath(); ctx.arc(ox,oy,o.sz,0,Math.PI*2);
      ctx.fillStyle='rgba(240,168,122,0.9)'; ctx.shadowColor='#f0a87a'; ctx.shadowBlur=12; ctx.fill(); ctx.shadowBlur=0;
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
      '<button class="cb-copy">COPY</button></div><pre><code>'+esc(code.replace(/\n$/,''))+'</code></pre></div>');
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
// strip plain text of markdown/HUD for speech
function plain(text){
  return stripHud(text).replace(/```[\s\S]*?```/g,' code block ')
    .replace(/[#*`>_]/g,'').replace(/\[([^\]]+)\]\([^)]+\)/g,'$1').replace(/\s+/g,' ').trim();
}
function scrollDown(){ log.scrollTop=log.scrollHeight; }
function getThread(){ let t=log.querySelector('.j-thread'); if(!t){t=document.createElement('div');t.className='j-thread';log.appendChild(t);} return t; }
function clearLog(){ log.innerHTML=''; }
function avatarHTML(){ return '<div class="j-avatar">C</div>'; }
function setOrbLabel(text){ const l=$('#orbLabel'); if(l) l.textContent=(text||'New Chat'); }
function compactOrb(on){ const z=$('#orbZone'); if(z) z.classList.toggle('compact', on); }

// ---- HUD --------------------------------------------------------------------
const HUD_RX = /```hud\s*\n?([\s\S]*?)```/g;
function extractHud(text){
  const out=[]; let m;
  HUD_RX.lastIndex=0;
  while((m=HUD_RX.exec(text||''))!==null){
    try{ out.push({raw:m[1].trim(), obj:JSON.parse(m[1].trim())}); }catch(e){}
  }
  return out;
}
function stripHud(text){ return (text||'').replace(HUD_RX,'').trim(); }

function renderHudPanels(text){
  const found=extractHud(text);
  if(!found.length) return;
  // Handle clear directives first
  found.forEach(({obj})=>{ if((obj.panel||'').toLowerCase()==='clear') clearViewport(); });
  const nonClear=found.filter(({obj})=>(obj.panel||'').toLowerCase()!=='clear');
  if(!nonClear.length) return;
  const layer=$('#windowLayer');
  nonClear.forEach(({raw,obj})=>{
    if(state.renderedPanels.has(raw)) return;
    state.renderedPanels.add(raw);
    const inner=buildPanelInner(obj); if(!inner) return;
    const pos=_nextWinPos();
    const win=document.createElement('div'); win.className='hud-window';
    win.style.left=pos.x+'px'; win.style.top=pos.y+'px';
    const title=obj.title||((obj.panel||'').charAt(0).toUpperCase()+(obj.panel||'').slice(1));
    win.innerHTML='<div class="hud-win-head"><span class="hud-win-title">'+esc(title)+'</span>'+
      '<button class="hud-win-close" title="Close">&times;</button></div>'+
      '<div class="hud-win-body">'+inner+'</div>'+
      '<div class="hud-win-resize"></div>';
    win.querySelector('.hud-win-close').addEventListener('mousedown',e=>{e.stopPropagation();win.remove();});
    layer.appendChild(win);
    _makeDraggable(win);
    _makeResizable(win);
  });
}
function buildPanelInner(p){
  if(!p||typeof p!=='object') return null;
  const title=p.title?'<div class="vpanel-title">'+esc(p.title)+'</div>':'';
  let inner='';
  switch((p.panel||'').toLowerCase()){
    case 'stats':
      inner=(p.items||[]).map(it=>'<div class="vp-stat-row"><span class="l">'+esc(it.label||'')+
        '</span><span class="v '+(it.accent||'')+'">'+esc(String(it.value??''))+'</span></div>').join('');
      break;
    case 'metric':
      inner='<div class="vp-metric"><div class="big">'+esc(String(p.value??''))+
        (p.unit?'<span class="unit">'+esc(p.unit)+'</span>':'')+'</div>'+
        (p.trend?'<div class="trend '+esc(p.trend)+'">'+({up:'▲ RISING',down:'▼ FALLING',flat:'■ STABLE'}[p.trend]||'')+'</div>':'')+
        (p.sub?'<div class="sub">'+esc(p.sub)+'</div>':'')+'</div>';
      break;
    case 'list':
      inner='<ul class="vp-list">'+(p.items||[]).map(i=>'<li>'+esc(String(i))+'</li>').join('')+'</ul>';
      break;
    case 'table':
      inner='<table class="vp-table"><thead><tr>'+(p.columns||[]).map(c=>'<th>'+esc(c)+'</th>').join('')+
        '</tr></thead><tbody>'+(p.rows||[]).map(r=>'<tr>'+r.map(c=>'<td>'+esc(String(c))+'</td>').join('')+'</tr>').join('')+'</tbody></table>';
      break;
    case 'image':
      inner='<div class="vp-image"><img src="'+esc(p.url||'')+'" alt="" onerror="this.style.display=\'none\'"/>'+
        (p.caption?'<div class="cap">'+esc(p.caption)+'</div>':'')+'</div>';
      break;
    case 'web':
      inner=(p.results||[]).map(r=>'<div class="vp-web-item">'+
        '<a href="'+esc(r.url||'#')+'" target="_blank" rel="noopener">'+esc(r.title||r.url||'')+'</a>'+
        (r.url?'<div class="url">'+esc(r.url)+'</div>':'')+
        (r.snippet?'<div class="snip">'+esc(r.snippet)+'</div>':'')+'</div>').join('');
      break;
    case 'alert':
      const lvl=(p.level||'info').toLowerCase();
      return '<div class="vp-alert '+lvl+'"><div class="at">'+esc(p.title||lvl.toUpperCase())+
        '</div><div class="ax">'+esc(p.text||'')+'</div></div>';
    case 'progress':
      inner=(p.items||[]).map(it=>{const pct=Math.max(0,Math.min(100,+it.pct||0));
        return '<div class="vp-prog-row"><div class="pl"><span>'+esc(it.label||'')+'</span><span>'+pct+'%</span></div>'+
        '<div class="vp-prog-bar"><div class="vp-prog-fill" style="width:'+pct+'%"></div></div></div>';}).join('');
      break;
    case 'map':
      inner='<div class="vp-map"><div class="mgrid"></div><div class="crosshair"></div>'+
        '<div class="mlabel">'+esc(p.label||((p.lat??'?')+', '+(p.lon??'?')))+'</div></div>';
      break;
    case 'bar':{ const vals=(p.values||[]).map(Number); const labs=p.labels||vals.map((_,i)=>String(i+1));
      const maxV=Math.max(...vals,1); const col=p.color||'#f0a87a';
      const W2=320,H2=160,padL=36,padR=10,padT=14,padB=22;
      const plotW=W2-padL-padR, plotH=H2-padT-padB;
      const bw=Math.max(10,Math.min(36,Math.floor(plotW/Math.max(vals.length,1)*0.6)));
      const gap=Math.floor(plotW/Math.max(vals.length,1));
      // grid lines
      let grid='';
      for(let g=0;g<=4;g++){
        const gy=padT+plotH*(1-g/4);
        const gv=(maxV*g/4);
        grid+=`<line x1="${padL}" y1="${gy}" x2="${W2-padR}" y2="${gy}" stroke="#2a2235" stroke-width="1"/>`;
        grid+=`<text x="${padL-4}" y="${gy+3}" text-anchor="end" font-size="8" fill="#6b5f7a">${gv%1===0?gv:gv.toFixed(1)}</text>`;
      }
      // gradient def
      const gid='bg'+(_winCascade||0);
      let bars=grid;
      bars+=`<defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${esc(col)}" stop-opacity="1"/><stop offset="100%" stop-color="${esc(col)}" stop-opacity="0.45"/></linearGradient></defs>`;
      vals.forEach((v,i)=>{
        const bh=Math.round((v/maxV)*plotH); const x=padL+i*gap+(gap-bw)/2; const y=padT+plotH-bh;
        bars+=`<rect x="${x}" y="${y}" width="${bw}" height="${bh}" fill="url(#${gid})" rx="3" ry="3"/>`;
        bars+=`<text x="${x+bw/2}" y="${H2-4}" text-anchor="middle" font-size="9" fill="#b0a6ba">${esc(String(labs[i]||''))}</text>`;
        bars+=`<text x="${x+bw/2}" y="${y-4}" text-anchor="middle" font-size="8" font-weight="600" fill="${esc(col)}">${esc(String(v))}</text>`;
      });
      inner=`<svg viewBox="0 0 ${W2} ${H2}" style="width:100%;height:auto">${bars}</svg>`; break; }
    case 'line':{ const ds=(p.datasets||[{values:p.values||[],label:'',color:'#f0a87a'}]);
      const labs=p.labels||[];  const maxAll=Math.max(...ds.flatMap(d=>d.values||[]).map(Number),1);
      const W2=320,H2=160,padL=36,padR=10,padT=14,padB=22;
      const plotW=W2-padL-padR, plotH=H2-padT-padB;
      let lines=''; const colors=['#f0a87a','#8ecf95','#e3a978','#c97fd4','#e5928f'];
      // grid
      for(let g=0;g<=4;g++){
        const gy=padT+plotH*(1-g/4); const gv=(maxAll*g/4);
        lines+=`<line x1="${padL}" y1="${gy}" x2="${W2-padR}" y2="${gy}" stroke="#2a2235" stroke-width="1"/>`;
        lines+=`<text x="${padL-4}" y="${gy+3}" text-anchor="end" font-size="8" fill="#6b5f7a">${gv%1===0?gv:gv.toFixed(1)}</text>`;
      }
      ds.forEach((d,di)=>{ const vals=(d.values||[]).map(Number); const col=d.color||colors[di%colors.length];
        if(!vals.length) return;
        const pts=vals.map((v,i)=>{const x=padL+i*plotW/Math.max(vals.length-1,1); const y=padT+plotH*(1-v/maxAll); return `${x},${y}`;});
        // area fill
        const areaPts=[`${padL},${padT+plotH}`,...pts,`${padL+plotW},${padT+plotH}`].join(' ');
        const aid='la'+(_winCascade||0)+di;
        lines+=`<defs><linearGradient id="${aid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${esc(col)}" stop-opacity="0.25"/><stop offset="100%" stop-color="${esc(col)}" stop-opacity="0.02"/></linearGradient></defs>`;
        lines+=`<polygon points="${areaPts}" fill="url(#${aid})"/>`;
        lines+=`<polyline points="${pts.join(' ')}" fill="none" stroke="${esc(col)}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>`;
        pts.forEach((pt,i)=>{ const[x,y]=pt.split(',');
          lines+=`<circle cx="${x}" cy="${y}" r="3.5" fill="#16111c" stroke="${esc(col)}" stroke-width="2"/>`; });
        if(d.label){ const lastPt=pts[pts.length-1].split(',');
          lines+=`<text x="${+lastPt[0]+6}" y="${+lastPt[1]+3}" font-size="9" font-weight="600" fill="${esc(col)}">${esc(d.label)}</text>`; }
      });
      labs.forEach((l,i)=>{ const x=padL+i*plotW/Math.max(labs.length-1,1);
        lines+=`<text x="${x}" y="${H2-4}" text-anchor="middle" font-size="9" fill="#b0a6ba">${esc(String(l))}</text>`; });
      inner=`<svg viewBox="0 0 ${W2} ${H2}" style="width:100%;height:auto">${lines}</svg>`; break; }
    case 'pie':{ const vals=(p.values||[]).map(Number); const labs=p.labels||vals.map((_,i)=>String(i+1));
      const total=vals.reduce((a,b)=>a+b,0)||1;
      const colors=['#f0a87a','#8ecf95','#e3a978','#c97fd4','#e5928f','#b0a6ba','#7ec8e3','#d4a76a'];
      const cx=100,cy=80,r=62,ri=32; let angle=-Math.PI/2; let slices=''; let legend='';
      // shadow ring
      slices+=`<circle cx="${cx+1}" cy="${cy+2}" r="${r+2}" fill="none" stroke="#0a0810" stroke-width="4" opacity="0.4"/>`;
      vals.forEach((v,i)=>{ const sweep=2*Math.PI*(v/total); const col=colors[i%colors.length];
        const mid=angle+sweep/2;
        const x1=cx+r*Math.cos(angle),y1=cy+r*Math.sin(angle);
        const x2=cx+r*Math.cos(angle+sweep),y2=cy+r*Math.sin(angle+sweep);
        const xi1=cx+ri*Math.cos(angle),yi1=cy+ri*Math.sin(angle);
        const xi2=cx+ri*Math.cos(angle+sweep),yi2=cy+ri*Math.sin(angle+sweep);
        const lg=sweep>Math.PI?1:0;
        // slight explode for large slices
        const ex=sweep>0.3?2*Math.cos(mid):0, ey=sweep>0.3?2*Math.sin(mid):0;
        slices+=`<path d="M${xi1+ex} ${yi1+ey} L${x1+ex} ${y1+ey} A${r} ${r} 0 ${lg} 1 ${x2+ex} ${y2+ey} L${xi2+ex} ${yi2+ey} A${ri} ${ri} 0 ${lg} 0 ${xi1+ex} ${yi1+ey}" fill="${col}" opacity="0.9" stroke="#16111c" stroke-width="1"/>`;
        // percentage label inside slice
        if(sweep>0.25){
          const lr=(r+ri)/2, lx=cx+lr*Math.cos(mid)+ex, ly=cy+lr*Math.sin(mid)+ey;
          const pct=Math.round(v/total*100);
          slices+=`<text x="${lx}" y="${ly+3}" text-anchor="middle" font-size="9" font-weight="600" fill="#fff">${pct}%</text>`;
        }
        const pct=Math.round(v/total*100);
        legend+=`<rect x="190" y="${8+i*18}" width="10" height="10" rx="2" fill="${col}"/>`;
        legend+=`<text x="204" y="${17+i*18}" font-size="10" fill="#cdbbd8">${esc(String(labs[i]))} <tspan fill="#8a7e96">${pct}%</tspan></text>`;
        angle+=sweep; });
      // center label
      slices+=`<circle cx="${cx}" cy="${cy}" r="${ri-4}" fill="#16111c" opacity="0.6"/>`;
      inner=`<svg viewBox="0 0 320 165" style="width:100%;height:auto">${slices}${legend}</svg>`; break; }
    default: return null;
  }
  return title+inner;
}

// ---- FLOATING HUD WINDOWS ----------------------------------------------------
let _winCascade = 0;
function _nextWinPos(){
  const layer=$('#windowLayer');
  const lw=layer.clientWidth, lh=layer.clientHeight;
  const off=(_winCascade%8)*30;
  _winCascade++;
  return {x: Math.min(lw-260, 40+off), y: Math.min(lh-200, 60+off)};
}
function _makeDraggable(win){
  const head=win.querySelector('.hud-win-head');
  let dragging=false, sx,sy,ox,oy;
  function start(cx,cy){
    dragging=true; sx=cx; sy=cy;
    ox=parseInt(win.style.left)||0; oy=parseInt(win.style.top)||0;
    win.classList.add('dragging');
  }
  function move(cx,cy){
    if(!dragging) return;
    win.style.left=(ox+cx-sx)+'px';
    win.style.top=(oy+cy-sy)+'px';
  }
  function stop(){ if(dragging){ dragging=false; win.classList.remove('dragging'); } }
  head.addEventListener('mousedown',e=>{
    if(e.target.classList.contains('hud-win-close')) return;
    start(e.clientX,e.clientY); e.preventDefault();
  });
  document.addEventListener('mousemove',e=>move(e.clientX,e.clientY));
  document.addEventListener('mouseup',stop);
  // touch support
  head.addEventListener('touchstart',e=>{
    if(e.target.classList.contains('hud-win-close')) return;
    const t=e.touches[0]; start(t.clientX,t.clientY); e.preventDefault();
  },{passive:false});
  document.addEventListener('touchmove',e=>{
    if(!dragging) return; const t=e.touches[0]; move(t.clientX,t.clientY);
  },{passive:true});
  document.addEventListener('touchend',stop);
}
function _makeResizable(win){
  const handle=win.querySelector('.hud-win-resize');
  if(!handle) return;
  let resizing=false, rsx,rsy,rsw,rsh;
  function rstart(cx,cy){
    resizing=true; rsx=cx; rsy=cy;
    rsw=win.offsetWidth; rsh=win.offsetHeight;
    win.classList.add('resizing');
  }
  function rmove(cx,cy){
    if(!resizing) return;
    win.style.width=Math.max(220,rsw+(cx-rsx))+'px';
    win.style.height=Math.max(100,rsh+(cy-rsy))+'px';
  }
  function rstop(){ if(resizing){ resizing=false; win.classList.remove('resizing'); } }
  handle.addEventListener('mousedown',e=>{ rstart(e.clientX,e.clientY); e.preventDefault(); e.stopPropagation(); });
  document.addEventListener('mousemove',e=>rmove(e.clientX,e.clientY));
  document.addEventListener('mouseup',rstop);
  handle.addEventListener('touchstart',e=>{ const t=e.touches[0]; rstart(t.clientX,t.clientY); e.preventDefault(); e.stopPropagation(); },{passive:false});
  document.addEventListener('touchmove',e=>{ if(!resizing) return; const t=e.touches[0]; rmove(t.clientX,t.clientY); },{passive:true});
  document.addEventListener('touchend',rstop);
}
function clearViewport(){
  state.renderedPanels.clear();
  _winCascade=0;
  const layer=$('#windowLayer');
  if(layer) layer.innerHTML='';
}

// bring window to front on click
$('#windowLayer').addEventListener('mousedown',e=>{
  const win=e.target.closest('.hud-window');
  if(win && !e.target.classList.contains('hud-win-close')){
    // move to end of DOM = top of stack
    e.currentTarget.appendChild(win);
  }
});

// ---- EMPTY STATE ------------------------------------------------------------
const QUICK = [
  {icon:'🔍', title:'Search the web',  sub:'Find and summarise anything online', prompt:'Search the web for '},
  {icon:'🖥️', title:'Read my screen',  sub:'Summarise what\'s in my browser tab', prompt:'Read my screen and summarise what you see'},
  {icon:'📊', title:'Show me stats',   sub:'Render live data as floating panels',  prompt:'Show me a status panel of my system'},
  {icon:'📈', title:'Draw a chart',    sub:'Bar, line, or pie - visualise data',   prompt:'Show me a bar chart comparing '},
  {icon:'📝', title:'Take a note',     sub:'Remember something for later',        prompt:'Take a note: '},
  {icon:'⏰', title:'Set a reminder',  sub:'Add something to my reminder list',   prompt:'Add a reminder: '},
  {icon:'📂', title:'Browse files',    sub:'List or read files on your machine',  prompt:'List files in my current directory'},
];
function showEmpty() {
  clearLog();
  const wrap=document.createElement('div'); wrap.className='j-empty';
   wrap.innerHTML='<div class="j-empty-title">Select a chat or type a message</div><div class="quick-cards">'+
    QUICK.map(q=>`<div class="qcard" data-prompt="${esc(q.prompt)}"><span class="qcard-icon">${q.icon}</span>`+
      `<span class="qcard-title">${esc(q.title)}</span><span class="qcard-sub">${esc(q.sub)}</span></div>`).join('')+'</div>';
  log.appendChild(wrap);
  wrap.querySelectorAll('.qcard').forEach(c=>{ c.onclick=()=>{ input.value=c.dataset.prompt; autoGrow(); input.focus(); }; });
}

// ---- RENDERING --------------------------------------------------------------
let _userMsgIdx=0;
function addUser(text){
  const idx=_userMsgIdx++;
  const r=document.createElement('div'); r.className='msg-row user'; r.dataset.idx=idx;
  r.innerHTML='<div class="bubble">'+esc(text)+'</div><div class="msg-actions"><button class="msg-act-btn" data-act="resend" title="Resend">&#8635; resend</button><button class="msg-act-btn" data-act="edit" title="Edit">&#9998; edit</button><button class="msg-act-btn del-btn" data-act="delete" title="Delete">&#10005; delete</button></div>';
  r.querySelector('[data-act="resend"]').onclick=()=>resendMsg(idx,r);
  r.querySelector('[data-act="edit"]').onclick=()=>editMsg(idx,r,text);
  r.querySelector('[data-act="delete"]').onclick=()=>deleteMsg(idx,r);
  getThread().appendChild(r); scrollDown();
}
// ---- DOM HELPERS (shared) ---------------------------------------------------
function truncateAfter(row, includeSelf){
  // Remove all DOM siblings after (and optionally including) the given row.
  const thread=getThread();
  const toRemove=[];
  let cutting=false;
  for(const ch of thread.children){
    if(ch===row){ cutting=true; if(includeSelf) toRemove.push(ch); continue; }
    if(cutting) toRemove.push(ch);
  }
  toRemove.forEach(ch=>ch.remove());
}

function resendMsg(idx,row){
  if(state.busy) return;
  const bubble=row.querySelector('.bubble');
  const text=bubble.textContent||'';
  truncateAfter(row, false);
  streamEdit(idx,text);
}
function streamEdit(idx,text){
  setBusy(true); compactOrb(true);
  live={body:null,raw:'',toolRow:null,thinking:null,turnStart:null};
  showThinking(); setOrbLabel('Thinking\u2026');
  fetch('/api/chat/edit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:idx,message:text})})
  .then(r=>{ if(!r.ok||!r.body) throw new Error(r.status); return readSSE(r,handle); })
  .then(()=>{clearThinking();if(state.busy)finishTurn();})
  .catch(()=>{clearThinking();addNote('CONNECTION FAILURE',true);finishTurn();});
}

function deleteMsg(idx,row){
  if(state.busy) return;
  truncateAfter(row, true);
  // If no messages left, show empty state
  if(!getThread().children.length) showEmpty();
  // Tell backend to truncate history
  fetch('/api/chat/delete-msg',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:idx})})
  .then(r=>r.json()).then(d=>{
    if(d.messages) { /* reload chat state */ }
    refreshChats();
  }).catch(()=>{});
}

function editMsg(idx,row,origText){
  if(state.busy) return;
  row.classList.add('editing');
  const bubble=row.querySelector('.bubble');
  const actions=row.querySelector('.msg-actions');
  const ta=document.createElement('textarea'); ta.className='edit-area'; ta.value=origText;
  bubble.innerHTML=''; bubble.appendChild(ta);
  ta.style.height=Math.min(ta.scrollHeight,130)+'px';
  ta.focus();
  actions.innerHTML='<button class="msg-act-btn edit-save" title="Save &amp; send">&#10003; save</button><button class="msg-act-btn edit-cancel" title="Cancel">&#10005; cancel</button>';
  const save=()=>{
    const newText=ta.value.trim(); if(!newText){cancel();return;}
    row.classList.remove('editing');
    truncateAfter(row, false);
    // Update the bubble with new text
    bubble.innerHTML=esc(newText);
    streamEdit(idx,newText);
  };
  const cancel=()=>{
    row.classList.remove('editing');
    bubble.innerHTML=esc(origText);
    actions.innerHTML='<button class="msg-act-btn" data-act="resend" title="Resend">&#8635; resend</button><button class="msg-act-btn" data-act="edit" title="Edit">&#9998; edit</button><button class="msg-act-btn del-btn" data-act="delete" title="Delete">&#10005; delete</button>';
    row.querySelector('[data-act="resend"]').onclick=()=>resendMsg(idx,row);
    row.querySelector('[data-act="edit"]').onclick=()=>editMsg(idx,row,origText);
    row.querySelector('[data-act="delete"]').onclick=()=>deleteMsg(idx,row);
  };
  actions.querySelector('.edit-save').onclick=save;
  actions.querySelector('.edit-cancel').onclick=cancel;
  ta.addEventListener('keydown',e=>{
    if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();save();}
    if(e.key==='Escape'){e.preventDefault();cancel();}
  });
}
function addAssistant(html, tools){
  const r=document.createElement('div'); r.className='msg-row assistant';
  r.innerHTML=avatarHTML()+'<div class="msg-body">'+(html||'')+'</div>';
  getThread().appendChild(r);
  (tools||[]).forEach(t=>addToolRow({name:t},true));
  scrollDown(); return r.querySelector('.msg-body');
}
function addToolRow(t, done){
  // Collapse same-name tool calls, skipping over non-tool-row elements
  // (info notes, assistant text, etc.) so that calls across iterations
  // of the tool loop still collapse into one row.
  const thread=getThread();
  let prev=null;
  for(let el=thread.lastElementChild; el; el=el.previousElementSibling){
    if(el.classList.contains('tool-row')){ prev=el; break; }
  }
  if(prev && prev.dataset.name===t.name){
    let cnt=prev.dataset.cnt ? parseInt(prev.dataset.cnt)+1 : 2;
    prev.dataset.cnt=cnt;
    const nameEl=prev.querySelector('.tname');
    if(nameEl) nameEl.textContent=t.name+' \u00d7'+cnt;
    if(t.summary){
      const sumEl=prev.querySelector('.tsum');
      if(sumEl) sumEl.textContent=t.summary;
    }
    if(!done){ prev.classList.remove('ok','bad'); prev.classList.add('pending'); }
    scrollDown(); return prev;
  }
  const row=document.createElement('div'); row.className='tool-row'+(done?'':' pending');
  row.dataset.name=t.name||'';
  const isCmd=(t.name||'').startsWith('run_')||(t.name||'').startsWith('bash');
  const iconColor=isCmd?'var(--warn)':'var(--accent)';
  row.innerHTML='<span class="tool-icon" style="color:'+iconColor+'">'+icon+'</span>'+
    '<span class="tname">'+esc(t.name||'')+'</span>'+
    (t.summary?'<span class="tsum">'+esc(t.summary)+'</span>':'')+
    (done?'':'<span class="tres">RUNNING&#8230;</span>');
  getThread().appendChild(row); scrollDown(); return row;
}
function addNote(text, isErr){
  const n=document.createElement('div'); n.className='note-row'+(isErr?' err':'');
  n.textContent=text||''; getThread().appendChild(n); scrollDown();
}
function showPermission(d){
  const box=document.createElement('div'); box.className='perm-box';
  box.innerHTML='<div class="pq">AUTHORIZATION REQUIRED: <code>'+esc(d.tool)+'</code>'+(d.summary?' &mdash; '+esc(d.summary):'')+' </div>';
  const btns=document.createElement('div'); btns.className='perm-btns';
  const answer=(a,past)=>{
    box.innerHTML='<div class="pq"><code>'+esc(d.tool)+'</code></div><div class="perm-decided">&#8594; '+past.toUpperCase()+'</div>';
    fetch('/api/permission',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answer:a})});
  };
  [['yes','APPROVE','approved'],['always','ALWAYS ALLOW','always allowed'],['no','DENY','denied']].forEach(([a,l,p])=>{
    const b=document.createElement('button'); b.className=a; b.textContent=l; b.onclick=()=>answer(a,p); btns.appendChild(b);
  });
  box.appendChild(btns); getThread().appendChild(box); scrollDown();
}

// ---- SSE STREAM READER (shared) -------------------------------------------
async function readSSE(response, onEvent){
  if(!response||!response.body) return;
  const reader=response.body.getReader(), dec=new TextDecoder(); let buf='';
  while(true){
    let chunk; try{chunk=await reader.read();}catch(e){break;}
    if(chunk.done) break;
    buf+=dec.decode(chunk.value,{stream:true}); let i;
    while((i=buf.indexOf('\n\n'))>=0){ const line=buf.slice(0,i); buf=buf.slice(i+2);
      if(line.startsWith('data: ')){ try{onEvent(JSON.parse(line.slice(6)));}catch(e){} } }
  }
}

// ---- LIVE TURN --------------------------------------------------------------
let live={body:null,raw:'',toolRow:null,thinking:null,turnStart:null};
let _thinkTimer=null;
function showThinking(){
  const t=document.createElement('div'); t.className='thinking-row';
  t.innerHTML=avatarHTML()+'<span>Thinking\u2026</span><div class="thinking-dots"><span></span><span></span><span></span></div><span class="thinking-timer" id="thinkTimer"></span>';
  getThread().appendChild(t); scrollDown(); live.thinking=t;
  live.turnStart=Date.now();
  const timerEl=t.querySelector('#thinkTimer');
  _thinkTimer=setInterval(()=>{ if(!live.turnStart){clearInterval(_thinkTimer);_thinkTimer=null;return;} const s=((Date.now()-live.turnStart)/1000).toFixed(1); if(timerEl) timerEl.textContent=s+'s'; },100);
}
function clearThinking(){ if(live.thinking){live.thinking.remove();live.thinking=null;} if(_thinkTimer){clearInterval(_thinkTimer);_thinkTimer=null;} }
function handle(ev){
  const k=ev.kind, d=ev.data||{};
  if(k!=='user') clearThinking();
  if(k==='delta'){
    if(!live.body){live.body=addAssistant('');live.raw='';}
    live.raw+=d.text||'';
    live.body.innerHTML=md(stripHud(live.raw));
    live.body.classList.add('cursor'); scrollDown();
  } else if(k==='assistant'){
    const txt=(d.text||'');
    if(!live.body&&stripHud(txt).trim()){live.body=addAssistant(md(stripHud(txt)));live.raw=txt;}
    else if(live.body){ live.raw=txt; live.body.innerHTML=md(stripHud(txt)); }
    if(live.body) live.body.classList.remove('cursor');
    renderHudPanels(txt);
    if(state.voiceOut){ const p=plain(txt); if(p) speak(p); }
  } else if(k==='plan'){
    const p=document.createElement('div'); p.className='plan-box';
    p.innerHTML='<div class="ph">&#9658; Plan</div><ol>'+(d.steps||[]).map(s=>'<li>'+esc(s)+'</li>').join('')+'</ol>';
    getThread().appendChild(p); live.body=null; scrollDown();
  } else if(k==='tool_call'){
    live.body=null; live.toolRow=addToolRow({name:d.name,summary:d.summary},false);
  } else if(k==='tool_result'){
    if(live.toolRow){
      live.toolRow.classList.remove('pending'); live.toolRow.classList.add(d.ok?'ok':'bad');
      const res=live.toolRow.querySelector('.tres')||document.createElement('span');
      res.className='tres'; res.textContent=(d.ok?'✓ ':'✗ ')+(d.first_line||'').slice(0,90);
      if(!res.parentNode) live.toolRow.appendChild(res); live.toolRow=null;
    }
  } else if(k==='permission'){ live.body=null; showPermission(d);
  } else if(k==='info'||k==='warn'){ addNote(d.text,false); live.body=null;
  } else if(k==='error'){ addNote(d.text||'ERROR: SYSTEM FAULT',true); live.body=null;
  } else if(k==='done'){
    if(live.body) live.body.classList.remove('cursor'); live.body=null;
    if(_thinkTimer){clearInterval(_thinkTimer);_thinkTimer=null;}
    const usage=d.usage||{};
    const hasStats=usage.input||usage.output||usage.ms;
    if(hasStats||live.turnStart){
      const row=document.createElement('div'); row.className='done-stats';
      let parts=[];
      if(usage.input||usage.output) parts.push('tokens in/out '+usage.input+'/'+usage.output);
      if(usage.ms) parts.push(Math.round(usage.ms)+'ms');
      if(live.turnStart){
        const elapsed=((Date.now()-live.turnStart)/1000).toFixed(1);
        parts.push(elapsed+'s');
      }
      row.innerHTML=parts.join('<span class="ds-sep">·</span>');
      getThread().appendChild(row); scrollDown();
    }
    live.turnStart=null;
  } else if(k==='end'){ finishTurn(); }
  scrollDown();
}
log.addEventListener('click',e=>{
  const btn=e.target.closest('.cb-copy'); if(!btn) return;
  const code=btn.closest('.codeblock').querySelector('pre code');
  navigator.clipboard.writeText(code.textContent||'').then(()=>{ btn.textContent='COPIED'; setTimeout(()=>{btn.textContent='COPY';},1400); });
});

// ---- VOICE OUTPUT (TTS) -----------------------------------------------------
let voices=[];
function loadVoices(){ voices=window.speechSynthesis ? speechSynthesis.getVoices() : []; populateVoiceSelect(); }
if(window.speechSynthesis){ speechSynthesis.onvoiceschanged=loadVoices; loadVoices(); }
function pickVoice(){
  if(!voices.length) return null;
  if(state.voiceName){ const v=voices.find(v=>v.name===state.voiceName); if(v) return v; }
  // prefer a deep/UK English voice
  return voices.find(v=>/en-GB/i.test(v.lang)&&/male|daniel|arthur/i.test(v.name))
      || voices.find(v=>/en-GB/i.test(v.lang)) || voices.find(v=>/^en/i.test(v.lang)) || voices[0];
}
function speak(text){
  if(!window.speechSynthesis) return;
  speechSynthesis.cancel();
  const u=new SpeechSynthesisUtterance(text.slice(0,600));
  const v=pickVoice(); if(v) u.voice=v;
  u.rate=1.0; u.pitch=0.9;
  window.__jSpeak=true; const z=$('#orbZone'); if(z) z.classList.add('speaking'); setOrbLabel('SPEAKING');
  u.onend=()=>{ window.__jSpeak=false; if(z) z.classList.remove('speaking'); setOrbLabel(currentTitle()); };
  speechSynthesis.speak(u);
}
function populateVoiceSelect(){
  const sel=$('#setVoice'); if(!sel) return;
  sel.innerHTML='<option value="">Auto</option>'+
    voices.filter(v=>/^en/i.test(v.lang)).map(v=>'<option value="'+esc(v.name)+'">'+esc(v.name+' — '+v.lang)+'</option>').join('');
  sel.value=state.voiceName||'';
}

// ---- VOICE INPUT (STT) ------------------------------------------------------
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let recog=null, recognizing=false;
if(SR){
  recog=new SR(); recog.lang='en-US'; recog.interimResults=true; recog.continuous=false;
  recog.onstart=()=>{ recognizing=true; $('#micBtn').classList.add('listening'); $('#cmdBox').classList.add('listening');
    $('#orbZone').classList.add('listening'); setOrbLabel('LISTENING'); };
  recog.onend=()=>{ recognizing=false; $('#micBtn').classList.remove('listening'); $('#cmdBox').classList.remove('listening');
    $('#orbZone').classList.remove('listening'); setOrbLabel(currentTitle()); };
  recog.onerror=()=>{ recognizing=false; $('#micBtn').classList.remove('listening'); $('#cmdBox').classList.remove('listening'); };
  recog.onresult=e=>{
    let txt=''; for(let i=0;i<e.results.length;i++) txt+=e.results[i][0].transcript;
    input.value=txt; autoGrow();
    if(e.results[e.results.length-1].isFinal){ setTimeout(()=>{ if(input.value.trim()) submit(); },350); }
  };
}
function toggleMic(){
  if(!SR){ addNote('Voice input not supported in this browser (try Chrome).',true); return; }
  if(recognizing){ recog.stop(); } else { try{ recog.start(); }catch(e){} }
}

// ---- CONFIRM DIALOG ---------------------------------------------------------
let _confirmCb=null;
function showConfirm(msg,cb){
  _confirmCb=cb;
  $('#confirmMsg').textContent=msg;
  $('#confirmModal').classList.remove('hidden');
}
$('#confirmOk').onclick=()=>{ $('#confirmModal').classList.add('hidden'); if(_confirmCb) _confirmCb(); _confirmCb=null; };
$('#confirmCancel').onclick=()=>{ $('#confirmModal').classList.add('hidden'); _confirmCb=null; };
$('#confirmClose').onclick=()=>{ $('#confirmModal').classList.add('hidden'); _confirmCb=null; };
$('#confirmModal').addEventListener('click',e=>{ if(e.target.id==='confirmModal'){ $('#confirmModal').classList.add('hidden'); _confirmCb=null; } });

// ---- CONTEXT MENU (⋮) -------------------------------------------------------
let _ctxChatId=null, _projCtxId=null, _ctxMode='chat';
function showCtx(e,chatId){
  _ctxChatId=chatId; _ctxMode='chat';
  const m=$('#ctxMenu');
  m.innerHTML='<div class="ctx-item" data-action="rename">&#9998; Rename</div>'+
    '<div class="ctx-item" data-action="project">&#128193; Add to Project</div>'+
    '<div class="ctx-sep"></div>'+
    '<div class="ctx-item ctx-danger" data-action="delete">&#128465; Delete</div>';
  m.classList.remove('hidden');
  m.style.left=Math.min(e.clientX,window.innerWidth-170)+'px';
  m.style.top=Math.min(e.clientY,window.innerHeight-120)+'px';
}
function showNewProjectModal(){
  $('#newProjectInput').value='';
  $('#newProjectModal').classList.remove('hidden');
  setTimeout(()=>$('#newProjectInput').focus(),50);
}
function closeNewProjectModal(){ $('#newProjectModal').classList.add('hidden'); }

function showProjectCtx(e,projectId){
  _projCtxId=projectId; _ctxMode='project';
  const m=$('#ctxMenu');
  m.innerHTML='<div class="ctx-item" data-action="proj-config">&#9881; Config</div>'+
    '<div class="ctx-item" data-action="proj-rename">&#9998; Rename</div>'+
    '<div class="ctx-item ctx-danger" data-action="proj-delete">&#128465; Delete</div>';
  m.classList.remove('hidden');
  m.style.left=Math.min(e.clientX,window.innerWidth-170)+'px';
  m.style.top=Math.min(e.clientY,window.innerHeight-120)+'px';
}
function hideCtx(){ $('#ctxMenu').classList.add('hidden'); _ctxChatId=null; _projCtxId=null; }
document.addEventListener('click',e=>{
  const m=$('#ctxMenu');
  if(m.contains(e.target)){
    e.stopPropagation();
    const action=e.target.dataset.action; if(!action) return;
    const chatId=_ctxChatId, projId=_projCtxId;
    hideCtx();
    if(action==='delete'){ showConfirm('Delete this chat?',()=>deleteChat(chatId)); }
    else if(action==='rename'){ showRename(chatId); }
    else if(action==='project'){ showProjectPicker(chatId); }
    else if(action==='proj-rename'){ showRenameProject(projId); }
    else if(action==='proj-delete'){
      showConfirm('Delete this project?',async()=>{
        const r=await api('/api/projects/delete',{id:projId});
        state.projects=r.projects; state.activeProjectId=null; renderSessions();
      });
    }
    else if(action==='proj-config'){ showProjectConfig(projId); }
  } else { hideCtx(); }
});

// ---- RENAME MODAL -----------------------------------------------------------
let _renameId=null;
let _renameMode='chat'; // 'chat' or 'project'
function showRename(chatId){
  _renameId=chatId; _renameMode='chat';
  const c=state.chats.find(c=>c.id===chatId);
  $('#renameInput').value=c?c.title:'';
  $('#renameModal').classList.remove('hidden');
  setTimeout(()=>$('#renameInput').focus(),50);
}
function showRenameProject(projectId){
  _renameId=projectId; _renameMode='project';
  const p=state.projects.find(p=>p.id===projectId);
  $('#renameInput').value=p?p.name:'';
  $('#renameModal').classList.remove('hidden');
  setTimeout(()=>$('#renameInput').focus(),50);
}
function closeRename(){ $('#renameModal').classList.add('hidden'); _renameId=null; _renameMode='chat'; }
$('#renameClose').onclick=closeRename;
$('#renameCancel').onclick=closeRename;
$('#renameOk').onclick=async()=>{
  const val=$('#renameInput').value.trim(); if(!val||!_renameId) return;
  if(_renameMode==='project'){
    const r=await api('/api/projects/rename',{id:_renameId,name:val});
    state.projects=r.projects; renderSessions(); closeRename();
  } else {
    const r=await api('/api/chats/rename',{id:_renameId,title:val});
    state.chats=r.chats; renderSessions(); closeRename();
  }
};
$('#renameInput').addEventListener('keydown',e=>{ if(e.key==='Enter') $('#renameOk').click(); });
$('#renameModal').addEventListener('click',e=>{ if(e.target.id==='renameModal') closeRename(); });
$('#newProjectModal').addEventListener('click',e=>{ if(e.target.id==='newProjectModal') closeNewProjectModal(); });

// ---- PROJECT PICKER MODAL ---------------------------------------------------
let _projPickChatId=null;
async function showProjectPicker(chatId){
  _projPickChatId=chatId;
  const body=$('#projectModalBody'); body.innerHTML='';
  if(!state.projects.length){
     body.innerHTML='<div class="empty-hint md">No projects yet. Create one below.</div>';
  } else {
    state.projects.forEach(p=>{
      const d=document.createElement('div');
      d.className='proj-item-j';
      d.innerHTML='<span class="proj-dot" style="background:'+esc(p.color)+'"></span><span class="proj-name">'+esc(p.name)+'</span>';
      d.onclick=async()=>{
        const r=await api('/api/projects/add_chat',{project_id:p.id,chat_id:_projPickChatId});
        state.projects=r.projects; state.chats=r.chats; renderSessions();
        closeProjectPicker();
      };
      body.appendChild(d);
    });
  }
  $('#projectModal').classList.remove('hidden');
}
function closeProjectPicker(){ $('#projectModal').classList.add('hidden'); _projPickChatId=null; }
$('#projectModalClose').onclick=closeProjectPicker;
$('#projectModalCancel').onclick=closeProjectPicker;
$('#projectModal').addEventListener('click',e=>{ if(e.target.id==='projectModal') closeProjectPicker(); });
$('#projectModalNewBtn').onclick=()=>{
  closeProjectPicker();
  showNewProjectModal();
  // After creating, the new project modal handler will refresh
};

// ---- PROJECT CONFIG MODAL ---------------------------------------------------
let _projConfigId=null;
function showProjectConfig(projectId){
  _projConfigId=projectId;
  const p=state.projects.find(p=>p.id===projectId);
  $('#projConfigPrompt').value=p?(p.system_prompt||''):'';
  $('#projConfigContext').value=p?(p.context||''):'';
  $('#projConfigModal').classList.remove('hidden');
  setTimeout(()=>$('#projConfigPrompt').focus(),50);
}
function closeProjectConfig(){ $('#projConfigModal').classList.add('hidden'); _projConfigId=null; }
$('#projConfigClose').onclick=closeProjectConfig;
$('#projConfigCancel').onclick=closeProjectConfig;
$('#projConfigModal').addEventListener('click',e=>{ if(e.target.id==='projConfigModal') closeProjectConfig(); });
$('#projConfigSave').onclick=async()=>{
  if(!_projConfigId) return;
  const r=await api('/api/projects/config',{
    id:_projConfigId,
    system_prompt:$('#projConfigPrompt').value,
    context:$('#projConfigContext').value
  });
  state.projects=r.projects; closeProjectConfig();
};

// ---- SESSIONS ---------------------------------------------------------------
function currentTitle(){ const c=state.chats.find(c=>c.id===state.currentId); return c?c.title:'New Chat'; }
function renderSessions(){
  const list=$('#sessionList'); if(!list) return; list.innerHTML='';
  // Build a map of project_id -> chat list
  const projChats={};
  state.projects.forEach(p=>{ projChats[p.id]=state.chats.filter(c=>c.project_id===p.id); });
  const unaffiliated=state.chats.filter(c=>!c.project_id);
  // --- Projects expandable group ---
  const projGrp=document.createElement('div'); projGrp.className='sess-group';
  const projHead=document.createElement('div'); projHead.className='sess-group-head'+(state._openProjectsRoot?' open':'');
  projHead.innerHTML='<span class="sg-caret">&#9654;</span><span class="sg-dot" style="background:var(--accent)"></span><span class="sg-name">Projects</span><span class="sg-count">'+state.projects.length+'</span><button class="sg-add" title="New Project">+</button>';
  projHead.querySelector('.sg-add').onclick=e=>{ e.stopPropagation(); showNewProjectModal(); };
  projHead.onclick=e=>{
    if(e.target.closest('.sg-add')) return;
    state._openProjectsRoot=!state._openProjectsRoot;
    renderSessions();
  };
  projGrp.appendChild(projHead);
  const projBody=document.createElement('div'); projBody.className='sess-group-chats'+(state._openProjectsRoot?' open':'');
  if(!state.projects.length){
     projBody.innerHTML='<div class="empty-hint">No projects yet</div>';
  } else {
    state.projects.forEach(p=>{
      const chats=projChats[p.id]||[];
      const isOpen=state._openProjects.has(p.id);
      const pGrp=document.createElement('div'); pGrp.className='sess-group';
      const pHead=document.createElement('div'); pHead.className='sess-group-head'+(isOpen?' open':'');
      pHead.innerHTML='<span class="sg-caret">&#9654;</span><span class="sg-dot" style="background:'+esc(p.color)+'"></span><span class="sg-name">'+esc(p.name)+'</span><span class="sg-count">'+chats.length+'</span><button class="sg-menu" title="Menu">&#8942;</button>';
      pHead.querySelector('.sg-menu').onclick=e=>{ e.stopPropagation(); showProjectCtx(e,p.id); };
      pHead.onclick=e=>{
        if(e.target.closest('.sg-menu')) return;
        if(state._openProjects.has(p.id)) state._openProjects.delete(p.id); else state._openProjects.add(p.id);
        renderSessions();
      };
      pGrp.appendChild(pHead);
      const pBody=document.createElement('div'); pBody.className='sess-group-chats'+(isOpen?' open':'');
      chats.forEach(c=>{ pBody.appendChild(makeChatItem(c)); });
      if(!chats.length) pBody.innerHTML='<div class="empty-hint">No chats</div>';
      pGrp.appendChild(pBody);
      projBody.appendChild(pGrp);
    });
  }
  projGrp.appendChild(projBody);
  list.appendChild(projGrp);
  // --- Chats expandable group ---
  const chatGrp=document.createElement('div'); chatGrp.className='sess-group';
  const chatHead=document.createElement('div'); chatHead.className='sess-group-head'+(state._openUnaffiliated!==false?' open':'');
  chatHead.innerHTML='<span class="sg-caret">&#9654;</span><span class="sg-dot" style="background:var(--text-dim)"></span><span class="sg-name">Chats</span><span class="sg-count">'+unaffiliated.length+'</span>';
  chatHead.onclick=()=>{
    state._openUnaffiliated=state._openUnaffiliated===false?true:false;
    renderSessions();
  };
  chatGrp.appendChild(chatHead);
  const chatBody=document.createElement('div'); chatBody.className='sess-group-chats'+(state._openUnaffiliated!==false?' open':'');
  if(!unaffiliated.length){
    chatBody.innerHTML='<div class="empty-hint">No chats yet</div>';
  } else {
    unaffiliated.forEach(c=>{ chatBody.appendChild(makeChatItem(c)); });
  }
  chatGrp.appendChild(chatBody);
}
function makeChatItem(c){
  const item=document.createElement('div'); item.className='chat-item-j'+(c.id===state.currentId?' active':'');
  item.innerHTML='<span class="ci-arrow">&#9658;</span><span class="ci-title">'+esc(c.title)+'</span><button class="ci-menu-btn" title="Menu">&#8942;</button>';
  item.querySelector('.ci-title').onclick=()=>loadChat(c.id);
  item.querySelector('.ci-menu-btn').onclick=e=>{ e.stopPropagation(); showCtx(e,c.id); };
  return item;
}
function setCurrent(cur){
  state.currentId=cur.id;
  _userMsgIdx=0;
  const s=$('#jSession'); if(s) s.textContent=(cur.id||'--------').slice(0,8).toUpperCase();
  setOrbLabel(cur.title||'New Chat');
  clearLog();
  if(!cur.messages||!cur.messages.length){ showEmpty(); compactOrb(false); return; }
  compactOrb(true);
  cur.messages.forEach(m=>{
    if(m.role==='user') addUser(m.content);
    else {
      const html=md(stripHud(m.content));
      const hasContent=html&&html.trim();
      if(hasContent){ addAssistant(html,m.tools); renderHudPanels(m.content); }
      else { (m.tools||[]).forEach(t=>addToolRow({name:t},true)); }
    }
  });
  scrollDown();
}

// ---- NETWORK ----------------------------------------------------------------
async function api(path,body){
  const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  return r.json();
}
function setModelBadge(m){ const n=$('#msName'); if(n) n.textContent=(m||'').toUpperCase(); }
async function boot(){
  const b=await api('/api/bootstrap');
  state.chats=b.chats; state.settings=b.settings; state.projects=b.projects||[];
  setModelBadge(b.model);
  const vs=$('#versionSpan'); if(vs) vs.textContent=b.version||'--';
  renderModelMenu();
  renderSessions(); setCurrent(b.current);
}
async function newChat(){
  const r=await api('/api/chats/new',{}); state.chats=r.chats; renderSessions(); setCurrent(r.current);
  if(r.current&&r.current.model){state.settings.model=r.current.model;setModelBadge(r.current.model);renderModelMenu();}
  clearViewport(); closeSessions(); input.focus();
}
async function loadChat(id){
  const r=await api('/api/chats/load',{id}); state.chats=r.chats; clearViewport(); renderSessions(); setCurrent(r.current); if(r.current&&r.current.model){state.settings.model=r.current.model;setModelBadge(r.current.model);renderModelMenu();} closeSessions();
}
async function deleteChat(id){
  const r=await api('/api/chats/delete',{id}); state.chats=r.chats; state.projects=r.projects||state.projects; renderSessions(); setCurrent(r.current); closeSessions();
}
async function refreshChats(){
  const b=await api('/api/bootstrap'); state.chats=b.chats; state.projects=b.projects||[]; renderSessions(); setOrbLabel(b.current.title||'New Chat');
}

// ---- MODEL SWITCHER ---------------------------------------------------------
function renderModelMenu(){
  const menu=$('#modelMenu'); const models=state.settings.models||[];
  if(!models.length){ menu.innerHTML='<div class="mm-item">'+esc(state.settings.model||'no models')+'</div>'; return; }
  menu.innerHTML=models.map(m=>'<div class="mm-item'+(m===state.settings.model?' active':'')+'" data-m="'+esc(m)+'">'+
    '<span class="mm-tick">'+(m===state.settings.model?'✓':'')+'</span>'+esc(m)+'</div>').join('');
  menu.querySelectorAll('.mm-item').forEach(it=>{ if(it.dataset.m) it.onclick=()=>switchModel(it.dataset.m); });
}
async function switchModel(m){
  $('#modelMenu').classList.add('hidden');
  const r=await api('/api/model',{model:m});
  state.settings.model=r.model; setModelBadge(r.model); renderModelMenu();
  addNote('Model switched to '+r.model);
}
$('#modelSwitch').onclick=e=>{ e.stopPropagation(); $('#modelMenu').classList.toggle('hidden'); };
document.addEventListener('click',()=>$('#modelMenu').classList.add('hidden'));
$('#modelMenu').onclick=e=>e.stopPropagation();

// ---- DRAWER / MODAL ---------------------------------------------------------
function openSessions(){ $('#sessionsPanel').classList.add('open'); $('#backdrop').classList.remove('hidden'); }
function closeSessions(){ $('#sessionsPanel').classList.remove('open'); $('#backdrop').classList.add('hidden'); }
function openSettings(){
  closeSessions();
  const s=state.settings, sel=$('#setModel'); sel.innerHTML='';
  (s.models&&s.models.length?s.models:[s.model]).forEach(m=>{
    const o=document.createElement('option'); o.value=m; o.textContent=m; if(m===s.model) o.selected=true; sel.appendChild(o);
  });
  $('#setName').value=s.user_name||'';
  $('#setTemp').value=s.temperature; $('#tempVal').textContent=(+s.temperature).toFixed(2);
  $('#setStream').checked=!!s.stream; $('#setYolo').checked=!!s.yolo;
  populateVoiceSelect();
  $('#settingsModal').classList.remove('hidden');
}
function closeSettings(){ $('#settingsModal').classList.add('hidden'); }
async function saveSettings(){
  state.voiceName=$('#setVoice').value||'';
  try{ localStorage.setItem('cagentic_voice',state.voiceName); }catch(e){}
  state.settings=await api('/api/settings',{
    model:$('#setModel').value, user_name:$('#setName').value, temperature:parseFloat($('#setTemp').value),
    stream:$('#setStream').checked, yolo:$('#setYolo').checked });
  setModelBadge(state.settings.model); renderModelMenu(); closeSettings();
}

// ---- VOICE OUT TOGGLE -------------------------------------------------------
function toggleVoiceOut(){
  state.voiceOut=!state.voiceOut;
  $('#voiceOutBtn').classList.toggle('active', state.voiceOut);
  $('#voiceOutBtn').innerHTML='[ &#128264; Voice: '+(state.voiceOut?'ON':'OFF')+' ]';
  if(!state.voiceOut && window.speechSynthesis) speechSynthesis.cancel();
  try{ localStorage.setItem('cagentic_voiceout', state.voiceOut?'1':'0'); }catch(e){}
}

// ---- SEND -------------------------------------------------------------------
function setBusy(on){ state.busy=on; sendBtn.disabled=on; input.disabled=on; $('#busyLabel').classList.toggle('hidden',!on); }
function finishTurn(){ setBusy(false); input.focus(); refreshChats(); }
async function send(text){
  if(state.busy) return;
  setBusy(true); compactOrb(true);
  if(log.querySelector('.j-empty')) clearLog();
  addUser(text);
  live={body:null,raw:'',toolRow:null,thinking:null,turnStart:null};
  showThinking(); setOrbLabel('Thinking\u2026');
  let res;
  try{ res=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text})}); }
  catch(e){ clearThinking(); addNote('CONNECTION FAILURE',true); finishTurn(); return; }
  if(!res||!res.ok||!res.body){ clearThinking(); addNote('REQUEST FAILED: '+(res?res.status:'no response'),true); finishTurn(); return; }
  try{
    await readSSE(res, handle);
  }catch(e){ console.error('Stream read error:',e); }
  clearThinking(); if(state.busy) finishTurn();
}

// ---- COMPOSER + WIRING ------------------------------------------------------
function autoGrow(){ input.style.height='auto'; input.style.height=Math.min(input.scrollHeight,130)+'px'; }
function submit(){ const t=input.value.trim(); if(!t||state.busy)return; input.value=''; autoGrow(); send(t); }
input.addEventListener('input', autoGrow);
input.addEventListener('keydown', e=>{ if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();submit();} });
sendBtn.onclick=submit;
$('#micBtn').onclick=toggleMic;
$('#logsBtn').onclick=openSessions;
$('#newMissionBtn').onclick=newChat;
$('#configBtn').onclick=openSettings;
$('#voiceOutBtn').onclick=toggleVoiceOut;

$('#closeSessionsBtn').onclick=closeSessions;
$('#newProjectModalClose').onclick=closeNewProjectModal;
$('#newProjectCancel').onclick=closeNewProjectModal;
$('#newProjectOk').onclick=async()=>{
  const name=$('#newProjectInput').value.trim(); if(!name) return;
  closeNewProjectModal();
  const r=await api('/api/projects/create',{name});
  state.projects=r.projects; state._openProjectsRoot=true; state._openProjects.add(r.projects[r.projects.length-1].id); renderSessions();
};
$('#newProjectInput').addEventListener('keydown',e=>{ if(e.key==='Enter') $('#newProjectOk').click(); });
$('#backdrop').onclick=()=>{closeSessions();};
$('#closeSettings').onclick=closeSettings;
$('#cancelSettings').onclick=closeSettings;
$('#saveSettings').onclick=saveSettings;
$('#setTemp').addEventListener('input',e=>{$('#tempVal').textContent=(+e.target.value).toFixed(2);});
$('#settingsModal').addEventListener('click',e=>{if(e.target.id==='settingsModal')closeSettings();});
document.addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&e.key==='k'){ e.preventDefault(); newChat(); return; }
  if((e.ctrlKey||e.metaKey)&&e.key==='m'){ e.preventDefault(); toggleMic(); return; }
  if(e.key==='Escape'){
    if(!$('#confirmModal').classList.contains('hidden')){ $('#confirmModal').classList.add('hidden'); _confirmCb=null; }
    else if(!$('#newProjectModal').classList.contains('hidden')) closeNewProjectModal();
    else if(!$('#renameModal').classList.contains('hidden')) closeRename();
    else if(!$('#projectModal').classList.contains('hidden')) closeProjectPicker();
    else if(!$('#projConfigModal').classList.contains('hidden')) closeProjectConfig();
    else if(!$('#settingsModal').classList.contains('hidden')) closeSettings();
    else if($('#sessionsPanel').classList.contains('open')) closeSessions();
    else $('#modelMenu').classList.add('hidden');
  }
});


try{
  state.voiceName=localStorage.getItem('cagentic_voice')||'';
  if(localStorage.getItem('cagentic_voiceout')==='1') toggleVoiceOut();
}catch(e){}

boot();
"""
