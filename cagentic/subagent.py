"""Sub-agents (forkSubagent).

Forks a child QueryEngine with a FRESH messages[] but inherited state
(workspace, github_token, yolo, …). The child runs an isolated turn — its own
plan, tools, loop — and returns a single string answer to the parent, so the
parent's main conversation stays focused.

Three things it deliberately does NOT do any more:

* run silently. The child used to be a true black box: whatever it read, ran or
  wrote was invisible, and the only evidence was a paragraph of prose. `on_event`
  reports each tool call as it happens so the caller can show its work.
* share the parent's HTTP client. `requests.Session` is not thread-safe and the
  background executor already runs up to eight sub-agents at once, so the shared
  session was a real hazard before anything asked for fan-out. Each child now
  builds its own where it can.
* get the full toolset regardless. `tool_groups` narrows what the child is even
  offered — a researcher that only needs to read has no business holding
  `run_bash`, and a child that cannot write is safe to run in parallel.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from .permissions import auto_deny_resolver
from .state import AppState

if TYPE_CHECKING:  # pragma: no cover
    from .engine import QueryEngine

logger = logging.getLogger(__name__)


# Groups a sub-agent may be restricted to. Anything outside these can change
# the world, which is what makes a restricted child safe to fan out.
READ_ONLY_GROUPS = frozenset({"files", "web", "mcp", "system"})


def subagent_is_read_only(tool_groups) -> bool:
    """Whether a child limited to these groups can only look, never touch."""
    if not tool_groups:
        return False
    return set(tool_groups).issubset(READ_ONLY_GROUPS)


def _client_for(parent: "QueryEngine", model_spec: str | None):
    """The client this sub-agent should use — usually the parent's.

    Its own only when sharing would actually hurt: the cloud clients keep a
    single `requests.Session`, which is not thread-safe, and sub-agents already
    run concurrently through the background executor. Ollama posts per request
    and shares fine.

    Deliberately not "always build a fresh one". A caller that handed
    QueryEngine a particular client — a test, an embedder, a gateway actor —
    means it, and quietly substituting one built from config would ignore that.
    """
    needs_own = getattr(parent.client, "_session", None) is not None
    if not needs_own and not model_spec:
        return parent.client

    from .providers import build_client, parse_model

    spec = model_spec or getattr(parent.state, "active_model_spec", None) or parent.model
    try:
        provider, _name = parse_model(str(spec))
        return build_client(parent.config or {}, provider)
    except Exception:
        # A missing key or an unknown provider is not worth failing the call
        # over — the parent's client already works, shared session and all.
        logger.debug("sub-agent could not build its own client for %r", spec, exc_info=True)
        return parent.client


def fork_subagent(
    parent: "QueryEngine",
    prompt: str,
    *,
    model: str | None = None,
    title: str = "subagent",
    role: str | None = None,
    tool_groups: set[str] | None = None,
    on_event: "Callable[[str, dict], None] | None" = None,
) -> str:
    """Run a sub-agent and return its final answer.

    `role` (optional) is appended to the system prompt — used by the team
    coordinator to give each teammate a persistent persona without
    rewriting the engine's system-prompt builder.

    `tool_groups` narrows what the child is offered; None inherits the parent's.
    `on_event` is called with (kind, data) for each of the child's events, so a
    caller can render what it is doing instead of waiting on a silent string.
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
        tool_groups=(
            set(tool_groups)
            if tool_groups
            else (set(parent.state.tool_groups) if parent.state.tool_groups else None)
        ),
        # Fresh permission cache — a black-box sub-agent must not inherit or
        # mutate the parent's approvals. Unlike the fields above this one is a
        # record of answers already given, not a restriction, so starting empty
        # is the conservative choice.
        permissions={},
    )
    sub = QueryEngine(
        client=_client_for(parent, model),
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
            if on_event is not None:
                try:
                    on_event(ev.kind, ev.data)
                except Exception:
                    # Reporting progress must never be what kills the child.
                    logger.debug("sub-agent '%s' on_event raised", title, exc_info=True)
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
