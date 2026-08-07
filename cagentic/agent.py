"""Thin compatibility wrapper around QueryEngine.

The real loop lives in `cagentic.engine.QueryEngine`. This shim renders the
QueryEngine event stream to the terminal — which is what the REPL also does,
just inlined here.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from . import ui
from .engine import Message, QueryEngine
from .ollama_client import OllamaClient
from .state import AppState
from .tools import ToolContext


class Agent:
    def __init__(
        self,
        client: OllamaClient,
        model: str,
        root: Path,
        yolo: bool = False,
        temperature: float = 0.4,
        on_turn_complete: Optional[Callable] = None,
        tools_enabled: bool = True,
        on_tools_disabled: Optional[Callable] = None,
        config: Optional[dict] = None,
        stream: bool = True,
        compact_schemas: bool = True,
        user_name: Optional[str] = None,
        dry_run: bool = False,
    ) -> None:
        self.state = AppState(
            workspace=root,
            home=Path.home(),
            yolo=yolo,
            tools_enabled=tools_enabled,
            user_name=user_name,
            dry_run=dry_run,
            plan_mode=dry_run,
        )
        from .permissions import terminal_resolver

        self.engine = QueryEngine(
            client=client,
            state=self.state,
            model=model,
            temperature=temperature,
            config=config,
            permission_resolver=terminal_resolver,
            stream=stream,
            compact_schemas=compact_schemas,
        )
        self.client = client
        self.on_turn_complete = on_turn_complete
        self.on_tools_disabled = on_tools_disabled
        self.last_turn_failed = False

        if on_tools_disabled is not None:

            def _watch(state, key, val):
                if key == "tools_enabled" and val is False:
                    try:
                        on_tools_disabled(self)
                    except Exception:
                        pass

            self.state.subscribe(_watch)

    @property
    def model(self) -> str:
        return self.engine.model

    @model.setter
    def model(self, value: str) -> None:
        self.engine.model = value

    @property
    def messages(self) -> list[dict]:
        return self.engine.messages

    @property
    def tools_enabled(self) -> bool:
        return self.state.tools_enabled

    @tools_enabled.setter
    def tools_enabled(self, value: bool) -> None:
        self.state.update(tools_enabled=value)
        self.engine.refresh_system_prompt()

    @property
    def ctx(self) -> ToolContext:
        return ToolContext(
            **self.state.to_tool_ctx_kwargs(),
            state=self.state,
            engine=self.engine,
            background=self.engine.background,
            tasks=self.engine.task_graph,
            teams=self.engine.teams,
            read_cache=self.engine._read_cache,
            phone_action=self.engine.executor.phone_action,
            widget_action=self.engine.executor.widget_action,
        )

    def reset(self) -> None:
        self.engine.reset()

    def load_messages(self, messages: list[dict]) -> None:
        self.engine.load_messages(messages)

    def refresh_system_prompt(self) -> None:
        self.engine.refresh_system_prompt()

    _refresh_system_prompt = refresh_system_prompt

    def turn(self, user_input: str) -> str:
        self.last_turn_failed = False
        pre_ctx = sum(len(str(m.get("content") or "")) // 4 for m in self.engine.messages)
        bar = ui.StatusBar(ctx_tokens=pre_ctx)
        bar.start()
        rs = _RenderState()
        gen = self.engine.submit_message(user_input)
        try:
            for msg in gen:
                if msg.kind == "delta":
                    bar.on_delta(msg.data.get("text", ""))
                elif msg.kind == "done":
                    post_ctx = sum(
                        len(str(m.get("content") or "")) // 4 for m in self.engine.messages
                    )
                    bar.on_done(post_ctx)
                render_event(msg, rs)
        except KeyboardInterrupt:
            close = getattr(gen, "close", None)
            if callable(close):
                close()
            ui.stop_all_spinners()
            print()
            ui.warn("turn interrupted — back to prompt")
            rs.failed = True
        finally:
            bar.stop()
            self.last_turn_failed = rs.failed or not rs.completed
            if self.on_turn_complete:
                try:
                    self.on_turn_complete(self)
                except Exception:
                    pass
        return rs.final_text

    def stream(self, user_input: str) -> Iterator[Message]:
        return self.engine.submit_message(user_input)


@dataclass
class _ToolRun:
    """A run of consecutive successful tool calls of the same name —
    rendered as one line that updates in place ('read_file × 5')."""

    name: str
    count: int = 1


@dataclass
class _RenderState:
    final_text: str = ""
    streaming: bool = False
    streamed_any: bool = False
    streamed_visible: bool = False
    md: ui.StreamMarkdown | None = None
    pending_tool_call: tuple[str, str] | None = None
    run: _ToolRun | None = None
    failed: bool = False
    completed: bool = False


def _short_path(p: str) -> str:
    """Replace the home directory with '~' for display."""
    if not p:
        return p
    try:
        from pathlib import Path

        home = str(Path.home())
        if p == home:
            return "~"
        if p.startswith(home + "/") or p.startswith(home + "\\"):
            return "~" + p[len(home) :]
    except Exception:
        pass
    return p


def _extract_meta(first_line: str, summary: str) -> str:
    """Pull the 'metadata tail' off a tool result — '(203 lines)' from
    success, the error reason from failure — without repeating any path
    that's already shown in the tool_call summary."""
    if not first_line:
        return ""
    s = first_line
    if s.startswith("ERROR:"):
        s = s[len("ERROR:") :].lstrip()
    if summary and s.startswith(summary):
        return s[len(summary) :].lstrip()[:80]
    # Path may appear mid-message ("not found: /Users/.../foo"); drop it.
    if summary and summary in s:
        s = s.replace(summary, "").strip(" :")
    return s[:80]


def _print_tool_line(name: str, summary: str, meta: str, *, ok: bool, count: int) -> None:
    """Render a tool call as a single line. Successful runs of the same
    name update in place ('× N'); failures paint the whole line red."""
    name_label = ui.single_line(name) + (f"  × {count}" if count > 1 else "")
    summary = ui.single_line(summary)
    meta = ui.single_line(meta)
    if ok:
        marker = "  → "
        line = (
            ui.color(marker, ui.DUSK)
            + ui.color(name_label, ui.DUSK)
            + (("  " + ui.color(summary, ui.MUTED)) if summary else "")
            + ((ui.color("  ✓ ", ui.OK) + ui.color(meta, ui.MUTED)) if meta else "")
        )
    else:
        bits = [name_label]
        if summary:
            bits.append(summary)
        if meta:
            bits.append(meta)
        line = ui.color("  × " + "  ".join(bits), ui.ERR)
    print(ui.truncate(line, ui.width()))


def _end_stream_line(rs: _RenderState) -> None:
    if rs.streaming:
        if rs.md is not None:
            rs.md.flush()
            rs.streamed_visible = bool(getattr(rs.md, "visible_emitted", False))
        ui.sync_write("\n")
        rs.streaming = False
        rs.md = None


def _stream_emit(s: str) -> None:
    # Share the paint lock with the status bar / spinner so a background
    # paint frame can't slip between this write and its flush and clobber
    # the cursor mid-token.
    ui.sync_write(s)


def render_event(event: Message, rs: _RenderState) -> None:
    k, d = event.kind, event.data

    if k != "delta":
        _end_stream_line(rs)

    # Any event that prints something else closes the in-place tool run —
    # the next same-name tool can no longer reach the line above with \r↑.
    if k not in ("tool_call", "tool_result"):
        rs.run = None

    if k == "thinking":
        ui.thinking(d["text"])
    elif k == "plan":
        ui.plan(d["steps"])
    elif k == "narration":
        ui.meta(d["text"])
    elif k == "delta":
        if not rs.streaming:
            rs.md = ui.StreamMarkdown(
                emit=_stream_emit,
                first_prefix=ui.color("  ● ", ui.GLOW),
                cont_prefix="    ",
                dim_first_prefix=ui.color("  · ", ui.SOFT),
                dim_cont_prefix=ui.color("    ", ui.SOFT),
            )
            rs.streaming = True
        if rs.md is not None:
            rs.md.feed(d["text"])
        rs.streamed_any = True
    elif k == "assistant":
        rs.final_text = d["text"]
        if rs.streamed_any and rs.streamed_visible:
            rs.streamed_any = False
            rs.streamed_visible = False
        else:
            rs.streamed_any = False
            rs.streamed_visible = False
            if d["text"].strip():
                ui.assistant(d["text"])
    elif k == "tool_call":
        # The line isn't printed yet — defer until the result arrives so we
        # know if it should be suppressed (CACHED), collapsed into a run,
        # or painted red on failure.
        rs.pending_tool_call = (d["name"], d["summary"])
    elif k == "tool_result":
        name = d.get("name") or ""
        result = d.get("result") or ""
        first_line = d.get("first_line") or ""
        ok = bool(d.get("ok", True))
        if first_line.startswith("[CACHED"):
            rs.pending_tool_call = None
            return

        summary = ""
        if rs.pending_tool_call is not None:
            _, summary = rs.pending_tool_call
            rs.pending_tool_call = None

        # Special case: write_file / edit_file keeps the diff-style line.
        if ok and name in ("write_file", "edit_file"):
            import re as _re_edit

            m = _re_edit.match(r"OK:\s+(\w+)\s+(.+?)\s+\+(\d+)\s+-(\d+)\s*$", result)
            if m:
                op, path, adds, dels = m.group(1), m.group(2), m.group(3), m.group(4)
                line = (
                    ui.color("  ✓ ", ui.OK)
                    + " "
                    + ui.color(op, ui.DUSK)
                    + " "
                    + ui.color(ui.sanitize(_short_path(path)), ui.SURFACE)
                    + "  "
                    + ui.color(f"+{adds}", ui.OK)
                    + " "
                    + ui.color(f"-{dels}", ui.ERR)
                )
                print(ui.truncate(line, ui.width()))
                rs.run = None
                return

        meta = _extract_meta(first_line, summary)
        short_summary = _short_path(summary)

        if ok and rs.run is not None and rs.run.name == name and ui.supports_cursor_control():
            # Continue an existing run — overwrite the line above instead
            # of printing a new one, so "read_file × 5" shows in place.
            rs.run.count += 1
            ui.sync_write("\r\033[1A\033[2K")
            _print_tool_line(name, short_summary, meta, ok=True, count=rs.run.count)
        else:
            rs.run = _ToolRun(name=name) if ok else None
            _print_tool_line(name, short_summary, meta, ok=ok, count=1)
    elif k == "info":
        ui.info(d["text"])
    elif k == "warn":
        ui.warn(d["text"])
    elif k == "error":
        rs.failed = True
        ui.error(d["text"])
    elif k == "compact":
        strategy = d.get("strategy", "compact")
        ui.info(f"context {strategy}: {d['before']} → {d['after']} approx tokens")
    elif k == "tool_denied":
        if rs.pending_tool_call is not None:
            pname, psummary = rs.pending_tool_call
            rs.pending_tool_call = None
            _print_tool_line(pname, _short_path(psummary), "", ok=False, count=1)
        ui.warn(f"permission denied: {d['name']} ({d['reason']})")
        rs.run = None
    elif k == "done":
        rs.completed = True
        usage = d.get("usage", {})
        if any(usage.values()):
            ui.meta(
                f"tokens {usage.get('input', 0):,} in · {usage.get('output', 0):,} out"
                f" · {usage.get('ms', 0):,} ms"
            )
