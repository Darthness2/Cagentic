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
    ) -> None:
        self.state = AppState(
            workspace=root,
            home=Path.home(),
            yolo=yolo,
            tools_enabled=tools_enabled,
            user_name=user_name,
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
        return ToolContext(**self.state.to_tool_ctx_kwargs())

    def reset(self) -> None:
        self.engine.reset()

    def load_messages(self, messages: list[dict]) -> None:
        self.engine.load_messages(messages)

    def refresh_system_prompt(self) -> None:
        self.engine.refresh_system_prompt()

    _refresh_system_prompt = refresh_system_prompt

    def turn(self, user_input: str) -> str:
        rs = _RenderState()
        gen = self.engine.submit_message(user_input)
        try:
            for msg in gen:
                render_event(msg, rs)
        except KeyboardInterrupt:
            gen.close()
            ui.stop_all_spinners()
            print()
            ui.warn("turn interrupted — back to prompt")
        finally:
            if self.on_turn_complete:
                try:
                    self.on_turn_complete(self)
                except Exception:
                    pass
        return rs.final_text

    def stream(self, user_input: str) -> Iterator[Message]:
        return self.engine.submit_message(user_input)


@dataclass
class _RenderState:
    final_text: str = ""
    streaming: bool = False
    streamed_any: bool = False
    streamed_visible: bool = False
    md: object | None = None
    pending_tool_call: tuple[str, str] | None = None


def _end_stream_line(rs: _RenderState) -> None:
    if rs.streaming:
        if rs.md is not None:
            rs.md.flush()
            rs.streamed_visible = bool(getattr(rs.md, "visible_emitted", False))
        sys.stdout.write("\n")
        sys.stdout.flush()
        rs.streaming = False
        rs.md = None


def _stream_emit(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


def render_event(event: Message, rs: _RenderState) -> None:
    k, d = event.kind, event.data

    if k != "delta":
        _end_stream_line(rs)

    if k == "thinking":
        ui.thinking(d["text"])
    elif k == "plan":
        ui.plan(d["steps"])
    elif k == "narration":
        print(ui.color("  · ", ui.PLUM) + ui.color(d["text"], ui.MUTED))
    elif k == "delta":
        if not rs.streaming:
            rs.md = ui.StreamMarkdown(
                emit=_stream_emit,
                first_prefix=ui.color("  ✦ ", ui.GLOW),
                cont_prefix="    ",
                dim_first_prefix=ui.color("  ◦ ", ui.SOFT),
                dim_cont_prefix=ui.color("    ", ui.SOFT),
            )
            rs.streaming = True
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
        rs.pending_tool_call = (d["name"], d["summary"])
    elif k == "tool_result":
        name = d.get("name") or ""
        result = d.get("result") or ""
        first_line = d.get("first_line") or ""
        if first_line.startswith("[CACHED"):
            rs.pending_tool_call = None
            return
        if rs.pending_tool_call is not None:
            pname, psummary = rs.pending_tool_call
            ui.tool_call(pname, psummary)
            rs.pending_tool_call = None
        if d["ok"] and name in ("write_file", "edit_file"):
            import re as _re_edit
            m = _re_edit.match(r"OK:\s+(\w+)\s+(.+?)\s+\+(\d+)\s+-(\d+)\s*$", result)
            if m:
                op, path, adds, dels = m.group(1), m.group(2), m.group(3), m.group(4)
                mark = ui.color("    ✓", ui.OK)
                print(
                    mark + " " + ui.color(op, ui.DUSK)
                    + " " + ui.color(path, ui.SURFACE)
                    + "  " + ui.color(f"+{adds}", ui.OK)
                    + " " + ui.color(f"-{dels}", ui.ERR)
                )
                return
        ui.tool_result(first_line[:160], ok=d["ok"])
    elif k == "info":
        ui.info(d["text"])
    elif k == "warn":
        ui.warn(d["text"])
    elif k == "error":
        ui.error(d["text"])
    elif k == "compact":
        strategy = d.get("strategy", "compact")
        ui.info(f"context {strategy}: {d['before']} → {d['after']} approx tokens")
    elif k == "tool_denied":
        if rs.pending_tool_call is not None:
            pname, psummary = rs.pending_tool_call
            ui.tool_call(pname, psummary)
            rs.pending_tool_call = None
        ui.warn(f"permission denied: {d['name']} ({d['reason']})")
    elif k == "done":
        usage = d.get("usage", {})
        if any(usage.values()):
            print(ui.color(
                f"  ↳ tokens in/out {usage.get('input', 0)}/{usage.get('output', 0)}"
                f"  · {usage.get('ms', 0)}ms",
                ui.SOFT,
            ))
