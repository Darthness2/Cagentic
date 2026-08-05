"""Input prompt with slash-command auto-completion.

Uses prompt_toolkit if available — gives a real popup as you type `/`.
Falls back to readline tab-completion, then plain input().
"""

from __future__ import annotations

# The one command catalog: (name, argument-spec, hint), grouped for /help.
#
# Both the completion popup and `/help` are built from this, so they can't drift
# apart the way two hand-maintained lists did. Keep a command's entry next to
# the others in its section; the REPL dispatches on the name in cli.repl().
COMMAND_GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("conversation", [
        ("/new",      "[title]",       "start a fresh conversation"),
        ("/resume",   "[id|number]",   "list or resume a saved conversation"),
        ("/sessions", "",              "list saved conversations"),
        ("/search",   "<text>",        "search saved conversations"),
        ("/save",     "[title]",       "force-save the current conversation"),
        ("/rename",   "<new title>",   "rename the current conversation"),
        ("/delete",   "<id|number>",   "delete a saved conversation"),
        ("/clear",    "",              "wipe history (the saved session stays)"),
        ("/retry",    "",              "re-run your last message"),
    ]),
    ("context", [
        ("/context",  "",              "show context token usage"),
        ("/compact",  "",              "summarize older turns, keep recent ones"),
        ("/effort",   "low|med|high",  "how hard the model works per turn"),
    ]),
    ("what I remember", [
        ("/notes",    "",              "list saved notes (my knowledge base)"),
        ("/note",     "<name>",        "show a single note"),
        ("/remind",   "[add <text>]",  "list reminders, or add one"),
        ("/todo",     "[add|done|clear]", "the session todo list"),
        ("/name",     "<your name>",   "tell me what to call you"),
    ]),
    ("files", [
        ("/cd",       "[path]",        "show or change the workspace directory"),
        ("/diff",     "[N]",           "show file edits from this session"),
        ("/undo",     "",              "revert the most recent file edit"),
    ]),
    ("tools & permissions", [
        ("/tools",    "",              "list the tools I can call"),
        ("/groups",   "[enable|disable <g>]", "which tool groups I'm given"),
        ("/plan",     "on|off",        "plan mode — read-only, no changes"),
        ("/yolo",     "[on|off]",      "auto-approve tool calls"),
    ]),
    ("connections", [
        ("/mcp",      "[server]",      "MCP servers, or one server's tools"),
        ("/browser",  "",              "Chrome extension status + setup"),
        ("/gateway",  "[off]",         "start or stop the web UI"),
        ("/login",    "<service> <key>", "save a github / openai / anthropic key"),
        ("/logout",   "<service>",     "remove a saved key"),
        ("/whoami",   "",              "show the authenticated GitHub user"),
    ]),
    ("system", [
        ("/model",    "[name]",        "show or switch model"),
        ("/models",   "",              "list available models"),
        ("/host",     "[url]",         "show or change the Ollama host"),
        ("/stream",   "on|off",        "toggle live token streaming"),
        ("/config",   "",              "show current config (tokens redacted)"),
        ("/set",      "<key> <value>", "set a config value"),
        ("/diag",     "",              "model / workspace / tools / data status"),
        ("/help",     "",              "show this list"),
        ("/exit",     "",              "leave Cagentic"),
    ]),
]

# Flat (name, hint) pairs for the completion popup, derived from the catalog.
SLASH_COMMANDS: list[tuple[str, str]] = [
    (name, f"{args}  —  {hint}" if args else hint)
    for _section, entries in COMMAND_GROUPS
    for name, args, hint in entries
] + [("/quit", "leave Cagentic")]

# Every command name the REPL knows, for the "did you mean" hint.
ALL_COMMANDS: list[str] = [name for name, _ in SLASH_COMMANDS] + ["/reminders"]


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
    # row.
    style = Style.from_dict(
        {
            "completion-menu": "bg:#241c2e #cdbbd8",
            "completion-menu.completion": "bg:#241c2e #cdbbd8",
            "completion-menu.completion.current": "bg:#e3a978 #2a1e10 bold",
            "completion-menu.meta": "bg:#241c2e #8f7f9e",
            "completion-menu.meta.current": "bg:#d39a6a #2a1e10",
            "scrollbar.background": "bg:#241c2e",
            "scrollbar.button": "bg:#8a6f86",
        }
    )

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
            return f"slash-command popup OFF — {reason}. TAB still completes /commands."
        return f"slash-command popup OFF — {reason}."

    def ask(self, prompt: str) -> str:
        if self._pt is not None:
            try:
                from prompt_toolkit.formatted_text import ANSI

                return self._pt.prompt(ANSI(prompt))
            except Exception:
                return self._pt.prompt(prompt)
        return input(prompt)
