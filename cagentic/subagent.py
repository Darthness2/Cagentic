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
        default_repository=parent.state.default_repository,
        workspace_boundary=parent.state.workspace_boundary,
        yolo=parent.state.yolo,
        insecure_ssl=parent.state.insecure_ssl,
        tools_enabled=parent.state.tools_enabled,
        # Every restriction the parent is under has to come across too. This
        # used to copy `yolo` and nothing else, so the one permissive flag
        # propagated while `dry_run`, the deny rules, plan mode and the enabled
        # tool groups all silently reset to their permissive defaults — a
        # sub-agent forked from a `--dry-run` or plan-mode session could write
        # files, and a project `deny` rule did not apply inside one. dry_run in
        # particular is documented as a gate that yolo cannot open.
        dry_run=parent.state.dry_run,
        plan_mode=parent.state.plan_mode,
        approval_mode=parent.state.approval_mode,
        ask_all=parent.state.ask_all,
        permission_rules=dict(parent.state.permission_rules),
        tool_groups=set(parent.state.tool_groups) if parent.state.tool_groups else None,
        # Fresh permission cache — a black-box sub-agent must not inherit or
        # mutate the parent's approvals. Unlike the fields above this one is a
        # record of answers already given, not a restriction, so starting empty
        # is the conservative choice.
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
