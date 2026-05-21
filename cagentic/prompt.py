"""Input prompt with slash-command auto-completion.

Uses prompt_toolkit if available — gives a real popup as you type `/`.
Falls back to readline tab-completion, then plain input().
"""
from __future__ import annotations

# (name, hint) pairs shown in the popup. Personal-assistant flavored.
SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/help", "show available commands"),
    ("/tools", "list tools the model can call"),
    ("/groups", "show/change which tool groups are sent to the model"),
    ("/cd", "show or change the workspace directory"),
    ("/notes", "list saved notes (knowledge base)"),
    ("/note", "show a single note: /note <name>"),
    ("/remind", "list reminders, add one: /remind add <text>"),
    ("/mcp", "list MCP servers / tools: /mcp [server]"),
    ("/browser", "Chrome extension status + setup instructions"),
    ("/gateway", "start/stop the Cagentic web UI"),
    ("/plan", "toggle plan mode (read-only)"),
    ("/todo", "view or modify the session todo list"),
    ("/stream", "toggle token streaming on/off"),
    ("/diag", "print model / workspace / tools / mcp status"),
    ("/model", "show or switch model"),
    ("/models", "list installed Ollama models"),
    ("/host", "show or change Ollama host"),
    ("/config", "show current config (tokens redacted)"),
    ("/set", "set a config value: /set <key> <value>"),
    ("/name", "tell the assistant what to call you: /name Alex"),
    ("/login", "/login github <token>"),
    ("/logout", "/logout github"),
    ("/whoami", "show authenticated GitHub user"),
    ("/clear", "reset conversation history"),
    ("/diff", "show file edits this session"),
    ("/undo", "revert the most recent file edit"),
    ("/retry", "re-run your last message"),
    ("/new", "start a new conversation"),
    ("/resume", "list/resume saved conversations"),
    ("/sessions", "list saved conversations"),
    ("/save", "force-save / set title of current conversation"),
    ("/rename", "rename the current conversation"),
    ("/delete", "delete a saved conversation"),
    ("/yolo", "toggle / set auto-approve (/yolo on|off)"),
    ("/exit", "leave Cagentic"),
    ("/quit", "leave Cagentic"),
]


def _build_pt_session():
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.shortcuts import CompleteStyle
        from prompt_toolkit.styles import Style
    except ImportError as e:
        return None, f"prompt_toolkit not installed ({e}). Run: pip install prompt_toolkit"
    except Exception as e:
        return None, f"prompt_toolkit import failed: {type(e).__name__}: {e}"

    from .config import config_dir

    class SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith("/"):
                return
            if " " in text:
                return
            for name, hint in SLASH_COMMANDS:
                if name.startswith(text):
                    yield Completion(
                        name,
                        start_position=-len(text),
                        display=name,
                        display_meta=hint,
                    )

    history_path = config_dir() / "history"
    history_path.parent.mkdir(parents=True, exist_ok=True)

    # Slash-command popup styled in Cagentic's warm-dusk palette — dark
    # plum menu, soft mauve text, a copper-peach highlight on the selected
    # row (no leftover teal/blue from the Collama days).
    style = Style.from_dict({
        "completion-menu":                    "bg:#241c2e #cdbbd8",
        "completion-menu.completion":         "bg:#241c2e #cdbbd8",
        "completion-menu.completion.current": "bg:#e3a978 #2a1e10 bold",
        "completion-menu.meta":               "bg:#241c2e #8f7f9e",
        "completion-menu.meta.current":       "bg:#d39a6a #2a1e10",
        "scrollbar.background":               "bg:#241c2e",
        "scrollbar.button":                   "bg:#8a6f86",
    })

    try:
        session = PromptSession(
            completer=SlashCompleter(),
            complete_while_typing=True,
            complete_style=CompleteStyle.MULTI_COLUMN,
            reserve_space_for_menu=8,
            history=FileHistory(str(history_path)),
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
    def __init__(self) -> None:
        self._pt, self._pt_error = _build_pt_session()
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
            return (f"slash-command popup OFF — {reason}. "
                    f"TAB still completes /commands.")
        return f"slash-command popup OFF — {reason}."

    def ask(self, prompt: str) -> str:
        if self._pt is not None:
            try:
                from prompt_toolkit.formatted_text import ANSI
                return self._pt.prompt(ANSI(prompt))
            except Exception:
                return self._pt.prompt(prompt)
        return input(prompt)
