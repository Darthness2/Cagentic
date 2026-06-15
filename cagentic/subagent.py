"""Sub-agents (forkSubagent).

Forks a child QueryEngine with a FRESH messages[] but inherited state
(workspace, github_token, yolo, …). The child runs an isolated turn —
its own plan, tools, loop — and returns a single string answer to the
parent. The parent's main conversation stays focused; the sub-agent
acts as a black-box research/specialist call.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .permissions import auto_deny_resolver
from .state import AppState

if TYPE_CHECKING:  # pragma: no cover
    from .engine import QueryEngine

logger = logging.getLogger(__name__)


def fork_subagent(
    parent: "QueryEngine",
    prompt: str,
    *,
    model: str | None = None,
    title: str = "subagent",
    role: str | None = None,
) -> str:
    """Run a sub-agent and return its final answer.

    `role` (optional) is appended to the system prompt — used by the team
    coordinator to give each teammate a persistent persona without
    rewriting the engine's system-prompt builder.
    """
    from .engine import QueryEngine  # local import — avoids circular at module load

    sub_state = AppState(
        workspace=parent.state.workspace,
        home=parent.state.home,
        github_token=parent.state.github_token,
        yolo=parent.state.yolo,
        insecure_ssl=parent.state.insecure_ssl,
        tools_enabled=parent.state.tools_enabled,
        # Fresh permission cache — a black-box sub-agent must not inherit or
        # mutate the parent's approvals.
        permissions={},
    )
    sub = QueryEngine(
        client=parent.client,
        state=sub_state,
        model=model or parent.model,
        temperature=parent.temperature,
        config=parent.config,
        # Non-interactive: a sub-agent runs headless, so it must never block on
        # a terminal y/n prompt. Read-only tools are still allowed by the
        # permission gate; everything mutating is auto-denied. (If the parent
        # is in yolo mode, sub_state.yolo carries that through and approves.)
        permission_resolver=auto_deny_resolver,
        stream=False,
    )

    if role and sub.messages and sub.messages[0].get("role") == "system":
        sub.messages[0]["content"] = sub.messages[0]["content"] + "\n\n=== ROLE ===\n" + role

    final = ""
    try:
        for ev in sub.submit_message(prompt):
            if ev.kind in ("assistant", "done"):
                text = ev.data.get("text") or ""
                if text:
                    final = text
            elif ev.kind == "error":
                return f"sub-agent error: {ev.data.get('text', '')}"
    except Exception as e:
        # A raised provider/engine exception must not crash the parent turn —
        # the caller only handles a returned string.
        logger.warning("sub-agent '%s' crashed", title, exc_info=True)
        return f"sub-agent error: {type(e).__name__}: {e}"
    return final or "(sub-agent returned no answer)"
