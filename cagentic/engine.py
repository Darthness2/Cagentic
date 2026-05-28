"""QueryEngine — the core agent loop, decoupled from any UI.

Adapted from Collama for a personal-assistant context: same event-stream
architecture, different system prompt, no teams/coordinator.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal

from . import ui
from .background import BackgroundExecutor
from .config import set_value
from .ollama_client import OllamaClient, OllamaError, ToolsUnsupportedError
from .permissions import CONCURRENT_SAFE, Resolver, auto_deny_resolver, can_use_tool
from .services.compact import BOUNDARY_MARKER, manage_context
from .services.transcript import record as record_transcript
from .state import AppState
from .tasks import TaskGraph, TaskKind, new_id
from .tools import ToolContext, all_tool_schemas, dispatch


MAX_TOOL_ITERATIONS = 1000
# How many identical tool-call/result repeats to tolerate before steering
# the model. Was 2 — far too eager: any innocent duplicate tripped a steer.
# At 4 a tool can repeat three times before we intervene, which still
# catches genuine infinite loops without crying wolf on normal retries.
LOOP_THRESHOLD = 4
COMPACT_TOKENS = 12000
COMPACT_KEEP_RECENT = 12


EventKind = Literal[
    "system", "user", "thinking", "plan", "narration", "delta", "assistant",
    "tool_call", "tool_denied", "tool_result",
    "info", "warn", "error", "compact", "done",
]


@dataclass
class Message:
    kind: EventKind
    data: dict = field(default_factory=dict)
    task_id: str | None = None


_TOOL_TAG_RX = re.compile(r"<tool>\s*(\{.*?\})\s*</tool>", re.DOTALL)
_PLAN_RX = re.compile(r"<plan>(.*?)</plan>", re.DOTALL | re.IGNORECASE)
_THINK_RX = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_FENCE_RX = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
_DS_BAR = r"[｜|]"
_DEEPSEEK_CALL_RX = re.compile(
    rf"<{_DS_BAR}tool▁call▁begin{_DS_BAR}>"
    rf"\s*(?:function\s*<{_DS_BAR}tool▁sep{_DS_BAR}>\s*)?"
    rf"([A-Za-z_][\w-]*)"
    rf".*?```(?:json)?\s*(\{{.*?\}})\s*```"
    rf".*?<{_DS_BAR}tool▁call▁end{_DS_BAR}>",
    re.DOTALL,
)
_DEEPSEEK_OUTPUTS_BLOCK_RX = re.compile(
    rf"<{_DS_BAR}tool▁outputs?▁begin{_DS_BAR}>.*?<{_DS_BAR}tool▁outputs?▁end{_DS_BAR}>",
    re.DOTALL,
)
_DEEPSEEK_STRAY_TOKEN_RX = re.compile(
    rf"<{_DS_BAR}tool▁outputs?▁(?:begin|end){_DS_BAR}>"
)


def _looks_like_call(obj):
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("tool") or obj.get("function")
    if not isinstance(name, str) or not name:
        return None
    args = obj.get("arguments")
    if args is None:
        args = obj.get("args") or obj.get("parameters") or {}
    if not isinstance(args, dict):
        return None
    return name, args


def _try_bare_json(text: str):
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, end = decoder.raw_decode(text[i:])
        except (json.JSONDecodeError, ValueError):
            continue
        call = _looks_like_call(obj)
        if not call:
            continue
        name, args = call
        return name, args, i, i + end
    return None


def _extract_tool_call(content: str):
    m = _TOOL_TAG_RX.search(content)
    if m:
        try:
            payload = json.loads(m.group(1))
            call = _looks_like_call(payload)
            if call:
                return call[0], call[1], m.start(), m.end()
        except json.JSONDecodeError:
            pass
    m = _DEEPSEEK_CALL_RX.search(content)
    if m:
        try:
            args = json.loads(m.group(2))
            if isinstance(args, dict):
                return m.group(1), args, m.start(), m.end()
        except json.JSONDecodeError:
            pass
    for m in _FENCE_RX.finditer(content):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        call = _looks_like_call(obj)
        if call:
            return call[0], call[1], m.start(), m.end()
    bare = _try_bare_json(content)
    if bare:
        return bare
    return None


def _extract_plan(text: str) -> tuple[list[str], str]:
    m = _PLAN_RX.search(text)
    if not m:
        return [], text
    body = m.group(1).strip()
    steps: list[str] = []
    for raw in body.splitlines():
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r"^(?:[-*•]|\d+[.)])\s*", "", s)
        if s:
            steps.append(s)
    cleaned = (text[:m.start()] + text[m.end():]).strip()
    return steps, cleaned


def _extract_thinking(text: str) -> tuple[list[str], str]:
    blocks: list[str] = []
    def _consume(m):
        blocks.append(m.group(1).strip())
        return ""
    cleaned = _THINK_RX.sub(_consume, text).strip()
    return blocks, cleaned


def _strip_fakes(text: str) -> tuple[bool, str]:
    had = bool(_DEEPSEEK_OUTPUTS_BLOCK_RX.search(text) or _DEEPSEEK_STRAY_TOKEN_RX.search(text))
    out = _DEEPSEEK_OUTPUTS_BLOCK_RX.sub("", text)
    out = _DEEPSEEK_STRAY_TOKEN_RX.sub("", out)
    return had, out


def _summarize_args(name: str, args: dict) -> str:
    if name in ("read_file", "write_file", "edit_file"):
        return str(args.get("path", ""))
    if name == "list_dir":
        return str(args.get("path", "."))
    if name == "grep":
        return f"/{args.get('pattern', '')}/  in {args.get('path', '.')}"
    if name in ("run_bash", "bash_async"):
        cmd = str(args.get("command", ""))
        return cmd if len(cmd) < 80 else cmd[:77] + "…"
    if name in ("note_write", "note_get", "note_delete"):
        return str(args.get("name", ""))
    if name == "note_search":
        return str(args.get("query", ""))
    if name == "reminder_add":
        text = str(args.get("text", ""))
        when = args.get("when")
        return text[:60] + (f"  @ {when}" if when else "")
    if name == "mcp_call":
        return f"{args.get('server', '?')}/{args.get('tool', '?')}"
    if name in ("web_fetch", "web_search"):
        return str(args.get("url") or args.get("query") or "")
    if name in ("browser_open", "browser_navigate"):
        return str(args.get("url", ""))
    if name == "browser_click":
        return str(args.get("selector") or args.get("text") or "")
    if name == "browser_fill":
        return str(args.get("selector", ""))
    if name == "browser_eval":
        code = str(args.get("code", ""))
        return code if len(code) < 60 else code[:57] + "…"
    if name == "browser_scroll":
        return str(args.get("selector") or
                   (f"y={args['y']}" if args.get("y") is not None else "")
                   or args.get("to") or "bottom")
    if name == "browser_click_at":
        return f"({args.get('x')}, {args.get('y')})"
    return ""


def _poke_browser(state, *, model: str | None = None, activity: str | None = None) -> None:
    """Push the assistant's live status to the browser bridge (if one is up)
    so the Chrome extension popup can show what Cagentic is doing."""
    bridge = getattr(state, "browser", None)
    if bridge is not None and hasattr(bridge, "set_status"):
        try:
            bridge.set_status(model=model, activity=activity)
        except Exception:
            pass


def _load_memory(workspace: Path, home: Path) -> str:
    """Load any CAGENTIC.md / AGENTS.md memory files."""
    seen: set[Path] = set()
    chunks: list[str] = []
    cur = workspace.resolve()
    home = home.resolve()
    while True:
        for name in ("CAGENTIC.md", "AGENTS.md", ".cagentic.md"):
            p = cur / name
            if p.exists() and p not in seen:
                seen.add(p)
                try:
                    text = p.read_text(errors="replace")
                except OSError:
                    continue
                chunks.append(f"--- {p} ---\n{text.strip()}")
        if cur == cur.parent or cur == home.parent:
            break
        cur = cur.parent
    return "\n\n".join(chunks)


def fetch_system_prompt_parts(state: AppState) -> str:
    """Assemble the personal-assistant system prompt."""
    workspace = state.workspace
    home = state.home
    user_name = state.user_name or "the user"
    tools_enabled = state.tools_enabled

    base = f"""You are Cagentic — {user_name}'s personal assistant, running entirely on their own machine via Ollama. Nothing leaves the device unless {user_name} asks you to reach out.

Who you are: warm, attentive, and unflappable. You're the friend who keeps the calendar straight and remembers the small things. You have a light, dry sense of humor but you never make the user feel managed or lectured. You're genuinely on their side.

You help with anything a personal assistant would: looking things up on the web, keeping notes and reminders, drafting messages, working with files, opening apps, planning the day, and reaching external services (Notion, Google Drive, Slack, …) through MCP.

Environment:
- User home dir:  {home}
- Working dir:    {workspace}
- {user_name} is talking to you in a terminal. Be conversational but concise.
- Address them as "{user_name}" (or "you" — both fine). Use their name lightly,
  the way a person would — not in every sentence.

Tone & style:
- Friendly, calm, helpful. Plain language. No corporate hedging.
- When you don't know something, say so and offer to look it up.
- When the user asks for a quick thing, do it — don't pad with disclaimers.
- For multi-step requests, briefly outline what you'll do, then do it.
- Don't refuse benign personal tasks; they're exactly what you're for.

Tools you have:
- **Knowledge base** (note_write, note_get, note_list, note_search, note_delete):
  use these to REMEMBER things across sessions. When the user mentions a fact
  worth keeping ("my partner's name is X", "I'm allergic to Y", "my work hours
  are 9-5"), save it to a sensible note (e.g. note_write name="profile" body="..."
  append=true). Read notes proactively when context suggests they're relevant
  ("what should I make for dinner?" → note_get "food-preferences").
- **Reminders** (reminder_add, reminder_list, reminder_done, reminder_delete,
  reminder_update): persistent to-dos. Always use reminder_add when the user
  says "remind me to X" — never let it live only in chat history.
- **Web** (web_search, web_fetch): for anything current. Search first, fetch
  the most promising result. Pass text_only=true to web_fetch when reading
  articles to strip noise.
- **MCP** (mcp_list_servers, mcp_list_tools, mcp_call, mcp_list_resources,
  mcp_read_resource): bridges to Notion, Google Drive, Slack, etc. If a
  request needs one of those, run mcp_list_servers first to see what's
  configured, then mcp_list_tools to discover the right call, then mcp_call.
- **Browser** (browser_status, browser_tabs, browser_read, browser_open,
  browser_navigate, browser_click, browser_fill, browser_scroll,
  browser_screenshot, browser_click_at, browser_eval, browser_close):
  control the user's Chrome browser through the companion extension. Call
  browser_status FIRST — if it's not connected, tell the user to set up the
  extension (mention the /browser command) and stop. Once connected, use
  browser_read to see a page, browser_tabs to list tabs, browser_open /
  browser_navigate to go places, and browser_click / browser_fill to act.
  If a CSS-selector click fails (CSP-blocked page, Shadow-DOM widget,
  framework that ignores .click()), fall back to browser_screenshot —
  vision-capable models will see the page — then browser_click_at with
  the (x, y) coordinates of the target.
- **Files** (read_file, write_file, edit_file, list_dir, grep, glob): edit
  any file on disk. Use absolute paths or set_workspace into the right dir.
  read_file also pulls the text out of PDF and Word (.docx) documents — so
  you can read résumés, contracts, letters, and reports directly. Just call
  read_file on the .pdf / .docx path; don't ask the user to convert it.
- **Shell** (run_bash): for opening apps, running scripts, etc. Each call
  asks the user to approve.
- **Background** (bash_async, task_status, task_wait): for slow commands.

Working principles:
- ACT, don't narrate. If the user says "remind me to call mom at 5", call
  reminder_add immediately. Don't write paragraphs about how you're going to.
- One step at a time: call a tool, look at the result, decide the next move.
- Wrap private reasoning in <think>...</think>. The user sees it dimmed.
- For complex multi-step requests, emit a <plan>step\\nstep\\nstep</plan>
  ONCE at the start of your first reply, then just execute.
- Don't re-read a note or file you just read this turn. The content is still
  in your context.
- When you finish, give a short final summary — what you did and any
  follow-up the user might want.

If tools fail or you genuinely can't help:
- Say so clearly, one sentence. Suggest a workaround if you have one.
- For ambiguous requests, ask ONE specific clarifying question rather than
  guessing.
"""
    if not tools_enabled:
        base += """
=== TEXT TOOL PROTOCOL (your runtime does NOT support native tool calls) ===
Call a tool by emitting ONE call and STOPPING. Any of these works:

  <tool>{"name":"TOOL_NAME","arguments":{...}}</tool>
  ```json
  {"name":"TOOL_NAME","arguments":{...}}
  ```

After the call, STOP. The harness emits tool outputs; you must NOT.
"""

    # Personal-context memory: CAGENTIC.md / AGENTS.md in workspace + parents,
    # plus any persistent profile / preferences notes the user has stashed.
    memory = _load_memory(workspace, home)
    if memory:
        base += "\n\n=== PROJECT / WORKSPACE MEMORY ===\n" + memory

    # Pull in the user's profile note if present — gives the model an instant
    # picture of who it's talking to ("Sam, vegetarian, lives in Seattle…").
    try:
        from . import notes as _notes
        for profile_name in ("profile", "about-me", "me"):
            n = _notes.get(profile_name)
            if n:
                base += f"\n\n=== USER PROFILE (note: {n.name}) ===\n{n.body.strip()}"
                break
    except Exception:
        pass

    return base


_MENTION_RX = re.compile(r"(?<![\w/.])@([A-Za-z0-9_./\\-]+(?::\d+(?:-\d+)?)?)")


def _resolve_mention(token: str, workspace: Path, home: Path):
    line_start: int | None = None
    line_end: int | None = None
    path_part = token
    if ":" in token:
        head, tail = token.rsplit(":", 1)
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", tail)
        if m:
            path_part = head
            line_start = int(m.group(1))
            line_end = int(m.group(2)) if m.group(2) else line_start
    import os as _os
    expanded = _os.path.expanduser(_os.path.expandvars(path_part))
    p = Path(expanded)
    if not p.is_absolute():
        p = workspace / p
    if p.exists() and p.is_file():
        return p, line_start, line_end
    return None


def process_user_input(raw: str, workspace: Path | None = None, home: Path | None = None) -> dict:
    if not workspace or "@" not in raw:
        return {"role": "user", "content": raw}
    attachments: list[str] = []
    seen: set[Path] = set()
    for m in _MENTION_RX.finditer(raw):
        resolved = _resolve_mention(m.group(1), workspace, home or Path.home())
        if resolved is None:
            continue
        p, s, e = resolved
        if p in seen:
            continue
        seen.add(p)
        # @-mentioning a PDF or .docx inlines its extracted text, same as
        # any plain file — so "summarize @report.pdf" works in one shot.
        try:
            from . import documents as _documents
            if _documents.is_document(p):
                text = _documents.extract_text(p)
            else:
                text = p.read_text(errors="replace")
        except Exception as exc:
            attachments.append(f"--- @{p} (read failed: {exc}) ---")
            continue
        lines = text.splitlines()
        lo = (s - 1) if s else 0
        hi = e if e else len(lines)
        lo = max(0, lo)
        hi = min(len(lines), hi)
        selected = lines[lo:hi]
        numbered = "\n".join(f"{lo + i + 1:>5}  {ln}" for i, ln in enumerate(selected))
        range_hdr = f":{s}-{e}" if s else ""
        attachments.append(f"--- @{p}{range_hdr}  ({len(lines)} lines total) ---\n{numbered}")
    if not attachments:
        return {"role": "user", "content": raw}
    body = raw + "\n\n" + "\n\n".join(attachments)
    return {"role": "user", "content": body}


def normalize_messages_for_api(messages: list[dict]) -> list[dict]:
    # 'images' carries inline screenshots through to vision-capable models.
    keep_keys = {"role", "content", "tool_calls", "name", "tool_call_id", "images"}
    out: list[dict] = []
    for m in messages:
        cleaned = {k: v for k, v in m.items() if k in keep_keys}
        if cleaned.get("role") == "system" and BOUNDARY_MARKER in (cleaned.get("content") or ""):
            continue
        out.append(cleaned)
    return out


class StreamingToolExecutor:
    def __init__(
        self,
        state: AppState,
        resolver: Resolver,
        max_workers: int = 4,
        engine: object | None = None,
        background: object | None = None,
        tasks: object | None = None,
    ) -> None:
        self.state = state
        self.resolver = resolver
        self.max_workers = max_workers
        self.engine = engine
        self.background = background
        self.tasks = tasks

    def execute(self, calls):
        if not calls:
            return
        concurrent_calls = [c for c in calls if c[0] in CONCURRENT_SAFE]
        serial_calls = [c for c in calls if c[0] not in CONCURRENT_SAFE]

        if len(concurrent_calls) > 1:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = [pool.submit(self._run_one_collect, c) for c in concurrent_calls]
                for fut in as_completed(futures):
                    yield from fut.result()
        else:
            for c in concurrent_calls:
                yield from self._run_one(c)

        for c in serial_calls:
            yield from self._run_one(c)

    def _run_one(self, call):
        name, args, role = call
        tid = new_id(TaskKind.BASH if name == "run_bash" else TaskKind.TOOL)
        summary = _summarize_args(name, args)
        yield Message("tool_call", {
            "id": tid, "name": name, "args": args, "role": role,
            "summary": summary,
        }, task_id=tid)

        allowed, reason = can_use_tool(name, args, self.state, self.resolver)
        if not allowed:
            err = f"ERROR: permission denied ({reason})"
            yield Message("tool_denied", {"id": tid, "name": name, "reason": reason}, task_id=tid)
            yield Message("tool_result", {
                "id": tid, "name": name, "result": err,
                "ok": False, "first_line": err, "role": role,
            }, task_id=tid)
            return

        ctx = ToolContext(
            **self.state.to_tool_ctx_kwargs(),
            state=self.state,
            engine=self.engine,
            background=self.background,
            tasks=self.tasks,
            read_cache=getattr(self.engine, "_read_cache", None),
        )

        spin_label = f"{name}  {summary[:60]}" if summary else name
        with ui.Spinner(spin_label):
            try:
                result = dispatch(name, args, ctx)
            except Exception as e:
                result = f"ERROR: {type(e).__name__}: {e}"

        ok = not result.startswith("ERROR")
        first_line = result.splitlines()[0] if result else ""
        # Tools (browser_screenshot) stash inline images here; pass them
        # through so _execute_and_record can attach them to the message.
        images = list(getattr(ctx, "pending_images", None) or [])
        yield Message("tool_result", {
            "id": tid, "name": name, "result": result,
            "ok": ok, "first_line": first_line, "role": role,
            "ctx_root": str(ctx.root),
            "images": images,
        }, task_id=tid)

    def _run_one_collect(self, call):
        return list(self._run_one(call))


class QueryEngine:
    def __init__(
        self,
        client: OllamaClient,
        state: AppState,
        model: str,
        temperature: float = 0.4,
        config: dict | None = None,
        session_id: str | None = None,
        permission_resolver: Resolver | None = None,
        stream: bool = True,
        compact_schemas: bool = True,
    ) -> None:
        self.client = client
        self.state = state
        self.model = model
        self.temperature = temperature
        self.compact_schemas = compact_schemas
        self.config = config
        self.session_id = session_id
        self.stream = stream
        self.permission_resolver: Resolver = permission_resolver or auto_deny_resolver
        self.task_graph = TaskGraph()
        self.background = BackgroundExecutor(tasks=self.task_graph)
        self.executor = StreamingToolExecutor(
            state, self.permission_resolver,
            engine=self, background=self.background,
            tasks=self.task_graph,
        )
        self.messages: list[dict] = [{"role": "system", "content": fetch_system_prompt_parts(state)}]
        self._recent_calls: list[tuple[str, str]] = []
        self._recent_results: list[tuple[str, str]] = []
        self._abort_turn = False
        self._plan_shown_this_turn = False
        self._usage = {"input": 0, "output": 0, "ms": 0}
        self._read_cache: dict = {}

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": fetch_system_prompt_parts(self.state)}]
        self._usage = {"input": 0, "output": 0, "ms": 0}

    def load_messages(self, messages: list[dict]) -> None:
        if not messages or messages[0].get("role") != "system":
            messages = [{"role": "system", "content": fetch_system_prompt_parts(self.state)}] + list(messages)
        self.messages = list(messages)

    def refresh_system_prompt(self) -> None:
        prompt = fetch_system_prompt_parts(self.state)
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = prompt
        else:
            self.messages.insert(0, {"role": "system", "content": prompt})

    def submit_message(self, prompt: str) -> Iterator[Message]:
        self._plan_shown_this_turn = False
        self._recent_calls = []
        self._recent_results = []
        self._abort_turn = False
        self._read_cache = {}
        resets = {}
        if self.state.edit_fails:
            resets["edit_fails"] = {}
        if self.state.files_read:
            resets["files_read"] = set()
        if resets:
            self.state.update(**resets)
        _poke_browser(self.state, model=self.model, activity="thinking")

        user_msg = process_user_input(prompt, workspace=self.state.workspace, home=self.state.home)
        yield Message("user", {"text": prompt})

        record_transcript(self.session_id or "", "user", prompt)
        self.messages.append(user_msg)

        _pre = sum(len(str(m.get("content") or "")) // 4 for m in self.messages)
        will_compact = _pre > COMPACT_TOKENS
        if will_compact:
            yield Message("info", {
                "text": f"compacting context (~{_pre:,} → ≤{COMPACT_TOKENS:,} tokens)…"
            })
        summarize_fn = (
            self._summarize_with_model
            if (self.config and self.config.get("ollama", {}).get("llm_summarize"))
            else None
        )
        for r in manage_context(
            self.messages,
            max_tokens=COMPACT_TOKENS,
            keep_recent=COMPACT_KEEP_RECENT,
            summarize_with_model=summarize_fn,
        ):
            if r.triggered:
                yield Message("compact", {
                    "strategy": r.strategy, "before": r.before, "after": r.after,
                })

        for _ in range(MAX_TOOL_ITERATIONS):
            for note in self.background.drain_notifications():
                inject = (
                    f"[background] {note['kind']} {note['id']} "
                    f"finished ({note['status']}): {note['label']}\n{note['result']}"
                )
                self.messages.append({"role": "user", "content": inject})
                yield Message("warn", {"text": f"background {note['id']} {note['status']} — injected"})
            est_tokens = sum(
                len(str(m.get("content") or "")) // 4 for m in self.messages
            )
            if est_tokens >= 4000:
                tool_count = len(all_tool_schemas(self.state.tool_groups)) if self.state.tools_enabled else 0
                yield Message("info", {
                    "text": (
                        f"sending ~{est_tokens:,} tokens to {self.model}"
                        + (f" · {tool_count} tools" if tool_count else "")
                    ),
                })
            try:
                msg, usage = self._chat_once(yield_deltas=self.stream)
                if isinstance(msg, _StreamGen):
                    final_msg = None
                    spinner = ui.Spinner(
                        "thinking",
                        escalations=[
                            (10.0, "thinking · prompt-eval"),
                            (30.0, "still thinking · check VRAM in /diag"),
                            (60.0, "still thinking · Ctrl+C to abort"),
                        ],
                    )
                    spinner.start()
                    watchdog = ui.SilenceWatchdog()
                    got_first = False
                    try:
                        for kind, payload in msg.iter():
                            if kind == "delta":
                                if not got_first:
                                    spinner.stop()
                                    watchdog.start()
                                    got_first = True
                                else:
                                    watchdog.ping()
                                yield Message("delta", {"text": payload})
                            elif kind == "done":
                                final_msg = payload
                    except OllamaError as e:
                        spinner.stop()
                        watchdog.stop()
                        yield Message("warn", {
                            "text": f"streaming connection broke ({e}); retrying without streaming."
                        })
                        msg = self._chat_nonstream()
                        if msg is None:
                            yield Message("error", {"text": "non-streaming retry also failed"})
                            return
                        usage = {}
                        final_msg = "RETRIED"
                    finally:
                        spinner.stop()
                        watchdog.stop()
                    if final_msg is None:
                        yield Message("warn", {
                            "text": "stream produced no chunks — try /retry"
                        })
                        return
                    if final_msg != "RETRIED":
                        if final_msg.get("truncated"):
                            yield Message("warn", {
                                "text": "response was truncated (stream closed early); using what arrived"
                            })
                        msg = final_msg["message"]
                        usage = {
                            "input": final_msg.get("prompt_eval_count", 0),
                            "output": final_msg.get("eval_count", 0),
                            "ms": final_msg.get("total_duration_ns", 0) // 1_000_000,
                        }
            except ToolsUnsupportedError:
                yield Message("warn", {"text": f"model '{self.model}' lacks native tool support — switching to text-protocol fallback."})
                self.state.update(tools_enabled=False)
                self.refresh_system_prompt()
                if self.config is not None:
                    set_value(self.config, f"models.{self.model}.tools_supported", False)
                continue
            except OllamaError as e:
                yield Message("error", {"text": str(e)})
                return

            self._usage["input"] += usage.get("input", 0)
            self._usage["output"] += usage.get("output", 0)
            self._usage["ms"] += usage.get("ms", 0)

            raw = msg.get("content") or ""
            had_fakes, cleaned = _strip_fakes(raw)
            cleaned = cleaned.strip()
            msg["content"] = cleaned
            self.messages.append(msg)

            record_transcript(
                self.session_id or "", "assistant", cleaned,
                tool_calls=msg.get("tool_calls") or [],
            )

            if had_fakes:
                yield Message("warn", {"text": "model fabricated tool outputs — asking it to retry."})
                self.messages.append({
                    "role": "system",
                    "content": (
                        "STOP. Tool outputs are mine to emit. Send a single tool call and STOP."
                    ),
                })
                continue

            thinks, content = _extract_thinking(cleaned)
            if not self.stream:
                for t in thinks:
                    if t:
                        yield Message("thinking", {"text": t})
            steps, content = _extract_plan(content)
            content = content.strip()
            if steps and not self._plan_shown_this_turn:
                yield Message("plan", {"steps": steps})
                self._plan_shown_this_turn = True

            calls, narration = self._extract_calls(msg, content)

            if not calls:
                if narration:
                    yield Message("assistant", {"text": narration})
                _poke_browser(self.state, activity="idle")
                yield Message("done", {
                    "text": narration,
                    "usage": dict(self._usage),
                    "session_id": self.session_id,
                })
                return

            if narration and not self.stream:
                yield Message("narration", {"text": narration})
            elif narration and self.stream:
                yield Message("assistant", {"text": narration})

            yield from self._execute_and_record(calls)

            if self._abort_turn:
                salvaged = self._last_assistant_narration()
                if salvaged:
                    yield Message("assistant", {"text": (
                        salvaged + "\n\n_(I got stuck. Try /retry.)_"
                    )})
                _poke_browser(self.state, activity="idle")
                yield Message("done", {
                    "text": "Stopped: stuck in a loop. Try /retry or /new for a fresh context.",
                    "usage": dict(self._usage),
                    "session_id": self.session_id,
                })
                return

        yield Message("error", {"text": f"hit tool-call limit ({MAX_TOOL_ITERATIONS}); stopping."})

    def _chat_once(self, *, yield_deltas: bool):
        api_messages = normalize_messages_for_api(self.messages)
        tools = all_tool_schemas(self.state.tool_groups, compact=self.compact_schemas) if self.state.tools_enabled else None

        if yield_deltas and hasattr(self.client, "chat_stream_assembled"):
            gen = self.client.chat_stream_assembled(
                model=self.model,
                messages=api_messages,
                tools=tools,
                options={"temperature": self.temperature},
            )
            return _StreamGen(gen), {}

        with ui.Spinner("thinking"):
            msg = self.client.chat(
                model=self.model,
                messages=api_messages,
                tools=tools,
                options={"temperature": self.temperature},
            )
        return msg, {}

    def _chat_nonstream(self) -> dict | None:
        api_messages = normalize_messages_for_api(self.messages)
        tools = all_tool_schemas(self.state.tool_groups, compact=self.compact_schemas) if self.state.tools_enabled else None
        try:
            with ui.Spinner("retrying (no stream)"):
                return self.client.chat(
                    model=self.model,
                    messages=api_messages,
                    tools=tools,
                    options={"temperature": self.temperature},
                )
        except OllamaError:
            return None

    def _summarize_with_model(self, middle: list[dict]) -> str | None:
        if not middle:
            return None
        try:
            short = []
            for m in middle:
                content = (m.get("content") or "")[:1500]
                short.append({"role": m.get("role"), "content": content})
            req = [
                {"role": "system", "content": "Summarize the following conversation as a tight bullet list. <=200 words."},
                {"role": "user", "content": json.dumps(short)},
            ]
            res = self.client.chat(
                model=self.model, messages=req, tools=None,
                options={"temperature": 0.0},
            )
            return (res.get("content") or "").strip() or None
        except Exception:
            return None

    def _extract_calls(self, msg: dict, content: str):
        native = msg.get("tool_calls") or []
        if native:
            calls: list[tuple[str, dict, str]] = []
            for c in native:
                fn = c.get("function", {}) or {}
                name = fn.get("name", "")
                raw = fn.get("arguments", {})
                if isinstance(raw, str):
                    try:
                        args = json.loads(raw) if raw else {}
                    except json.JSONDecodeError:
                        args = {}
                else:
                    args = raw or {}
                calls.append((name, args, "tool"))
            return calls, content
        if not content:
            return [], content
        ext = _extract_tool_call(content)
        if not ext:
            return [], content
        name, args, start, end = ext
        narration = (content[:start] + content[end:]).strip()
        return [(name, args, "user")], narration

    def _check_loop(self, name: str, args: dict) -> bool:
        key = (name, json.dumps(args, sort_keys=True, default=str))
        self._recent_calls.append(key)
        run = 1
        for prev in reversed(self._recent_calls[:-1]):
            if prev == key:
                run += 1
            else:
                break
        self._recent_calls = self._recent_calls[-12:]
        if run >= LOOP_THRESHOLD:
            self._recent_calls = []
            self.messages.append({
                "role": "user",
                "content": (
                    f"You called {name} with the same arguments {run} times in a row. "
                    f"Stop repeating. Try a different approach or ask a clarifying question."
                ),
            })
            return True
        return False

    def _last_assistant_narration(self) -> str:
        for m in reversed(self.messages):
            if m.get("role") != "assistant":
                continue
            content = (m.get("content") or "").strip()
            content = _PLAN_RX.sub("", content)
            content = _THINK_RX.sub("", content).strip()
            if not content:
                continue
            if content.startswith("{") and content.endswith("}"):
                continue
            return content
        return ""

    def _result_loop_count(self, name: str, result: str) -> int:
        # The "[CACHED — you already read X]" nudges are an INTERNAL
        # recovery signal, not a real loop — don't let them pile up
        # toward a steer.
        if (result or "").lstrip().startswith("[CACHED"):
            return 0
        key = (name, (result or "")[:240])
        self._recent_results.append(key)
        self._recent_results = self._recent_results[-30:]
        return self._recent_results.count(key)

    def _execute_and_record(self, calls):
        cleaned_calls = []
        for name, args, role in calls:
            if self._check_loop(name, args):
                yield Message("warn", {"text": f"loop detected on {name} — steered."})
                continue
            cleaned_calls.append((name, args, role))

        for ev in self.executor.execute(cleaned_calls):
            if ev.kind == "tool_call":
                _poke_browser(self.state, activity=f"running {ev.data.get('name', 'a tool')}")
            yield ev
            if ev.kind == "tool_result":
                d = ev.data
                name = d["name"]
                result = d["result"]
                role = d.get("role", "user")

                if name == "set_workspace" and d.get("ok"):
                    new_root = Path(d.get("ctx_root") or self.state.workspace)
                    self.state.update(workspace=new_root)
                    self.refresh_system_prompt()

                images = d.get("images") or []
                if role == "tool":
                    msg = {"role": "tool", "name": name, "content": result}
                else:
                    msg = {
                        "role": "user",
                        "content": f"Tool result for {name}:\n{result}",
                    }
                if images:
                    msg["images"] = images
                self.messages.append(msg)
                record_transcript(self.session_id or "", "tool", result, name=name)

                seen = self._result_loop_count(name, result)
                if seen == LOOP_THRESHOLD:
                    yield Message("warn", {
                        "text": f"loop: {name} returned the same result {seen}× — steering hard"
                    })
                    self.messages.append({
                        "role": "system",
                        "content": (
                            f"STOP. You have called {name} and received the SAME result {seen} "
                            f"times. Do NOT call {name} again. Either act on what you have, "
                            f"or give the user a short final answer."
                        ),
                    })
                elif seen >= LOOP_THRESHOLD * 2:
                    yield Message("warn", {
                        "text": f"loop unbroken after {seen} identical results — ending turn"
                    })
                    self._abort_turn = True
                    return


class _StreamGen:
    def __init__(self, gen):
        self.gen = gen
    def iter(self):
        return self.gen
