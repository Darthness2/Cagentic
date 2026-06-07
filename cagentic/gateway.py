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

Interactive panels (these render INLINE in the conversation, not as floating
windows — the user clicks them and their choice is sent back to you as a reply):
  {"panel":"actions","title":"...","buttons":[{"label":"Yes, proceed","prompt":"Yes, go ahead"},{"label":"Cancel","prompt":"Cancel that"}]}
  {"panel":"choices","title":"...","prompt":"I choose ","options":["Red","Green","Blue"]}
  {"panel":"form","title":"...","fields":[{"name":"city","label":"City","placeholder":"e.g. Paris"}],"submit":"What's the weather in {city}?","button":"Check"}
  {"panel":"checklist","title":"...","items":["First step","Second step","Third step"]}

Rules:
- Emit `hud` blocks ONLY for things worth visualizing. Don't wrap every reply.
- Each block = exactly one JSON object, valid JSON, double quotes.
- Use charts (bar/line/pie) for numeric comparisons, trends, and distributions.
- Use interactive panels when you want the user to pick, confirm, or fill in
  something — clicking a button/choice or submitting a form sends that text
  back to you as their next message, so phrase prompts as the user would reply.
- For `form`, the `submit` string is a template; {name} placeholders are filled
  with the matching field values when the user submits.
- Emit {"panel":"clear"} before new panels when replacing the previous display.
- After tool calls that return structured data, a matching panel is a nice touch.
- Still write a brief natural-language reply alongside the panels.
- Data panels appear as draggable floating windows; interactive panels appear
  inline in the chat so the user can act on them in context.
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
        self._active_source: str = "pc"  # "ios" or "pc" — who initiated this turn

        self._perm_cv = threading.Condition()
        self._perm_answer: str | None = None

        # Computer control approval bridge (iOS client approves/denies PC actions)
        self._comp_cv = threading.Condition()
        self._comp_approvals: dict[str, bool] = {}  # action_id -> approved

        # Phone action result bridge (iOS client returns results from phone actions)
        self._phone_cv = threading.Condition()
        self._phone_results: dict[str, dict] = {}  # action_id -> result dict

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
        # Wire up phone action callback — will be activated when source=ios
        self._setup_phone_action()
        # Wire up widget action callback — emits SSE widget events
        self._setup_widget_action()
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

    # -- computer control approval bridge -----------------------------------

    def deliver_computer_approval(self, action_id: str, approved: bool) -> None:
        """Receive an approval/denial from the iOS client for a computer control action."""
        with self._comp_cv:
            self._comp_approvals[action_id] = approved
            self._comp_cv.notify_all()

    def wait_computer_approval(self, action_id: str, emit, event_type: str, data: dict, timeout: float = 300) -> bool:
        """Emit a computer control SSE event and wait for the iOS client to approve.
        Returns True if approved, False if denied or timed out."""
        with self._comp_cv:
            self._comp_approvals.pop(action_id, None)
        emit(event_type, data)
        with self._comp_cv:
            deadline = time.monotonic() + timeout
            while action_id not in self._comp_approvals:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._comp_cv.wait(remaining)
            return self._comp_approvals.pop(action_id, False)

    # -- phone action result bridge -----------------------------------------

    def deliver_phone_result(self, action_id: str, result: dict) -> None:
        """Receive an execution result from the iOS client for a phone action."""
        with self._phone_cv:
            self._phone_results[action_id] = result
            self._phone_cv.notify_all()

    def wait_phone_result(self, action_id: str, emit, event_type: str, data: dict, timeout: float = 120) -> dict | None:
        """Emit a phone action SSE event and wait for the iOS client to execute it.
        Returns the result dict, or None on timeout."""
        with self._phone_cv:
            self._phone_results.pop(action_id, None)
        emit(event_type, data)
        with self._phone_cv:
            deadline = time.monotonic() + timeout
            while action_id not in self._phone_results:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._phone_cv.wait(remaining)
            return self._phone_results.pop(action_id, None)

    # -- device context injection -------------------------------------------

    _DEVICE_PROMPT_IOS = """

=== DEVICE CONTEXT ===
The user is prompting you from their iPhone (iOS device). You can control BOTH the PC (where this gateway runs) AND the iPhone. When you want to control the iPhone, use phone-specific tools:
- phone_shell: Run a shell command on the iPhone
- phone_clipboard_read: Read the iPhone clipboard
- phone_clipboard_write: Write to the iPhone clipboard
- phone_open_app: Open an app on the iPhone by scheme or URL
- phone_open_url: Open a URL on the iPhone (in Safari)
- phone_screenshot: The iPhone user can take a screenshot and share it with you

When you want to control the PC, use the normal tools (shell, file operations, etc.) as usual. The user will approve PC actions from their phone.

=== WIDGET DISPLAY ===
You can display rich visual widgets on the user's iPhone using the show_widget tool. This renders structured data as native iOS cards — much better than plain text for visual information. Use show_widget whenever you have structured data to present:

- **Stocks**: Use type "stocks" with items containing symbol, name, price, change, change_pct. Include a chart object with labels (time periods) and values (prices) to show an interactive stock graph: chart: {labels: ["9:30","10:00",...], values: [178.50,179.20,...]}
- **Sports scores**: Use type "sports" with league and games containing home/away teams, scores, and status.
- **Weather**: Use type "weather" with current conditions and forecast array.
- **Crypto**: Use type "crypto" with items containing symbol, name, price, change_24h, change_pct. Include chart data for price graphs.
- **Calendar**: Use type "calendar" with date and events array.
- **Stats**: Use type "stats" with items containing label, value, and trend.
- **Progress**: Use type "progress" with steps array showing build/deployment progress.
- **Lists**: Use type "list" with items containing text and checked status.
- **Tables**: Use type "table" with headers and rows for any tabular data.

Always prefer show_widget over plain text when presenting visual/structured data. The user's phone will render it beautifully.

IMPORTANT: When you use show_widget, do NOT repeat the widget title or type in your text response. The widget card already shows what it is. Just give a brief 1-sentence intro like "Here's the info:" or skip the text entirely — never say "Here are the stocks:" then also show a stocks widget with title "Stocks".
"""

    _DEVICE_PROMPT_PC = ""
    def _inject_device_context(self, source: str) -> None:
        """Update the system prompt and tool groups based on who initiated the turn."""
        from .tools import DEFAULT_GROUPS
        suffix = self.engine.system_suffix or ""
        marker = "\n\n=== DEVICE CONTEXT ==="
        # Remove any previous device context injection
        if marker in suffix:
            suffix = suffix[:suffix.index(marker)]
        if source == "ios":
            suffix += self._DEVICE_PROMPT_IOS
            # Enable phone tool group for iOS-originated turns
            groups = self.engine.state.tool_groups
            if groups is None:
                groups = set(DEFAULT_GROUPS)
            groups = groups | {"phone"}
            self.engine.state.tool_groups = groups
        else:
            # Disable phone tool group for PC-originated turns
            groups = self.engine.state.tool_groups
            if groups is not None:
                groups = groups - {"phone"}
                self.engine.state.tool_groups = groups
        self.engine.system_suffix = suffix
        self.engine.refresh_system_prompt()
        self.engine.refresh_tool_cache()

    def _setup_phone_action(self) -> None:
        """Wire up the phone_action callback on the engine's tool executor."""
        gw = self

        def phone_action(action_type: str, payload: dict) -> dict | None:
            """Callback for phone control tools. Emits SSE event and waits for result."""
            import uuid
            action_id = str(uuid.uuid4())
            emit = gw._active_emit
            if emit is None:
                return {"success": False, "error": "no active connection"}
            event_type = f"phone_{action_type}"
            data = {"id": action_id, **payload}
            return gw.wait_phone_result(action_id, emit, event_type, data)

        self.engine.executor.phone_action = phone_action

    def _setup_widget_action(self) -> None:
        """Wire up the widget_action callback on the engine's tool executor."""
        gw = self

        def widget_action(widget_type: str, title: str, data: dict) -> str | None:
            """Callback for show_widget tool. Emits SSE widget event."""
            emit = gw._active_emit
            if emit is None:
                return None  # No active stream — tool returns text fallback
            emit("widget", {"type": widget_type, "title": title, "data": data})
            return None  # Signal to tool that widget was emitted

        self.engine.executor.widget_action = widget_action

    # -- chats --------------------------------------------------------------
    # -- chats --------------------------------------------------------------
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
        gw_cfg = self.config.get("gateway") or {}
        return {
            "model": current,
            "models": models,
            "temperature": self.engine.temperature,
            "user_name": self.agent.state.user_name or "",
            "stream": self.engine.stream,
            "yolo": self.agent.state.yolo,
            "gateway_port": int(gw_cfg.get("port", 8700)),
            "gateway_auto_start": bool(gw_cfg.get("auto_start", True)),
            "system_prompt": self.engine.system_suffix or "",
        }

    def abort_generation(self) -> None:
        """Abort the current streaming generation."""
        self.engine._abort_turn = True

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
        if "gateway_port" in data:
            try:
                port = int(data["gateway_port"])
                if 1 <= port <= 65535:
                    _config.set_value(cfg, "gateway.port", port)
            except (TypeError, ValueError):
                pass
        if "gateway_auto_start" in data:
            _config.set_value(cfg, "gateway.auto_start", bool(data["gateway_auto_start"]))
        if "system_prompt" in data:
            self.engine.system_suffix = str(data["system_prompt"]).strip()
            self.engine.refresh_system_prompt()
            cfg["system_prompt"] = self.engine.system_suffix
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

    def run_turn(self, message: str, emit, source: str = "pc") -> None:
        if not self._turn_lock.acquire(blocking=False):
            emit("error", {"text": "Cagentic is still working on the previous message."})
            return
        self._active_emit = emit
        self._active_source = source
        self.engine.model = self.agent.model
        # Inject device context into system prompt
        self._inject_device_context(source)
        try:
            for ev in self.engine.submit_message(message):
                emit(ev.kind, ev.data)
        except _ClientGone:
            raise
        except Exception as e:
            emit("error", {"text": f"{type(e).__name__}: {e}"})
        finally:
            self._active_emit = None
            self._active_source = "pc"
            try:
                self._save_current()
            except Exception:
                pass
            self._turn_lock.release()

    # -- slash command handler for web UI -----------------------------------

    def handle_cmd(self, cmd: str, arg1: str = "", arg2: str = "") -> dict:
        """Execute a slash command from the web UI and return a result dict.

        Supported commands mirror the CLI: /new, /clear, /model, /models,
        /diag, /tools, /groups, /yolo, /help, /plan, /stream, /name, /host,
        /retry, /undo, /save, /notes, /mcp, /config, /set.
        """
        from .tools import DEFAULT_GROUPS, _all_tools
        from . import config as _cfg

        cmd = cmd.lstrip("/").lower()
        agent = self.agent
        cfg = self.config

        if cmd in ("help", "?"):
            return {"ok": True, "text": (
                "/new — start a new chat\n"
                "/clear — clear the current chat\n"
                "/model <name> — switch model\n"
                "/models — list available models\n"
                "/diag — show diagnostics\n"
                "/tools — list active tools\n"
                "/groups [enable|disable <name>] — show/change tool groups\n"
                "/yolo [on|off] — toggle auto-approve\n"
                "/plan [on|off] — toggle plan mode\n"
                "/stream [on|off] — toggle streaming\n"
                "/name <name> — set your name\n"
                "/host <url> — change Ollama host\n"
                "/save — save current chat\n"
                "/notes — list notes\n"
                "/mcp — list MCP servers\n"
                "/config — show config\n"
                "/set <key> <value> — set config value\n"
                "/undo — undo last exchange\n"
                "/retry — retry last turn\n"
            )}

        if cmd == "new":
            cur = self.new_chat()
            return {"ok": True, "text": "new chat started", "current": cur}

        if cmd == "clear":
            self.engine.messages.clear()
            self._save_current()
            return {"ok": True, "text": "chat cleared"}

        if cmd == "model":
            if not arg1:
                return {"ok": True, "text": f"current model: {agent.model}"}
            result = self.set_model(arg1)
            if "error" in result:
                return {"ok": False, "text": result["error"]}
            return {"ok": True, "text": f"model → {result['model']}", "model": result["model"]}

        if cmd == "models":
            try:
                models = agent.client.list_models()
            except Exception as e:
                return {"ok": False, "text": f"could not list models: {e}"}
            return {"ok": True, "text": "available models:\n" + "\n".join(f"  {m}" for m in models), "models": models}

        if cmd == "diag":
            groups = agent.state.tool_groups if agent.state.tool_groups is not None else DEFAULT_GROUPS
            lines = [f"model:    {agent.model}"]
            lines.append(f"name:     {agent.state.user_name or '(not set)'}")
            lines.append(f"workspace: {agent.state.workspace}")
            lines.append(f"tools:    {'native' if agent.tools_enabled else 'text-protocol fallback'}")
            lines.append(f"groups:   {', '.join(sorted(groups))}")
            lines.append(f"stream:   {'on' if agent.engine.stream else 'off'}")
            try:
                status = agent.client.model_vram_status(agent.model)
                if status is None:
                    lines.append("vram:     model not currently loaded")
                elif status["fully_gpu"]:
                    lines.append(f"vram:     {status['size_vram'] / (1024**3):.1f} GB · fully on GPU ✓")
                else:
                    size_gb = status["size"] / (1024**3)
                    cpu_gb = status["cpu_bytes"] / (1024**3)
                    pct = status["cpu_percent"]
                    lines.append(f"vram:     {cpu_gb:.1f}/{size_gb:.1f} GB on CPU ({pct:.0f}% offloaded — slow)")
            except Exception:
                lines.append("vram:     (not available)")
            mcp_servers = list(((cfg.get("mcp") or {}).get("servers") or {}).keys())
            lines.append(f"mcp:      {len(mcp_servers)} configured ({', '.join(mcp_servers) or 'none'})")
            return {"ok": True, "text": "\n".join(lines)}

        if cmd == "tools":
            mode = "native" if agent.tools_enabled else "text-protocol fallback"
            tools = _all_tools()
            lines = [f"{len(tools)} tools ({mode}):"]
            for t in tools:
                lines.append(f"  {t['function']['name']}")
            return {"ok": True, "text": "\n".join(lines)}

        if cmd == "groups":
            active = agent.state.tool_groups if agent.state.tool_groups is not None else DEFAULT_GROUPS
            if not arg1:
                from .tools import TOOL_GROUPS
                lines = ["tool groups (✓ = sent to the model):"]
                for g, names in sorted(TOOL_GROUPS.items()):
                    mark = "✓" if g in active else "✗"
                    lines.append(f"  {mark} {g} ({len(names)} tools)")
                return {"ok": True, "text": "\n".join(lines)}
            if arg1 == "enable" and arg2:
                groups = set(active)
                groups.add(arg2)
                agent.state.tool_groups = groups
                _cfg.set_value(cfg, "tool_groups", sorted(groups))
                _cfg.save(cfg)
                self.engine.refresh_system_prompt()
                return {"ok": True, "text": f"enabled '{arg2}' — {len(groups)} group(s) active"}
            if arg1 == "disable" and arg2:
                groups = set(active)
                groups.discard(arg2)
                agent.state.tool_groups = groups
                _cfg.set_value(cfg, "tool_groups", sorted(groups))
                _cfg.save(cfg)
                self.engine.refresh_system_prompt()
                return {"ok": True, "text": f"disabled '{arg2}' — {len(groups)} group(s) active"}
            return {"ok": False, "text": "usage: /groups  |  /groups enable <name>  |  /groups disable <name>"}

        if cmd == "yolo":
            want = arg1.lower() if arg1 else ("off" if agent.state.yolo else "on")
            if want not in ("on", "off"):
                return {"ok": False, "text": "usage: /yolo on|off"}
            agent.state.update(yolo=(want == "on"))
            cfg["yolo"] = agent.state.yolo
            _cfg.save(cfg)
            return {"ok": True, "text": f"yolo mode: {'ON (auto-approve)' if agent.state.yolo else 'OFF (ask every time)'}"}

        if cmd == "plan":
            want = arg1.lower() if arg1 else ("off" if agent.state.plan_mode else "on")
            if want not in ("on", "off"):
                return {"ok": False, "text": "usage: /plan on|off"}
            agent.state.update(plan_mode=(want == "on"))
            self.engine.refresh_system_prompt()
            return {"ok": True, "text": f"plan mode: {'ON (read-only)' if agent.state.plan_mode else 'OFF'}"}

        if cmd == "stream":
            want = arg1.lower() if arg1 else ("off" if agent.engine.stream else "on")
            if want not in ("on", "off"):
                return {"ok": False, "text": "usage: /stream on|off"}
            agent.engine.stream = (want == "on")
            _cfg.set_value(cfg, "ollama.stream", agent.engine.stream)
            _cfg.save(cfg)
            return {"ok": True, "text": f"streaming: {'on' if agent.engine.stream else 'off'} (saved)"}

        if cmd == "name":
            if not arg1:
                return {"ok": True, "text": f"your name: {agent.state.user_name or '(not set)'}"}
            agent.state.update(user_name=arg1)
            self.engine.refresh_system_prompt()
            _cfg.set_value(cfg, "user_name", arg1)
            _cfg.save(cfg)
            return {"ok": True, "text": f"name set to: {arg1}"}

        if cmd == "host":
            if not arg1:
                return {"ok": True, "text": f"ollama host: {agent.client.host}"}
            try:
                agent.client.host = arg1.rstrip("/")
                _cfg.set_value(cfg, "ollama.host", agent.client.host)
                _cfg.save(cfg)
                return {"ok": True, "text": f"host → {agent.client.host}"}
            except Exception as e:
                return {"ok": False, "text": f"error: {e}"}

        if cmd == "save":
            self._save_current()
            return {"ok": True, "text": "chat saved"}

        if cmd == "notes":
            from .notes import _notes
            all_notes = _notes.list_all()
            if not all_notes:
                return {"ok": True, "text": "no notes"}
            lines = [f"{n['title']}  ({n['id']})" for n in all_notes]
            return {"ok": True, "text": "\n".join(lines)}

        if cmd == "mcp":
            mcp_servers = list(((cfg.get("mcp") or {}).get("servers") or {}).keys())
            if not mcp_servers:
                return {"ok": True, "text": "no MCP servers configured"}
            return {"ok": True, "text": "MCP servers:\n" + "\n".join(f"  {s}" for s in mcp_servers)}

        if cmd == "config":
            import copy
            safe = copy.deepcopy(cfg)
            gh = safe.get("github") or {}
            tok = gh.get("token")
            if tok and len(tok) > 8:
                gh["token"] = tok[:4] + "…" + tok[-4:]
            return {"ok": True, "text": json.dumps(safe, indent=2)}

        if cmd == "set":
            if not arg1 or not arg2:
                return {"ok": False, "text": "usage: /set <key> <value>"}
            _cfg.set_value(cfg, arg1, arg2)
            _cfg.save(cfg)
            return {"ok": True, "text": f"set {arg1} = {arg2}"}

        if cmd == "undo":
            # Remove last user + assistant messages
            msgs = self.engine.messages
            while msgs and msgs[-1].get("role") != "user":
                msgs.pop()
            if msgs and msgs[-1].get("role") == "user":
                msgs.pop()
            self._save_current()
            return {"ok": True, "text": "undid last exchange"}

        if cmd == "retry":
            # Remove last assistant message so the engine re-generates
            msgs = self.engine.messages
            while msgs and msgs[-1].get("role") == "assistant":
                msgs.pop()
            self._save_current()
            return {"ok": True, "text": "removed last reply — send a message to retry"}

        return {"ok": False, "text": f"unknown command: /{cmd}. Type /help for available commands."}


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
        if path == "/api/abort":
            gw.abort_generation()
            self._json({"ok": True})
            return
        if path == "/api/chat":
            b = self._body()
            source = str(b.get("source", "pc"))
            self._stream_chat(str(b.get("message", "")).strip(), source=source)
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
        if path == "/api/computer/approve":
            b = self._body()
            action_id = str(b.get("id", ""))
            approved = bool(b.get("approved", False))
            gw.deliver_computer_approval(action_id, approved)
            self._json({"ok": True})
            return
        if path == "/api/computer/result":
            b = self._body()
            action_id = str(b.get("id", ""))
            result = b.get("result", {})
            gw.deliver_phone_result(action_id, result)
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
        if path == "/api/cmd":
            b = self._body()
            self._json(gw.handle_cmd(str(b.get("cmd", "")), str(b.get("arg1", "")), str(b.get("arg2", ""))))
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

    def _stream_chat(self, message: str, source: str = "pc") -> None:
        emit = self._begin_sse()
        if not message:
            try:
                emit("error", {"text": "empty message"})
            except _ClientGone:
                pass
            return
        try:
            self._gw().run_turn(message, emit, source=source)
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
  <div id="restorePill" class="restore-pill" style="display:none">
    <span class="restore-pill-icon">↩</span><span class="restore-pill-count">0</span> panels
    <div class="restore-dropdown" id="restoreDropdown"></div>
  </div>

  <div class="cmd-area">
    <div class="cmd-box" id="cmdBox">
      <span class="cmd-prompt">&gt;_</span>
      <textarea id="input" rows="1" placeholder="Type a message&#8230;"></textarea>
      <button id="micBtn" class="mic-btn" title="Voice input">&#127908;</button>
      <button id="send" class="exec-btn">EXECUTE</button>
      <button id="stopBtn" class="exec-btn stop-btn hidden">&#9632; STOP</button>
    </div>
    <div class="cmd-footer">
      <span>CAGENTIC v<span id="versionSpan">--</span></span>
      <span id="hintText">Enter to send &bull; Shift+Enter for newline &bull; Ctrl+K New Chat &bull; Ctrl+S Settings</span>
      <span id="busyLabel" class="busy-label hidden">&#9679; Thinking&#8230;</span>
      <span id="tokenStats" class="token-stats hidden"></span>
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
      <hr class="settings-divider" />
      <div class="section-label">GATEWAY</div>
      <div class="field">
        <span class="field-label">PORT</span>
        <input id="setGwPort" type="number" min="1" max="65535" placeholder="8700" />
      </div>
      <div class="field row">
        <span class="field-label">Auto-start on launch</span>
        <label class="toggle"><input id="setGwAuto" type="checkbox" /><span></span></label>
      </div>
      <div class="field-hint">Port takes effect next launch. Auto-start can be toggled anytime.</div>
      <hr class="settings-divider" />
      <div class="section-label">SYSTEM PROMPT</div>
      <div class="field">
        <span class="field-label">Custom instructions</span>
        <textarea id="setSysPrompt" rows="4" placeholder="Additional instructions appended to the system prompt&#8230;"></textarea>
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
    <div class="modal-head">
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
  --ease: cubic-bezier(.22,.61,.36,1);
  --ease-out: cubic-bezier(.16,1,.3,1);
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; }
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
  transition: background .15s var(--ease), border-color .15s var(--ease), transform .1s var(--ease); white-space: nowrap;
}
.nav-btn:hover { background: rgba(240,168,122,.22); border-color: var(--border-h); }
.nav-btn:active { transform: scale(.95); }
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
  position: relative; overflow: hidden;
  padding: 14px 16px; border: 1px solid var(--border);
  background: rgba(240,168,122,.03); cursor: pointer;
  transition: background .2s var(--ease), border-color .2s var(--ease), transform .2s var(--ease), box-shadow .2s var(--ease);
  text-align: left;
  opacity: 0; animation: cardIn .4s var(--ease-out) forwards; animation-delay: calc(var(--i, 0) * 45ms);
}
.qcard::after {
  content: ''; position: absolute; left: 0; top: 0; height: 100%; width: 2px;
  background: var(--accent); opacity: 0; transform: scaleY(.3);
  transition: opacity .2s var(--ease), transform .25s var(--ease);
}
.qcard:hover { background: rgba(240,168,122,.08); border-color: var(--border-h); transform: translateY(-3px); box-shadow: 0 6px 22px rgba(0,0,0,.35); }
.qcard:hover::after { opacity: 1; transform: scaleY(1); }
.qcard:active { transform: translateY(-1px) scale(.99); }
@keyframes cardIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
.qcard-icon { font-size: 18px; margin-bottom: 7px; display: block; transition: transform .2s var(--ease); }
.qcard:hover .qcard-icon { transform: scale(1.12); }
.qcard-title { font-size: 11px; color: #d8c8e0; letter-spacing: .05em; display: block; margin-bottom: 3px; }
.qcard-sub   { font-size: 9px;  color: var(--text-2); letter-spacing: .04em; line-height: 1.5; display: block; }

/* messages */
.msg-row { margin: 10px 0; animation: messageIn .32s var(--ease-out) both; animation-delay: calc(var(--i, 0) * 60ms); }
.msg-row.user { --slide-x: 30px; }
.msg-row.assistant { --slide-x: -30px; }
@keyframes messageIn { from { opacity: 0; transform: translate(var(--slide-x, 0), 12px); } to { opacity: 1; transform: translate(0, 0); } }
.msg-row.user .bubble { transition: background .2s var(--ease), border-color .2s var(--ease); }
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
  font-weight: bold; animation: avatarGlow 4s ease-in-out infinite;
}
@keyframes avatarGlow { 0%,100% { box-shadow: 0 0 8px var(--accent-glow); } 50% { box-shadow: 0 0 16px var(--accent-glow); } }
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
  border-left: 2px solid var(--text-dim);
  transition: border-color .25s var(--ease), background .25s var(--ease);
  animation: messageIn .3s var(--ease-out) both;
  animation-delay: calc(var(--i, 0) * 60ms);
}
.tool-row .tool-icon { transition: transform .2s var(--ease); }
.tool-row.ok .tool-icon, .tool-row.bad .tool-icon { transform: scale(1.05); }
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
.token-stats { font-size: 9px; color: var(--text-dim); letter-spacing: .06em; }

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
.restore-pill {
  position: fixed; bottom: 20px; right: 20px; z-index: 200;
  background: rgba(22,17,24,.92); border: 1px solid var(--border-h);
  box-shadow: 0 4px 20px rgba(0,0,0,.5), 0 0 12px rgba(240,168,122,.08);
  backdrop-filter: blur(6px);
  padding: 8px 14px; border-radius: 20px;
  color: var(--accent); font-size: 11px; letter-spacing: .06em;
  cursor: pointer; user-select: none;
  transition: transform .2s var(--ease), box-shadow .2s var(--ease);
  pointer-events: auto;
}
.restore-pill:hover { transform: translateY(-2px); box-shadow: 0 6px 24px rgba(0,0,0,.6), 0 0 16px rgba(240,168,122,.12); }
.restore-pill-icon { margin-right: 4px; }
.restore-pill-count { font-weight: 700; }
.restore-dropdown {
  display: none; position: absolute; bottom: calc(100% + 8px); right: 0;
  background: rgba(22,17,24,.96); border: 1px solid var(--border-h);
  box-shadow: 0 8px 30px rgba(0,0,0,.6);
  backdrop-filter: blur(8px);
  border-radius: 10px; min-width: 180px; max-width: 260px;
  padding: 6px 0; overflow: hidden;
}
.restore-pill.open .restore-dropdown { display: block; }
.restore-item {
  padding: 8px 14px; font-size: 11px; color: #d8c8e0; cursor: pointer;
  transition: background .15s;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.restore-item:hover { background: rgba(240,168,122,.1); color: var(--accent); }
.hud-window {
  position: absolute; pointer-events: auto;
  min-width: 220px; min-height: 100px;
  background: rgba(22,17,24,.92); border: 1px solid var(--border-h);
  box-shadow: 0 4px 30px rgba(0,0,0,.55), 0 0 20px rgba(240,168,122,.06);
  display: flex; flex-direction: column;
  animation: hudWinIn .4s var(--ease-out) both;
  animation-delay: calc(var(--i, 0) * 80ms);
  backdrop-filter: blur(6px);
  transition: box-shadow .25s var(--ease);
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
.hud-window.resizing { opacity: .9; box-shadow: 0 8px 40px rgba(0,0,0,.7), 0 0 30px rgba(240,168,122,.12); }
@keyframes hudWinIn { from { opacity: 0; transform: scale(.88) translateY(18px); } to { opacity: 1; transform: scale(1) translateY(0); } }
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

/* ---- INTERACTIVE PANELS (inline, clickable) ------------------------------- */
.ix-panel {
  margin: 10px 0 12px; padding: 13px 15px;
  border: 1px solid var(--border-h); border-left: 2px solid var(--accent);
  background: linear-gradient(180deg, rgba(240,168,122,.05), rgba(34,27,42,.55));
  position: relative; animation: messageIn .32s var(--ease-out) both;
}
.ix-panel::before {
  content: ''; position: absolute; top: -1px; left: 0; width: 40px; height: 1px;
  background: linear-gradient(90deg, var(--accent), transparent);
}
.ix-title {
  font-size: 9px; color: var(--accent); letter-spacing: .14em; text-transform: uppercase;
  margin-bottom: 11px; text-shadow: 0 0 8px var(--accent-glow);
}
.ix-panel.ix-used { opacity: .62; border-left-color: var(--text-dim); }
.ix-panel.ix-used::before { background: linear-gradient(90deg, var(--ok), transparent); }

/* action buttons */
.ix-actions-row { display: flex; flex-wrap: wrap; gap: 9px; }
.ix-btn {
  flex: 0 1 auto; padding: 9px 16px; cursor: pointer;
  border: 1px solid var(--accent); background: rgba(240,168,122,.1);
  color: var(--accent); font: 10px var(--mono); letter-spacing: .08em;
  text-transform: uppercase; position: relative; overflow: hidden;
  transition: background .18s var(--ease), box-shadow .2s var(--ease), transform .1s var(--ease), color .18s var(--ease);
}
.ix-btn:hover { background: rgba(240,168,122,.24); box-shadow: 0 4px 18px rgba(240,168,122,.22); transform: translateY(-1px); }
.ix-btn:active { transform: translateY(0) scale(.97); }
.ix-btn.ix-active { background: var(--accent); color: #161118; border-color: var(--accent); }
.ix-btn:disabled { cursor: default; opacity: .5; box-shadow: none; transform: none; }
.ix-btn:disabled:hover { background: rgba(240,168,122,.1); box-shadow: none; transform: none; }

/* choices list */
.ix-choices-list { display: flex; flex-direction: column; gap: 6px; }
.ix-choice {
  display: flex; align-items: center; gap: 9px; text-align: left; width: 100%;
  padding: 9px 13px; cursor: pointer; color: var(--text);
  border: 1px solid var(--border); background: rgba(240,168,122,.03);
  font: 11px var(--mono); letter-spacing: .03em;
  transition: background .18s var(--ease), border-color .18s var(--ease), padding-left .18s var(--ease), color .18s var(--ease);
}
.ix-choice .ix-choice-mark { color: var(--accent); transition: transform .18s var(--ease); }
.ix-choice:hover { background: rgba(240,168,122,.1); border-color: var(--border-h); padding-left: 18px; color: #fff; }
.ix-choice:hover .ix-choice-mark { transform: translateX(2px); }
.ix-choice:active { background: rgba(240,168,122,.18); }
.ix-choice.ix-active { border-color: var(--accent); background: rgba(240,168,122,.16); color: var(--accent); }
.ix-choice:disabled { cursor: default; opacity: .55; }
.ix-choice:disabled:hover { background: rgba(240,168,122,.03); padding-left: 13px; color: var(--text); }

/* form */
.ix-form-fields { display: flex; flex-direction: column; gap: 10px; margin-bottom: 11px; }
.ix-field { display: flex; flex-direction: column; gap: 4px; }
.ix-flabel { font-size: 9px; color: var(--text-2); letter-spacing: .1em; text-transform: uppercase; }
.ix-input {
  width: 100%; background: rgba(22,17,24,.7); border: 1px solid var(--border-h);
  color: var(--text); padding: 8px 10px; font: 12px var(--mono); letter-spacing: .03em;
  transition: border-color .18s var(--ease), box-shadow .18s var(--ease);
}
.ix-input:focus { outline: 0; border-color: var(--accent); box-shadow: 0 0 14px rgba(240,168,122,.15); }
.ix-input:disabled { opacity: .55; }
.ix-submit {
  padding: 9px 20px; cursor: pointer; border: 1px solid var(--accent);
  background: rgba(240,168,122,.12); color: var(--accent); font: 10px var(--mono);
  letter-spacing: .14em; text-transform: uppercase;
  transition: background .18s var(--ease), box-shadow .2s var(--ease), transform .1s var(--ease);
}
.ix-submit:hover { background: rgba(240,168,122,.26); box-shadow: 0 4px 18px rgba(240,168,122,.22); }
.ix-submit:active { transform: scale(.97); }
.ix-submit:disabled { cursor: default; opacity: .5; box-shadow: none; transform: none; }

/* checklist */
.ix-checklist-list { display: flex; flex-direction: column; gap: 3px; }
.ix-check { display: flex; align-items: center; gap: 10px; padding: 6px 4px; cursor: pointer; user-select: none; font: 11px var(--mono); color: var(--text-2); transition: color .18s var(--ease); }
.ix-check:hover { color: var(--text); }
.ix-check input { position: absolute; opacity: 0; width: 0; height: 0; }
.ix-box { width: 15px; height: 15px; flex-shrink: 0; border: 1px solid var(--border-h); background: rgba(240,168,122,.04); position: relative; transition: background .18s var(--ease), border-color .18s var(--ease); }
.ix-box::after { content: '\2713'; position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 11px; color: #161118; opacity: 0; transform: scale(.4); transition: opacity .18s var(--ease), transform .18s var(--ease); }
.ix-check.checked .ix-box { background: var(--accent); border-color: var(--accent); box-shadow: 0 0 10px var(--accent-glow); }
.ix-check.checked .ix-box::after { opacity: 1; transform: scale(1); }
.ix-check.checked .ix-clabel { color: var(--text-dim); text-decoration: line-through; text-decoration-color: var(--text-dim); }
.ix-clabel { transition: color .18s var(--ease); }

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
  cursor: pointer; letter-spacing: .16em; text-transform: uppercase;
  transition: background .15s var(--ease), box-shadow .2s var(--ease), transform .1s var(--ease);
}
.exec-btn:hover    { background: rgba(240,168,122,.24); box-shadow: 0 0 18px rgba(240,168,122,.25); }
.exec-btn:active   { transform: scale(.96); }
.exec-btn:disabled { opacity: .28; cursor: default; box-shadow: none; transform: none; }
.stop-btn { background: rgba(220,60,60,.18); color: #e06060; border-color: rgba(220,60,60,.4); }
.stop-btn:hover { background: rgba(220,60,60,.32); box-shadow: 0 0 18px rgba(220,60,60,.25); }
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
.settings-divider { border: none; border-top: 1px solid var(--border); margin: 4px 0; }
.section-label { font-size: 9px; color: var(--accent); letter-spacing: .14em; text-transform: uppercase; font-weight: 600; }
.field-hint { font-size: 10px; color: var(--text-dim); line-height: 1.4; }
.field input[type=number] { background: rgba(34,27,42,.9); border: 1px solid var(--border); color: var(--text); padding: 8px 11px; font: 11.5px var(--mono); letter-spacing: .04em; width: 100%; }
.field input[type=number]:focus { outline: 0; border-color: var(--accent); }
#setSysPrompt { background: rgba(34,27,42,.9); border: 1px solid var(--border); color: var(--text); padding: 8px 11px; font: 11.5px var(--mono); letter-spacing: .04em; width: 100%; resize: vertical; min-height: 60px; }
#setSysPrompt:focus { outline: 0; border-color: var(--accent); }
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
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(240,168,122,.22); border-radius: 4px; border: 2px solid transparent; background-clip: padding-box; }
::-webkit-scrollbar-thumb:hover { background: rgba(240,168,122,.42); background-clip: padding-box; border: 2px solid transparent; }
::-webkit-scrollbar-corner { background: transparent; }
/* Firefox scrollbar */
* { scrollbar-width: thin; scrollbar-color: rgba(240,168,122,.28) transparent; }
@media (max-width: 900px) {
  .hud-window { max-width: 90vw; }
  .j-sub { display: none; }
  .quick-cards { grid-template-columns: 1fr 1fr; }
}

/* ===== Specialty widget cards (stocks, weather, crypto, sports, calendar) ===== */
/* Shared shell */
.sw-window { min-width: 280px; }
.sw-window .hud-win-body { padding: 0; gap: 0; }
.sw-card { padding: 12px 14px; font-family: var(--mono); }
.sw-card .sw-spark, .sw-card .sw-sub, .sw-card .sw-watch { margin-top: 10px; }

/* ---- Stocks / Crypto ---- */
.sw-stocks, .sw-crypto {
  display: flex; flex-direction: column;
}
.sw-stocks .sw-hero, .sw-crypto .sw-hero {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 12px;
}
.sw-stocks .sw-sym, .sw-crypto .sw-sym {
  font-size: 22px; color: #fff; letter-spacing: .04em; font-weight: 700;
  text-shadow: 0 0 14px var(--accent-glow); line-height: 1;
}
.sw-stocks .sw-name, .sw-crypto .sw-name {
  font-size: 10px; color: var(--text-2); margin-top: 4px; letter-spacing: .04em;
  max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sw-stocks .sw-price, .sw-crypto .sw-price {
  font-size: 26px; color: #fff; line-height: 1; text-align: right;
  text-shadow: 0 0 16px var(--accent-glow); letter-spacing: .02em;
}
.sw-stocks .sw-chg, .sw-crypto .sw-chg {
  font-size: 11px; margin-top: 5px; text-align: right; letter-spacing: .03em;
}
.sw-stocks .sw-chg.ok, .sw-crypto .sw-chg.ok { color: var(--ok); }
.sw-stocks .sw-chg.hot, .sw-crypto .sw-chg.hot { color: var(--hot); }
.sw-stocks .sw-chg-pct, .sw-crypto .sw-chg-pct { opacity: .85; font-weight: 600; }
.sw-stocks .sw-spark, .sw-crypto .sw-spark {
  background: linear-gradient(180deg, rgba(240,168,122,.04), transparent);
  border-top: 1px solid var(--border); border-bottom: 1px solid var(--border);
  padding: 6px 0; margin: 8px 0 0;
}
.sw-stocks .sw-spark svg, .sw-crypto .sw-spark svg { width: 100%; height: 60px; display: block; }
.sw-stocks .sw-sub, .sw-crypto .sw-sub {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 0;
  border-top: 1px solid var(--border); padding-top: 8px;
}
.sw-stocks .sw-subcell, .sw-crypto .sw-subcell {
  display: flex; flex-direction: column; align-items: flex-start;
}
.sw-stocks .sw-subl, .sw-crypto .sw-subl {
  font-size: 8px; color: var(--text-dim); letter-spacing: .12em; text-transform: uppercase;
}
.sw-stocks .sw-subv, .sw-crypto .sw-subv {
  font-size: 11px; color: var(--text); margin-top: 2px;
}
.sw-stocks .sw-watch, .sw-crypto .sw-watch {
  display: flex; flex-direction: column; gap: 0;
  border-top: 1px solid var(--border); padding-top: 6px;
}
.sw-stocks .sw-watch-row, .sw-crypto .sw-watch-row {
  display: grid; grid-template-columns: 60px 1fr 70px 56px;
  align-items: center; gap: 8px; padding: 5px 0;
  font-size: 10px; border-bottom: 1px solid rgba(240,168,122,.04);
}
.sw-stocks .sw-watch-row:last-child, .sw-crypto .sw-watch-row:last-child { border-bottom: 0; }
.sw-stocks .sw-watch-sym, .sw-crypto .sw-watch-sym {
  color: var(--accent); letter-spacing: .04em; font-weight: 600;
}
.sw-stocks .sw-watch-name, .sw-crypto .sw-watch-name {
  color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sw-stocks .sw-watch-price, .sw-crypto .sw-watch-price {
  color: var(--text); text-align: right;
}
.sw-stocks .sw-watch-chg, .sw-crypto .sw-watch-chg {
  text-align: right; font-weight: 600;
}
.sw-stocks .sw-watch-chg.ok, .sw-crypto .sw-watch-chg.ok { color: var(--ok); }
.sw-stocks .sw-watch-chg.hot, .sw-crypto .sw-watch-chg.hot { color: var(--hot); }

/* ---- Weather ---- */
.sw-weather { padding: 0; }
.sw-weather .ww-hero {
  display: flex; align-items: center; gap: 16px;
  padding: 16px 16px 12px;
  background: linear-gradient(135deg, rgba(240,168,122,.10), rgba(240,168,122,0) 70%);
  border-bottom: 1px solid var(--border);
}
.sw-weather .ww-ic {
  font-size: 56px; line-height: 1; color: var(--accent);
  text-shadow: 0 0 18px var(--accent-glow); flex-shrink: 0;
}
.sw-weather .ww-hero-r { flex: 1; min-width: 0; }
.sw-weather .ww-loc {
  font-size: 11px; color: var(--accent); letter-spacing: .14em;
  text-transform: uppercase; text-shadow: 0 0 8px var(--accent-glow);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sw-weather .ww-temp {
  font-size: 42px; color: #fff; line-height: 1; margin-top: 4px;
  text-shadow: 0 0 22px var(--accent-glow); letter-spacing: .02em;
}
.sw-weather .ww-cond {
  font-size: 11px; color: var(--text); margin-top: 4px; letter-spacing: .04em;
}
.sw-weather .ww-meta {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(70px, 1fr));
  gap: 0; padding: 10px 14px;
  border-bottom: 1px solid var(--border);
}
.sw-weather .ww-metacell { padding: 0 6px; border-left: 1px solid var(--border); }
.sw-weather .ww-metacell:first-child { border-left: 0; padding-left: 0; }
.sw-weather .ww-metal {
  font-size: 8px; color: var(--text-dim); letter-spacing: .12em; text-transform: uppercase;
}
.sw-weather .ww-metav { font-size: 12px; color: var(--text); margin-top: 2px; }
.sw-weather .ww-fc {
  display: grid; grid-auto-flow: column; grid-auto-columns: 1fr;
  padding: 10px 6px 12px;
}
.sw-weather .ww-fcday {
  display: flex; flex-direction: column; align-items: center; gap: 4px;
  padding: 6px 2px; border-radius: 3px;
  transition: background .2s var(--ease);
}
.sw-weather .ww-fcday:hover { background: rgba(240,168,122,.06); }
.sw-weather .ww-fcd {
  font-size: 9px; color: var(--text-dim); letter-spacing: .12em; text-transform: uppercase;
}
.sw-weather .ww-fci { font-size: 18px; color: var(--accent); line-height: 1; }
.sw-weather .ww-fch { font-size: 11px; color: var(--text); font-weight: 600; }
.sw-weather .ww-fcl { font-size: 10px; color: var(--text-dim); }

/* ---- Sports ---- */
.sw-sports { display: flex; flex-direction: column; }
.sw-sports .sx-row {
  display: grid; grid-template-columns: 1fr auto 1fr;
  align-items: center; gap: 12px; padding: 11px 4px;
  border-bottom: 1px solid var(--border);
}
.sw-sports .sx-row:last-child { border-bottom: 0; }
.sw-sports .sx-side { min-width: 0; }
.sw-sports .sx-side.win .sx-name { color: var(--accent); text-shadow: 0 0 8px var(--accent-glow); }
.sw-sports .sx-home { text-align: right; }
.sw-sports .sx-name {
  font-size: 12px; color: var(--text); font-weight: 600; letter-spacing: .03em;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sw-sports .sx-rec { font-size: 9px; color: var(--text-dim); margin-top: 2px; letter-spacing: .04em; }
.sw-sports .sx-mid { display: flex; flex-direction: column; align-items: center; gap: 3px; min-width: 60px; }
.sw-sports .sx-score {
  font-size: 18px; color: var(--text-dim); letter-spacing: .04em; line-height: 1;
  display: flex; align-items: center; gap: 6px;
}
.sw-sports .sx-score .win { color: #fff; font-weight: 700; text-shadow: 0 0 8px var(--accent-glow); }
.sw-sports .sx-dash { color: var(--text-dim); }
.sw-sports .sx-status {
  font-size: 9px; color: var(--text-2); letter-spacing: .12em; text-transform: uppercase;
  display: flex; align-items: center; gap: 5px;
}
.sw-sports .sx-status.live { color: var(--hot); }
.sw-sports .sx-status.final { color: var(--ok); }
.sw-sports .sx-status .sx-dot {
  width: 6px; height: 6px; border-radius: 50%; background: var(--hot);
  box-shadow: 0 0 6px var(--hot); animation: pulse 1.2s ease infinite;
}
.sw-sports .sx-note {
  font-size: 9px; color: var(--text-dim); margin-top: 2px; max-width: 140px;
  text-align: center; line-height: 1.3;
}

/* ---- Calendar ---- */
.sw-cal { display: flex; flex-direction: column; }
.sw-cal .cx-row {
  display: grid; grid-template-columns: 80px 1fr; gap: 12px;
  padding: 9px 4px; border-bottom: 1px solid var(--border);
  border-left: 2px solid var(--cx-color, var(--accent));
  padding-left: 10px; margin-left: -2px;
}
.sw-cal .cx-row:last-child { border-bottom: 0; }
.sw-cal .cx-time { display: flex; flex-direction: column; align-items: flex-start; }
.sw-cal .cx-time-t { font-size: 13px; color: var(--accent); font-weight: 600; letter-spacing: .02em; }
.sw-cal .cx-time-d { font-size: 9px; color: var(--text-dim); margin-top: 2px; letter-spacing: .04em; }
.sw-cal .cx-body { min-width: 0; }
.sw-cal .cx-title { font-size: 12px; color: var(--text); font-weight: 600; letter-spacing: .02em; }
.sw-cal .cx-loc { font-size: 10px; color: var(--text-2); margin-top: 3px; letter-spacing: .03em; }
.sw-cal .cx-note { font-size: 10px; color: var(--text-dim); margin-top: 4px; line-height: 1.4; }

/* =========================================================================
   v2 SPECIALTY WIDGETS — Robinhood / Apple-Weather / ESPN / Calendar look
   ========================================================================= */

/* shared */
.sw-card .st-tab,
.sw-card .st-chip,
.sw-card .sx-status-lbl,
.sw-card .sx-dot,
.sw-card .ww-fcbar-fill,
.sw-card .st-range-fill { font-family: var(--mono); }

/* ---- STOCKS / CRYPTO ---- */
.sw-stocks { padding: 0; }
.sw-stocks .st-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 14px; padding: 14px 16px 12px;
  background: linear-gradient(180deg, rgba(240,168,122,.07), rgba(240,168,122,0) 80%);
  border-bottom: 1px solid var(--border);
}
.sw-stocks .st-head-l { min-width: 0; flex: 1; }
.sw-stocks .st-sym {
  font-size: 26px; color: #fff; letter-spacing: .04em; font-weight: 700;
  text-shadow: 0 0 16px var(--accent-glow); line-height: 1; font-family: var(--mono);
}
.sw-stocks .st-name {
  font-size: 10.5px; color: var(--text-2); margin-top: 4px; letter-spacing: .03em;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sw-stocks .st-chips { display: flex; gap: 5px; margin-top: 8px; flex-wrap: wrap; }
.sw-stocks .st-chip {
  font-size: 8.5px; padding: 2px 7px; letter-spacing: .1em; text-transform: uppercase;
  border: 1px solid; line-height: 1.5;
}
.sw-stocks .st-chip.st-ex { color: var(--text-2); border-color: var(--border-h); background: rgba(240,168,122,.04); }
.sw-stocks .st-chip.st-ms-open   { color: var(--ok);  border-color: rgba(142,207,149,.35); background: rgba(142,207,149,.06); }
.sw-stocks .st-chip.st-ms-closed { color: var(--text-2); border-color: var(--border); background: rgba(255,255,255,.02); }
.sw-stocks .st-head-r { text-align: right; flex-shrink: 0; }
.sw-stocks .st-price {
  font-size: 30px; color: #fff; line-height: 1; letter-spacing: .01em;
  text-shadow: 0 0 18px var(--accent-glow); font-variant-numeric: tabular-nums;
  font-family: var(--mono); font-weight: 600;
}
.sw-stocks .st-chg {
  font-size: 11px; margin-top: 6px; letter-spacing: .04em; font-family: var(--mono);
  display: inline-flex; align-items: baseline; gap: 8px;
}
.sw-stocks .st-chg.ok  { color: var(--ok); }
.sw-stocks .st-chg.hot { color: var(--hot); }
.sw-stocks .st-chg.flat{ color: var(--text-2); }
.sw-stocks .st-chg-pct { font-weight: 700; opacity: .92; }

/* chart panel */
.sw-stocks .st-chart { padding: 8px 12px 0; border-bottom: 1px solid var(--border); }
.sw-stocks .st-tabs {
  display: flex; gap: 0; margin: 0 0 6px;
  border-bottom: 1px solid rgba(240,168,122,.08);
}
.sw-stocks .st-tab {
  padding: 5px 11px; font-size: 9.5px; color: var(--text-2);
  letter-spacing: .1em; text-transform: uppercase; cursor: default;
  border-bottom: 1px solid transparent; margin-bottom: -1px;
  transition: color .15s var(--ease), border-color .15s var(--ease);
}
.sw-stocks .st-tab:hover { color: var(--text); }
.sw-stocks .st-tab.st-tab-active {
  color: var(--accent); border-bottom-color: var(--accent);
  text-shadow: 0 0 8px var(--accent-glow);
}
.sw-stocks .st-chart-svg { width: 100%; height: 160px; display: block; }

/* stats grid */
.sw-stocks .st-stats {
  display: grid; grid-template-columns: 1fr 1fr; gap: 0;
  border-bottom: 1px solid var(--border);
}
.sw-stocks .st-stat {
  display: flex; flex-direction: column; gap: 3px;
  padding: 9px 14px;
  border-right: 1px solid var(--border);
  border-bottom: 1px solid rgba(240,168,122,.04);
}
.sw-stocks .st-stat:nth-child(2n)  { border-right: 0; }
.sw-stocks .st-stat:nth-last-child(-n+2) { border-bottom: 0; }
.sw-stocks .st-stat-l {
  font-size: 8.5px; color: var(--text-dim); letter-spacing: .12em; text-transform: uppercase;
}
.sw-stocks .st-stat-v {
  font-size: 12.5px; color: #fff; font-weight: 600; letter-spacing: .02em;
  font-family: var(--mono); font-variant-numeric: tabular-nums;
}
.sw-stocks .st-stat-r { display: flex; flex-direction: column; gap: 4px; }
.sw-stocks .st-range {
  position: relative; height: 6px; margin-top: 4px;
}
.sw-stocks .st-range-track {
  position: absolute; inset: 0;
  background: linear-gradient(90deg, rgba(229,146,143,.35), rgba(230,192,115,.4), rgba(142,207,149,.35));
  border-radius: 1px; opacity: .6;
}
.sw-stocks .st-range-fill {
  position: absolute; top: 0; bottom: 0; left: 0;
  background: rgba(240,168,122,.18);
}
.sw-stocks .st-range-tick {
  position: absolute; top: -3px; width: 2px; height: 12px; background: var(--accent);
  box-shadow: 0 0 6px var(--accent-glow);
  transform: translateX(-1px);
}
.sw-stocks .st-range-vals {
  display: flex; justify-content: space-between;
  font-size: 9.5px; color: var(--text-2); font-family: var(--mono);
  font-variant-numeric: tabular-nums;
}

/* watchlist */
.sw-stocks .st-watch {
  display: flex; flex-direction: column; gap: 0;
  border-bottom: 1px solid var(--border);
}
.sw-stocks .st-watch-row {
  display: grid; grid-template-columns: 60px 64px 1fr 64px 60px;
  align-items: center; gap: 10px; padding: 7px 14px;
  font-size: 10.5px; border-bottom: 1px solid rgba(240,168,122,.05);
  transition: background .15s var(--ease);
}
.sw-stocks .st-watch-row:last-child { border-bottom: 0; }
.sw-stocks .st-watch-row:hover { background: rgba(240,168,122,.04); }
.sw-stocks .st-watch-sym {
  color: var(--accent); letter-spacing: .04em; font-weight: 700; font-family: var(--mono);
  text-shadow: 0 0 6px var(--accent-glow);
}
.sw-stocks .st-watch-spk { display: flex; align-items: center; height: 18px; }
.sw-stocks .st-watch-spk svg { width: 100%; height: 18px; }
.sw-stocks .st-watch-name {
  color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: 10px;
}
.sw-stocks .st-watch-price {
  color: var(--text); text-align: right; font-family: var(--mono);
  font-variant-numeric: tabular-nums;
}
.sw-stocks .st-watch-chg {
  text-align: right; font-weight: 700; font-family: var(--mono);
  font-variant-numeric: tabular-nums;
}
.sw-stocks .st-watch-chg.ok  { color: var(--ok); }
.sw-stocks .st-watch-chg.hot { color: var(--hot); }

/* news */
.sw-stocks .st-news { padding: 8px 14px 12px; }
.sw-stocks .st-news-row {
  display: flex; gap: 9px; padding: 6px 0; align-items: flex-start;
  border-bottom: 1px solid rgba(240,168,122,.05);
}
.sw-stocks .st-news-row:last-child { border-bottom: 0; }
.sw-stocks .st-news-dot {
  width: 6px; height: 6px; border-radius: 50%; background: var(--accent);
  margin-top: 5px; flex-shrink: 0; box-shadow: 0 0 6px var(--accent-glow);
}
.sw-stocks .st-news-body { min-width: 0; flex: 1; }
.sw-stocks .st-news-title { font-size: 10.5px; color: var(--text); line-height: 1.4; letter-spacing: .02em; }
.sw-stocks .st-news-meta {
  display: flex; gap: 5px; margin-top: 3px;
  font-size: 8.5px; color: var(--text-dim); letter-spacing: .08em; text-transform: uppercase;
}
.sw-stocks .st-news-sep { opacity: .6; }

/* crypto variant — same chassis, slight tint */
.sw-stocks.sw-crypto { background: linear-gradient(180deg, rgba(142,100,120,.04), transparent 30%); }
.sw-stocks.sw-crypto .st-head { background: linear-gradient(180deg, rgba(229,146,143,.08), rgba(229,146,143,0) 80%); }

/* ---- WEATHER ---- */
.sw-weather { padding: 0; position: relative; overflow: hidden; }
.sw-weather .ww-hero {
  position: relative;
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 18px 18px 16px; border-bottom: 1px solid var(--border);
  isolation: isolate;
}
.sw-weather .ww-hero::before {
  content: ''; position: absolute; inset: 0; z-index: -1; opacity: .9;
  background: linear-gradient(135deg, rgba(240,168,122,.18), rgba(240,168,122,0) 70%);
  transition: background .6s var(--ease);
}
.sw-weather.ww-tone-day .ww-hero::before   { background: linear-gradient(135deg, rgba(240,168,122,.22), rgba(230,144,115,.04) 70%); }
.sw-weather.ww-tone-cloud .ww-hero::before { background: linear-gradient(135deg, rgba(176,166,186,.22), rgba(120,110,130,.05) 70%); }
.sw-weather.ww-tone-rain .ww-hero::before  { background: linear-gradient(135deg, rgba(110,140,180,.28), rgba(70,90,130,.08) 70%); }
.sw-weather.ww-tone-snow .ww-hero::before  { background: linear-gradient(135deg, rgba(220,225,235,.25), rgba(180,190,210,.06) 70%); }
.sw-weather.ww-tone-fog .ww-hero::before   { background: linear-gradient(135deg, rgba(160,155,170,.22), rgba(110,105,120,.06) 70%); }
.sw-weather.ww-tone-night .ww-hero::before { background: linear-gradient(135deg, rgba(80,70,120,.30), rgba(40,30,70,.10) 70%); }

.sw-weather .ww-hero-l { min-width: 0; flex: 1; }
.sw-weather .ww-loc {
  font-size: 11px; color: var(--accent); letter-spacing: .16em; text-transform: uppercase;
  text-shadow: 0 0 8px var(--accent-glow); font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sw-weather .ww-upd {
  font-size: 8.5px; color: var(--text-dim); margin-top: 2px; letter-spacing: .08em; text-transform: uppercase;
}
.sw-weather .ww-temp {
  font-size: 64px; color: #fff; line-height: .95; margin-top: 6px; font-weight: 200;
  text-shadow: 0 0 28px var(--accent-glow); letter-spacing: -.02em;
  font-variant-numeric: tabular-nums;
}
.sw-weather .ww-temp-u {
  font-size: 36px; color: var(--accent); vertical-align: top; line-height: 1; margin-left: 2px;
  font-weight: 300;
}
.sw-weather .ww-cond {
  font-size: 12px; color: var(--text); margin-top: 4px; letter-spacing: .04em; font-weight: 500;
}
.sw-weather .ww-hl {
  font-size: 11px; color: var(--text-2); margin-top: 6px; letter-spacing: .04em;
  display: flex; gap: 6px; align-items: baseline; font-family: var(--mono);
}
.sw-weather .ww-hl-sep { color: var(--text-dim); }

.sw-weather .ww-ic {
  font-size: 84px; line-height: 1; flex-shrink: 0;
  text-shadow: 0 0 22px var(--accent-glow);
  animation: wwIcBob 6s ease-in-out infinite;
}
@keyframes wwIcBob { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-3px); } }

.sw-weather .ww-hourly {
  padding: 12px 6px 6px; border-bottom: 1px solid var(--border);
  background: linear-gradient(180deg, rgba(240,168,122,.03), transparent);
}
.sw-weather .ww-hourly-svg { width: 100%; height: 92px; display: block; }

.sw-weather .ww-meta {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 0;
  border-bottom: 1px solid var(--border);
  background: rgba(34,27,42,.4);
}
.sw-weather .ww-metacell {
  display: flex; flex-direction: column; gap: 3px;
  padding: 10px 12px; border-left: 1px solid var(--border);
}
.sw-weather .ww-metacell:first-child { border-left: 0; }
.sw-weather .ww-metal {
  font-size: 8.5px; color: var(--text-dim); letter-spacing: .12em; text-transform: uppercase;
}
.sw-weather .ww-metav {
  font-size: 12.5px; color: #fff; font-weight: 600; font-family: var(--mono);
  font-variant-numeric: tabular-nums;
}

.sw-weather .ww-fc {
  display: grid; grid-auto-flow: column; grid-auto-columns: 1fr; gap: 0;
  padding: 10px 4px 12px;
}
.sw-weather .ww-fcday {
  display: flex; flex-direction: column; align-items: center; gap: 4px;
  padding: 8px 4px; border-radius: 4px;
  transition: background .2s var(--ease);
}
.sw-weather .ww-fcday:hover { background: rgba(240,168,122,.06); }
.sw-weather .ww-fcd {
  font-size: 9px; color: var(--text-2); letter-spacing: .12em; text-transform: uppercase; font-weight: 600;
}
.sw-weather .ww-fci { font-size: 20px; color: var(--accent); line-height: 1; margin: 2px 0; }
.sw-weather .ww-fch { font-size: 12px; color: #fff; font-weight: 600; font-family: var(--mono); }
.sw-weather .ww-fcl { font-size: 10.5px; color: var(--text-dim); font-family: var(--mono); }
.sw-weather .ww-fcbar {
  width: 70%; height: 4px; background: rgba(240,168,122,.08);
  position: relative; border-radius: 1px; margin: 2px 0 0; overflow: visible;
}
.sw-weather .ww-fcbar-fill {
  position: absolute; top: 0; bottom: 0;
  background: linear-gradient(90deg, #8ecf95, #f0a87a 50%, #e5928f);
  border-radius: 1px;
}
.sw-weather .ww-fcp {
  font-size: 8.5px; color: var(--text-2); letter-spacing: .04em; font-family: var(--mono);
  margin-top: 2px;
}
.sw-weather .ww-fcp:empty, .sw-weather .ww-fcday:not(:has(.ww-fcp)) .ww-fcp { display: none; }

.sw-weather .ww-sun {
  padding: 8px 14px 12px; border-top: 1px solid var(--border);
  background: linear-gradient(180deg, rgba(230,192,115,.04), transparent);
}
.sw-weather .ww-sun-svg { width: 100%; height: 58px; display: block; }

/* ---- SPORTS ---- */
.sw-sports { padding: 0; display: flex; flex-direction: column; }
.sw-sports .sx-league {
  padding: 7px 16px; font-size: 9px; color: var(--accent); letter-spacing: .16em; text-transform: uppercase;
  background: rgba(240,168,122,.06); border-bottom: 1px solid var(--border); font-weight: 600;
  text-shadow: 0 0 6px var(--accent-glow);
}
.sw-sports .sx-row {
  padding: 12px 16px; border-bottom: 1px solid var(--border);
  position: relative;
}
.sw-sports .sx-row:last-child { border-bottom: 0; }
.sw-sports .sx-status-row {
  display: flex; align-items: center; gap: 7px; margin-bottom: 9px;
  font-size: 9px; letter-spacing: .14em; text-transform: uppercase;
}
.sw-sports .sx-status-lbl {
  padding: 2px 7px; font-weight: 700; line-height: 1.4;
}
.sw-sports .sx-live   { color: #fff; background: var(--hot); box-shadow: 0 0 8px rgba(229,146,143,.4); }
.sw-sports .sx-final  { color: var(--text-2); background: rgba(255,255,255,.04); border: 1px solid var(--border); padding: 1px 6px; }
.sw-sports .sx-sched  { color: var(--text-2); }
.sw-sports .sx-time   { color: var(--text-2); font-size: 9px; margin-left: auto; font-family: var(--mono); letter-spacing: .04em; }
.sw-sports .sx-dot {
  width: 7px; height: 7px; border-radius: 50%; background: var(--hot);
  box-shadow: 0 0 8px var(--hot); animation: sxPulse 1.2s ease infinite;
  flex-shrink: 0;
}
@keyframes sxPulse { 50% { opacity: .35; transform: scale(.8); } }

.sw-sports .sx-game { display: flex; flex-direction: column; gap: 7px; }
.sw-sports .sx-team {
  display: grid; grid-template-columns: 32px 1fr auto;
  align-items: center; gap: 12px;
  padding: 4px 0;
  transition: opacity .2s var(--ease);
}
.sw-sports .sx-team.sx-win { /* winner */ }
.sw-sports .sx-team:not(.sx-win) { opacity: .7; }

.sw-sports .sx-logo {
  width: 30px; height: 30px; border-radius: 6px;
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 700; color: #fff;
  border: 1px solid; font-family: var(--mono);
  letter-spacing: .02em;
  text-shadow: 0 1px 2px rgba(0,0,0,.4);
}
.sw-sports .sx-team-info { min-width: 0; }
.sw-sports .sx-name {
  font-size: 13px; color: var(--text); font-weight: 600; letter-spacing: .02em;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  line-height: 1.2;
}
.sw-sports .sx-team.sx-win .sx-name { color: #fff; text-shadow: 0 0 8px var(--accent-glow); }
.sw-sports .sx-rec { font-size: 9px; color: var(--text-dim); margin-top: 1px; letter-spacing: .04em; font-family: var(--mono); }
.sw-sports .sx-score {
  font-size: 22px; color: var(--text-dim); letter-spacing: .04em; line-height: 1;
  font-family: var(--mono); font-variant-numeric: tabular-nums; font-weight: 600;
  min-width: 36px; text-align: right;
}
.sw-sports .sx-score.sx-score-win {
  color: #fff; text-shadow: 0 0 10px var(--accent-glow);
}
.sw-sports .sx-note {
  font-size: 9.5px; color: var(--text-dim); margin-top: 7px; line-height: 1.4;
  padding-left: 44px; font-style: italic;
}

/* ---- CALENDAR ---- */
.sw-cal { padding: 0; display: flex; flex-direction: column; }
.sw-cal .cx-date {
  padding: 12px 16px 10px; border-bottom: 1px solid var(--border);
  display: flex; align-items: baseline; gap: 10px;
  background: linear-gradient(180deg, rgba(240,168,122,.06), transparent);
}
.sw-cal .cx-date::before { content: ''; }
.sw-cal .cx-date-num {
  font-size: 22px; color: #fff; font-weight: 200; letter-spacing: -.01em;
  text-shadow: 0 0 14px var(--accent-glow);
  font-variant-numeric: tabular-nums;
}
.sw-cal .cx-allday {
  display: flex; flex-wrap: wrap; gap: 5px; padding: 8px 16px;
  border-bottom: 1px solid var(--border); background: rgba(34,27,42,.3);
}
.sw-cal .cx-allday-pill {
  font-size: 10px; color: var(--text); padding: 3px 9px;
  background: color-mix(in srgb, var(--cx-color, var(--accent)) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--cx-color, var(--accent)) 35%, transparent);
  border-left: 2px solid var(--cx-color, var(--accent));
  border-radius: 2px; letter-spacing: .02em;
}

.sw-cal .cx-timeline {
  display: grid; grid-template-columns: 48px 1fr;
  position: relative; min-height: 220px;
}
.sw-cal .cx-hours { position: relative; padding: 4px 0; }
.sw-cal .cx-hour {
  display: flex; align-items: flex-start; gap: 6px;
  height: 50px; padding: 0 6px 0 0;
  border-bottom: 1px dashed rgba(240,168,122,.06);
}
.sw-cal .cx-hour:last-child { border-bottom: 0; }
.sw-cal .cx-hour-lbl {
  font-size: 8.5px; color: var(--text-dim); letter-spacing: .04em;
  font-family: var(--mono); text-align: right; flex-shrink: 0;
  width: 30px; padding-top: 1px;
}
.sw-cal .cx-track {
  position: relative; border-left: 1px solid var(--border); padding: 4px 0;
  min-height: 100%;
}
.sw-cal .cx-ev {
  position: absolute; left: 8px; right: 10px;
  display: flex; gap: 8px; padding: 5px 8px 5px 10px;
  background: color-mix(in srgb, var(--cx-color, var(--accent)) 14%, transparent);
  border: 1px solid color-mix(in srgb, var(--cx-color, var(--accent)) 30%, transparent);
  border-left: 2px solid var(--cx-color, var(--accent));
  border-radius: 2px; overflow: hidden;
  transition: transform .15s var(--ease), box-shadow .15s var(--ease);
}
.sw-cal .cx-ev:hover {
  transform: translateX(1px);
  box-shadow: 0 4px 14px rgba(0,0,0,.4);
}
.sw-cal .cx-ev-bar { display: none; }
.sw-cal .cx-ev-body { min-width: 0; flex: 1; }
.sw-cal .cx-ev-time {
  font-size: 8.5px; color: var(--text-2); letter-spacing: .06em; text-transform: uppercase;
  font-family: var(--mono);
}
.sw-cal .cx-ev-title {
  font-size: 11px; color: #fff; font-weight: 600; letter-spacing: .02em;
  line-height: 1.25; margin-top: 1px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sw-cal .cx-ev-loc {
  font-size: 9px; color: var(--text-2); margin-top: 2px; letter-spacing: .03em;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sw-cal .cx-now {
  position: absolute; left: 0; right: 0; height: 0;
  display: flex; align-items: center; pointer-events: none;
  z-index: 2;
}
.sw-cal .cx-now-dot {
  width: 8px; height: 8px; border-radius: 50%; background: var(--hot);
  box-shadow: 0 0 8px var(--hot); margin-left: -4px; flex-shrink: 0;
  position: relative; z-index: 1;
}
.sw-cal .cx-now-line {
  flex: 1; height: 1px; background: var(--hot);
  box-shadow: 0 0 4px var(--hot);
}
"""

_JS = r"""
// Cagentic
const $ = s => document.querySelector(s);
const log = $('#log'), input = $('#input'), sendBtn = $('#send');
let state = {
  chats: [], currentId: null, settings: {}, busy: false,
  voiceOut: false, voiceName: '', renderedPanels: new Set(), closedWindows: [],
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

// Panel types that render inline in the chat and the user can act on.
const INTERACTIVE_TYPES=new Set(['actions','choices','form','checklist']);
function renderPanels(text){
  const found=extractHud(text);
  if(!found.length) return;
  // Handle clear directives first
  found.forEach(({obj})=>{ if((obj.panel||'').toLowerCase()==='clear') clearViewport(); });
  const layer=$('#windowLayer');
  found.forEach(({raw,obj})=>{
    const type=(obj.panel||'').toLowerCase();
    if(type==='clear') return;
    if(state.renderedPanels.has(raw)) return;
    // Interactive panels live inline in the conversation thread.
    if(INTERACTIVE_TYPES.has(type)){
      const el=buildInteractive(obj); if(!el) return;
      state.renderedPanels.add(raw);
      getThread().appendChild(el); scrollDown();
      return;
    }
    // Data panels render as draggable floating windows.
    const inner=buildPanelInner(obj); if(!inner) return;
    state.renderedPanels.add(raw);
    const idx=_winCascade; // capture before _nextWinPos increments
    const pos=_nextWinPos();
    const win=document.createElement('div'); win.className='hud-window';
    win.style.cssText='left:'+pos.x+'px;top:'+pos.y+'px;--i:'+idx;
    const title=obj.title||(type.charAt(0).toUpperCase()+type.slice(1));
    win.innerHTML='<div class="hud-win-head"><span class="hud-win-title">'+esc(title)+'</span>'+
      '<button class="hud-win-close" title="Close">&times;</button></div>'+
      '<div class="hud-win-body">'+inner+'</div>'+
      '<div class="hud-win-resize"></div>';
    win.querySelector('.hud-win-close').addEventListener('pointerdown',e=>{e.stopPropagation();_closeWindow(win);});
    layer.appendChild(win);
    _initWindow(win);
  });
}
// ---- INTERACTIVE WIDGETS ----------------------------------------------------
function _sendFromWidget(text){
  text=(text||'').trim();
  if(!text||state.busy) return;
  if(log.querySelector('.j-empty')) clearLog();
  send(text);
}
function _markUsed(wrap, activeEl){
  wrap.classList.add('ix-used');
  if(activeEl) activeEl.classList.add('ix-active');
  wrap.querySelectorAll('button,input').forEach(el=>{ el.disabled=true; });
}
function buildInteractive(p){
  const type=(p.panel||'').toLowerCase();
  const wrap=document.createElement('div');
  wrap.className='ix-panel ix-'+type;
  const title=p.title?'<div class="ix-title">'+esc(p.title)+'</div>':'';
  if(type==='actions'){
    const btns=(p.buttons||p.items||[]);
    wrap.innerHTML=title+'<div class="ix-actions-row">'+btns.map((b,i)=>{
      const label=typeof b==='string'?b:(b.label||b.prompt||'');
      return '<button class="ix-btn" data-i="'+i+'">'+esc(label)+'</button>';
    }).join('')+'</div>';
    wrap.querySelectorAll('.ix-btn').forEach(btn=>{ btn.onclick=()=>{
      const b=btns[+btn.dataset.i];
      const prompt=typeof b==='string'?b:(b.prompt||b.send||b.label||'');
      _markUsed(wrap,btn); _sendFromWidget(prompt);
    }; });
  } else if(type==='choices'){
    const opts=(p.options||p.items||[]); const pre=p.prompt||'';
    wrap.innerHTML=title+'<div class="ix-choices-list">'+opts.map((o,i)=>{
      const label=typeof o==='string'?o:(o.label||'');
      return '<button class="ix-choice" data-i="'+i+'"><span class="ix-choice-mark">&#9656;</span>'+esc(label)+'</button>';
    }).join('')+'</div>';
    wrap.querySelectorAll('.ix-choice').forEach(btn=>{ btn.onclick=()=>{
      const o=opts[+btn.dataset.i];
      const val=typeof o==='string'?o:(o.label||'');
      const prompt=(typeof o==='object'&&o.prompt)?o.prompt:(pre+val);
      _markUsed(wrap,btn); _sendFromWidget(prompt);
    }; });
  } else if(type==='form'){
    const fields=(p.fields||[]); const btnLabel=p.button||'Submit';
    wrap.innerHTML=title+'<div class="ix-form-fields">'+fields.map((f,i)=>{
      const name=f.name||('field'+i);
      const lab=f.label?'<label class="ix-flabel">'+esc(f.label)+'</label>':'';
      return '<div class="ix-field">'+lab+'<input class="ix-input" data-name="'+esc(name)+'" placeholder="'+esc(f.placeholder||'')+'" value="'+esc(f.value||'')+'"/></div>';
    }).join('')+'</div><button class="ix-submit">'+esc(btnLabel)+'</button>';
    const submit=()=>{
      const vals={};
      wrap.querySelectorAll('.ix-input').forEach(inp=>{ vals[inp.dataset.name]=inp.value.trim(); });
      let out=p.submit||p.prompt||'';
      if(out) out=out.replace(/\{(\w+)\}/g,(m,k)=>vals[k]!==undefined?vals[k]:m);
      else out=Object.values(vals).filter(Boolean).join(' ');
      _markUsed(wrap,wrap.querySelector('.ix-submit')); _sendFromWidget(out);
    };
    wrap.querySelector('.ix-submit').onclick=submit;
    wrap.querySelectorAll('.ix-input').forEach(inp=>inp.addEventListener('keydown',e=>{
      if(e.key==='Enter'){ e.preventDefault(); submit(); }
    }));
  } else if(type==='checklist'){
    const items=(p.items||[]);
    wrap.innerHTML=title+'<div class="ix-checklist-list">'+items.map(it=>{
      const label=typeof it==='string'?it:(it.label||'');
      const done=(typeof it==='object'&&it.done);
      return '<label class="ix-check'+(done?' checked':'')+'"><input type="checkbox" '+(done?'checked':'')+'/><span class="ix-box"></span><span class="ix-clabel">'+esc(label)+'</span></label>';
    }).join('')+'</div>';
    wrap.querySelectorAll('.ix-check').forEach(c=>{
      const cb=c.querySelector('input');
      cb.addEventListener('change',()=>c.classList.toggle('checked',cb.checked));
    });
  } else { return null; }
  return wrap;
}
function buildPanelInner(p){
  if(!p||typeof p!=='object') return null;
  const title='';
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
    case 'stocks':{
      const inner=buildStocksCard(p);
      if(!inner) return null;
      return title+inner;
    }
    case 'crypto':{
      const inner=buildCryptoCard(p);
      if(!inner) return null;
      return title+inner;
    }
    case 'weather':{
      const inner=buildWeatherCard(p);
      if(!inner) return null;
      return title+inner;
    }
    case 'sports':{
      const inner=buildSportsCard(p);
      if(!inner) return null;
      return title+inner;
    }
    case 'calendar':{
      const inner=buildCalendarCard(p);
      if(!inner) return null;
      return title+inner;
    }
    default: return null;
  }
  return title+inner;
}

// ---- SPECIALTY WIDGETS (stocks, weather, crypto, sports, calendar) --------
//
// `show_widget` is the agent-facing tool. It emits an SSE `widget` event with
// {type, title, data}; the frontend drops it into a draggable HUD window.
// We share the renderer with the inline `hud` panels (panels also use
// {panel: 'stocks', ...}) so the model can pick either channel.
function _fmtNum(n, dp){
  const x=Number(n); if(!isFinite(x)) return '—';
  if(dp===undefined){
    if(Math.abs(x)>=1000) return x.toLocaleString('en-US',{maximumFractionDigits:0});
    return x.toLocaleString('en-US',{maximumFractionDigits:2});
  }
  return x.toLocaleString('en-US',{minimumFractionDigits:dp,maximumFractionDigits:dp});
}
// ============================================================================
// SPECIALTY WIDGETS — stocks, crypto, weather, sports, calendar
// ============================================================================
//
// `show_widget` is the agent-facing tool. It emits an SSE `widget` event with
// {type, title, data}; the frontend drops it into a draggable HUD window.
// We share the renderer with the inline `hud` panels (panels also use
// {panel: 'stocks', ...}) so the model can pick either channel.
//
// The cards here are designed to look like small versions of the real apps:
//   * stocks / crypto  → Robinhood × Bloomberg-terminal feel
//   * weather          → Apple-Weather feel with condition-tinted header
//   * sports           → ESPN scoreboard
//   * calendar         → Google Calendar × Fantastical timeline

function _fmtVol(n){
  const x=Number(n); if(!isFinite(x)) return '—';
  const a=Math.abs(x);
  if(a>=1e12) return (x/1e12).toFixed(2)+'T';
  if(a>=1e9)  return (x/1e9).toFixed(2)+'B';
  if(a>=1e6)  return (x/1e6).toFixed(2)+'M';
  if(a>=1e3)  return (x/1e3).toFixed(1)+'K';
  return String(x);
}
function _fmtBig(n){ // for mkt cap, p/e, etc.
  const x=Number(n); if(!isFinite(x)) return '—';
  const a=Math.abs(x);
  if(a>=1e12) return (x/1e12).toFixed(2)+'T';
  if(a>=1e9)  return (x/1e9).toFixed(2)+'B';
  if(a>=1e6)  return (x/1e6).toFixed(1)+'M';
  return x.toLocaleString('en-US',{maximumFractionDigits:0});
}
function _fmtPct(n, dp){
  const x=Number(n); if(!isFinite(x)) return '—';
  return x.toFixed(dp===undefined?2:dp)+'%';
}
function _greetingColor(n){
  // color by % change; thresholds tuned for a peach/rose palette
  const x=Number(n);
  if(!isFinite(x)) return 'var(--text-2)';
  if(x>=1) return 'var(--ok)';
  if(x<=-1) return 'var(--hot)';
  return 'var(--text-2)';
}
function _marketState(now){
  // Heuristic US-market state from local hour (server clock, no TZ awareness).
  const d=new Date(now||Date.now());
  const day=d.getDay(); if(day===0||day===6) return {open:false,label:'CLOSED · WKND'};
  const h=d.getHours(), m=d.getMinutes();
  const t=h*60+m;
  if(t<4*60)        return {open:false,label:'CLOSED'};
  if(t<9*60+30)     return {open:false,label:'PRE-MKT'};
  if(t<16*60)       return {open:true, label:'● OPEN'};
  if(t<20*60)       return {open:false,label:'AFTER-HRS'};
  return {open:false,label:'CLOSED'};
}

// --- SVG chart primitives ---
function _uid(p){ return (p||'id')+Math.random().toString(36).slice(2,8); }

function _sparkSVG(vals, color, w, h){
  // Compact gradient-filled line chart, like the existing 'line' panel but
  // tighter and used as a hero accent for stocks/crypto.
  const vs=(vals||[]).map(Number).filter(v=>isFinite(v));
  if(vs.length<2) return '';
  const W=w||120, H=h||34, padL=2, padR=2, padT=3, padB=3;
  const min=Math.min(...vs), max=Math.max(...vs);
  const span=Math.max(max-min, 1e-9);
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const xFor=i=>padL+i*plotW/(vs.length-1);
  const yFor=v=>padT+plotH-(v-min)/span*plotH;
  const pts=vs.map((v,i)=>xFor(i)+','+yFor(v));
  const area=[xFor(0)+','+(padT+plotH), ...pts, xFor(vs.length-1)+','+(padT+plotH)].join(' ');
  const gid=_uid('spk');
  const last=vs[vs.length-1], first=vs[0];
  const up=last>=first;
  const stroke=up?color:'#e5928f';
  return '<svg viewBox="0 0 '+W+' '+H+'" class="vp-spark" preserveAspectRatio="none">'+
    '<defs><linearGradient id="'+gid+'" x1="0" y1="0" x2="0" y2="1">'+
    '<stop offset="0%" stop-color="'+stroke+'" stop-opacity=".35"/>'+
    '<stop offset="100%" stop-color="'+stroke+'" stop-opacity="0"/></linearGradient></defs>'+
    '<polygon points="'+area+'" fill="url(#'+gid+')"/>'+
    '<polyline points="'+pts.join(' ')+'" fill="none" stroke="'+stroke+'" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'+
    '<circle cx="'+xFor(vs.length-1)+'" cy="'+yFor(last)+'" r="2.4" fill="#16111c" stroke="'+stroke+'" stroke-width="1.5"/>'+
    '</svg>';
}

function _areaChartSVG(vals, opts){
  // Bigger chart: gridlines, area+line, current-price marker on the right.
  // opts: {w, h, color, showAxis, padL, padR, padT, padB, gradient}
  const vs=(vals||[]).map(Number).filter(v=>isFinite(v));
  if(vs.length<2) return '';
  const o=opts||{};
  const W=o.w||480, H=o.h||160;
  const padL=o.padL||36, padR=o.padR||52, padT=o.padT||10, padB=o.padB||18;
  const stroke=o.color||'#f0a87a';
  const min=Math.min(...vs), max=Math.max(...vs);
  const span=Math.max(max-min, 1e-9);
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const xFor=i=>padL+i*plotW/(vs.length-1);
  const yFor=v=>padT+plotH-(v-min)/span*plotH;
  const pts=vs.map((v,i)=>[xFor(i),yFor(v)]);
  const area='M '+xFor(0)+' '+(padT+plotH)+' L '+pts.map(p=>p[0]+' '+p[1]).join(' L ')+' L '+xFor(vs.length-1)+' '+(padT+plotH)+' Z';
  const line=pts.map((p,i)=>(i?'L':'M')+' '+p[0]+' '+p[1]).join(' ');
  const gid=_uid('ar');
  const last=vs[vs.length-1], first=vs[0];
  const up=last>=first;
  const lineStroke=up?stroke:'#e5928f';
  // gridlines: 4 horizontal, plus min/max labels on the y-axis
  const grid=[];
  for(let g=0; g<=4; g++){
    const y=padT+(g/4)*plotH;
    const val=max-(g/4)*span;
    grid.push('<line x1="'+padL+'" y1="'+y+'" x2="'+(W-padR)+'" y2="'+y+'" stroke="rgba(240,168,122,.08)" stroke-width="1" stroke-dasharray="'+(g===0||g===4?'0':'2 3')+'"/>');
    if(o.showAxis!==false){
      grid.push('<text x="'+(padL-6)+'" y="'+(y+3)+'" font-size="9" fill="#7d7388" text-anchor="end" font-family="inherit">'+_fmtNum(val, val<10?2:0)+'</text>');
    }
  }
  // current-price marker on the right
  const lastY=yFor(last);
  const marker=o.gradient===false ? '' : (
    '<line x1="'+xFor(vs.length-1)+'" y1="'+padT+'" x2="'+xFor(vs.length-1)+'" y2="'+(padT+plotH)+'" stroke="'+lineStroke+'" stroke-width="1" stroke-dasharray="2 2" opacity=".6"/>'+
    '<rect x="'+(W-padR+2)+'" y="'+(lastY-9)+'" width="'+(padR-6)+'" height="18" rx="2" fill="'+lineStroke+'" opacity=".95"/>'+
    '<text x="'+(W-padR/2-2)+'" y="'+(lastY+4)+'" font-size="10" fill="#16111c" text-anchor="middle" font-weight="700">'+_fmtNum(last,2)+'</text>'
  );
  return '<svg viewBox="0 0 '+W+' '+H+'" class="st-chart-svg" preserveAspectRatio="none">'+
    '<defs><linearGradient id="'+gid+'" x1="0" y1="0" x2="0" y2="1">'+
    '<stop offset="0%" stop-color="'+lineStroke+'" stop-opacity=".45"/>'+
    '<stop offset="100%" stop-color="'+lineStroke+'" stop-opacity="0"/></linearGradient></defs>'+
    grid.join('')+
    '<path d="'+area+'" fill="url(#'+gid+')"/>'+
    '<path d="'+line+'" fill="none" stroke="'+lineStroke+'" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'+
    '<circle cx="'+xFor(vs.length-1)+'" cy="'+lastY+'" r="3" fill="#16111c" stroke="'+lineStroke+'" stroke-width="1.8"/>'+
    marker+
  '</svg>';
}

function _candleChartSVG(ohlc, opts){
  // ohlc: array of {o,h,l,c}. Pure-SVG candlesticks.
  const vs=(ohlc||[]).filter(c=>c&&isFinite(c.o)&&isFinite(c.h)&&isFinite(c.l)&&isFinite(c.c));
  if(vs.length<2) return '';
  const o=opts||{};
  const W=o.w||480, H=o.h||160;
  const padL=o.padL||36, padR=o.padR||12, padT=o.padT||8, padB=o.padB||12;
  const min=Math.min(...vs.map(c=>c.l));
  const max=Math.max(...vs.map(c=>c.h));
  const span=Math.max(max-min, 1e-9);
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const yFor=v=>padT+plotH-(v-min)/span*plotH;
  const colW=plotW/vs.length;
  const bodyW=Math.max(2, colW*0.62);
  // gridlines
  const grid=[];
  for(let g=0; g<=4; g++){
    const y=padT+(g/4)*plotH;
    const val=max-(g/4)*span;
    grid.push('<line x1="'+padL+'" y1="'+y+'" x2="'+(W-padR)+'" y2="'+y+'" stroke="rgba(240,168,122,.08)" stroke-width="1" stroke-dasharray="'+(g===0||g===4?'0':'2 3')+'"/>');
    grid.push('<text x="'+(padL-6)+'" y="'+(y+3)+'" font-size="9" fill="#7d7388" text-anchor="end" font-family="inherit">'+_fmtNum(val, val<10?2:0)+'</text>');
  }
  const candles=vs.map((c,i)=>{
    const cx=padL+i*colW+colW/2;
    const up=c.c>=c.o;
    const color=up?'#8ecf95':'#e5928f';
    const yo=yFor(c.o), yc=yFor(c.c), yh=yFor(c.h), yl=yFor(c.l);
    const top=Math.min(yo,yc), bot=Math.max(yo,yc);
    return '<line x1="'+cx+'" y1="'+yh+'" x2="'+cx+'" y2="'+yl+'" stroke="'+color+'" stroke-width="1"/>'+
      '<rect x="'+(cx-bodyW/2)+'" y="'+top+'" width="'+bodyW+'" height="'+Math.max(1,bot-top)+'" fill="'+color+'"/>';
  }).join('');
  return '<svg viewBox="0 0 '+W+' '+H+'" class="st-chart-svg" preserveAspectRatio="none">'+
    grid.join('')+candles+
  '</svg>';
}

function _hourlyTempSVG(hourly, opts){
  // hourly: [{h: '1p'|'13', t: 72, icon?: 'sun'}]. Renders a smooth 24h temp
  // curve with hour labels and a current-hour highlight band.
  const hs=(hourly||[]).map(h=>({h:String(h.h||''), t:Number(h.t), icon:h.icon||''})).filter(h=>isFinite(h.t));
  if(hs.length<2) return '';
  const o=opts||{};
  const W=o.w||480, H=o.h||92;
  const padL=o.padL||10, padR=o.padR||10, padT=o.padT||16, padB=o.padB||22;
  const ts=hs.map(h=>h.t);
  const tmin=Math.min(...ts), tmax=Math.max(...ts);
  const span=Math.max(tmax-tmin, 1);
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const xFor=i=>padL+(i/(hs.length-1))*plotW;
  const yFor=v=>padT+plotH-(v-tmin)/span*plotH;
  // smooth path via Catmull-Rom → cubic Bezier
  const pts=hs.map((h,i)=>[xFor(i),yFor(h.t)]);
  let path='';
  if(pts.length>=2){
    path='M '+pts[0][0]+' '+pts[0][1];
    for(let i=0; i<pts.length-1; i++){
      const p0=pts[i-1]||pts[i], p1=pts[i], p2=pts[i+1], p3=pts[i+2]||p2;
      const t=0.18;
      const c1x=p1[0]+(p2[0]-p0[0])*t, c1y=p1[1]+(p2[1]-p0[1])*t;
      const c2x=p2[0]-(p3[0]-p1[0])*t, c2y=p2[1]-(p3[1]-p1[1])*t;
      path+=' C '+c1x+' '+c1y+', '+c2x+' '+c2y+', '+p2[0]+' '+p2[1];
    }
  }
  const area=path+' L '+pts[pts.length-1][0]+' '+(padT+plotH)+' L '+pts[0][0]+' '+(padT+plotH)+' Z';
  const gid=_uid('hr');
  // current-hour band (first item)
  const currentIdx=0;
  const bx0=xFor(currentIdx)-plotW/(hs.length-1)/2, bx1=bx0+plotW/(hs.length-1);
  // hour labels: show every 6th
  const labels=hs.map((h,i)=>{
    if(i!==0 && i!==hs.length-1 && i%6!==0) return '';
    return '<text x="'+xFor(i)+'" y="'+(H-6)+'" font-size="9" fill="#b0a6ba" text-anchor="middle" font-family="inherit">'+esc(h.h)+'</text>';
  }).join('');
  // spot dots every 6
  const dots=hs.map((h,i)=> i%6===0 || i===hs.length-1
    ? '<circle cx="'+xFor(i)+'" cy="'+yFor(h.t)+'" r="2.4" fill="#16111c" stroke="#f0a87a" stroke-width="1.4"/>'
    : '').join('');
  return '<svg viewBox="0 0 '+W+' '+H+'" class="ww-hourly-svg" preserveAspectRatio="none">'+
    '<defs><linearGradient id="'+gid+'" x1="0" y1="0" x2="0" y2="1">'+
    '<stop offset="0%" stop-color="#f0a87a" stop-opacity=".35"/>'+
    '<stop offset="100%" stop-color="#f0a87a" stop-opacity="0"/></linearGradient></defs>'+
    '<rect x="'+bx0+'" y="'+padT+'" width="'+(bx1-bx0)+'" height="'+plotH+'" fill="rgba(240,168,122,.10)"/>'+
    '<path d="'+area+'" fill="url(#'+gid+')"/>'+
    '<path d="'+path+'" fill="none" stroke="#f0a87a" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'+
    dots+labels+
  '</svg>';
}

function _sunArcSVG(sunrise, sunset, now){
  // sunrise/sunset: "HH:MM" strings. now: current "HH:MM" or null.
  if(!sunrise||!sunset) return '';
  const toMin=s=>{const p=s.split(':').map(Number); return p[0]*60+(p[1]||0);};
  const sr=toMin(sunrise), ss=toMin(sunset);
  const mid=(sr+ss)/2;
  const total=ss-sr;
  const W=240, H=58, padL=18, padR=18, baseline=H-10;
  const ax0=padL, ax1=W-padR, ay=baseline;
  const xFor=m=>ax0+((m-sr)/total)*(ax1-ax0);
  const yFor=m=>{ const t=(m-sr)/total; return ay - Math.sin(t*Math.PI)*40; };
  // arc + ground line
  const sx0=xFor(sr), sy0=yFor(sr);
  const sx1=xFor(ss), sy1=yFor(ss);
  const mx=xFor(mid), my=yFor(mid);
  const arc='M '+sx0+' '+sy0+' Q '+mx+' '+(my-12)+', '+sx1+' '+sy1;
  const gid=_uid('sn');
  // sun position from `now` (defaults to actual current time of day)
  const nowMin = now ? toMin(now) : (()=>{const d=new Date(); return d.getHours()*60+d.getMinutes();})();
  const sunFrac = Math.max(0, Math.min(1, (nowMin-sr)/total));
  const sunX = ax0 + sunFrac*(ax1-ax0);
  const sunY = ay - Math.sin(sunFrac*Math.PI)*40;
  const sunOnArc = nowMin>=sr && nowMin<=ss;
  const sun = sunOnArc
    ? '<circle cx="'+sunX+'" cy="'+sunY+'" r="4.2" fill="#f0a87a" stroke="#16111c" stroke-width="1.4"/>'
    : '<circle cx="'+sunOnArc?sunX:(nowMin<sr?sx0:sx1)+'" cy="'+(sunOnArc?sunY:ay-3)+'" r="3" fill="#5a4e69"/>';
  return '<svg viewBox="0 0 '+W+' '+H+'" class="ww-sun-svg" preserveAspectRatio="xMidYMid meet">'+
    '<defs><linearGradient id="'+gid+'" x1="0" y1="0" x2="1" y2="0">'+
    '<stop offset="0%" stop-color="#e5928f" stop-opacity=".55"/>'+
    '<stop offset="50%" stop-color="#f0a87a" stop-opacity=".85"/>'+
    '<stop offset="100%" stop-color="#e6c073" stop-opacity=".55"/></linearGradient></defs>'+
    '<line x1="'+ax0+'" y1="'+ay+'" x2="'+ax1+'" y2="'+ay+'" stroke="rgba(240,168,122,.18)" stroke-dasharray="2 3"/>'+
    '<path d="'+arc+'" fill="none" stroke="url(#'+gid+')" stroke-width="1.6" stroke-linecap="round"/>'+
    sun+
    '<text x="'+sx0+'" y="'+(H-1)+'" font-size="9" fill="#b0a6ba" text-anchor="middle" font-family="inherit">'+esc(sunrise)+'</text>'+
    '<text x="'+sx1+'" y="'+(H-1)+'" font-size="9" fill="#b0a6ba" text-anchor="middle" font-family="inherit">'+esc(sunset)+'</text>'+
  '</svg>';
}

// ============================================================================
// STOCKS / CRYPTO
// ============================================================================
//
// A Robinhood × Bloomberg-terminal look. The card has:
//   1. Hero row:  market status pill · exchange chip · symbol · name ·
//                 price · colored change row
//   2. Timeframe tab strip:  1D | 1W | 1M | 3M | 1Y | All   (CSS-only)
//   3. Main chart:           area+line with gridlines + current-price marker,
//                             or candlesticks if `ohlc` is provided
//   4. Stats grid:           Open, High, Low, Volume, Mkt Cap, 52W Range
//   5. Watchlist:            symbol · mini-spark · price · change%
//   6. News strip (optional)

function _stTimeframeTabs(active){
  const tabs=['1D','1W','1M','3M','1Y','All'];
  return '<div class="st-tabs">'+
    tabs.map(t=>'<div class="st-tab'+(t===active?' st-tab-active':'')+'">'+t+'</div>').join('')+
  '</div>';
}

function _stStatsGrid(p, isCrypto){
  // Open/High/Low/Volume + Mkt Cap + 52W Range with mini position bar
  const cells=[];
  if(p.open!==undefined)  cells.push(['Open',  _fmtNum(p.open,2)]);
  if(p.high!==undefined)  cells.push(['High',  _fmtNum(p.high,2)]);
  if(p.low!==undefined)   cells.push(['Low',   _fmtNum(p.low,2)]);
  if(p.volume!==undefined)cells.push([isCrypto?'24h Vol':'Volume', _fmtVol(p.volume)]);
  if(p.market_cap!==undefined) cells.push(['Mkt Cap', _fmtBig(p.market_cap)]);
  if(isCrypto && p.dominance!==undefined) cells.push(['Dominance', _fmtPct(p.dominance,1)]);
  if(!isCrypto && p.pe!==undefined) cells.push(['P/E', _fmtNum(p.pe,2)]);
  // 52W Range with mini bar
  if(p.low_52w!==undefined && p.high_52w!==undefined){
    const lo=Number(p.low_52w), hi=Number(p.high_52w), cur=Number(p.price);
    const span=Math.max(hi-lo, 1e-9);
    const pct=isFinite(cur)?Math.max(0,Math.min(100, ((cur-lo)/span)*100)):0;
    const rangeHtml='<div class="st-stat"><div class="st-stat-l">52W Range</div>'+
      '<div class="st-stat-r"><div class="st-range">'+
      '<div class="st-range-track"></div>'+
      '<div class="st-range-fill" style="width:'+pct.toFixed(1)+'%"></div>'+
      '<div class="st-range-tick" style="left:'+pct.toFixed(1)+'%"></div>'+
      '</div>'+
      '<div class="st-range-vals"><span>'+_fmtNum(lo,2)+'</span><span>'+_fmtNum(hi,2)+'</span></div></div></div>';
    cells.push(['__raw__', rangeHtml]);
  }
  const grid=cells.map(c=>{
    if(c[0]==='__raw__') return c[1];
    return '<div class="st-stat"><div class="st-stat-l">'+esc(c[0])+'</div><div class="st-stat-v">'+esc(c[1])+'</div></div>';
  }).join('');
  return '<div class="st-stats">'+grid+'</div>';
}

function _stWatchlist(items){
  if(!items||!items.length) return '';
  return '<div class="st-watch">'+
    items.map(it=>{
      const u=Number(it.change)>=0;
      const spk=(it.chart&&it.chart.values)?_sparkSVG(it.chart.values, u?'#8ecf95':'#e5928f', 56, 18):'';
      return '<div class="st-watch-row">'+
        '<div class="st-watch-sym">'+esc(it.symbol||'')+'</div>'+
        '<div class="st-watch-spk">'+spk+'</div>'+
        '<div class="st-watch-name">'+esc(it.name||'')+'</div>'+
        '<div class="st-watch-price">'+_fmtNum(it.price,2)+'</div>'+
        '<div class="st-watch-chg '+(u?'ok':'hot')+'">'+(u?'+':'')+_fmtNum(it.change_pct,2)+'%</div>'+
      '</div>';
    }).join('')+
  '</div>';
}

function _stNews(news){
  if(!news||!news.length) return '';
  return '<div class="st-news">'+
    news.map(n=>'<div class="st-news-row">'+
      '<div class="st-news-dot"></div>'+
      '<div class="st-news-body">'+
        '<div class="st-news-title">'+esc(n.title||'')+'</div>'+
        '<div class="st-news-meta"><span>'+esc(n.source||'')+'</span>'+(n.time?'<span class="st-news-sep">·</span><span>'+esc(n.time)+'</span>':'')+'</div>'+
      '</div>'+
    '</div>').join('')+
  '</div>';
}

function buildStocksCard(p){
  // p: {title, symbol, name, price, change, change_pct, exchange, market_state,
  //     open, high, low, volume, market_cap, pe, low_52w, high_52w,
  //     chart: {labels, values, timeframe}, ohlc: [...], items: [...], news: [...]}
  const items=(p.items||[]);
  const hasTopLevel=(p.price!==undefined||p.change!==undefined||p.change_pct!==undefined||p.symbol);
  if(!items.length && !p.chart && !p.ohlc && !hasTopLevel) return null;

  // hero
  const sym=(p.symbol||(items[0]&&items[0].symbol)||'').toUpperCase();
  const name=p.name||(items[0]&&items[0].name)||'';
  const price=(p.price!==undefined)?p.price:(items[0]&&items[0].price);
  const chg=(p.change!==undefined)?p.change:(items[0]&&items[0].change);
  const chgPct=(p.change_pct!==undefined)?p.change_pct:(items[0]&&items[0].change_pct);
  const up=Number(chg)>=0;
  const arrow=up?'\u25B2':'\u25BC';
  const chgColorClass = up?'ok':(Number(chg)<0?'hot':'flat');

  const ms = p.market_state || _marketState();
  const msClass = ms.open ? 'st-ms-open' : 'st-ms-closed';
  const ex = p.exchange || (sym.endsWith('-USD')?'CRYPTO':(sym.length<=4?'NYSE':'NASDAQ'));

  // chart
  const tf = (p.chart&&p.chart.timeframe) || '1D';
  const chart = p.ohlc
    ? _candleChartSVG(p.ohlc, {w:480,h:160,padL:36,padR:12})
    : (p.chart&&p.chart.values ? _areaChartSVG(p.chart.values, {w:480,h:160,color:'#f0a87a',padL:36,padR:52}) : '');

  // watchlist — strip the first item if it duplicates the hero
  const rest = items.length>1 ? items.slice(1) : [];

  return '<div class="sw-card sw-stocks">'+
    '<div class="st-head">'+
      '<div class="st-head-l">'+
        '<div class="st-sym">'+esc(sym)+'</div>'+
        '<div class="st-name">'+esc(name)+'</div>'+
        '<div class="st-chips">'+
          '<span class="st-chip st-ex">'+esc(ex)+'</span>'+
          '<span class="st-chip '+msClass+'">'+esc(ms.label)+'</span>'+
        '</div>'+
      '</div>'+
      '<div class="st-head-r">'+
        '<div class="st-price">'+_fmtNum(price,2)+'</div>'+
        '<div class="st-chg '+chgColorClass+'">'+arrow+' '+(up?'+':'')+_fmtNum(chg,2)+' <span class="st-chg-pct">'+(up?'+':'')+_fmtNum(chgPct,2)+'%</span></div>'+
      '</div>'+
    '</div>'+
    (chart ? ('<div class="st-chart">'+_stTimeframeTabs(tf)+chart+'</div>') : '')+
    _stStatsGrid(p, false)+
    _stWatchlist(rest)+
    _stNews(p.news)+
  '</div>';
}

function buildCryptoCard(p){
  // Mirror stocks with crypto labels. Reuses everything via the same code path
  // by flipping isCrypto=true for the stats grid and the exchange chip.
  const items=(p.items||[]);
  const hasTopLevel=(p.price!==undefined||p.change!==undefined||p.change_pct!==undefined||p.symbol);
  if(!items.length && !p.chart && !p.ohlc && !hasTopLevel) return null;

  const sym=(p.symbol||(items[0]&&items[0].symbol)||'').toUpperCase();
  const name=p.name||(items[0]&&items[0].name)||'';
  const price=(p.price!==undefined)?p.price:(items[0]&&items[0].price);
  const chg=(p.change!==undefined)?p.change:(items[0]&&items[0].change);
  const chgPct=(p.change_pct!==undefined)?p.change_pct:(items[0]&&items[0].change_pct);
  const up=Number(chg)>=0;
  const arrow=up?'\u25B2':'\u25BC';
  const chgColorClass = up?'ok':(Number(chg)<0?'hot':'flat');

  const tf = (p.chart&&p.chart.timeframe) || '24H';
  const chart = p.ohlc
    ? _candleChartSVG(p.ohlc, {w:480,h:160,padL:36,padR:12})
    : (p.chart&&p.chart.values ? _areaChartSVG(p.chart.values, {w:480,h:160,color:'#f0a87a',padL:36,padR:52}) : '');
  const rest = items.length>1 ? items.slice(1) : [];
  const ex = p.exchange || (sym.endsWith('-USD')?'GLOBAL':(sym.length<=4?'CEX':'DEX'));

  return '<div class="sw-card sw-stocks sw-crypto">'+
    '<div class="st-head">'+
      '<div class="st-head-l">'+
        '<div class="st-sym">'+esc(sym)+'</div>'+
        '<div class="st-name">'+esc(name)+'</div>'+
        '<div class="st-chips">'+
          '<span class="st-chip st-ex">'+esc(ex)+'</span>'+
          '<span class="st-chip st-ms-open">● 24H</span>'+
        '</div>'+
      '</div>'+
      '<div class="st-head-r">'+
        '<div class="st-price">'+_fmtNum(price,2)+'</div>'+
        '<div class="st-chg '+chgColorClass+'">'+arrow+' '+(up?'+':'')+_fmtNum(chg,2)+' <span class="st-chg-pct">'+(up?'+':'')+_fmtNum(chgPct,2)+'%</span></div>'+
      '</div>'+
    '</div>'+
    (chart ? ('<div class="st-chart">'+_stTimeframeTabs(tf)+chart+'</div>') : '')+
    _stStatsGrid(p, true)+
    _stWatchlist(rest)+
  '</div>';
}

// ============================================================================
// WEATHER
// ============================================================================
//
// Apple-Weather feel: condition-tinted header gradient, big temp, 24h hourly
// curve, 5-day forecast with hi/lo bars + precip, sunrise/sunset arc, 4-up
// wind/humidity/UV/pressure grid.

function _wxIcon(s){
  const m={
    'sunny':'\u2600','clear':'\u2600',
    'partly':'\u26C5','partly cloudy':'\u26C5','cloudy':'\u2601','overcast':'\u2601',
    'rain':'\u2602','rainy':'\u2602','showers':'\u2602','drizzle':'\u2602',
    'thunder':'\u26C8','thunderstorm':'\u26C8',
    'snow':'\u2744','snowy':'\u2744','sleet':'\u2745',
    'fog':'\u2601','mist':'\u2601','haze':'\u2601',
    'wind':'\u2638','windy':'\u2638',
    'night':'\u263E','clear night':'\u263E',
    'hot':'\u2600','cold':'\u2744'
  };
  return m[(s||'').toLowerCase()]||'\u2601';
}
function _wxToneClass(s){
  const k=(s||'').toLowerCase();
  if(k.includes('thunder')||k.includes('rain')||k.includes('shower')||k.includes('drizzle')) return 'ww-tone-rain';
  if(k.includes('snow')||k.includes('sleet')) return 'ww-tone-snow';
  if(k.includes('fog')||k.includes('mist')||k.includes('haze')||k.includes('overcast')) return 'ww-tone-fog';
  if(k.includes('night')||k.includes('clear')&&k.includes('night')) return 'ww-tone-night';
  if(k.includes('cloud')||k.includes('partly')) return 'ww-tone-cloud';
  return 'ww-tone-day';
}

function buildWeatherCard(p){
  // p: {title, location, updated, current:{temp, condition, icon, feels, humidity, wind, uv, pressure, visibility},
  //     hourly: [{h, t, icon}], forecast: [{day, high, low, icon, condition, precip}],
  //     sunrise, sunset}
  const cur=p.current||{};
  const fc=p.forecast||[];
  const hourly=p.hourly||[];
  const tone=_wxToneClass(cur.icon||cur.condition||'sunny');
  const tempStr = cur.temp!==undefined ? Math.round(Number(cur.temp)) : '—';
  const iconStr = _wxIcon(cur.icon||cur.condition||'sunny');
  const hl=fc.length?{hi:Math.max(...fc.map(d=>Number(d.high||d.hi||0))), lo:Math.min(...fc.map(d=>Number(d.low||d.lo||0)))}:{hi:0,lo:0};
  // meta grid (humidity, wind, uv, pressure, visibility, feels)
  const meta=[];
  if(cur.humidity!==undefined)    meta.push(['Humidity',   cur.humidity+'%']);
  if(cur.wind!==undefined)        meta.push(['Wind',       typeof cur.wind==='number'?cur.wind+' mph':String(cur.wind)]);
  if(cur.feels!==undefined)       meta.push(['Feels Like', Math.round(Number(cur.feels))+'°']);
  if(cur.uv!==undefined)          meta.push(['UV Index',   String(cur.uv)]);
  if(cur.pressure!==undefined)    meta.push(['Pressure',   String(cur.pressure)]);
  if(cur.visibility!==undefined)  meta.push(['Visibility', typeof cur.visibility==='number'?cur.visibility+' mi':String(cur.visibility)]);
  // forecast with hi/lo bars
  const fcHtml = fc.length ? (
    '<div class="ww-fc">'+
      fc.map(d=>{
        const hi=Number(d.high??d.hi), lo=Number(d.low??d.lo);
        // bar position within the day's overall hi/lo range
        const dayMin=Math.min(lo, hl.lo);
        const dayMax=Math.max(hi, hl.hi);
        const span=Math.max(dayMax-dayMin, 1e-9);
        const a=((lo-dayMin)/span)*100, b=((hi-dayMin)/span)*100;
        return '<div class="ww-fcday">'+
          '<div class="ww-fcd">'+esc((d.day||'').slice(0,3))+'</div>'+
          '<div class="ww-fci">'+_wxIcon(d.icon||d.condition)+'</div>'+
          '<div class="ww-fch">'+Math.round(hi)+'°</div>'+
          '<div class="ww-fcbar"><div class="ww-fcbar-fill" style="left:'+a.toFixed(1)+'%;width:'+(b-a).toFixed(1)+'%"></div></div>'+
          '<div class="ww-fcl">'+Math.round(lo)+'°</div>'+
          (d.precip!==undefined?'<div class="ww-fcp">'+Math.round(Number(d.precip))+'%</div>':'')+
        '</div>';
      }).join('')+
    '</div>'
  ) : '';
  const hourlySvg = hourly.length ? _hourlyTempSVG(hourly) : '';
  const sunSvg = _sunArcSVG(p.sunrise, p.sunset, p.now);

  return '<div class="sw-card sw-weather '+tone+'">'+
    '<div class="ww-hero">'+
      '<div class="ww-hero-l">'+
        '<div class="ww-loc">'+esc(p.location||p.title||'Current Location')+'</div>'+
        (p.updated?'<div class="ww-upd">Updated '+esc(p.updated)+'</div>':'')+
        '<div class="ww-temp">'+tempStr+'<span class="ww-temp-u">°</span></div>'+
        '<div class="ww-cond">'+esc(cur.condition||'')+'</div>'+
        (fc.length?'<div class="ww-hl"><span>H '+Math.round(hl.hi)+'°</span><span class="ww-hl-sep">·</span><span>L '+Math.round(hl.lo)+'°</span></div>':'')+
      '</div>'+
      '<div class="ww-ic">'+iconStr+'</div>'+
    '</div>'+
    (hourlySvg?'<div class="ww-hourly">'+hourlySvg+'</div>':'')+
    (meta.length?'<div class="ww-meta">'+meta.map(([l,v])=>
      '<div class="ww-metacell"><div class="ww-metal">'+esc(l)+'</div><div class="ww-metav">'+esc(String(v))+'</div></div>'
    ).join('')+'</div>':'')+
    fcHtml+
    (sunSvg?'<div class="ww-sun">'+sunSvg+'</div>':'')+
  '</div>';
}

// ============================================================================
// SPORTS
// ============================================================================
//
// ESPN scoreboard: league chip, status pill with pulsing dot, two team rows
// with logo-block + record + big score, winner accent-tinted.

function _sxInitials(name){
  return (name||'').split(/\s+/).filter(Boolean).slice(0,2).map(w=>w[0].toUpperCase()).join('')||'?';
}
function _sxTint(name){
  // Deterministic hue from team name, 0-360
  let h=0; for(let i=0; i<name.length; i++) h=(h*31+name.charCodeAt(i))%360;
  return 'hsl('+h+',55%,52%)';
}

function buildSportsCard(p){
  // p: {title, league, games: [{home, away, home_score, away_score, status, time, note, home_record, away_record, home_color, away_color}]}
  const games=p.games||p.items||[];
  if(!games.length) return null;
  const league=p.league||'';
  return '<div class="sw-card sw-sports">'+
    (league?'<div class="sx-league">'+esc(league)+'</div>':'')+
    games.map(g=>{
      const hs=Number(g.home_score), as=Number(g.away_score);
      const hasScore=isFinite(hs)&&isFinite(as);
      const homeWin=hasScore&&hs>as, awayWin=hasScore&&as>hs;
      const status=(g.status||'').toLowerCase();
      const live=status==='live'||status==='in progress'||status==='in_progress';
      const final=status==='final'||status==='finished';
      const scheduled=status==='scheduled'||status==='pre'||(!hasScore&&!live&&!final);
      const homeCol=g.home_color||_sxTint(g.home||'H');
      const awayCol=g.away_color||_sxTint(g.away||'A');
      return '<div class="sx-row">'+
        '<div class="sx-status-row">'+
          (live?'<span class="sx-dot"></span><span class="sx-status-lbl sx-live">LIVE</span>':'')+
          (final?'<span class="sx-status-lbl sx-final">FINAL</span>':'')+
          (scheduled?'<span class="sx-status-lbl sx-sched">'+esc(g.status||g.time||'SCHEDULED')+'</span>':'')+
          (g.time&&hasScore?'<span class="sx-time">'+esc(g.time)+'</span>':'')+
        '</div>'+
        '<div class="sx-game">'+
          '<div class="sx-team '+(homeWin?' sx-win':'')+'">'+
            '<div class="sx-logo" style="background:'+esc(homeCol)+'22;border-color:'+esc(homeCol)+'">'+esc(_sxInitials(g.home))+'</div>'+
            '<div class="sx-team-info">'+
              '<div class="sx-name">'+esc(g.home||'')+'</div>'+
              (g.home_record?'<div class="sx-rec">'+esc(g.home_record)+'</div>':'')+
            '</div>'+
            (hasScore?'<div class="sx-score '+(homeWin?' sx-score-win':'')+'">'+hs+'</div>':'')+
          '</div>'+
          '<div class="sx-team '+(awayWin?' sx-win':'')+'">'+
            '<div class="sx-logo" style="background:'+esc(awayCol)+'22;border-color:'+esc(awayCol)+'">'+esc(_sxInitials(g.away))+'</div>'+
            '<div class="sx-team-info">'+
              '<div class="sx-name">'+esc(g.away||'')+'</div>'+
              (g.away_record?'<div class="sx-rec">'+esc(g.away_record)+'</div>':'')+
            '</div>'+
            (hasScore?'<div class="sx-score '+(awayWin?' sx-score-win':'')+'">'+as+'</div>':'')+
          '</div>'+
        '</div>'+
        (g.note?'<div class="sx-note">'+esc(g.note)+'</div>':'')+
      '</div>';
    }).join('')+
  '</div>';
}

// ============================================================================
// CALENDAR
// ============================================================================
//
// Google-Calendar × Fantastical timeline: date header, all-day events as
// pill rows, hourly gutter on the left, color-coded events, current-time
// red horizontal line.

function _cxToMin(s){
  if(!s) return null;
  const m=String(s).match(/^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/i);
  if(!m) return null;
  let h=parseInt(m[1],10), mm=parseInt(m[2]||'0',10);
  const ap=(m[3]||'').toLowerCase();
  if(ap==='pm'&&h<12) h+=12;
  if(ap==='am'&&h===12) h=0;
  return h*60+mm;
}
function _cxFormatHour(h){
  const ap=h<12?'a':'p';
  const v=h%12===0?12:h%12;
  return v+ap;
}

function buildCalendarCard(p){
  // p: {title, date, day_name, now: 'HH:MM', events: [{start:'9:00', end:'10:30', title, location, note, color, all_day}]}
  const events=p.events||p.items||[];
  if(!events.length) return null;
  const allDay = events.filter(e=>e.all_day);
  const timed  = events.filter(e=>!e.all_day);
  // collect hour gutter bounds
  const startMins = timed.map(e=>_cxToMin(e.start)).filter(v=>v!==null);
  const endMins   = timed.map(e=>_cxToMin(e.end||e.start)).filter(v=>v!==null);
  let hourStart = startMins.length ? Math.floor(Math.min(...startMins)/60) : 8;
  let hourEnd   = endMins.length   ? Math.ceil(Math.max(...endMins)/60)   : 18;
  hourStart = Math.max(0, Math.min(23, hourStart-1));
  hourEnd   = Math.max(hourStart+4, Math.min(24, hourEnd+1));
  const spanMins = (hourEnd-hourStart)*60;
  const nowMin = _cxToMin(p.now);
  const nowPct = (nowMin!==null) ? ((nowMin-hourStart*60)/spanMins)*100 : null;

  // all-day pills
  const allDayHtml = allDay.length
    ? '<div class="cx-allday">'+allDay.map(e=>{
        const color=e.color||'#f0a87a';
        return '<div class="cx-allday-pill" style="--cx-color:'+esc(color)+'">'+esc(e.title||'')+'</div>';
      }).join('')+'</div>'
    : '';
  // timeline
  const hourLabels=[];
  for(let h=hourStart; h<=hourEnd; h++){
    hourLabels.push('<div class="cx-hour"><div class="cx-hour-lbl">'+_cxFormatHour(h)+'</div><div class="cx-hour-line"></div></div>');
  }
  const eventsHtml = timed.map(e=>{
    const s=_cxToMin(e.start);
    let en=_cxToMin(e.end||e.start);
    if(en===null||en<=s) en=s+30;
    const top=((s-hourStart*60)/spanMins)*100;
    const height=Math.max(4, ((en-s)/spanMins)*100);
    const color=e.color||'#f0a87a';
    return '<div class="cx-ev" style="top:'+top.toFixed(2)+'%;height:'+height.toFixed(2)+'%;--cx-color:'+esc(color)+'">'+
      '<div class="cx-ev-bar"></div>'+
      '<div class="cx-ev-body">'+
        '<div class="cx-ev-time">'+esc((e.start||'')+(e.end?' – '+e.end:''))+'</div>'+
        '<div class="cx-ev-title">'+esc(e.title||'')+'</div>'+
        (e.location?'<div class="cx-ev-loc">'+esc(e.location)+'</div>':'')+
      '</div>'+
    '</div>';
  }).join('');

  return '<div class="sw-card sw-cal">'+
    (p.date||p.day_name?'<div class="cx-date">'+esc(p.day_name||'')+(p.date?'<span class="cx-date-num">'+esc(p.date)+'</span>':'')+'</div>':'')+
    allDayHtml+
    '<div class="cx-timeline">'+
      '<div class="cx-hours">'+hourLabels.join('')+'</div>'+
      '<div class="cx-track">'+eventsHtml+(nowPct!==null?'<div class="cx-now" style="top:'+nowPct.toFixed(2)+'%"><div class="cx-now-dot"></div><div class="cx-now-line"></div></div>':'')+'</div>'+
    '</div>'+
  '</div>';
}

// Orchestrator: turn a `show_widget` SSE event into a draggable HUD window.
function renderWidget(d){
  const type=(d.type||'').toLowerCase();
  const title=d.title||((type||'').charAt(0).toUpperCase()+(type||'').slice(1));
  const data=d.data||{};
  // Build a synthetic panel payload so buildPanelInner does the work.
  const p={panel:type, title:title, ...data};
  const inner=buildPanelInner(p);
  const layer=$('#windowLayer');
  if(!inner){ return; }
  const idx=_winCascade;
  const pos=_nextWinPos();
  const win=document.createElement('div'); win.className='hud-window sw-window sw-'+type;
  win.style.cssText='left:'+pos.x+'px;top:'+pos.y+'px;--i:'+idx;
  win.innerHTML='<div class="hud-win-head"><span class="hud-win-title">'+esc(title)+'</span>'+
    '<button class="hud-win-close" title="Close">&times;</button></div>'+
    '<div class="hud-win-body sw-body">'+inner+'</div>'+
    '<div class="hud-win-resize"></div>';
  win.querySelector('.hud-win-close').addEventListener('pointerdown',e=>{e.stopPropagation();_closeWindow(win);});
  layer.appendChild(win);
  _initWindow(win);
}

// ---- FLOATING HUD WINDOWS ----------------------------------------------------
let _winCascade = 0;
function _nextWinPos(){
  const layer=$('#windowLayer');
  const lw=layer.clientWidth, lh=layer.clientHeight;
  const cols=Math.max(1, Math.floor((lw-40)/280));
  const rows=Math.max(1, Math.floor((lh-40)/240));
  const col=_winCascade%cols, row=Math.floor(_winCascade/cols)%rows;
  const cellW=(lw-40)/cols, cellH=(lh-40)/rows;
  const x=20+col*cellW+20, y=20+row*cellH+20;
  _winCascade++;
  return {x: Math.min(x, lw-260), y: Math.min(y, lh-200)};
}
// One shared pointer manager drives every window's drag + resize, so windows
// don't each leak a set of document-level listeners. Pointer events unify
// mouse and touch in a single path.
let _drag=null; // {win, mode:'move'|'resize', sx, sy, ox, oy, ow, oh}
function _initWindow(win){
  const head=win.querySelector('.hud-win-head');
  if(head) head.addEventListener('pointerdown',e=>{
    if(e.target.classList.contains('hud-win-close')) return;
    _drag={win, mode:'move', sx:e.clientX, sy:e.clientY,
      ox:parseInt(win.style.left)||0, oy:parseInt(win.style.top)||0};
    win.classList.add('dragging'); e.preventDefault();
  });
  const handle=win.querySelector('.hud-win-resize');
  if(handle) handle.addEventListener('pointerdown',e=>{
    _drag={win, mode:'resize', sx:e.clientX, sy:e.clientY,
      ow:win.offsetWidth, oh:win.offsetHeight};
    win.classList.add('resizing'); e.preventDefault(); e.stopPropagation();
  });
}
document.addEventListener('pointermove',e=>{
  if(!_drag) return;
  const dx=e.clientX-_drag.sx, dy=e.clientY-_drag.sy;
  if(_drag.mode==='move'){
    _drag.win.style.left=(_drag.ox+dx)+'px';
    _drag.win.style.top=(_drag.oy+dy)+'px';
  } else {
    _drag.win.style.width=Math.max(220,_drag.ow+dx)+'px';
    _drag.win.style.height=Math.max(100,_drag.oh+dy)+'px';
  }
});
document.addEventListener('pointerup',()=>{
  if(!_drag) return;
  _drag.win.classList.remove('dragging','resizing');
  _drag=null;
});
function _closeWindow(win){
  win.style.display='none';
  state.closedWindows.push(win);
  _updateRestorePill();
}
function _restoreWindow(win){
  win.style.display='';
  state.closedWindows=state.closedWindows.filter(w=>w!==win);
  // Re-trigger entrance animation
  win.style.animation='none'; win.offsetHeight; win.style.animation='';
  _updateRestorePill();
}
function _updateRestorePill(){
  const pill=$('#restorePill');
  const dd=$('#restoreDropdown');
  if(!pill) return;
  if(state.closedWindows.length===0){
    pill.style.display='none';
    pill.classList.remove('open');
    return;
  }
  pill.style.display='';
  pill.querySelector('.restore-pill-count').textContent=state.closedWindows.length;
  dd.innerHTML=state.closedWindows.map((w,i)=>{
    const t=w.querySelector('.hud-win-title');
    const label=t?t.textContent:('Panel '+(i+1));
    return '<div class="restore-item" data-ri="'+i+'">↩ '+esc(label)+'</div>';
  }).join('');
  dd.querySelectorAll('.restore-item').forEach(el=>{
    el.onclick=()=>{ const idx=+el.dataset.ri; _restoreWindow(state.closedWindows[idx]); };
  });
}
$('#restorePill').addEventListener('click',e=>{
  if(e.target.closest('.restore-item')) return;
  e.currentTarget.classList.toggle('open');
});
// Close dropdown when clicking outside
document.addEventListener('pointerdown',e=>{
  const pill=$('#restorePill');
  if(pill && !pill.contains(e.target)) pill.classList.remove('open');
});
function clearViewport(){
  state.renderedPanels.clear();
  state.closedWindows=[];
  _winCascade=0;
  const layer=$('#windowLayer');
  if(layer) layer.innerHTML='';
  _updateRestorePill();
}

// bring window to front on interaction
$('#windowLayer').addEventListener('pointerdown',e=>{
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
  {icon:'🎛️', title:'Interactive panel', sub:'Buttons & forms I can click',       prompt:'Show me an interactive panel with a few action buttons I can click'},
];
function showEmpty() {
  clearLog();
  const wrap=document.createElement('div'); wrap.className='j-empty';
   wrap.innerHTML='<div class="j-empty-title">Select a chat or type a message</div><div class="quick-cards">'+
    QUICK.map((q,i)=>`<div class="qcard" style="--i:${i}" data-prompt="${esc(q.prompt)}"><span class="qcard-icon">${q.icon}</span>`+
      `<span class="qcard-title">${esc(q.title)}</span><span class="qcard-sub">${esc(q.sub)}</span></div>`).join('')+'</div>';
  log.appendChild(wrap);
  wrap.querySelectorAll('.qcard').forEach(c=>{ c.onclick=()=>{ input.value=c.dataset.prompt; autoGrow(); input.focus(); }; });
}

// ---- RENDERING --------------------------------------------------------------
let _userMsgIdx=0;
const MSG_ACTIONS_HTML='<button class="msg-act-btn" data-act="resend" title="Resend">&#8635; resend</button><button class="msg-act-btn" data-act="edit" title="Edit">&#9998; edit</button><button class="msg-act-btn del-btn" data-act="delete" title="Delete">&#10005; delete</button>';
function wireMsgActions(row, idx, text){
  row.querySelector('[data-act="resend"]').onclick=()=>resendMsg(idx,row);
  row.querySelector('[data-act="edit"]').onclick=()=>editMsg(idx,row,text);
  row.querySelector('[data-act="delete"]').onclick=()=>deleteMsg(idx,row);
}
function addUser(text){
    const idx=_userMsgIdx++;
    const r=document.createElement('div'); r.className='msg-row user'; r.dataset.idx=idx;
    r.innerHTML='<div class="bubble">'+esc(text)+'</div><div class="msg-actions">'+MSG_ACTIONS_HTML+'</div>';
    wireMsgActions(r, idx, text);
    getThread().appendChild(r); scrollDown(); return r;
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
  live={body:null,raw:'',toolRow:null,thinking:null,turnStart:null,tokensIn:0,tokensOut:0};
  showThinking(); setOrbLabel(_curVerb+'\u2026'); setBusy(true); compactOrb(true);
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
    actions.innerHTML=MSG_ACTIONS_HTML;
    wireMsgActions(row, idx, origText);
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
    scrollDown(); return r;
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
  const icon=isCmd?'&#9654;':'&#9889;';
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
let live={body:null,raw:'',toolRow:null,thinking:null,turnStart:null,tokensIn:0,tokensOut:0};
let _thinkTimer=null;
const _VERBS=['hatching','orbiting','pondering','brewing','simmering','marinating','percolating','crystallizing','weaving','conjuring','manifesting','distilling','synthesizing','calculating','reverberating','catalyzing','assembling','composting','fermenting','spinning','dreaming','musing','ruminating','cooking','germinating','blossoming','incubating','metabolizing','transmuting','alchemizing'];
let _curVerb='thinking';
function randomVerb(){ return _VERBS[Math.floor(Math.random()*_VERBS.length)]; }
function showThinking(){
  _curVerb=randomVerb();
  const t=document.createElement('div'); t.className='thinking-row';
  t.innerHTML=avatarHTML()+'<span>'+_curVerb+'\u2026</span><div class="thinking-dots"><span></span><span></span><span></span></div><span class="thinking-timer" id="thinkTimer"></span>';
  getThread().appendChild(t); scrollDown(); live.thinking=t;
  live.turnStart=Date.now(); live.tokensIn=0; live.tokensOut=0;
  const timerEl=t.querySelector('#thinkTimer');
  _thinkTimer=setInterval(()=>{ if(!live.turnStart){clearInterval(_thinkTimer);_thinkTimer=null;return;} const s=((Date.now()-live.turnStart)/1000).toFixed(1); let parts=[s+'s']; if(live.tokensIn) parts.push('\u2193'+live.tokensIn); if(live.tokensOut) parts.push('\u2191'+live.tokensOut); if(timerEl) timerEl.textContent=parts.join(' \u00b7 '); },100);
}
function clearThinking(){ if(live.thinking){live.thinking.remove();live.thinking=null;} if(_thinkTimer){clearInterval(_thinkTimer);_thinkTimer=null;} }
function handle(ev){
  const k=ev.kind, d=ev.data||{};
  if(k!=='user') clearThinking();
  if(k==='delta'){
    if(!live.body){const _r=addAssistant('');live.body=_r.querySelector('.msg-body');live.raw='';}
    live.raw+=d.text||'';
    live.tokensOut+=Math.round((d.text||'').length/4);
    live.body.innerHTML=md(stripHud(live.raw));
    live.body.classList.add('cursor'); scrollDown();
  } else if(k==='assistant'){
    const txt=(d.text||'');
    if(!live.body&&stripHud(txt).trim()){const _r=addAssistant(md(stripHud(txt)));live.body=_r.querySelector('.msg-body');live.raw=txt;}
    else if(live.body){ live.raw=txt; live.body.innerHTML=md(stripHud(txt)); }
    if(live.body) live.body.classList.remove('cursor');
    renderPanels(txt);
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
    /* If the info message mentions tokens, update live.tokensIn */
    const m=d.text&&d.text.match(/~(\d[\d,]*)\s*tokens/);
    if(m) live.tokensIn=parseInt(m[1].replace(/,/g,''),10);
  } else if(k==='error'){ addNote(d.text||'ERROR: SYSTEM FAULT',true); live.body=null;
  } else if(k==='widget'){
    // show_widget SSE event — render a dedicated specialty card. We translate
    // the (type, title, data) payload into a HUD panel so it benefits from
    // dragging/resizing like the inline ```hud``` panels.
    live.body=null;
    renderWidget(d);
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
      row.innerHTML=parts.join('<span class="ds-sep">\u00b7</span>');
      getThread().appendChild(row); scrollDown();
    }
    /* Show token stats in footer */
    const ts=$('#tokenStats');
    if(ts){ let tp=[]; if(usage.input) tp.push('\u2193'+usage.input); if(usage.output) tp.push('\u2191'+usage.output); if(tp.length){ts.textContent=tp.join(' ');ts.classList.remove('hidden');}else{ts.classList.add('hidden');} }
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
  list.appendChild(chatGrp);
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
  let idx=0;
  cur.messages.forEach(m=>{
    let row;
    if(m.role==='user'){ row=addUser(m.content); idx++; }
    else {
      const html=md(stripHud(m.content));
      const hasContent=html&&html.trim();
      if(hasContent){ row=addAssistant(html,m.tools); renderPanels(m.content); idx++; }
      else { (m.tools||[]).forEach(t=>{ const tr=addToolRow({name:t},true); if(tr) tr.style.setProperty('--i',idx++); }); }
    }
    if(row) row.style.setProperty('--i',idx-1);
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
  $('#setGwPort').value=s.gateway_port||8700;
  $('#setGwAuto').checked=!!(s.gateway_auto_start!==false);
  $('#setSysPrompt').value=s.system_prompt||'';
  populateVoiceSelect();
  $('#settingsModal').classList.remove('hidden');
}
function closeSettings(){ $('#settingsModal').classList.add('hidden'); }
async function saveSettings(){
  state.voiceName=$('#setVoice').value||'';
  try{ localStorage.setItem('cagentic_voice',state.voiceName); }catch(e){}
  state.settings=await api('/api/settings',{
    model:$('#setModel').value, user_name:$('#setName').value, temperature:parseFloat($('#setTemp').value),
    stream:$('#setStream').checked, yolo:$('#setYolo').checked,
    gateway_port:parseInt($('#setGwPort').value)||8700,
    gateway_auto_start:$('#setGwAuto').checked,
    system_prompt:$('#setSysPrompt').value });
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
function setBusy(on){ state.busy=on; sendBtn.disabled=on; input.disabled=on; const bl=$('#busyLabel'); if(bl){bl.textContent='\u25CF '+_curVerb+'\u2026';bl.classList.toggle('hidden',!on);} $('#stopBtn').classList.toggle('hidden',!on); }
function finishTurn(){ setBusy(false); const ts=$('#tokenStats'); if(ts) ts.classList.add('hidden'); input.focus(); refreshChats(); }
let _abortCtrl=null;
async function abortGeneration(){
  if(!state.busy) return;
  try{ await fetch('/api/abort',{method:'POST'}); }catch(e){}
  if(_abortCtrl) try{ _abortCtrl.abort(); }catch(e){}
  clearThinking(); addNote('Generation stopped.',false); finishTurn();
}
async function send(text){
  if(state.busy) return;
  // Slash commands → /api/cmd instead of /api/chat
  if(text.startsWith('/')){
    const parts=text.split(/\s+/);
    const cmd=parts[0].slice(1);
    const arg1=parts[1]||'';
    const arg2=parts.slice(2).join(' ')||'';
    showThinking(); setOrbLabel(_curVerb+'\u2026'); setBusy(true);
    try{
      const r=await fetch('/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd,arg1,arg2})});
      const d=await r.json();
      if(d.current) setCurrent(d.current);
      if(d.model) { state.settings.model=d.model; setModelBadge(d.model); renderModelMenu(); }
      addNote(d.text||'Done',!d.ok);
    }catch(e){ addNote('Command failed: '+e,true); }
    clearThinking(); setBusy(false);
    return;
  }
  if(log.querySelector('.j-empty')) clearLog();
  addUser(text);
  live={body:null,raw:'',toolRow:null,thinking:null,turnStart:null,tokensIn:0,tokensOut:0};
  showThinking(); setOrbLabel(_curVerb+'\u2026'); setBusy(true); compactOrb(true);
  _abortCtrl=new AbortController();
  let res;
  try{ res=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text}),signal:_abortCtrl.signal}); }
  catch(e){ if(e.name==='AbortError'){finishTurn();return;} clearThinking(); addNote('CONNECTION FAILURE',true); finishTurn(); return; }
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
$('#stopBtn').onclick=abortGeneration;
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
  if((e.ctrlKey||e.metaKey)&&e.key==='s'){ e.preventDefault(); openSettings(); return; }
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
