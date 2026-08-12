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

import errno
import json
import logging
import math
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from . import (
    command_utils,
    inbox,
    personal_os,
    proactive,
    projects,
    reminders,
    routines,
    sessions,
)
from . import config as _config
from .providers import (
    build_client as _build_client,
)
from .providers import (
    list_all_models as _all_models,
)
from .providers import (
    parse_model as _parse_model,
)
from .providers import (
    warm_model_cache as _warm_model_cache,
)
from .services.compact import SUMMARY_MARKER, auto_compact
from .token_count import count_messages
from .workspaces import WorkspaceError, WorkspacePolicy

if TYPE_CHECKING:
    from .engine import QueryEngine

_log = logging.getLogger(__name__)

# Hostnames we treat as the loopback interface for Host/Origin checks.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})

# The web command catalog is intentionally smaller than the terminal catalog:
# chat save/delete/resend already have dedicated controls, and terminal-only
# Terminal-only commands such as /login and /gateway have no honest web equivalent.
GATEWAY_COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("help", "", "show web slash commands"),
    ("new", "[title]", "start a new chat"),
    ("clear", "", "clear the current chat"),
    ("retry", "", "regenerate the most recent reply"),
    ("model", "[name]", "show or switch model"),
    ("models", "", "list available models"),
    ("effort", "[low|medium|high]", "set reasoning effort"),
    ("stream", "[on|off]", "toggle provider token streaming"),
    ("tools", "", "list active tools"),
    ("groups", "[enable|disable <name>]", "show or change tool groups"),
    ("plan", "[on|off]", "toggle read-only plan mode"),
    ("yolo", "[on|off]", "toggle automatic tool approval"),
    ("notes", "", "list saved notes"),
    ("mcp", "[server]", "list MCP servers or one server's tools"),
    ("name", "[name]", "show or set your name"),
    ("host", "[url]", "show or change the Ollama host"),
    ("config", "", "show redacted configuration"),
    ("set", "<key> <value>", "set a validated config value"),
    ("diag", "", "show runtime diagnostics"),
)
GATEWAY_COMMAND_NAMES = frozenset(name for name, _args, _hint in GATEWAY_COMMANDS)
_GATEWAY_ARGUMENTS = {name: args for name, args, _hint in GATEWAY_COMMANDS}


def _gateway_help_text() -> str:
    usages = [f"/{name}{f' {args}' if args else ''}" for name, args, _ in GATEWAY_COMMANDS]
    width = max(len(usage) for usage in usages) + 2
    lines = ["Web slash commands:"]
    for usage, (_name, _args, hint) in zip(usages, GATEWAY_COMMANDS):
        lines.append(f"  {usage:<{width}}{hint}")
    return "\n".join(lines)


def _gateway_usage(name: str) -> str:
    args = _GATEWAY_ARGUMENTS.get(name, "")
    return f"usage: /{name}{f' {args}' if args else ''}"


_HIDDEN_USER_PREFIXES = (
    "Tool result for ",
    "[background] ",
    "STOP. ",
    "You called ",
    "[older context, compacted]",
)


def _visible_user_indices(messages: list[dict]) -> list[int]:
    """Indices whose user messages are actually rendered in the web chat."""
    return [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "user"
        and bool(str(message.get("content") or "").strip())
        and not str(message.get("content") or "").strip().startswith(_HIDDEN_USER_PREFIXES)
    ]


class _GatewayHTTPServer(ThreadingHTTPServer):
    # Makes a quick stop/start reliable when the old listener is in TIME_WAIT.
    allow_reuse_address = True


class _ClientGone(Exception):
    """Raised when the browser hangs up mid-stream."""


from .engine import _PLAN_RX, _THINK_RX

_STEP_RX = re.compile(r"<step\s+\d+(?:\s*/\s*\d+)?\s*>", re.IGNORECASE)

# Browser attachments live inside the workspace so the existing @path mention
# pipeline can read them back; see Gateway.save_upload.
UPLOAD_DIR = ".cagentic/uploads"
MAX_UPLOAD_BYTES = 16 * 1024 * 1024
# One page of browser context; a whole article would swamp the turn.
MAX_PAGE_CONTEXT = 8000
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_UNSAFE_NAME_RX = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_upload_name(name: str) -> str:
    """Reduce a browser-supplied filename to something safe to join onto a path.

    Deliberately strict rather than clever: strip every directory component,
    collapse anything outside [A-Za-z0-9._-], and refuse names that are only
    dots. `_resolve_contained` is still the real boundary — this just means a
    hostile name never gets that far.
    """
    base = str(name or "").replace("\\", "/").split("/")[-1].strip()
    base = _UNSAFE_NAME_RX.sub("_", base).strip("._")
    if not base:
        return ""
    return base[:120]


def _search_snippet(messages: list, needle: str, width: int = 90) -> str:
    """A short window of text around the first match, for the search results."""
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        content = str(m.get("content") or "")
        at = content.lower().find(needle)
        if at < 0:
            continue
        lo = max(0, at - width // 3)
        text = " ".join(content[lo : lo + width].split())
        return ("…" if lo else "") + text + ("…" if lo + width < len(content) else "")
    return ""


def _upload_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in {".pdf", ".docx"}:
        return "document"
    return "text"


def _clean(text: str) -> str:
    text = _THINK_RX.sub("", text)
    text = _PLAN_RX.sub("", text)
    text = _STEP_RX.sub("", text)
    return text.strip()


def _tool_details(tool_calls: list[dict], messages: list[dict], start: int) -> list[dict]:
    """Pair an assistant message's stored tool_calls with the result
    messages recorded right after it, in call order.

    Results are stored as {"role": "tool", "name": ..., "content": ...} or
    user messages of the form "Tool result for <name>:\\n<result>". Returns
    one {name, summary, ok, first_line} dict per call; ok is None when no
    result was found (e.g. the turn was aborted mid-call).
    """
    from .engine import _summarize_args

    results: list[tuple[str, str]] = []  # (tool name, result text)
    for m in messages[start : start + len(tool_calls) * 2]:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "tool":
            results.append((m.get("name", "?"), content))
        elif role == "user" and content.startswith("Tool result for "):
            header, _, rest = content.partition("\n")
            results.append((header[len("Tool result for ") :].rstrip(":"), rest))
        elif role == "system":
            continue  # loop-steering notes interleave with results
        else:
            break  # next conversation turn — stop pairing

    details = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        name = fn.get("name", "?")
        args = fn.get("arguments") or {}
        if isinstance(args, str):  # OpenAI-style JSON-encoded arguments
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        ok = None
        first_line = ""
        for j, (rname, result) in enumerate(results):
            if rname == name:
                ok = not result.startswith("ERROR")
                first_line = result.splitlines()[0][:120] if result else ""
                results.pop(j)
                break
        details.append(
            {
                "name": name,
                "summary": _summarize_args(name, args),
                "ok": ok,
                "first_line": first_line,
            }
        )
    return details


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
        self.requested_port = port
        self.port_fallback = False
        self.start_notice: str | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.error: str | None = None
        configured_model = str(config.get("model") or "").strip()
        _, configured_name = _parse_model(configured_model)
        self._model_spec = (
            agent.state.active_model_spec
            or (configured_model if configured_model and configured_name == agent.model else None)
            or agent.model
        )
        _warm_model_cache(config)

        # Per-process secret required on every /api/* request. Bound to
        # localhost only, but this token defends against other local users
        # and (with the Host/Origin checks) against DNS-rebinding / CSRF.
        # A stable token can be pinned via config gateway.token so paired
        # clients survive gateway restarts.
        raw_gw_cfg = config.get("gateway")
        gw_cfg = raw_gw_cfg if isinstance(raw_gw_cfg, dict) else {}
        raw_token = gw_cfg.get("token")
        pinned = raw_token.strip() if isinstance(raw_token, str) else ""
        self.token = pinned or secrets.token_urlsafe(32)

        # Opt-in LAN exposure (config gateway.lan=true): binds all interfaces
        # so other machines on the network can connect. Every /api/* request
        # still requires the secret token — set gateway.token and enter it on
        # the device. Off by default: loopback only.
        raw_lan = gw_cfg.get("lan", False)
        self.lan = raw_lan if isinstance(raw_lan, bool) else False

        # Restore the persisted effort dial (set live via /api/effort).
        effort = str(config.get("effort") or "").strip().lower()
        if effort in ("low", "medium", "high"):
            agent.state.update(effort=effort)

        # Simple in-memory rate limit for the email-verification endpoint:
        # list of monotonic timestamps of recent sends.
        self._turn_lock = threading.Lock()
        self._actors_lock = threading.Lock()
        self._actors: dict[str, dict] = {}
        self._thread_context = threading.local()
        self._active_emit = None
        self._default_workspace = agent.state.workspace.resolve()
        self.workspace_policy = WorkspacePolicy.from_config(config, self._default_workspace)
        self.proactive_monitor = proactive.ProactiveMonitor(
            config,
            browser_connected=self._browser_connected,
            on_routine=self._run_proactive_routine,
        )

        # Permission prompt bridge. Each prompt gets a unique id; the client
        # must echo that id back when answering, so a stale/duplicate answer
        # can't resolve a different prompt (avoids the old set-before-clear
        # race on a single process-global answer).
        self._perm_cv = threading.Condition()
        self._perm_id: str | None = None  # id of the prompt currently awaiting an answer
        self._perm_pending: set[str] = set()
        self._perm_answers: dict[str, str] = {}  # prompt_id -> answer
        # prompt_id -> the rule strings that prompt offered, so an answer
        # can only install a rule the user was actually shown.
        self._perm_rules: dict[str, tuple[str, ...]] = {}

        # Page context queued by the extension side panel, folded into the
        # next message the user sends.
        self._context_lock = threading.Lock()
        self._pending_context: list[str] = []

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
            stream=agent.engine.stream,
        )
        # Teach this engine (gateway only) to drive the holographic HUD. The
        # user's custom system prompt (if any) is kept separate from the HUD
        # instructions so the settings UI can edit one without clobbering the
        # other — both are composed into system_suffix by _rebuild_suffix().
        configured_prompt = config.get("system_prompt")
        self._user_system_prompt = (
            configured_prompt.strip() if isinstance(configured_prompt, str) else ""
        )
        self._rebuild_suffix()
        # Wire up widget action callback — emits SSE widget events
        self._setup_widget_action()
        # The current chat is a session record (shared store with the REPL).
        self.session = sessions.make(self._model_spec)
        self.engine.session_id = self.session["id"]

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> bool:
        if self._server is not None:
            return True
        if (
            isinstance(self.requested_port, bool)
            or not isinstance(self.requested_port, int)
            or not 1 <= self.requested_port <= 65535
        ):
            self.error = f"invalid gateway port {self.requested_port!r}; expected 1-65535"
            return False
        # Loopback by default — the gateway exposes the full assistant
        # (shell, files, browser, MCP). LAN binding is an explicit opt-in
        # (config gateway.lan=true) and keeps the token requirement.
        bind = "0.0.0.0" if self.lan else "127.0.0.1"
        raw_gw_cfg = self.config.get("gateway")
        gw_cfg = raw_gw_cfg if isinstance(raw_gw_cfg, dict) else {}
        raw_auto_port = gw_cfg.get("auto_port", True)
        auto_port = raw_auto_port if isinstance(raw_auto_port, bool) else True
        ports = [self.requested_port]
        if auto_port:
            ports.extend(range(self.requested_port + 1, min(65535, self.requested_port + 20) + 1))
        server = None
        last_error: OSError | None = None
        for candidate in ports:
            try:
                server = _GatewayHTTPServer((bind, candidate), _Handler)
                self.port = int(server.server_address[1])
                break
            except OSError as e:
                last_error = e
                if e.errno not in (errno.EADDRINUSE, 48, 98, 10048):
                    break
        if server is None:
            detail = last_error or "no available port"
            self.error = f"could not bind {bind}:{self.requested_port} ({detail})"
            return False
        self.error = None
        self.port_fallback = self.port != self.requested_port
        if self.port_fallback:
            self.start_notice = (
                f"port {self.requested_port} was already in use; started on {self.port} instead"
            )
        else:
            self.start_notice = None
        server.gateway = self  # type: ignore[attr-defined]
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        self.proactive_monitor.start()
        return True

    def stop(self) -> None:
        self.proactive_monitor.stop()
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                _log.warning("error shutting down gateway server", exc_info=True)
            self._server = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def url(self) -> str:
        return f"http://localhost:{self.port}"

    def _browser_connected(self) -> bool:
        browser = getattr(self.agent.state, "browser", None)
        try:
            return bool(browser and browser.is_connected())
        except Exception:
            return False

    # -- permission bridge --------------------------------------------------

    def _resolve(self, name: str, args: dict, state) -> str:
        emit = getattr(self._thread_context, "emit", None) or self._active_emit
        if emit is None:
            return "no"
        import uuid

        from .engine import _summarize_args
        from .permissions import describe_change, suggest_rule

        prompt_id = str(uuid.uuid4())
        with self._perm_cv:
            self._perm_answers.pop(prompt_id, None)
            self._perm_pending.add(prompt_id)
            self._perm_id = prompt_id
        payload: dict[str, object] = {
            "id": prompt_id,
            "tool": name,
            "summary": _summarize_args(name, args),
        }
        # Same patch the REPL shows before a y/n — plain text, since ANSI
        # escapes would be rendered literally in the browser.
        patch = describe_change(name, args, state, colorize=False)
        if patch:
            payload["diff"] = patch
        # The narrow standing approval the terminal already offers. Without it
        # the browser's only durable choice is "always allow this whole tool",
        # which is exactly the coarse grant Phase 1d set out to replace.
        rule = suggest_rule(name, args)
        if rule:
            payload["rule"] = rule
            with self._perm_cv:
                self._perm_rules[prompt_id] = (rule,)
        # Say which confinement a shell command will actually run under —
        # "approve this command" means something different with the sandbox off.
        if name in ("run_bash", "bash_async"):
            from . import sandbox as _sandbox

            shell_cfg = self.config.get("shell") if isinstance(self.config, dict) else None
            shell_cfg = shell_cfg if isinstance(shell_cfg, dict) else {}
            if str(shell_cfg.get("sandbox", "auto")).lower() == "off":
                payload["sandbox"] = "unconfined (shell.sandbox=off)"
            else:
                net = bool(args.get("network")) or (
                    str(shell_cfg.get("network", "deny")).lower() == "allow"
                )
                payload["sandbox"] = _sandbox.describe() + (
                    " · network allowed" if net else " · no network"
                )
            payload["network"] = bool(args.get("network"))
        emit("permission", payload)
        try:
            with self._perm_cv:
                deadline = time.monotonic() + 300
                while prompt_id not in self._perm_answers:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return "no"
                    self._perm_cv.wait(remaining)
                return self._perm_answers.pop(prompt_id)
        finally:
            with self._perm_cv:
                if self._perm_id == prompt_id:
                    self._perm_id = next(iter(self._perm_pending - {prompt_id}), None)
                self._perm_pending.discard(prompt_id)
                self._perm_rules.pop(prompt_id, None)

    def deliver_permission(
        self, answer: str, prompt_id: str | None = None, rule: str | None = None
    ) -> None:
        """Record an answer for a pending permission prompt.

        When `prompt_id` is given it must match the prompt currently awaiting
        an answer; a mismatched/stale id is ignored. When omitted (e.g. an
        internal abort), the answer is applied to whatever prompt is active.

        `answer == "rule"` means "allow, and remember this narrow pattern" —
        the browser's equivalent of the terminal resolver's `rule` choice. The
        rule is session-scoped; persisting it stays an explicit /rules call so
        a single click can't silently edit config on disk.
        """
        if answer == "rule" and rule:
            rules = dict(getattr(self.agent.state, "permission_rules", None) or {})
            allow = list(rules.get("allow") or [])
            # Only accept a rule this prompt actually offered, so a crafted
            # request can't inject an arbitrary standing approval.
            if rule in self._perm_rules.get(prompt_id or "", ()):
                if rule not in allow:
                    allow.append(rule)
                rules["allow"] = allow
                self.agent.state.update(permission_rules=rules)
            else:
                _log.warning("ignoring permission rule that was not offered for this prompt")
            answer = "yes"
        answer = answer if answer in ("yes", "no", "always", "never") else "no"
        with self._perm_cv:
            target = prompt_id or self._perm_id
            if target is None:
                return
            if prompt_id is not None and prompt_id not in self._perm_pending:
                _log.warning("ignoring permission answer for stale/unknown prompt id")
                return
            self._perm_answers[target] = answer
            self._perm_cv.notify_all()

    def _composed_suffix(self) -> str:
        """Build the live gateway prompt without a per-turn device tail."""
        parts: list[str] = []
        if self._user_system_prompt:
            parts.append(self._user_system_prompt)
        parts.append(_HUD_INSTRUCTIONS)
        parts.append(personal_os.system_context())
        parts.append(inbox.system_context())
        parts.append(routines.system_context())
        return "\n\n".join(parts)

    def _rebuild_suffix(self) -> None:
        """Refresh the primary gateway engine's live, device-neutral context."""
        self.engine.system_suffix = self._composed_suffix()
        self.engine.refresh_system_prompt()

    def _setup_widget_action(self) -> None:
        """Wire up the widget_action callback on the engine's tool executor."""
        gw = self

        def widget_action(widget_type: str, title: str, data: dict) -> str | None:
            """Callback for show_widget tool. Emits SSE widget event."""
            emit = getattr(gw._thread_context, "emit", None) or gw._active_emit
            if emit is None:
                return None  # No active stream — tool returns text fallback
            emit("widget", {"type": widget_type, "title": title, "data": data})
            return "emitted"

        self.engine.executor.widget_action = widget_action

    def _refresh_shared_prompts(self) -> None:
        """Refresh both engines after changing their shared AppState."""
        self.engine.refresh_system_prompt()
        if self.agent.engine is not self.engine:
            self.agent.engine.refresh_system_prompt()

    def _apply_shared_runtime_setting(self, key: str, value) -> bool:
        """Apply one global setting to the web and terminal engines."""
        applied = command_utils.apply_runtime_setting(self.agent.state, self.engine, key, value)
        if self.agent.engine is not self.engine:
            applied = (
                command_utils.apply_runtime_setting(self.agent.state, self.agent.engine, key, value)
                or applied
            )
        if key == "proactive.enabled":
            self.proactive_monitor.enabled = value
            if value and self.running:
                self.proactive_monitor.start()
            elif not value:
                self.proactive_monitor.stop()
            return True
        if key == "proactive.desktop_notifications":
            self.proactive_monitor.desktop_notifications = value
            return True
        if key == "proactive.interval":
            self.proactive_monitor.interval = value
            return True
        return applied

    def _persist_command_config(self) -> str | None:
        try:
            _config.save(self.config)
            return None
        except (OSError, TypeError, ValueError) as exc:
            _log.warning("gateway command could not save config", exc_info=True)
            return f"could not save config: {exc}; any live change lasts only until restart"

    # -- chats --------------------------------------------------------------

    def _save_current(self, *, allow_empty: bool = False) -> None:
        msgs = [
            m
            for m in self.engine.messages
            if m.get("role") != "system" or SUMMARY_MARKER in str(m.get("content") or "")
        ]
        if not msgs and not allow_empty:
            return  # don't persist empty chats
        self.session["model"] = self._model_spec
        self.session["messages"] = msgs
        sessions.save(self.session)

    def _interrupt_turn(self, timeout: float = 15.0) -> bool:
        """Abort any in-flight turn and wait for it to finish saving into the
        current session. Chat switching must hold the turn lock — otherwise a
        live turn keeps appending into the freshly swapped-in session and its
        final save writes the old conversation into the new chat.

        Returns True when the lock was acquired (caller must release)."""
        if self._turn_lock.acquire(blocking=False):
            return True  # no turn running
        self.engine._abort_turn = True
        # Unblock a pending permission wait too, or the lock acquisition
        # below would time out against its 300s wait.
        self.deliver_permission("no")
        got = self._turn_lock.acquire(timeout=timeout)
        if got:
            # We own the lock now, so the turn is finished; clear the flag so
            # the next turn can run. If we DIDN'T get the lock the turn is
            # still live — leave the abort flag set so it stops ASAP and
            # don't pretend we interrupted it.
            self.engine._abort_turn = False
        return got

    def is_busy(self) -> bool:
        """Is a turn in flight, on the main engine or any saved-session actor?

        Used by the auto-reloader to avoid restarting the daemon out from under
        a request. Non-blocking and best-effort: a false negative costs at most
        one dropped reply, and the reloader re-checks every second anyway.
        """
        if self._turn_lock.locked():
            return True
        with self._actors_lock:
            actors = list(self._actors.values())
        return any(a["lock"].locked() for a in actors)

    def list_chats(self) -> list[dict]:
        self._save_current()
        out = []
        for s in sessions.list_all():
            out.append(
                {
                    "id": s["id"],
                    "title": s["title"] if s["title"] not in (None, "", "untitled") else "New chat",
                    "updated_at": s["updated_at"],
                    "turns": s["turns"],
                    "project_id": s.get("project_id", ""),
                    "project": s.get("project"),
                }
            )
        return out

    def render_messages(self, messages: list[dict]) -> list[dict]:
        """Turn stored messages into display items for the web UI."""
        out: list[dict] = []
        visible_users = set(_visible_user_indices(messages))
        for i, m in enumerate(messages):
            role = m.get("role")
            if role == "system":
                continue
            content = (m.get("content") or "").strip()
            if role == "user":
                if i in visible_users:
                    out.append({"role": "user", "content": content})
            elif role == "assistant":
                tool_calls = m.get("tool_calls") or []
                tools = [(tc.get("function") or {}).get("name", "?") for tc in tool_calls]
                cleaned = _clean(content)
                if cleaned or tools:
                    msg = {"role": "assistant", "content": cleaned, "tools": tools}
                    if tool_calls:
                        # Rich per-call info (args summary + outcome) so
                        # clients can re-render tool chips after a reload.
                        msg["tool_details"] = _tool_details(tool_calls, messages, i + 1)
                    out.append(msg)
        return out

    def add_page_context(self, text: str) -> dict:
        """Queue browser page context to ride along with the next message.

        Used by the extension's side panel "Add page" button. Held rather than
        sent immediately because context on its own isn't a turn — the user
        still has to say what they want done with it.
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "empty context"}
        if len(text) > MAX_PAGE_CONTEXT:
            text = text[:MAX_PAGE_CONTEXT] + "\n…(truncated)"
        with self._context_lock:
            self._pending_context.append(text)
            del self._pending_context[:-4]  # keep the last few, not a backlog
            pending = len(self._pending_context)
        return {"ok": True, "pending": pending}

    def _consume_pending_context(self, message: str) -> str:
        """Prepend and clear any queued page context. Called once per turn."""
        with self._context_lock:
            if not self._pending_context:
                return message
            blocks = list(self._pending_context)
            self._pending_context.clear()
        return "\n\n".join(blocks) + "\n\n" + message

    def search_chats(self, query: str, limit: int = 40) -> dict:
        """Title+content search over saved chats, for the sidebar's search box.

        Reuses `sessions.search` (the same index behind `/search` and
        `--search`) so the three surfaces can't disagree about what matches.
        """
        query = (query or "").strip()
        if not query:
            return {"query": "", "results": []}
        try:
            found = sessions.search(query, limit=limit)
        except Exception:
            _log.warning("chat search failed", exc_info=True)
            return {"query": query, "results": []}
        needle = query.lower()
        results = []
        for data in found:
            if not isinstance(data, dict) or not data.get("id"):
                continue
            results.append(
                {
                    "id": data.get("id"),
                    "title": data.get("title") or "Untitled",
                    "project_id": data.get("project_id"),
                    "snippet": _search_snippet(data.get("messages") or [], needle),
                }
            )
        return {"query": query, "results": results}

    # -- attachments ---------------------------------------------------------

    def save_upload(self, name: str, data_b64: str) -> tuple[dict, int]:
        """Persist a browser upload inside the workspace; return (payload, status).

        Files land under `.cagentic/uploads/<chat-id>/` so the model can reach
        them through the *existing* `@path` mention pipeline — that already
        extracts PDF/DOCX text and, as of this phase, inlines images for vision
        models. Routing uploads through it means one attachment code path
        serves the terminal and the browser instead of two that drift.
        """
        import base64

        from .tools import PathEscapeError, _resolve_contained

        safe = _safe_upload_name(name)
        if not safe:
            return {"error": "invalid file name"}, 400
        try:
            raw = base64.b64decode(data_b64 or "", validate=True)
        except (ValueError, TypeError):
            return {"error": "attachment was not valid base64"}, 400
        if not raw:
            return {"error": "attachment was empty"}, 400
        if len(raw) > MAX_UPLOAD_BYTES:
            limit = MAX_UPLOAD_BYTES // (1024 * 1024)
            return {"error": f"attachment is larger than {limit}MB"}, 413

        chat_id = str(self.session.get("id") or "chat")
        rel = f"{UPLOAD_DIR}/{_safe_upload_name(chat_id) or 'chat'}/{safe}"
        workspace = self.agent.state.workspace
        try:
            # The one boundary that matters: an upload must not be able to
            # write outside the workspace via a crafted name.
            target = _resolve_contained(rel, workspace)
        except PathEscapeError:
            return {"error": "attachment path escapes the workspace"}, 400
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Never silently clobber a same-named earlier upload.
            stem, suffix = target.stem, target.suffix
            n = 1
            while target.exists():
                target = target.with_name(f"{stem}-{n}{suffix}")
                n += 1
            target.write_bytes(raw)
        except OSError as exc:
            _log.warning("upload failed", exc_info=True)
            return {"error": f"could not save attachment: {exc}"}, 500

        try:
            # Compare against the RESOLVED workspace: _resolve_contained hands
            # back a realpath, so on macOS (/tmp -> /private/tmp) and anywhere
            # with a symlinked project root the naive relative_to fails and the
            # chip ends up showing an absolute path.
            mention = str(target.relative_to(workspace.resolve())).replace("\\", "/")
        except ValueError:  # pragma: no cover - _resolve_contained guarantees it
            mention = str(target)
        return {
            "ok": True,
            "name": target.name,
            "path": mention,
            "size": len(raw),
            "kind": _upload_kind(target),
        }, 200

    def current_chat(self) -> dict:
        current = {
            "id": self.session["id"],
            "title": self.session.get("title") or "New chat",
            "model": self._model_spec,
            "messages": self.render_messages(self.engine.messages),
        }
        if self.session.get("project") is not None:
            current["project"] = self.session["project"]
        return current

    def new_chat(self, project: dict | None = None) -> dict:
        locked = self._interrupt_turn()
        if not locked:
            return {"error": "Cagentic is still working on the previous message."}
        try:
            return self._new_chat_unlocked(project)
        finally:
            self._turn_lock.release()

    def _new_chat_unlocked(self, project: dict | None = None) -> dict:
        self._save_current()
        if project is not None:
            project = self.validate_project(project)
        self.session = sessions.make(self._model_spec, project=project)
        self.engine.project_system_prompt = ""
        self.engine.project_context = ""
        self.engine.reset()
        self.engine.session_id = self.session["id"]
        self._activate_session_project()
        if project is not None:
            sessions.save(self.session)
        return self.current_chat()

    def validate_project(self, project) -> dict:
        if not isinstance(project, dict):
            raise WorkspaceError("project must be an object with kind and value")
        kind, value = project.get("kind"), project.get("value")
        if kind == "gatewayFolder":
            return {"kind": kind, "value": str(self.workspace_policy.validate(value))}
        if kind == "repository":
            value = str(value or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
                raise WorkspaceError("repository project value must be owner/repository")
            return {"kind": kind, "value": value}
        raise WorkspaceError("project kind must be repository or gatewayFolder")

    def pin_collama_project(self, project) -> dict:
        """Validate and assign once; callers must hold the turn/switch lock."""
        requested = self.validate_project(project)
        existing = self.session.get("project")
        if existing is not None and existing != requested:
            raise WorkspaceError("session project is immutable", 409)
        if existing is None:
            self.session["project"] = requested
            try:
                self._activate_session_project()
            except Exception:
                self.session["project"] = None
                raise
            sessions.save(self.session)
        else:
            self._activate_session_project()
        return requested

    def _activate_session_project(self) -> None:
        project = self.session.get("project")
        workspace = self._default_workspace
        repository = None
        boundary = None
        context_parts = []
        pid = self.session.get("project_id")
        if pid:
            local_project = projects.load(pid)
            if local_project and local_project.get("context"):
                context_parts.append(local_project["context"])
        if project and project.get("kind") == "gatewayFolder":
            workspace = self.workspace_policy.validate(project.get("value"))
            boundary = workspace
            context_parts.append(
                f"Pinned gateway folder project: {workspace}. Use it as the working directory."
            )
        elif project and project.get("kind") == "repository":
            repository = project.get("value")
            context_parts.append(
                f"Pinned GitHub repository project: {repository}. Use it as the default repository for GitHub operations."
            )
        self.agent.state.update(
            workspace=workspace,
            default_repository=repository,
            workspace_boundary=boundary,
            worktree_stack=[],
        )
        self.engine.project_context = "\n\n".join(context_parts)
        self.engine.refresh_system_prompt()

    def load_chat(self, chat_id: str) -> dict:
        locked = self._interrupt_turn()
        if not locked:
            return {"error": "Cagentic is still working on the previous message."}
        try:
            return self._load_chat_unlocked(chat_id)
        finally:
            self._turn_lock.release()

    def _load_chat_unlocked(self, chat_id: str) -> dict:
        self._save_current()
        with self._actors_lock:
            actor = self._actors.get(chat_id)
            if actor is not None and actor["lock"].locked():
                return {"error": "chat is busy in another request"}
        data = sessions.load(chat_id)
        if not data:
            return {"error": f"chat {chat_id} not found"}
        if data.get("project") is not None:
            try:
                data["project"] = self.validate_project(data["project"])
            except WorkspaceError as exc:
                return {"error": f"cannot load chat project: {exc}"}
        model_error = self._activate_model(data.get("model") or self._model_spec)
        if model_error:
            return {"error": f"cannot load chat model: {model_error}"}
        self.session = data
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
        self._activate_session_project()
        return self.current_chat()

    def delete_chat(self, chat_id: str) -> dict:
        locked = self._interrupt_turn()
        if not locked:
            return {"error": "Cagentic is still working on the previous message."}
        try:
            with self._actors_lock:
                actor = self._actors.get(chat_id)
                if actor is not None and actor["lock"].locked():
                    return {"error": "chat is busy in another request"}
                self._actors.pop(chat_id, None)
            sessions.delete(chat_id)
            # Remove from any project
            for proj in projects.list_all():
                if chat_id in proj.get("chats", []):
                    projects.remove_chat(proj["id"], chat_id)
            if chat_id == self.session.get("id"):
                self._new_chat_unlocked()
            return {
                "chats": self.list_chats(),
                "current": self.current_chat(),
                "projects": self.list_projects(),
            }
        finally:
            self._turn_lock.release()

    def autotitle_chat(self) -> dict:
        """Ask the model itself for a short title for the current chat."""
        msgs = [
            m
            for m in self.engine.messages
            if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
        ]
        if not msgs:
            return {"title": self.session.get("title") or "New chat"}

        convo = "\n".join(f"{m['role']}: {_clean(m.get('content') or '')[:300]}" for m in msgs[:4])
        ask = [
            {
                "role": "user",
                "content": (
                    "Give this conversation a short title: 3-6 plain words, no "
                    "quotes, no trailing punctuation. Reply with the title only.\n\n" + convo
                ),
            }
        ]
        title = ""
        try:
            reply = self.agent.client.chat(self.agent.model, ask, options={"temperature": 0.2})
            lines = _clean(reply.get("content") or "").strip().strip("\"'").splitlines()
            title = lines[0].strip()[:60] if lines else ""
        except Exception:
            _log.warning("autotitle: model call failed, falling back", exc_info=True)
            title = ""
        if not title:
            # Model unavailable — fall back to the first user message
            title = sessions.derive_title(self.session.get("messages") or [])
        self.session["title"] = title
        self._save_current()
        return {"title": title, "chats": self.list_chats()}

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
        return {
            "project": proj,
            "projects": self.list_projects(),
            "chats": self.list_chats(),
        }

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
        return {
            "project": proj,
            "projects": self.list_projects(),
            "chats": self.list_chats(),
        }

    def update_project_config(self, project_id: str, system_prompt: str, context: str) -> dict:
        proj = projects.update_config(project_id, system_prompt, context)
        # If the current chat belongs to this project, refresh the system prompt
        if self.session.get("project_id") == project_id:
            self.engine.project_system_prompt = system_prompt
            self.engine.project_context = context
            self.engine.refresh_system_prompt()
        return {"project": proj, "projects": self.list_projects()}

    # -- personal OS --------------------------------------------------------

    def _run_proactive_routine(self, routine: dict) -> str:
        """Run isolated proactive inference without touching chat history."""
        if not self._turn_lock.acquire(blocking=False):
            return routines.fallback_output(routine)
        try:
            result = self.agent.client.chat(
                model=self.agent.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are Cagentic's proactive personal-OS planner. "
                            "Use only the supplied local context, be concise, and propose concrete actions."
                        ),
                    },
                    {"role": "user", "content": routines.build_prompt(routine)},
                ],
                tools=None,
                options={"temperature": 0.2},
            )
            return _clean(str(result.get("content") or "")) or routines.fallback_output(routine)
        except Exception:
            _log.warning("proactive routine model call failed; using fallback", exc_info=True)
            return routines.fallback_output(routine)
        finally:
            self._turn_lock.release()

    # -- settings -----------------------------------------------------------

    def get_settings(self) -> dict:
        # Collect models from all configured providers, flattened to one list.
        all_provider_models = _all_models(self.config, cached_only=True)
        models: list[str] = []
        for _provider, mlist in all_provider_models.items():
            models.extend(mlist)
        # Current model shown with provider prefix if cloud.
        current = self._model_spec
        models = list(dict.fromkeys(models))
        if current not in models:
            models.insert(0, current)
        raw_gw_cfg = self.config.get("gateway")
        gw_cfg = raw_gw_cfg if isinstance(raw_gw_cfg, dict) else {}
        raw_proactive_cfg = self.config.get("proactive")
        proactive_cfg = raw_proactive_cfg if isinstance(raw_proactive_cfg, dict) else {}
        try:
            raw_port = gw_cfg.get("port", 8700)
            gateway_port = 0 if isinstance(raw_port, bool) else int(raw_port)
        except (TypeError, ValueError):
            gateway_port = 8700
        if not 1 <= gateway_port <= 65535:
            gateway_port = 8700
        return {
            "model": current,
            "models": models,
            "temperature": self.engine.temperature,
            "user_name": self.agent.state.user_name or "",
            "stream": self.engine.stream,
            "yolo": self.agent.state.yolo,
            "gateway_port": gateway_port,
            "gateway_auto_start": command_utils.boolean_value(gw_cfg.get("auto_start", True), True),
            "proactive_enabled": command_utils.boolean_value(
                proactive_cfg.get("enabled", True), True
            ),
            "desktop_notifications": command_utils.boolean_value(
                proactive_cfg.get("desktop_notifications", True), True
            ),
            "system_prompt": self._user_system_prompt or "",
        }

    def abort_generation(self) -> None:
        """Abort the current streaming generation."""
        self.engine._abort_turn = True
        # A turn blocked in a permission wait holds the turn lock for up to
        # 300s — answer "no" so it wakes up and winds down immediately.
        self.deliver_permission("no")

    def update_settings(self, data: dict) -> dict:
        cfg = self.config
        errors: list[str] = []

        def incoming_bool(field: str) -> bool | None:
            value = data[field]
            if isinstance(value, bool):
                return value
            errors.append(f"{field} must be true or false")
            return None

        if data.get("model"):
            error = self._activate_model(str(data["model"]))
            if error:
                return {**self.get_settings(), "error": error}
            cfg["model"] = self._model_spec
        if "temperature" in data:
            try:
                raw_temperature = data["temperature"]
                if isinstance(raw_temperature, bool):
                    raise ValueError
                t = float(raw_temperature)
                if not math.isfinite(t):
                    raise ValueError
                t = max(0.0, min(2.0, t))
                self._apply_shared_runtime_setting("temperature", t)
                cfg["temperature"] = t
            except (TypeError, ValueError):
                _log.warning("ignoring invalid temperature %r", data.get("temperature"))
        if "user_name" in data:
            name = str(data.get("user_name") or "").strip() or None
            self._apply_shared_runtime_setting("user_name", name)
            cfg["user_name"] = name
        if "stream" in data:
            enabled = incoming_bool("stream")
            if enabled is not None:
                self._apply_shared_runtime_setting("ollama.stream", enabled)
                _config.set_value(cfg, "ollama.stream", self.engine.stream)
        if "yolo" in data:
            enabled = incoming_bool("yolo")
            if enabled is not None:
                self.agent.state.update(yolo=enabled)
                cfg["yolo"] = enabled
        if "gateway_port" in data:
            try:
                raw_port = data["gateway_port"]
                if isinstance(raw_port, bool):
                    raise ValueError
                port = int(raw_port)
                if 1 <= port <= 65535:
                    _config.set_value(cfg, "gateway.port", port)
            except (TypeError, ValueError):
                _log.warning("ignoring invalid gateway_port %r", data.get("gateway_port"))
        if "gateway_auto_start" in data:
            enabled = incoming_bool("gateway_auto_start")
            if enabled is not None:
                _config.set_value(cfg, "gateway.auto_start", enabled)
        if "proactive_enabled" in data:
            enabled = incoming_bool("proactive_enabled")
            if enabled is not None:
                _config.set_value(cfg, "proactive.enabled", enabled)
                self.proactive_monitor.enabled = enabled
                if enabled and self.running:
                    self.proactive_monitor.start()
                elif not enabled:
                    self.proactive_monitor.stop()
        if "desktop_notifications" in data:
            enabled = incoming_bool("desktop_notifications")
            if enabled is not None:
                _config.set_value(cfg, "proactive.desktop_notifications", enabled)
                self.proactive_monitor.desktop_notifications = enabled
        if "system_prompt" in data:
            self._user_system_prompt = str(data["system_prompt"]).strip()
            self._rebuild_suffix()
            cfg["system_prompt"] = self._user_system_prompt
        try:
            _config.save(cfg)
        except Exception:
            _log.warning("failed to save config after settings update", exc_info=True)
        result = self.get_settings()
        if errors:
            result["error"] = "; ".join(errors)
        return result

    def set_model(self, model: str) -> dict:
        """Switch the active model instantly (used by the header dropdown).

        Accepts plain model names (Ollama) or 'provider:model' strings
        (e.g. 'openai:gpt-4o', 'anthropic:claude-opus-4-8').
        """
        model = (model or "").strip()
        if not model:
            return {"model": self._model_spec}
        error = self._activate_model(model)
        if error:
            return {"error": error, "model": self._model_spec}
        self.config["model"] = self._model_spec
        save_error = self._persist_command_config()
        if save_error:
            return {"error": save_error, "model": self._model_spec}
        return {"model": self._model_spec}

    def _activate_model(self, model_spec: str) -> str | None:
        """Switch model and provider clients as one atomic logical operation."""
        model_spec = str(model_spec or "").strip()
        provider, model_name = _parse_model(model_spec)
        if not model_name:
            return "model name is required"
        try:
            new_client = _build_client(self.config, provider)
        except RuntimeError as exc:
            return str(exc)
        self.agent.client = new_client
        self.agent.engine.client = new_client
        self.engine.client = new_client
        self.agent.model = model_name
        self.engine.model = model_name
        self._model_spec = model_spec if provider != "ollama" else model_name
        tools_supported = bool(
            _config.get_model_capability(self.config, self._model_spec, "tools_supported", True)
        )
        self.agent.state.update(
            active_model_spec=self._model_spec,
            tools_enabled=tools_supported,
        )
        self.agent.engine.refresh_system_prompt()
        self.engine.refresh_system_prompt()
        return None

    def bootstrap(self) -> dict:
        settings = self.get_settings()
        return {
            "version": __import__("cagentic").__version__,
            "user_name": self.agent.state.user_name,
            "model": self._model_spec,
            "chats": self.list_chats(),
            "current": self.current_chat(),
            "settings": settings,
            "projects": self.list_projects(),
            # Collama-compat keys, kept for existing API clients.
            "workspace": str(self.agent.state.workspace),
            "effort": getattr(self.agent.state, "effort", "medium"),
            "yolo": self.agent.state.yolo,
            "models": settings.get("models") or [],
        }

    # -- Collama-compat surface (alternate paths/shapes) ---------------------

    def set_workspace(self, path: object) -> dict:
        p = self.workspace_policy.validate(path)
        self._default_workspace = p
        self._activate_session_project()
        try:
            self.engine.refresh_system_prompt()
        except Exception:
            _log.warning("failed to refresh prompt after workspace switch", exc_info=True)
        return {"ok": True, "workspace": str(p.resolve())}

    def browse_workspace(self, path: str) -> dict:
        return self.workspace_policy.browse(path)

    def begin_collama_turn(self, project) -> None:
        """Preflight a Collama turn while retaining the turn lock for streaming."""
        if not self._turn_lock.acquire(blocking=False):
            raise WorkspaceError("Cagentic is still working on the previous message.", 409)
        try:
            if project is None and self.session.get("project") is None:
                raise WorkspaceError("a project is required for Collama requests")
            if project is not None:
                self.pin_collama_project(project)
            else:
                self._activate_session_project()
        except Exception:
            self._turn_lock.release()
            raise

    def compact_context(
        self, session_id: str, project, threshold: object = 0.90
    ) -> tuple[dict, int]:
        """Atomically compact one pinned Collama session."""
        if not self._turn_lock.acquire(blocking=False):
            return {"error": "session is busy; try compaction again after the turn finishes"}, 409
        actor_lock = None
        actor_lock_acquired = False
        try:
            try:
                if not isinstance(threshold, (str, int, float)):
                    raise TypeError
                threshold_value = float(threshold)
            except (TypeError, ValueError):
                return {"error": "threshold must be a number between 0 and 1"}, 400
            if not 0 < threshold_value <= 1:
                return {"error": "threshold must be greater than 0 and at most 1"}, 400
            try:
                requested_project = self.validate_project(project)
            except WorkspaceError as exc:
                return {"error": str(exc)}, 400 if exc.status == 404 else exc.status

            is_current = session_id == self.session.get("id")
            target: dict | None
            actor: dict | None = None
            if is_current:
                target = dict(self.session)
                working = [dict(message) for message in self.engine.messages]
            else:
                actor = self._session_actor(session_id)
                if actor is None:
                    # Reserve 404 for clients probing an unsupported endpoint.
                    return {"error": f"session {session_id!r} does not exist"}, 400
                actor_lock = actor["lock"]
                if not actor_lock.acquire(blocking=False):
                    return {"error": "session is busy"}, 409
                actor_lock_acquired = True
                target = actor["session"]
                working = [dict(message) for message in actor["engine"].messages]

            assert target is not None

            pinned = target.get("project")
            try:
                pinned = self.validate_project(pinned) if pinned is not None else None
            except WorkspaceError:
                pinned = target.get("project")
            if pinned != requested_project:
                return {"error": "project does not match the session's pinned project"}, 409

            # Size the budget against the session's own model, not the config
            # default — a session saved under a 200k model must not be
            # compacted as if it were running on an 8k local one.
            target_model = str(target.get("model") or self._model_spec)
            context_window = command_utils.context_window(self.config, model_spec=target_model)
            max_tokens = max(1, int(context_window * threshold_value))
            before = count_messages(working, target_model)
            report = auto_compact(
                working,
                max_tokens=max_tokens,
                keep_recent=6,
                summarize_with_model=lambda older: self._summarize_compaction(older, target_model),
            )
            after = count_messages(working, target_model) if report.triggered else before

            if report.triggered:
                persisted = [
                    message
                    for message in working
                    if message.get("role") != "system"
                    or SUMMARY_MARKER in str(message.get("content") or "")
                ]
                target["messages"] = persisted
                target["project"] = requested_project
                sessions.save(target)
                if is_current:
                    self.session = target
                    self.engine.messages = working
                else:
                    assert actor is not None
                    actor["engine"].messages = working

            return {
                "id": target["id"],
                "messages": self.render_messages(working),
                "before": before,
                "after": after,
                "usage": {"input": after},
                "project": requested_project,
            }, 200
        finally:
            if actor_lock is not None and actor_lock_acquired:
                actor_lock.release()
            self._turn_lock.release()

    def _summarize_compaction(self, older: list[dict], model_spec: str) -> str | None:
        """Create a durable handoff summary using the session's provider."""
        provider, model_name = _parse_model(model_spec)
        client = (
            self.engine.client
            if model_spec == self._model_spec
            else _build_client(self.config, provider)
        )
        transcript = json.dumps(older, ensure_ascii=False, default=str)
        prompt = (
            "Create a compact but complete hidden context summary of the conversation below. "
            "Preserve requirements, decisions and rationale, changed files, exact identifiers, "
            "tool outcomes, errors, attempted fixes, validation results, and unfinished work. "
            "Do not invent facts. Organize it for another model to continue the work without "
            "seeing the omitted messages.\n\n" + transcript
        )
        result = client.chat(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            tools=None,
            options={"temperature": 0.0},
        )
        return str(result.get("content") or "").strip() or None

    def set_effort(self, level: str) -> dict:
        from .engine import EFFORT_LEVELS

        level = (level or "").strip().lower()
        if level not in EFFORT_LEVELS:
            return {"error": f"effort must be one of {', '.join(EFFORT_LEVELS)}"}
        self.agent.state.update(effort=level)
        try:
            self._refresh_shared_prompts()
        except Exception:
            _log.warning("failed to refresh prompt after effort switch", exc_info=True)
        self.config["effort"] = level
        save_error = self._persist_command_config()
        if save_error:
            return {"error": save_error}
        return {"ok": True, "effort": level}

    def list_sessions_compat(self) -> dict:
        """Chats reshaped the way Collama-compat clients expect."""
        return {
            "sessions": [
                {
                    "id": c["id"],
                    "title": c["title"],
                    "createdAt": int(c.get("updated_at") or 0),
                    "messages": int(c.get("turns") or 0),
                    **({"project": c["project"]} if c.get("project") is not None else {}),
                }
                for c in self.list_chats()
            ]
        }

    # GitHub proxy — lets the web UI browse/edit GitHub with the host's token.
    def _github_ctx(self):
        """Duck-typed ToolContext for cagentic.github._request: needs
        .github_token and .insecure_ssl. Falls back to config github.token."""
        import types

        state = self.agent.state
        raw_github = self.config.get("github")
        github = raw_github if isinstance(raw_github, dict) else {}
        token = state.github_token or github.get("token")
        return types.SimpleNamespace(
            github_token=token,
            insecure_ssl=bool(getattr(state, "insecure_ssl", False)),
            default_repository=getattr(state, "default_repository", None),
        )

    def github_user(self) -> dict:
        from .github import _request

        status, body = _request("GET", "/user", self._github_ctx())
        if status != 200 or not isinstance(body, dict):
            return {"error": f"github user lookup failed (HTTP {status})"}
        return {
            "user": {
                "login": body.get("login") or "",
                "name": body.get("name"),
                "avatar": body.get("avatar_url"),
            }
        }

    def github_repos(self) -> dict:
        from .github import _request

        status, body = _request(
            "GET",
            "/user/repos",
            self._github_ctx(),
            params={"sort": "updated", "per_page": 100, "type": "all"},
        )
        if status != 200 or not isinstance(body, list):
            return {"error": f"github repo listing failed (HTTP {status})"}
        return {
            "repos": [
                {
                    "name": r.get("full_name") or "",
                    "private": bool(r.get("private")),
                    "language": r.get("language"),
                    "url": r.get("html_url"),
                }
                for r in body
                if isinstance(r, dict)
            ]
        }

    def github_file_get(self, repo: str, path: str, ref: str | None = None) -> dict:
        import base64
        from urllib.parse import quote

        from .github import _request

        repo = repo or self.agent.state.default_repository or ""
        if not repo or not path:
            return {"error": "repo and path are required"}
        url_path = f"/repos/{repo}/contents/{quote(path)}"
        params = {"ref": ref} if ref else None
        status, body = _request("GET", url_path, self._github_ctx(), params=params)
        if status != 200 or not isinstance(body, dict):
            return {"error": f"github file read failed (HTTP {status})"}
        try:
            content = base64.b64decode(body.get("content") or "").decode("utf-8", errors="replace")
        except Exception:
            content = ""
        return {"content": content, "sha": body.get("sha") or ""}

    def github_file_put(self, data: dict) -> dict:
        import base64
        from urllib.parse import quote

        from .github import _request

        repo = str(data.get("repo") or self.agent.state.default_repository or "")
        path = str(data.get("path") or "")
        if not repo or not path:
            return {"error": "repo and path are required"}
        payload = {
            "message": str(data.get("message") or f"Update {path}"),
            "content": base64.b64encode(str(data.get("content") or "").encode("utf-8")).decode(
                "ascii"
            ),
        }
        if data.get("sha"):
            payload["sha"] = str(data["sha"])
        if data.get("branch"):
            payload["branch"] = str(data["branch"])
        url_path = f"/repos/{repo}/contents/{quote(path)}"
        status, body = _request("PUT", url_path, self._github_ctx(), json=payload)
        if status not in (200, 201):
            return {"error": f"github file write failed (HTTP {status})"}
        return {"ok": True}

    # -- a chat turn --------------------------------------------------------

    def edit_and_resend(self, index: int, message: str, emit) -> None:
        """Truncate history after the *index*-th user message, replace its text
        with *message*, and re-run the turn from that point."""
        # Like delete_message and the other history-mutators, interrupt any
        # in-flight turn first so it can't keep appending into the history we
        # are about to rewrite.
        if not self._interrupt_turn():
            emit("error", {"text": "Cagentic is still working on the previous message."})
            return
        # Find user messages in the engine's message list
        user_indices = _visible_user_indices(self.engine.messages)
        if index < 0 or index >= len(user_indices):
            self._turn_lock.release()
            emit("error", {"text": f"invalid message index {index}"})
            return
        target = user_indices[index]
        # Truncate to before the target user message so submit_message adds it fresh
        self.engine.messages = self.engine.messages[:target]
        self._active_emit = emit
        self._thread_context.emit = emit
        self.engine.model = self.agent.model
        try:
            self._rebuild_suffix()
            for ev in self.engine.submit_message(message):
                emit(ev.kind, ev.data)
        except _ClientGone:
            raise
        except Exception as e:
            emit("error", {"text": f"{type(e).__name__}: {e}"})
        finally:
            self._active_emit = None
            self._thread_context.emit = None
            try:
                self._save_current()
            except Exception:
                _log.warning("failed to save chat after edit_and_resend", exc_info=True)
            self._turn_lock.release()

    def delete_message(self, index: int) -> dict:
        """Delete the *index*-th user message and everything after it."""
        locked = self._interrupt_turn()
        if not locked:
            return {"error": "Cagentic is still working on the previous message."}
        try:
            user_indices = _visible_user_indices(self.engine.messages)
            if index < 0 or index >= len(user_indices):
                return {"error": f"invalid message index {index}"}
            target = user_indices[index]
            # Truncate everything from this user message onward
            self.engine.messages = self.engine.messages[:target]
            self._save_current()
            return self.current_chat()
        finally:
            self._turn_lock.release()

    def run_turn(self, message: str, emit, prelocked: bool = False) -> None:
        if not prelocked and not self._turn_lock.acquire(blocking=False):
            emit("error", {"text": "Cagentic is still working on the previous message."})
            return
        self._active_emit = emit
        self._thread_context.emit = emit
        self.engine.model = self.agent.model
        try:
            # Refresh the user's live goals/calendar context before every turn.
            self._rebuild_suffix()
            message = self._consume_pending_context(message)
            for ev in self.engine.submit_message(message):
                emit(ev.kind, ev.data)
        except _ClientGone:
            raise
        except Exception as e:
            emit("error", {"text": f"{type(e).__name__}: {e}"})
        finally:
            self._active_emit = None
            self._thread_context.emit = None
            try:
                self._save_current()
            except Exception:
                _log.warning("failed to save chat after turn", exc_info=True)
            self._turn_lock.release()

    def _session_actor(self, session_id: str) -> dict | None:
        """Build an isolated engine/runtime for a saved-session API actor."""
        with self._actors_lock:
            cached = self._actors.get(session_id)
            if cached is not None:
                return cached
            data = sessions.load(session_id)
            if data is None:
                return None
            from .engine import QueryEngine
            from .state import AppState

            model_spec = str(data.get("model") or self._model_spec)
            provider, model_name = _parse_model(model_spec)
            try:
                client = _build_client(self.config, provider)
            except RuntimeError:
                return None
            shared = self.agent.state
            shared_groups = set(shared.tool_groups) if shared.tool_groups else set()
            state = AppState(
                workspace=self._default_workspace,
                home=shared.home,
                user_name=shared.user_name,
                github_token=shared.github_token,
                yolo=shared.yolo,
                insecure_ssl=shared.insecure_ssl,
                tools_enabled=shared.tools_enabled,
                effort=shared.effort,
                tool_groups=shared_groups or None,
                mcp=shared.mcp,
                browser=shared.browser,
                active_model_spec=model_spec,
            )
            project = data.get("project")
            context = ""
            if project and project.get("kind") == "gatewayFolder":
                folder = self.workspace_policy.validate(project.get("value"))
                state.update(workspace=folder, workspace_boundary=folder)
                context = f"Pinned gateway folder project: {folder}."
            elif project and project.get("kind") == "repository":
                state.update(default_repository=project.get("value"))
                context = f"Pinned GitHub repository project: {project.get('value')}."
            engine = QueryEngine(
                client=client,
                state=state,
                model=model_name,
                temperature=self.engine.temperature,
                config=self.config,
                permission_resolver=self._resolve,
                stream=True,
            )
            engine.executor.widget_action = self.engine.executor.widget_action
            engine.system_suffix = self._composed_suffix()
            engine.project_context = context
            engine.load_messages(data.get("messages", []))
            engine.session_id = session_id
            actor = {
                "lock": threading.Lock(),
                "engine": engine,
                "session": data,
                "model_spec": model_spec,
            }
            self._actors[session_id] = actor
            return actor

    def validate_session_turn(self, session_id: str, project) -> tuple[dict, int]:
        actor = self._session_actor(session_id)
        if actor is None:
            return {"error": f"session {session_id!r} does not exist"}, 400
        try:
            requested = self.validate_project(project)
        except WorkspaceError as exc:
            return {"error": str(exc)}, exc.status
        if actor["session"].get("project") != requested:
            return {"error": "project does not match the session's pinned project"}, 409
        if actor["lock"].locked():
            return {"error": "session is busy"}, 409
        return {"ok": True}, 200

    def run_session_turn(self, session_id: str, message: str, emit) -> None:
        actor = self._session_actor(session_id)
        if actor is None or not actor["lock"].acquire(blocking=False):
            emit("error", {"text": "session is busy or unavailable"})
            return
        engine = actor["engine"]
        self._thread_context.emit = emit
        try:
            engine.system_suffix = self._composed_suffix()
            for event in engine.submit_message(message):
                emit(event.kind, event.data)
        except _ClientGone:
            raise
        except Exception as exc:
            emit("error", {"text": f"{type(exc).__name__}: {exc}"})
        finally:
            self._thread_context.emit = None
            data = actor["session"]
            data["model"] = actor["model_spec"]
            data["messages"] = [
                item
                for item in engine.messages
                if item.get("role") != "system" or SUMMARY_MARKER in str(item.get("content") or "")
            ]
            sessions.save(data)
            actor["lock"].release()

    # -- slash command handler for web UI -----------------------------------

    def handle_cmd(self, cmd: str, arg1: str = "", arg2: str = "") -> dict:
        """Run a web slash command without letting one failure drop the API connection."""
        try:
            return self._handle_cmd(cmd, arg1, arg2)
        except Exception as exc:
            _log.warning("gateway slash command failed: /%s", cmd, exc_info=True)
            return {"ok": False, "text": f"command failed: {type(exc).__name__}: {exc}"}

    def _handle_cmd(self, cmd: str, arg1: str = "", arg2: str = "") -> dict:
        """Execute a slash command from the web UI and return a result dict.

        GATEWAY_COMMANDS is the authoritative help/completion inventory. The
        subset is deliberate: controls already cover save/delete/edit, while
        interactive terminal commands do not have safe web equivalents.
        """
        from . import config as _cfg
        from .tools import DEFAULT_GROUPS, TOOL_GROUPS, _all_tools

        cmd = cmd.lstrip("/").lower()
        arg1 = arg1.strip()
        arg2 = arg2.strip()
        full_arg = command_utils.full_argument(arg1, arg2)
        agent = self.agent
        cfg = self.config
        canonical_cmd = "help" if cmd == "?" else cmd
        if canonical_cmd in GATEWAY_COMMAND_NAMES:
            if not _GATEWAY_ARGUMENTS[canonical_cmd] and full_arg:
                return {"ok": False, "text": _gateway_usage(canonical_cmd)}
            if canonical_cmd in {"effort", "plan", "stream", "yolo"} and arg2:
                return {"ok": False, "text": _gateway_usage(canonical_cmd)}

        if cmd in ("help", "?"):
            return {"ok": True, "text": _gateway_help_text()}

        if cmd == "new":
            cur = self.new_chat()
            if cur.get("error"):
                return {"ok": False, "text": cur["error"]}
            if full_arg:
                self.session["title"] = full_arg
                cur = self.current_chat()
            return {"ok": True, "text": "new chat started", "current": cur}

        if cmd == "clear":
            if not self._interrupt_turn():
                return {
                    "ok": False,
                    "text": "Cagentic is still working on the previous message.",
                }
            try:
                self.engine.reset()
                self._save_current(allow_empty=True)
                return {
                    "ok": True,
                    "text": "chat cleared",
                    "current": self.current_chat(),
                }
            finally:
                self._turn_lock.release()

        if cmd == "model":
            if not full_arg:
                return {"ok": True, "text": f"current model: {self._model_spec}"}
            result = self.set_model(full_arg)
            if "error" in result:
                return {"ok": False, "text": result["error"]}
            return {
                "ok": True,
                "text": f"model → {result['model']}",
                "model": result["model"],
            }

        if cmd == "models":
            try:
                models = agent.client.list_models()
            except Exception as e:
                return {"ok": False, "text": f"could not list models: {e}"}
            if not models:
                return {
                    "ok": True,
                    "text": "no models reported by the current provider",
                    "models": [],
                }
            lines = ["available models:"]
            for model in models:
                _, bare_name = _parse_model(model)
                active = model == self._model_spec or bare_name == self.engine.model
                lines.append(f"  {model}{'  (active)' if active else ''}")
            return {"ok": True, "text": "\n".join(lines), "models": models}

        if cmd == "diag":
            groups = (
                agent.state.tool_groups if agent.state.tool_groups is not None else DEFAULT_GROUPS
            )
            lines = [f"model:     {self._model_spec}"]
            lines.append(f"name:     {agent.state.user_name or '(not set)'}")
            lines.append(f"workspace: {agent.state.workspace}")
            lines.append(
                f"tools:    {'native' if agent.tools_enabled else 'text-protocol fallback'}"
            )
            lines.append(f"groups:   {', '.join(sorted(groups))}")
            lines.append(f"stream:   {'on' if self.engine.stream else 'off'}")
            from .ollama_client import OllamaClient

            if isinstance(self.engine.client, OllamaClient):
                try:
                    status = self.engine.client.model_vram_status(self.engine.model)
                    if status is None:
                        lines.append("vram:     model not currently loaded")
                    elif status["fully_gpu"]:
                        lines.append(
                            f"vram:     {status['size_vram'] / (1024**3):.1f} GB · fully on GPU ✓"
                        )
                    else:
                        size_gb = status["size"] / (1024**3)
                        cpu_gb = status["cpu_bytes"] / (1024**3)
                        pct = status["cpu_percent"]
                        lines.append(
                            f"vram:     {cpu_gb:.1f}/{size_gb:.1f} GB on CPU "
                            f"({pct:.0f}% offloaded — slow)"
                        )
                except Exception:
                    _log.warning("could not read VRAM status", exc_info=True)
                    lines.append("vram:     (not available)")
            else:
                lines.append(f"provider: {_parse_model(self._model_spec)[0]}")
            mcp_servers = list(command_utils.mcp_server_config(cfg))
            lines.append(
                f"mcp:      {len(mcp_servers)} configured ({', '.join(mcp_servers) or 'none'})"
            )
            return {"ok": True, "text": "\n".join(lines)}

        if cmd == "tools":
            mode = "native" if agent.tools_enabled else "text-protocol fallback"
            known = set(_all_tools())
            active_groups = (
                agent.state.tool_groups if agent.state.tool_groups is not None else DEFAULT_GROUPS
            )
            active_names = {
                name
                for group in active_groups
                for name in TOOL_GROUPS.get(group, ())
                if name in known
            }
            lines = [f"{len(active_names)} of {len(known)} tools active ({mode}):"]
            for group in sorted(active_groups):
                names = sorted(name for name in TOOL_GROUPS.get(group, ()) if name in known)
                if names:
                    lines.append(f"  [{group}] {', '.join(names)}")
            return {"ok": True, "text": "\n".join(lines)}

        if cmd == "groups":
            active = (
                agent.state.tool_groups if agent.state.tool_groups is not None else DEFAULT_GROUPS
            )
            if not arg1:
                lines = ["tool groups (✓ = sent to the model):"]
                for g, names in sorted(TOOL_GROUPS.items()):
                    mark = "✓" if g in active else "✗"
                    lines.append(f"  {mark} {g} ({len(names)} tools)")
                return {"ok": True, "text": "\n".join(lines)}
            action = arg1.lower()
            group = arg2.lower()
            if action == "enable" and group:
                if group not in TOOL_GROUPS:
                    return {"ok": False, "text": f"unknown tool group '{arg2}'"}
                groups = set(active)
                groups.add(group)
                agent.state.update(tool_groups=groups)
                _cfg.set_value(cfg, "tool_groups", sorted(groups))
                save_error = self._persist_command_config()
                if save_error:
                    return {"ok": False, "text": save_error}
                self._refresh_shared_prompts()
                return {
                    "ok": True,
                    "text": f"enabled '{group}' — {len(groups)} group(s) active",
                }
            if action == "disable" and group:
                if group not in TOOL_GROUPS:
                    return {"ok": False, "text": f"unknown tool group '{arg2}'"}
                groups = set(active)
                groups.discard(group)
                agent.state.update(tool_groups=groups)
                _cfg.set_value(cfg, "tool_groups", sorted(groups))
                save_error = self._persist_command_config()
                if save_error:
                    return {"ok": False, "text": save_error}
                self._refresh_shared_prompts()
                return {
                    "ok": True,
                    "text": f"disabled '{group}' — {len(groups)} group(s) active",
                }
            return {
                "ok": False,
                "text": "usage: /groups  |  /groups enable <name>  |  /groups disable <name>",
            }

        if cmd == "yolo":
            enabled = command_utils.switch_value(arg1, agent.state.yolo)
            if enabled is None:
                return {"ok": False, "text": "usage: /yolo [on|off]"}
            agent.state.update(yolo=enabled)
            cfg["yolo"] = agent.state.yolo
            save_error = self._persist_command_config()
            if save_error:
                return {"ok": False, "text": save_error}
            return {
                "ok": True,
                "text": f"yolo mode: {'ON (auto-approve)' if agent.state.yolo else 'OFF (ask every time)'}",
            }

        if cmd == "effort":
            if not arg1:
                return {
                    "ok": True,
                    "text": f"effort: {getattr(agent.state, 'effort', 'medium')}",
                }
            res = self.set_effort(arg1)
            if res.get("error"):
                return {"ok": False, "text": res["error"]}
            return {"ok": True, "text": f"effort: {res['effort']}"}

        if cmd == "plan":
            enabled = command_utils.switch_value(arg1, agent.state.plan_mode)
            if enabled is None:
                return {"ok": False, "text": "usage: /plan [on|off]"}
            agent.state.update(plan_mode=enabled)
            self._refresh_shared_prompts()
            return {
                "ok": True,
                "text": f"plan mode: {'ON (read-only)' if agent.state.plan_mode else 'OFF'}",
            }

        if cmd == "stream":
            enabled = command_utils.switch_value(arg1, self.engine.stream)
            if enabled is None:
                return {"ok": False, "text": "usage: /stream [on|off]"}
            self._apply_shared_runtime_setting("ollama.stream", enabled)
            _cfg.set_value(cfg, "ollama.stream", self.engine.stream)
            save_error = self._persist_command_config()
            if save_error:
                return {"ok": False, "text": save_error}
            return {
                "ok": True,
                "text": f"streaming: {'on' if self.engine.stream else 'off'} (saved)",
            }

        if cmd == "name":
            if not full_arg:
                return {
                    "ok": True,
                    "text": f"your name: {agent.state.user_name or '(not set)'}",
                }
            self._apply_shared_runtime_setting("user_name", full_arg)
            _cfg.set_value(cfg, "user_name", full_arg)
            save_error = self._persist_command_config()
            if save_error:
                return {"ok": False, "text": save_error}
            return {"ok": True, "text": f"name set to: {full_arg}"}

        if cmd == "host":
            from .ollama_client import OllamaClient

            if not isinstance(agent.client, OllamaClient):
                return {
                    "ok": False,
                    "text": "/host applies to Ollama only; switch to a local model first",
                }
            if not full_arg:
                return {"ok": True, "text": f"ollama host: {agent.client.host}"}
            try:
                agent.client.host = full_arg
                cfg["host"] = agent.client.host
                save_error = self._persist_command_config()
                if save_error:
                    return {"ok": False, "text": save_error}
                return {"ok": True, "text": f"host → {agent.client.host}"}
            except Exception as e:
                return {"ok": False, "text": f"error: {e}"}

        if cmd == "notes":
            from . import notes as _notes

            all_notes = _notes.list_all()
            if not all_notes:
                return {"ok": True, "text": "no notes"}
            lines = [note.short() for note in all_notes[:40]]
            if len(all_notes) > 40:
                lines.append(f"…and {len(all_notes) - 40} more")
            return {"ok": True, "text": "\n".join(lines)}

        if cmd == "mcp":
            from .mcp_client import MCPManager

            if agent.state.mcp is None:
                agent.state.update(mcp=MCPManager(cfg))
            manager = agent.state.mcp
            if not full_arg:
                names = manager.names()
                if not names:
                    return {"ok": True, "text": "no MCP servers configured"}
                return {
                    "ok": True,
                    "text": "MCP servers:\n" + "\n".join(f"  {name}" for name in names),
                }
            try:
                tools = manager.list_tools(full_arg)
            except Exception as exc:
                return {"ok": False, "text": str(exc)}
            if not tools:
                return {"ok": True, "text": f"server '{full_arg}' exposes no tools"}
            lines = [f"{full_arg}: {len(tools)} tool(s)"]
            for tool in tools[:40]:
                name = tool.get("name", "?")
                description = str(tool.get("description") or "").splitlines()[0][:140]
                lines.append(f"  {name} — {description}")
            return {"ok": True, "text": "\n".join(lines)}

        if cmd == "config":
            # Redact ALL secret-bearing values (github token, provider api_keys,
            # SMTP/email password, MCP env secrets) before exposing the config.
            safe = _cfg.redact_secrets(cfg)
            return {"ok": True, "text": json.dumps(safe, indent=2)}

        if cmd == "set":
            if not arg1 or not arg2:
                return {"ok": False, "text": "usage: /set <key> <value>"}
            key_error = command_utils.validate_config_key(arg1)
            if key_error:
                return {"ok": False, "text": key_error}
            value = command_utils.parse_config_value(arg2)
            value_error = command_utils.validate_config_value(arg1, value)
            if value_error:
                return {"ok": False, "text": value_error}
            if arg1 == "tool_groups" and value is not None:
                unknown = sorted(set(value) - set(TOOL_GROUPS))
                if unknown:
                    return {"ok": False, "text": f"unknown tool group(s): {', '.join(unknown)}"}
            _cfg.set_value(cfg, arg1, value)
            save_error = self._persist_command_config()
            if save_error:
                return {"ok": False, "text": save_error}
            applied = self._apply_shared_runtime_setting(arg1, value)
            shown = "••••" if _cfg.is_secret_key(arg1) else value
            suffix = " (applied live)" if applied else " (saved for next launch)"
            return {"ok": True, "text": f"set {arg1} = {shown}{suffix}"}

        if cmd == "retry":
            user_indices = _visible_user_indices(self.engine.messages)
            if not user_indices:
                return {"ok": False, "text": "nothing to retry yet"}
            visible_index = len(user_indices) - 1
            content = str(self.engine.messages[user_indices[-1]].get("content") or "").strip()
            return {
                "ok": True,
                "text": "retrying the most recent message",
                "action": {"type": "retry", "index": visible_index, "message": content},
            }

        return {
            "ok": False,
            "text": f"unknown command: /{cmd}. Type /help for available commands.",
        }


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

    # Conservative CSP. The UI is built from inline <script>/<style>, so we
    # keep 'unsafe-inline' for those, but lock everything else down so a
    # successful HTML/JS injection can't exfiltrate to a third-party origin,
    # load plugins, or frame us.
    _CSP = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'"
    )

    def _gw(self) -> Gateway:
        return self.server.gateway  # type: ignore[attr-defined]

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", self._CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _host_is_local(self) -> bool:
        """Reject requests whose Host header isn't loopback (anti DNS-rebind)."""
        host = (self.headers.get("Host") or "").strip()
        # Strip an optional :port (and handle bracketed IPv6 literals).
        if host.startswith("["):
            hostname = host[1:].split("]", 1)[0]
        else:
            hostname = host.rsplit(":", 1)[0] if ":" in host else host
        return hostname.lower() in _LOCAL_HOSTS

    def _origin_ok(self) -> bool:
        """If an Origin header is present, its host must be a localhost origin.

        A missing Origin (normal for top-level navigations and same-origin
        GETs) is allowed; the token + Host check still gate /api/*.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        return (parsed.hostname or "").lower() in _LOCAL_HOSTS

    def _authorized(self) -> bool:
        """Gate every /api/* request against DNS-rebinding, cross-origin
        (CSRF), and the per-process secret token. Returns True if allowed."""
        # In LAN mode (explicit opt-in) the Host header is the PC's LAN
        # address, so the loopback-Host check can't apply — the secret token
        # below is the gate. Loopback mode keeps the anti-rebinding check.
        if not self._gw().lan and not self._host_is_local():
            _log.warning("rejected request with non-local Host header")
            return False
        if not self._origin_ok():
            _log.warning("rejected request with cross-origin Origin header")
            return False
        expected = self._gw().token
        # fetch() calls send the token via header; the SSE/EventSource stream
        # (which can't set headers) sends it via ?token= query param.
        supplied = self.headers.get("X-Cagentic-Token")
        if not supplied:
            qs = parse_qs(urlparse(self.path).query)
            vals = qs.get("token")
            supplied = vals[0] if vals else None
        if not supplied or not secrets.compare_digest(str(supplied), str(expected)):
            _log.warning("rejected /api request with missing/invalid token")
            return False
        return True

    def _deny(self) -> None:
        self._send(b"forbidden", "text/plain", status=403)

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # Client closed mid-response. Includes Windows' ConnectionAbortedError.
            pass

    def _json(self, obj, status: int = 200) -> None:
        self._send(json.dumps(obj).encode("utf-8"), "application/json", status)

    # Cap on a normal JSON request body. Uploads carry base64 (≈4/3 overhead)
    # plus the JSON envelope, so they get their own, larger ceiling — but both
    # are bounded, because an unbounded read is a trivial memory DoS.
    _MAX_JSON_BODY = 4 * 1024 * 1024
    _MAX_UPLOAD_BODY = MAX_UPLOAD_BYTES * 4 // 3 + 64 * 1024

    def _body(self, max_bytes: int | None = None) -> dict:
        limit = max_bytes or self._MAX_JSON_BODY
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            return {}
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        # Static page assets bootstrap the UI (and carry the token to the
        # browser), so they aren't token-gated — but they must still be a
        # same-host request to thwart DNS-rebinding.
        if path in ("/", "/app.css", "/app.js"):
            if not self._host_is_local() or not self._origin_ok():
                self._deny()
                return
            if path == "/":
                # Inject the per-session token so the page's fetch()/SSE can
                # authenticate. Done at serve time since _HTML is a constant.
                token = self._gw().token
                html = _HTML.replace("{{CAGENTIC_TOKEN}}", token)
                self._send(html.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/app.css":
                self._send(_CSS.encode("utf-8"), "text/css; charset=utf-8")
            else:
                self._send(_JS.encode("utf-8"), "application/javascript; charset=utf-8")
            return
        # Everything under /api/* requires auth.
        if path.startswith("/api/"):
            if not self._authorized():
                self._deny()
                return
        if path == "/api/bootstrap":
            self._json(self._gw().bootstrap())
        elif path == "/api/settings":
            self._json(self._gw().get_settings())
        elif path == "/api/projects":
            self._json(self._gw().list_projects())
        elif path == "/api/sessions":
            # Collama-compat: the alternate session list shape.
            self._json(self._gw().list_sessions_compat())
        elif path == "/api/workspace/browse":
            qs = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            try:
                self._json(self._gw().browse_workspace((qs.get("path") or [""])[0]))
            except WorkspaceError as exc:
                self._json({"error": str(exc)}, status=exc.status)
        elif path == "/api/github/user":
            self._json(self._gw().github_user())
        elif path == "/api/github/repos":
            self._json(self._gw().github_repos())
        elif path == "/api/github/file":
            qs = parse_qs(urlparse(self.path).query)
            ref = (qs.get("ref") or [""])[0] or None
            self._json(
                self._gw().github_file_get(
                    (qs.get("repo") or [""])[0],
                    (qs.get("path") or [""])[0],
                    ref,
                )
            )
        else:
            self._send(b"not found", "text/plain", status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        # Every POST route lives under /api/* — require auth (token + Host +
        # Origin) before doing anything. This blocks CSRF and unauthenticated
        # access to the shell/files/browser the assistant can drive.
        if not self._authorized():
            self._deny()
            return
        gw = self._gw()
        if path == "/api/abort":
            gw.abort_generation()
            self._json({"ok": True})
            return
        if path == "/api/context/compact":
            b = self._body()
            if b.get("client") != "collama":
                self._json({"error": "client must be collama"}, status=400)
                return
            result, status = gw.compact_context(
                str(b.get("id") or ""),
                b.get("project"),
                b.get("threshold", 0.90),
            )
            self._json(result, status=status)
            return
        if path == "/api/chat":
            b = self._body()
            # "message" is the web UI's key; "prompt" is the Collama-compat
            # alias kept for existing clients. Accept either.
            message = str(b.get("message") or b.get("prompt") or "").strip()
            prelocked = False
            session_id = str(b.get("id") or "")
            if b.get("client") == "collama":
                if session_id and session_id != gw.session.get("id"):
                    result, status = gw.validate_session_turn(session_id, b.get("project"))
                    if status != 200:
                        self._json(result, status=status)
                        return
                else:
                    try:
                        gw.begin_collama_turn(b.get("project"))
                        prelocked = True
                    except WorkspaceError as exc:
                        self._json({"error": str(exc)}, status=exc.status)
                        return
            self._stream_chat(
                message,
                prelocked=prelocked,
                session_id=session_id or None,
            )
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
            b = self._body()
            gw.deliver_permission(
                str(b.get("answer", "no")),
                str(b["id"]) if b.get("id") else None,
                str(b["rule"]) if b.get("rule") else None,
            )
            self._json({"ok": True})
            return
        # Collama-compat session routes — same chats, different paths and
        # response shapes.
        if path == "/api/session/new":
            b = self._body()
            try:
                project = (
                    gw.validate_project(b.get("project")) if b.get("client") == "collama" else None
                )
            except WorkspaceError as exc:
                self._json({"error": str(exc)}, status=exc.status)
                return
            if b.get("client") == "collama" and project is None:
                self._json({"error": "a project is required for Collama requests"}, status=400)
                return
            current = gw.new_chat(project)
            self._json(
                {
                    "ok": True,
                    "id": gw.session["id"],
                    "session": current,
                    "current": current,
                }
            )
            return
        if path == "/api/session/load":
            b = self._body()
            self._json(gw.load_chat(str(b.get("id", ""))))
            return
        if path == "/api/session/delete":
            b = self._body()
            gw.delete_chat(str(b.get("id", "")))
            self._json({"ok": True})
            return
        if path == "/api/workspace":
            b = self._body()
            try:
                self._json(gw.set_workspace(b.get("path")))
            except WorkspaceError as exc:
                self._json({"error": str(exc)}, status=exc.status)
            return
        if path == "/api/effort":
            b = self._body()
            res = gw.set_effort(str(b.get("effort", "")))
            self._json(res, status=200 if res.get("ok") else 400)
            return
        if path == "/api/github/file":
            self._json(gw.github_file_put(self._body()))
            return
        if path == "/api/chats/new":
            b = self._body()
            try:
                project = (
                    gw.validate_project(b.get("project")) if b.get("client") == "collama" else None
                )
            except WorkspaceError as exc:
                self._json({"error": str(exc)}, status=exc.status)
                return
            if b.get("client") == "collama" and project is None:
                self._json({"error": "a project is required for Collama requests"}, status=400)
                return
            self._json({"current": gw.new_chat(project), "chats": gw.list_chats()})
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
        if path == "/api/chats/autotitle":
            self._json(gw.autotitle_chat())
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
            self._json(
                gw.add_chat_to_project(str(b.get("project_id", "")), str(b.get("chat_id", "")))
            )
            return
        if path == "/api/projects/remove_chat":
            b = self._body()
            self._json(
                gw.remove_chat_from_project(str(b.get("project_id", "")), str(b.get("chat_id", "")))
            )
            return
        if path == "/api/projects/config":
            b = self._body()
            self._json(
                gw.update_project_config(
                    str(b.get("id", "")),
                    b.get("system_prompt", ""),
                    b.get("context", ""),
                )
            )
            return
        if path == "/api/context/page":
            b = self._body()
            self._json(gw.add_page_context(str(b.get("text") or "")))
            return
        if path == "/api/chats/search":
            b = self._body()
            self._json(gw.search_chats(str(b.get("query") or "")))
            return
        if path == "/api/upload":
            b = self._body(max_bytes=self._MAX_UPLOAD_BODY)
            if not b:
                self._json({"error": "attachment too large"}, status=413)
                return
            result, status = gw.save_upload(str(b.get("name") or ""), str(b.get("data") or ""))
            self._json(result, status=status)
            return
        if path == "/api/cmd":
            b = self._body()
            self._json(
                gw.handle_cmd(
                    str(b.get("cmd", "")),
                    str(b.get("arg1", "")),
                    str(b.get("arg2", "")),
                )
            )
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

    def _stream_chat(
        self,
        message: str,
        prelocked: bool = False,
        session_id: str | None = None,
    ) -> None:
        emit = self._begin_sse()
        if not message:
            try:
                emit("error", {"text": "empty message"})
            except _ClientGone:
                pass
            if prelocked:
                self._gw()._turn_lock.release()
            return
        try:
            if session_id and session_id != self._gw().session.get("id"):
                self._gw().run_session_turn(session_id, message, emit)
            else:
                self._gw().run_turn(message, emit, prelocked=prelocked)
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
def _asset_text(name: str) -> str:
    """Load packaged gateway assets once; package-data keeps wheel installs working."""
    from importlib.resources import files

    return files("cagentic.gateway_assets").joinpath(name).read_text(encoding="utf-8")


_HTML = _asset_text("index.html")
_CSS = _asset_text("app.css")
_JS = _asset_text("app.js")
