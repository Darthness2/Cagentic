"""Interactive input with command and workspace-file completion.

Uses prompt_toolkit if available — giving a real popup for slash commands and
``@path`` attachments, persistent history, and an explicit multiline binding.
Falls back to readline tab-completion, then plain input().
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from .config import is_secret_key

# The one command catalog: (name, argument-spec, hint), grouped for /help.
#
# Both the completion popup and `/help` are built from this, so they can't drift
# apart the way two hand-maintained lists did. Keep a command's entry next to
# the others in its section; the REPL dispatches on the name in cli.repl().
COMMAND_GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
    (
        "conversation",
        [
            ("/new", "[title]", "start a fresh conversation"),
            ("/resume", "[id|number]", "list or resume a saved conversation"),
            ("/sessions", "", "list saved conversations"),
            ("/search", "<text>", "search saved conversations"),
            ("/save", "[title]", "force-save the current conversation"),
            ("/rename", "<new title>", "rename the current conversation"),
            ("/delete", "<id|number>", "delete a saved conversation"),
            ("/clear", "", "wipe history (the saved session stays)"),
            ("/retry", "", "regenerate the most recent reply"),
        ],
    ),
    (
        "context",
        [
            ("/context", "", "show context token usage"),
            ("/compact", "", "summarize older turns, keep recent ones"),
            ("/effort", "[low|medium|high]", "show or set reasoning effort"),
        ],
    ),
    (
        "what I remember",
        [
            ("/notes", "", "list saved notes (my knowledge base)"),
            ("/note", "<name>", "show a single note"),
            ("/remind", "[all|add|done|delete|clear]", "manage persistent reminders"),
            ("/todo", "[add <text>|done <n>|clear]", "manage the session todo list"),
            ("/name", "[your name]", "show or set what to call you"),
        ],
    ),
    (
        "files",
        [
            ("/cd", "[path]", "show or change the workspace directory"),
            ("/init", "[force]", "write an AGENTS.md describing this project"),
            ("/diff", "[N]", "show file edits from this session"),
            ("/undo", "", "revert the most recent file edit"),
            ("/rewind", "[n]", "undo a whole turn — files and conversation"),
        ],
    ),
    (
        "tools & permissions",
        [
            ("/tools", "", "list the tools I can call"),
            ("/groups", "[enable|disable <g>]", "which tool groups I'm given"),
            ("/plan", "[on|off]", "plan mode — read-only, no changes"),
            ("/accept", "[on|off]", "auto-approve file edits in the workspace"),
            ("/yolo", "[on|off]", "auto-approve tool calls"),
            ("/rules", "[allow|deny <rule>|remove <rule>]", "pattern permission rules"),
        ],
    ),
    (
        "connections",
        [
            ("/mcp", "[server]", "MCP servers, or one server's tools"),
            ("/browser", "", "Chrome extension status + setup"),
            ("/gateway", "[on|off]", "start or stop the web UI"),
            ("/login", "<service>", "save a key using a hidden prompt"),
            ("/logout", "<service>", "remove a saved key"),
            ("/whoami", "", "show the authenticated GitHub user"),
        ],
    ),
    (
        "system",
        [
            ("/model", "[name]", "show or switch model"),
            ("/models", "", "list available models"),
            ("/host", "[url]", "show or change the Ollama host"),
            ("/stream", "[on|off]", "toggle live token streaming"),
            ("/config", "", "show current config (tokens redacted)"),
            ("/set", "<key> <value>", "set a config value"),
            ("/diag", "", "model / workspace / tools / data status"),
            ("/help", "", "show this list"),
            ("/quit", "", "leave Cagentic"),
        ],
    ),
]

# Flat (name, hint) pairs for the completion popup, derived from the catalog.
SLASH_COMMANDS: list[tuple[str, str]] = [
    (name, f"{args}  —  {hint}" if args else hint)
    for _section, entries in COMMAND_GROUPS
    for name, args, hint in entries
]

# Every command name the REPL knows, for the "did you mean" hint.
ALL_COMMANDS: list[str] = [name for name, _ in SLASH_COMMANDS] + ["/reminders"]


def _toolbar_clean(value: object) -> str:
    """Collapse untrusted runtime labels into one terminal-safe line."""
    return " ".join(
        "".join(char for char in str(value) if ord(char) >= 32 and ord(char) != 127).split()
    )


def _toolbar_path(value: object) -> str:
    text = _toolbar_clean(value)
    try:
        home = str(Path.home())
        if text == home:
            return "~"
        if text.startswith(home + os.sep):
            return "~" + text[len(home) :]
    except (OSError, RuntimeError):
        pass
    return text


def _toolbar_text(context: dict[str, object], columns: int) -> str:
    """Compose a responsive, information-first prompt footer.

    Runtime state wins space over shortcut hints. This keeps safety modes
    visible even in a narrow split pane, while wider terminals also advertise
    the highest-value keyboard interactions.
    """
    columns = max(1, columns)
    model = _toolbar_clean(context.get("model", "model")) or "model"
    workspace = _toolbar_path(context.get("workspace", "~")) or "~"
    mode = _toolbar_clean(context.get("mode", "act")) or "act"
    approval = _toolbar_clean(context.get("approval", "ask")) or "ask"
    tools = _toolbar_clean(context.get("tools", "tools on")) or "tools on"

    if len(model) > 24:
        model = model[:23] + "…"
    if len(workspace) > 28:
        workspace = "…" + workspace[-27:]

    if columns >= 104:
        parts = [mode, approval, tools, model, workspace]
        controls = "Enter send · Esc+Enter newline · / commands · @ files"
    elif columns >= 72:
        parts = [mode, approval, model, workspace]
        controls = "/ commands · @ files"
    elif columns >= 48:
        parts = [mode, approval, workspace]
        controls = "/ · @"
    else:
        parts = [mode, approval]
        controls = "/ · @"

    separator = "  |  "
    status_budget = max(1, columns - len(separator) - len(controls) - 2)
    status = " · ".join(parts)
    if len(status) > status_budget:
        status = status[: max(0, status_budget - 1)] + "…"
    text = " " + status + separator + controls + " "
    if len(text) <= columns:
        return text
    if columns == 1:
        return "…"
    return text[: columns - 1] + "…"


def _attachment_fragment(text: str) -> tuple[str, str, str | None] | None:
    """Return ``(raw, path, quote)`` for an unfinished ``@path`` token.

    ``@`` inside an email address is ignored. Quoted paths deliberately stay
    open while a directory is being completed, so users can drill into names
    containing spaces without escaping each segment.
    """
    at = text.rfind("@")
    if at < 0 or (at > 0 and not text[at - 1].isspace()):
        return None
    raw = text[at + 1 :]
    if not raw:
        return raw, raw, None
    if raw[0] in {'"', "'"}:
        quote = raw[0]
        fragment = raw[1:]
        if quote in fragment:
            return None
        return raw, fragment, quote
    if any(char.isspace() for char in raw):
        return None
    return raw, raw, None


def _attachment_completions(
    text: str,
    workspace: Path,
    *,
    limit: int = 80,
) -> list[tuple[str, str, str, int]]:
    """Build deterministic completion rows for the active ``@path`` token.

    Each row is ``(replacement, display, detail, replace_length)``. Keeping
    filesystem work in this small pure helper makes the prompt behavior easy
    to regression-test without starting a terminal application.
    """
    parsed = _attachment_fragment(text)
    if parsed is None:
        return []
    raw, fragment, quote = parsed
    expanded = os.path.expandvars(fragment)
    fragment_path = Path(expanded).expanduser()
    ends_with_separator = bool(fragment) and fragment.endswith(("/", "\\"))
    if ends_with_separator:
        literal_parent = fragment
        name_prefix = ""
        scan_parent = fragment_path
    else:
        literal_parent_path = Path(fragment).parent
        literal_parent = "" if str(literal_parent_path) == "." else str(literal_parent_path)
        name_prefix = Path(fragment).name
        scan_parent = fragment_path.parent

    if not scan_parent.is_absolute():
        scan_parent = workspace / scan_parent
    try:
        entries = list(scan_parent.iterdir())
    except (OSError, ValueError):
        return []

    def _is_dir(path: Path) -> bool:
        try:
            return path.is_dir()
        except OSError:
            return False

    entries.sort(key=lambda path: (not _is_dir(path), path.name.casefold()))
    rows: list[tuple[str, str, str, int]] = []
    for entry in entries:
        if len(rows) >= limit:
            break
        if not entry.name.casefold().startswith(name_prefix.casefold()):
            continue
        if entry.name.startswith(".") and not name_prefix.startswith("."):
            continue
        candidate = str(Path(literal_parent) / entry.name) if literal_parent else entry.name
        directory = _is_dir(entry)
        if directory:
            candidate += os.sep
        needs_quote = re.fullmatch(r"[~\w./\\-]+", candidate) is None
        if quote:
            if quote in candidate:
                continue
            replacement = quote + candidate + ("" if directory else quote)
        elif needs_quote:
            chosen_quote = '"' if '"' not in candidate else "'"
            if chosen_quote in candidate:
                continue
            replacement = chosen_quote + candidate + ("" if directory else chosen_quote)
        else:
            replacement = candidate
        display = entry.name + (os.sep if directory else "")
        rows.append((replacement, display, "directory" if directory else "file", len(raw)))
    return rows


def _safe_for_history(text: str) -> bool:
    """Keep credentials out of prompt_toolkit/readline history."""
    parts = text.strip().split(maxsplit=2)
    if not parts:
        return True
    command = parts[0].lower()
    if command == "/login":
        return False
    if command == "/set" and len(parts) >= 2 and is_secret_key(parts[1]):
        return False
    return True


def _build_pt_session(
    *,
    persist_history: bool = True,
    workspace_provider: Callable[[], Path] | None = None,
    context_provider: Callable[[], dict[str, object]] | None = None,
):
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory, InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.shortcuts import CompleteStyle
        from prompt_toolkit.styles import Style
    except ImportError as e:
        return None, f"prompt_toolkit not installed ({e}). Run: pip install prompt_toolkit"
    except Exception as e:
        return None, f"prompt_toolkit import failed: {type(e).__name__}: {e}"

    from .config import config_dir

    class InputCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if text.startswith("/") and " " not in text:
                for name, hint in SLASH_COMMANDS:
                    if name.startswith(text):
                        yield Completion(
                            name,
                            start_position=-len(text),
                            display=name,
                            display_meta=hint,
                        )
                return
            try:
                workspace = workspace_provider() if workspace_provider else Path.cwd()
            except (OSError, RuntimeError):
                workspace = Path.cwd()
            for replacement, display, detail, replace_length in _attachment_completions(
                text,
                workspace,
            ):
                yield Completion(
                    replacement,
                    start_position=-replace_length,
                    display=display,
                    display_meta=detail,
                )

    class SafeFileHistory(FileHistory):
        def store_string(self, string: str) -> None:
            if _safe_for_history(string):
                super().store_string(string)

    # Restrained graphite/indigo completion menu matching the transcript UI.
    style = Style.from_dict(
        {
            "completion-menu": "bg:#202331 #d5d9e5",
            "completion-menu.completion": "bg:#202331 #aeb8d8",
            "completion-menu.completion.current": "bg:#5f6fa8 #ffffff bold",
            "completion-menu.meta": "bg:#202331 #8b93a7",
            "completion-menu.meta.current": "bg:#5f6fa8 #e8eaf2",
            "scrollbar.background": "bg:#202331",
            "scrollbar.button": "bg:#596176",
            "bottom-toolbar": "bg:#171923 #8b93a7",
        }
    )

    bindings = KeyBindings()

    @bindings.add("escape", "enter")
    def _insert_newline(event) -> None:
        event.current_buffer.insert_text("\n")

    history: Any
    if persist_history:
        history_path = config_dir() / "history"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history = SafeFileHistory(str(history_path))
    else:
        history = InMemoryHistory()

    try:

        def _bottom_toolbar() -> str:
            try:
                context = context_provider() if context_provider else {}
            except (OSError, RuntimeError):
                context = {}
            columns = shutil.get_terminal_size((80, 24)).columns
            return _toolbar_text(context, columns)

        session: Any = PromptSession(
            completer=InputCompleter(),
            complete_while_typing=True,
            complete_in_thread=True,
            complete_style=CompleteStyle.COLUMN,
            reserve_space_for_menu=10,
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            enable_history_search=True,
            search_ignore_case=True,
            key_bindings=bindings,
            bottom_toolbar=_bottom_toolbar,
            style=style,
        )
    except Exception as e:
        return None, f"prompt_toolkit session build failed: {type(e).__name__}: {e}"
    return session, None


def _install_readline_fallback() -> bool:
    try:
        import readline
    except ImportError:
        return False

    names = [c[0] for c in SLASH_COMMANDS]

    def completer(text, state):
        if not text.startswith("/"):
            return None
        matches = [n for n in names if n.startswith(text)]
        return matches[state] if state < len(matches) else None

    readline.set_completer(completer)
    readline.parse_and_bind("tab: complete")
    readline.set_completer_delims(" \t\n")
    return True


class Prompt:
    def __init__(self, *, persist_history: bool = True) -> None:
        self._workspace_provider: Callable[[], Path] = Path.cwd
        self._context_provider: Callable[[], dict[str, object]] = lambda: {}
        self._pt, self._pt_error = _build_pt_session(
            persist_history=persist_history,
            workspace_provider=lambda: self._workspace_provider(),
            context_provider=lambda: self._context_provider(),
        )
        if self._pt is None:
            self._readline = _install_readline_fallback()
        else:
            self._readline = False

    @property
    def backend(self) -> str:
        if self._pt is not None:
            return "prompt_toolkit"
        if self._readline:
            return "readline"
        return "plain"

    @property
    def status_note(self) -> str | None:
        if self._pt is not None:
            return None
        reason = self._pt_error or "prompt_toolkit unavailable"
        if self._readline:
            return f"command menu unavailable — {reason}. Tab completion is still active."
        return f"command menu unavailable — {reason}."

    def set_workspace_provider(self, provider: Callable[[], Path]) -> None:
        """Keep ``@path`` completion aligned with the REPL's live ``/cd`` state."""
        self._workspace_provider = provider

    def set_context_provider(self, provider: Callable[[], dict[str, object]]) -> None:
        """Supply live mode/model data for the responsive bottom toolbar."""
        self._context_provider = provider

    def ask(self, prompt: str) -> str:
        if self._pt is not None:
            rendered_prompt: Any
            try:
                from prompt_toolkit.formatted_text import ANSI

                rendered_prompt = ANSI(prompt)
            except Exception:
                rendered_prompt = prompt
            return self._pt.prompt(rendered_prompt)
        result = input(prompt)
        if self._readline and not _safe_for_history(result):
            try:
                import readline

                index = readline.get_current_history_length() - 1
                if index >= 0:
                    readline.remove_history_item(index)
            except (ImportError, OSError, ValueError):
                pass
        return result
