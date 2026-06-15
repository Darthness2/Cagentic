"""can_use_tool(): formal permission gate.

Decision sources, in order:
    1. Per-tool cache in AppState.permissions ('always'/'never').
    2. Read-only tools — always allowed without prompting.
    3. yolo mode — always allowed.
    4. Resolver callback — interactive y/n/a prompt for the REPL,
       or auto-decide for the SDK / headless caller.
"""
from __future__ import annotations

from typing import Callable

from .state import AppState


# Tools that don't mutate anything we care about — always allowed.
READ_ONLY: set[str] = {
    # files / search / shell-less inspection
    "read_file", "list_dir", "grep", "glob", "tool_search",
    # asking the user is interactive but not destructive — skip the
    # extra approval prompt so it just shows the question.
    "ask_user_question",
    # web
    "web_fetch", "web_search",
    # tasks / state inspection
    "task_get", "task_list", "task_output", "task_status",
    "config_get", "sleep",
    # notes / reminders read paths
    "note_get", "note_list", "note_search",
    "reminder_list",
    # mcp inspection
    "mcp_list_servers", "mcp_list_tools", "mcp_list_resources", "mcp_read_resource",
    # browser inspection (acting in the browser is gated; looking is not)
    "browser_status", "browser_tabs", "browser_read", "browser_screenshot",
    "browser_links",
    # github read
    "gh_whoami", "gh_list_repos", "gh_get_repo", "gh_get_file",
    "gh_list_issues", "gh_list_pulls", "gh_get_pull", "gh_search_code",
}


# Tools that are safe to run concurrently (no shared mutable state).
# NB: `set_workspace` mutates shared AppState (the workspace path other
# concurrent tools resolve against), so it must NOT run in the thread pool —
# keep it serial.
CONCURRENT_SAFE: set[str] = set(READ_ONLY)


# Resolver: (tool_name, args, state) -> 'yes' | 'always' | 'no' | 'never'
Resolver = Callable[[str, dict, AppState], str]


def auto_deny_resolver(name: str, args: dict, state: AppState) -> str:
    """Default for headless / SDK use: never prompt, deny mutating ops."""
    return "no"


# Tools allowed in plan mode even though they aren't strictly read-only —
# things the model needs to organize its thinking without touching the world.
PLAN_MODE_EXTRAS = {
    "enter_plan_mode", "exit_plan_mode", "ask_user_question", "todo_write",
    "tool_search", "task_get", "task_list", "task_output", "config_get",
}


def can_use_tool(
    name: str,
    args: dict,
    state: AppState,
    resolver: Resolver = auto_deny_resolver,
) -> tuple[bool, str]:
    """Returns (allowed, reason)."""
    cached = state.permissions.get(name)
    if cached == "always":
        return True, "permission cache: always"
    if cached == "never":
        return False, "permission cache: never"

    # Plan-mode gate: only read-only tools are allowed.
    if getattr(state, "plan_mode", False) and name not in READ_ONLY and name not in PLAN_MODE_EXTRAS:
        return False, "plan mode active — mutating tools blocked"

    if name in READ_ONLY:
        return True, "read-only tool"

    if state.yolo:
        return True, "yolo mode"

    answer = resolver(name, args, state) or "no"
    if answer == "always":
        state.permissions[name] = "always"
        return True, "user approved (always)"
    if answer == "never":
        state.permissions[name] = "never"
        return False, "user denied (never)"
    if answer == "yes":
        return True, "user approved"
    return False, "user denied"


def terminal_resolver(name: str, args: dict, state: AppState) -> str:
    """Interactive REPL resolver.

    Choices (case-insensitive):
        y, yes          → allow this one call
        n, no, <Enter>  → deny this one call
        a, always       → always allow THIS tool
        yolo, all       → flip yolo mode ON: allow EVERYTHING this session
        never           → never allow THIS tool again
    """
    from . import ui

    ui.stop_all_spinners()
    import sys as _sys
    if _sys.stdout.isatty():
        _sys.stdout.write("\033[?25h")
        _sys.stdout.flush()

    detail = ""
    if name == "run_bash":
        detail = f": {args.get('command', '')}"
    elif name == "write_file":
        detail = f": write {args.get('path', '')}"
    elif name == "edit_file":
        detail = f": edit {args.get('path', '')}"
    elif name == "note_write":
        detail = f": note '{args.get('name', '')}'"
    elif name == "reminder_add":
        detail = f": '{args.get('text', '')[:60]}'"
    elif name == "mcp_call":
        detail = f": {args.get('server', '?')}/{args.get('tool', '?')}"
    elif name.startswith("gh_") or name == "github_api":
        detail = f": {name} {args}"
    ui.warn(f"\nApprove {name}{detail}?")
    try:
        ans = input("  [y]es / [n]o / [a]lways this tool / 'yolo' to approve all / never: ").strip().lower()
    except EOFError:
        return "no"

    if ans in ("yolo", "all"):
        state.update(yolo=True)
        ui.info("yolo mode ON — no further approval prompts this session. Toggle with /yolo.")
        return "yes"
    if ans in ("a", "always"):
        return "always"
    if ans == "never":
        return "never"
    if ans in ("y", "yes"):
        return "yes"
    return "no"
