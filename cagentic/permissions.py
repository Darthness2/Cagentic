"""can_use_tool(): formal permission gate.

Decision sources, in order:
    1. Dry-run hard gate for every mutating tool.
    2. Deny rules — beat everything below, including the cache and yolo. A
       deny list that any later source could override wouldn't be a deny list.
    3. Per-tool cache in AppState.permissions ('always'/'never').
    4. Plan-mode gate.
    5. Read-only tools — always allowed without prompting.
    6. Allow rules — pattern matches like run_bash(git status*).
    7. accept_edits mode — workspace-contained file changes go through.
    8. yolo mode — always allowed.
    9. Resolver callback — interactive y/n/a prompt for the REPL,
       or auto-decide for the SDK / headless caller.

Rules are `tool` or `tool(glob)`, where the glob is matched against the one
argument that decides what the call actually does — the command for a shell
tool, the path for a file tool, "server/tool" for mcp_call (see _subjects).
They live under "permissions" in config, and are merged from the user's
config.json plus the workspace's .cagentic/settings.json and
.cagentic/settings.local.json, so a project can ship its own approvals.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
from pathlib import Path
from typing import Callable

from .state import AppState

_log = logging.getLogger(__name__)

# Tools that don't mutate anything we care about — always allowed.
READ_ONLY: set[str] = {
    # files / search / shell-less inspection
    "read_file",
    "list_dir",
    "grep",
    "glob",
    "tool_search",
    # asking the user is interactive but not destructive — skip the
    # extra approval prompt so it just shows the question.
    "ask_user_question",
    # web
    "web_fetch",
    "web_search",
    # tasks / state inspection
    "task_get",
    "task_list",
    "task_output",
    "task_status",
    "config_get",
    "sleep",
    # coding inspection (parser-only; never executes the code under test)
    "check_syntax",
    # teams inspection
    "inbox",
    "team_list",
    "teammate_list",
    # notes / reminders read paths
    "note_get",
    "note_list",
    "note_search",
    "reminder_list",
    "goal_list",
    "calendar_event_list",
    "personal_briefing",
    "calendar_connection_list",
    "notification_list",
    "inbox_list",
    "email_connection_list",
    "routine_list",
    # mcp inspection
    "mcp_list_servers",
    "mcp_list_tools",
    "mcp_list_resources",
    "mcp_read_resource",
    # browser inspection (acting in the browser is gated; looking is not)
    "browser_status",
    "browser_tabs",
    "browser_read",
    "browser_screenshot",
    "browser_links",
    # presentation
    "show_widget",
    # github read
    "gh_whoami",
    "gh_list_repos",
    "gh_get_repo",
    "gh_get_file",
    "gh_list_issues",
    "gh_list_pulls",
    "gh_get_pull",
    "gh_search_code",
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
    "enter_plan_mode",
    "exit_plan_mode",
    "ask_user_question",
    "todo_write",
    "tool_search",
    "task_get",
    "task_list",
    "task_output",
    "config_get",
}


# Project-level rule files, most general first — a later file wins, and
# settings.local.json is the gitignored personal layer on top of the shared one.
PROJECT_SETTINGS_FILES = ("settings.json", "settings.local.json")
PROJECT_SETTINGS_DIR = ".cagentic"

# Tools whose only real effect is a file change inside the workspace. These are
# what accept_edits mode waves through; shell/network/browser deliberately are
# not, because their blast radius isn't bounded by the workspace.
EDIT_TOOLS: frozenset[str] = frozenset(
    {"write_file", "edit_file", "replace_lines", "multi_edit", "notebook_edit"}
)

# (path, mtime) -> parsed rules, so the gate doesn't re-read and re-parse two
# JSON files on every single tool call.
_PROJECT_CACHE: dict[str, tuple[float, dict]] = {}


def _subjects(name: str, args: dict) -> list[str]:
    """The strings a rule's glob is matched against for this call.

    More than one because a path is worth matching both as the model wrote it
    and as an absolute path — a rule saying `edit_file(src/*)` should hold
    whether the model passed "src/a.py" or "/home/me/proj/src/a.py".
    """
    if name in ("run_bash", "bash_async", "powershell"):
        return [str(args.get("command") or "")]
    if name == "mcp_call":
        return [f"{args.get('server', '')}/{args.get('tool', '')}"]
    if name.startswith("browser_"):
        return [str(args.get("url") or args.get("selector") or "")]
    if name.startswith("gh_") or name == "github_api":
        return [str(args.get("repo") or args.get("repository") or "")]
    path = args.get("path")
    if isinstance(path, str) and path:
        subjects = [path]
        try:
            resolved = str(Path(path).expanduser())
        except (OSError, ValueError, RuntimeError):
            resolved = ""
        if resolved and resolved != path:
            subjects.append(resolved)
        return subjects
    for key in ("name", "query", "url", "text"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return [value]
    return [""]


def _rule_matches(rule: str, name: str, args: dict) -> bool:
    """Does `rule` (either "tool" or "tool(glob)") cover this call?"""
    rule = (rule or "").strip()
    if not rule:
        return False
    if "(" not in rule or not rule.endswith(")"):
        return rule == name
    tool, _, pattern = rule[:-1].partition("(")
    if tool.strip() != name:
        return False
    pattern = pattern.strip()
    if not pattern or pattern == "*":
        return True
    for subject in _subjects(name, args):
        # fnmatch, not re: a glob is what a user can write correctly in a
        # config file, and '*' spanning '/' is the behaviour people expect
        # from `src/*`.
        if fnmatch.fnmatch(subject, pattern):
            return True
    return False


def _read_rules(blob: object) -> dict[str, list[str]]:
    """Pull {"allow": [...], "deny": [...]} out of an untrusted config blob."""
    out: dict[str, list[str]] = {"allow": [], "deny": []}
    if not isinstance(blob, dict):
        return out
    for key in ("allow", "deny"):
        entries = blob.get(key)
        if isinstance(entries, list):
            out[key] = [e for e in entries if isinstance(e, str) and e.strip()]
    return out


def _project_rules(workspace: Path | None) -> dict[str, list[str]]:
    """Rules declared by the workspace itself, if any."""
    merged: dict[str, list[str]] = {"allow": [], "deny": []}
    if workspace is None:
        return merged
    for filename in PROJECT_SETTINGS_FILES:
        path = workspace / PROJECT_SETTINGS_DIR / filename
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        key = str(path)
        cached = _PROJECT_CACHE.get(key)
        if cached is None or cached[0] != mtime:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # A malformed project settings file must not break the agent;
                # it just contributes no rules.
                _log.warning("ignoring unreadable %s", path)
                data = {}
            cached = (mtime, data if isinstance(data, dict) else {})
            _PROJECT_CACHE[key] = cached
        rules = _read_rules((cached[1] or {}).get("permissions"))
        merged["allow"].extend(rules["allow"])
        merged["deny"].extend(rules["deny"])
    return merged


def effective_rules(state: AppState) -> dict[str, list[str]]:
    """Rules in force right now: session-set ones plus the workspace's own.

    Resolved per call rather than cached on the state so that changing
    directory (or editing .cagentic/settings.json) takes effect immediately,
    without every call site having to remember to refresh.
    """
    session = _read_rules(getattr(state, "permission_rules", None))
    project = _project_rules(getattr(state, "workspace", None))
    return {
        "allow": session["allow"] + project["allow"],
        "deny": session["deny"] + project["deny"],
    }


def _matching_rule(rules: list[str], name: str, args: dict) -> str | None:
    for rule in rules:
        if _rule_matches(rule, name, args):
            return rule
    return None


def _is_workspace_edit(name: str, args: dict, state: AppState) -> bool:
    """True when this call only rewrites a file inside the workspace."""
    if name not in EDIT_TOOLS:
        return False
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return False
    try:
        from .tools import _resolve_contained

        # Raises PathEscapeError when the path leaves the workspace — which is
        # exactly the case accept_edits must not wave through.
        _resolve_contained(path, state.workspace)
    except Exception:
        return False
    return True


def can_use_tool(
    name: str,
    args: dict,
    state: AppState,
    resolver: Resolver = auto_deny_resolver,
) -> tuple[bool, str]:
    """Returns (allowed, reason)."""
    # Dry-run is stronger than every cached/session permission and cannot be
    # bypassed by yolo mode or by leaving plan mode.
    if getattr(state, "dry_run", False) and name not in READ_ONLY:
        return False, "dry run active — mutating tools blocked"

    rules = effective_rules(state)

    # Deny rules outrank everything below, including the session cache and
    # yolo. A "never run rm -rf" that yolo could switch off would be useless.
    denied = _matching_rule(rules["deny"], name, args)
    if denied is not None:
        return False, f"denied by rule {denied}"

    cached = state.permissions.get(name)
    if cached == "always":
        return True, "permission cache: always"
    if cached == "never":
        return False, "permission cache: never"

    # Plan-mode gate: only read-only tools are allowed. Above the allow rules
    # deliberately — plan mode is an explicit "not right now" and a standing
    # project approval must not quietly override it.
    if (
        getattr(state, "plan_mode", False)
        and name not in READ_ONLY
        and name not in PLAN_MODE_EXTRAS
    ):
        return False, "plan mode active — mutating tools blocked"

    if name in READ_ONLY:
        return True, "read-only tool"

    allowed = _matching_rule(rules["allow"], name, args)
    if allowed is not None:
        return True, f"allowed by rule {allowed}"

    if getattr(state, "approval_mode", "ask") == "accept_edits" and _is_workspace_edit(
        name, args, state
    ):
        return True, "accept-edits mode (workspace file change)"

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


# Tools whose effect is a file change we can show as a diff before approving.
DIFFABLE: frozenset[str] = frozenset({"write_file", "edit_file", "replace_lines", "multi_edit"})

# Keep the approval preview short enough to stay on one screen next to the
# prompt — the full change is always available afterwards via /diff.
PREVIEW_MAX_LINES = 24


def describe_change(name: str, args: dict, state: AppState, *, colorize: bool = True) -> str:
    """Rendered diff for a pending file change, or "" when there isn't one.

    Deliberately returns "" rather than raising or guessing: the approval
    prompt must still work when the file is unreadable, the match would fail,
    or the tool isn't a file mutation at all.
    """
    if name not in DIFFABLE:
        return ""
    try:
        from . import diff as _diff
        from .tools import preview_change

        planned = preview_change(name, args, state.workspace)
        if planned is None:
            return ""
        path, before, after = planned
        if before == after:
            return "  (no change — the file already matches)"
        body = _diff.render(before, after, path, max_lines=PREVIEW_MAX_LINES, colorize=colorize)
        if not body:
            return ""
        adds, dels = _diff.stats(before, after)
        header = f"  {path}  +{adds} -{dels}"
        return header + "\n" + body
    except Exception:
        # A broken preview must never block the approval it is only annotating.
        return ""


def suggest_rule(name: str, args: dict) -> str:
    """A narrow allow-rule covering this call, or "" if none reads naturally.

    Offered at the prompt so approving `git status` grants `git status`, not
    every shell command the model ever wants to run.
    """
    if name in ("run_bash", "bash_async", "powershell"):
        command = str(args.get("command") or "").strip()
        if not command:
            return ""
        # First two words is the useful granularity: "git status", "npm test",
        # "pytest -q" → "pytest". Anything with a shell operator is too varied
        # to generalise safely, so don't offer a rule at all.
        if any(ch in command for ch in "|;&><$`"):
            return ""
        head = " ".join(command.split()[:2])
        return f"{name}({head}*)" if head else ""
    if name == "mcp_call":
        server = str(args.get("server") or "").strip()
        return f"mcp_call({server}/*)" if server else ""
    if name in EDIT_TOOLS or name == "read_file":
        path = args.get("path")
        if isinstance(path, str) and path:
            parent = str(Path(path).parent).replace(os.sep, "/")
            if parent and parent not in (".", "/"):
                return f"{name}({parent}/*)"
    return ""


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

    ui.prepare_for_input()

    action = "Run tool"
    detail: object = ""
    command: object | None = None
    if name in ("run_bash", "bash_async", "powershell"):
        action = "Run shell command"
        command = args.get("command", "")
        if args.get("network"):
            # The sandbox denies network by default, so a call asking for it is
            # materially different from a normal command and must say so.
            action = "Run shell command WITH NETWORK ACCESS"
    elif name == "write_file":
        action = "Write file"
        detail = args.get("path", "")
    elif name == "edit_file":
        action = "Edit file"
        detail = args.get("path", "")
    elif name == "note_write":
        action = "Save persistent note"
        detail = args.get("name", "")
    elif name == "reminder_add":
        action = "Create reminder"
        detail = str(args.get("text", ""))[:80]
    elif name == "mcp_call":
        action = "Call connected service"
        detail = f"{args.get('server', '?')}/{args.get('tool', '?')}"
    elif name.startswith("browser_"):
        action = "Control browser"
        detail = args.get("url") or args.get("selector") or name.removeprefix("browser_")
    elif name.startswith("gh_") or name == "github_api":
        action = "Change GitHub data"
        detail = args.get("repo") or args.get("repository") or name

    print()
    ui.heading("Approval required")
    print()
    ui.list_item(action, detail=f"tool: {name}", marker="!")
    if command is not None:
        print()
        ui.code_block(str(command))
    # Show the actual patch, not just "replace 1 occurrence(s)" — approving a
    # change you can't see was the biggest trust gap in the REPL.
    patch = describe_change(name, args, state)
    if patch:
        print()
        print(patch)
    if detail:
        ui.field("target", detail)
    ui.field("workspace", ui._short_path(str(state.workspace)))
    print()
    ui.meta(f"Enter or n deny · y allow once · a always allow {name}")
    rule = suggest_rule(name, args)
    if rule:
        # Offer the narrow standing approval before the blanket one — "always
        # allow every shell command" is a much bigger grant than the user
        # usually means when they approve `git status`.
        ui.meta(f"rule: allow just {rule}")
    ui.meta("advanced: never deny this tool going forward · yolo auto-approves all changes")
    try:
        prompt_hint = "y/N/a/rule" if rule else "y/N/a"
        ans = ui.input_prompt("Decision", prompt_hint).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        ui.meta("approval cancelled · denied")
        return "no"

    if ans == "rule" and rule:
        rules = dict(getattr(state, "permission_rules", None) or {})
        allow = list(rules.get("allow") or [])
        if rule not in allow:
            allow.append(rule)
        rules["allow"] = allow
        state.update(permission_rules=rules)
        ui.info(f"rule added: {rule} · session only, persist it with /rules allow {rule}")
        return "yes"
    if ans in ("yolo", "all"):
        state.update(yolo=True)
        ui.warn("yolo mode enabled for this session · disable with /yolo off")
        return "yes"
    if ans in ("a", "always"):
        return "always"
    if ans == "never":
        return "never"
    if ans in ("y", "yes"):
        return "yes"
    if ans not in ("", "n", "no"):
        ui.warn(f"unknown approval choice {ans!r} · denied")
    return "no"
