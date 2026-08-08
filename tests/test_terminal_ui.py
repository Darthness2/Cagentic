"""Regression coverage for the interactive terminal presentation layer."""

from __future__ import annotations

import os

import pytest

from cagentic import sessions, ui
from cagentic.agent import _RenderState, render_event
from cagentic.cli import _print_sessions, print_help
from cagentic.engine import Message
from cagentic.permissions import terminal_resolver
from cagentic.state import AppState


def _set_terminal(monkeypatch, columns: int, lines: int = 24) -> None:
    size = os.terminal_size((columns, lines))
    monkeypatch.setattr(ui.shutil, "get_terminal_size", lambda _fallback: size)


def _assert_fits(output: str, columns: int) -> None:
    for line in output.splitlines():
        assert ui._vlen(line) <= columns, repr(line)


def test_no_color_markdown_is_rendered_instead_of_leaking_syntax(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")

    rendered = ui.render_markdown(
        "## Result\n"
        "A **clear** answer with `code`, *emphasis*, and [docs](https://example.test).\n"
        "- first\n"
        "```py\nprint('ok')\n```"
    )

    assert rendered == (
        "Result\n"
        "A clear answer with code, emphasis, and docs (https://example.test).\n"
        "• first\n"
        "│ print('ok')"
    )
    assert "\x1b" not in rendered


# Answers that exercise every inline rule, plus the punctuation that must NOT
# be read as markup (arithmetic, snake_case, a bare dash, a stray backtick).
_MD_CORPUS = {
    "bullets with bold": (
        "Here are ideas:\n\n"
        "- **Watch something** — I can look up episodes\n"
        "- **Learn something cool** — always something going on\n"
        "* **Star bullet** — the other marker\n"
    ),
    "headers": "# Big\n\n## Medium\n\n### Small\n\nbody text after\n",
    "inline code": "Run `pip install -e .` then `pytest -q` to check.\n",
    "links": "See [the docs](https://example.com/a) and [more](https://x.y/z).\n",
    "nested indent": "  - indented bullet **bold** here\n    - deeper one\n",
    "italics": "This is *italic* and _also italic_ and 2 * 3 = 6.\n",
    "bold beside punctuation": "**Alpha**, **Beta**; **Gamma**! done\n",
    "unclosed bold": "this has **an unclosed span that never closes at all\n",
    "stray backtick": "a lone ` backtick and then plenty more text follows here\n",
    "snake_case": "call some_function_name(foo_bar) and file_name.py now\n",
    "mixed": (
        "## Plan\n\n"
        "- **step one** with `code` and [a link](http://e.co)\n"
        "- *just italic* and **bold** together\n\n"
        "Final `word`.\n"
    ),
    "implausibly long span": "x **" + ("y" * 400) + " tail\n",
    "dash that is not a bullet": "well - this is not a bullet, just a dash\n",
    "blank lines": "one\n\n\ntwo\n",
}


def _stream(chunks) -> str:
    out: list[str] = []
    md = ui.StreamMarkdown(emit=out.append)
    for chunk in chunks:
        md.feed(chunk)
    md.flush()
    return "".join(out)


@pytest.mark.parametrize("name", sorted(_MD_CORPUS))
@pytest.mark.parametrize("colored", [False, True])
def test_streamed_markdown_does_not_depend_on_chunk_boundaries(monkeypatch, name, colored):
    """What the user sees must not depend on where the network split the stream.

    ``render_markdown`` renders exactly the string it is handed, and the
    streaming renderer hands it mid-line fragments. A fragment boundary inside
    ``**bold**`` used to leave both halves unrendered, and a fragment that
    happened to *begin* with ``* `` was read as a bullet — which is how a clean
    answer reached the terminal as ``**Watch something*• — I can look up…``.
    Splitting inside a word was worse: ``some_function_name`` cut after
    ``some`` leaves ``_function_name``, whose ``_`` then satisfies the italic
    pattern's ``(?<!\\w)`` lookbehind and the underscores are eaten.

    So: for any chunking, the result must equal the one-shot render.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(ui, "_supports_color", lambda stream=None: colored)

    text = _MD_CORPUS[name]
    baseline = _stream([text])
    assert baseline  # a corpus entry that renders to nothing proves nothing

    for size in range(1, 41):
        chunks = [text[i : i + size] for i in range(0, len(text), size)]
        assert _stream(chunks) == baseline, f"chunk size {size} rendered differently"


def test_streamed_markdown_survives_ragged_chunk_boundaries(monkeypatch):
    """Providers do not split on a fixed stride — fuzz ragged splits too."""
    import random

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(ui, "_supports_color", lambda stream=None: True)

    rng = random.Random(20240607)
    for name, text in _MD_CORPUS.items():
        baseline = _stream([text])
        for _ in range(40):
            chunks, i = [], 0
            while i < len(text):
                step = rng.randint(1, 12)
                chunks.append(text[i : i + step])
                i += step
            assert _stream(chunks) == baseline, f"{name} rendered differently when split ragged"


def test_banner_and_messages_fit_a_narrow_terminal(monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    _set_terminal(monkeypatch, 36)

    ui.banner(
        "openai:a-model-name-that-is-unusually-long",
        "/Users/example/a/very/long/workspace/path",
        tools_enabled=False,
        user_name="Alexandria",
        version="1.2.3",
        plan_mode=True,
    )
    ui.assistant(
        "## Result\nA response with a very-long-unbroken-token.example/path/to/a/resource."
    )
    ui.info("A status message that needs to wrap cleanly.")
    ui.warn("A warning that needs to wrap cleanly.")
    ui.error("An error that needs to wrap cleanly.")

    captured = capsys.readouterr()
    _assert_fits(captured.out, 36)
    _assert_fits(captured.err, 36)
    assert "##" not in captured.out
    assert "Cagentic" in captured.out
    assert "1.2.3" in captured.out
    assert "ready" in captured.out
    assert "model:" in captured.out
    assert "workspace:" in captured.out
    assert "plan" in captured.out
    assert "◆" not in captured.out


def test_permission_prompt_has_safe_hierarchy_and_explicit_choices(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr("builtins.input", lambda _prompt: "a")
    state = AppState(workspace=tmp_path, home=tmp_path)

    answer = terminal_resolver("run_bash", {"command": "python -m pytest -q"}, state)

    assert answer == "always"
    output = capsys.readouterr().out
    flattened = " ".join(output.split())
    assert "APPROVAL REQUIRED" in flattened
    assert "Run shell command" in flattened
    assert "python -m pytest -q" in flattened
    assert "workspace:" in flattened
    assert "Enter or n deny" in flattened
    assert "never deny this tool going forward" in flattened
    assert "yolo auto-approves all changes" in flattened


def test_unknown_permission_choice_is_visibly_denied(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr("builtins.input", lambda _prompt: "maybe")
    state = AppState(workspace=tmp_path, home=tmp_path)

    assert terminal_resolver("write_file", {"path": "important.txt"}, state) == "no"
    assert "unknown approval choice 'maybe' · denied" in capsys.readouterr().out


def test_help_reflows_and_advertises_only_quit(monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    _set_terminal(monkeypatch, 48)

    print_help()

    output = capsys.readouterr().out
    _assert_fits(output, 48)
    assert "/quit" in output
    assert "/exit" not in output


def test_session_list_reflows_metadata(monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    _set_terminal(monkeypatch, 40)
    monkeypatch.setattr(
        sessions,
        "list_all",
        lambda: [
            {
                "id": "session-123456789",
                "title": "A conversation title that is much too long for the viewport",
                "updated_at": 0,
                "turns": 12,
                "model": "openai:a-very-long-model-name",
            }
        ],
    )

    listed = _print_sessions(active_id="session-123456789")

    output = capsys.readouterr().out
    assert listed
    assert "A conversation title" in output
    assert "12 turns" in output
    _assert_fits(output, 40)


def test_panel_wraps_long_titles_and_unbroken_content(monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    _set_terminal(monkeypatch, 24)

    ui.panel(
        "a-very-long-unbroken-value-that-must-wrap",
        title="A title far too long",
    )

    _assert_fits(capsys.readouterr().out, 24)


def test_color_controls_cover_dumb_term_and_explicit_override(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("CAGENTIC_COLOR", "auto")
    assert ui.color("hello", ui.GLOW) == "hello"

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("CAGENTIC_COLOR", "always")
    assert "\x1b[" in ui.color("hello", ui.GLOW)


def test_reduced_motion_and_dumb_term_disable_cursor_painting(monkeypatch):
    stream = type("TTY", (), {"isatty": lambda self: True})()
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.delenv("CAGENTIC_MOTION", raising=False)
    assert ui.supports_cursor_control(stream) is False
    assert ui.motion_enabled(stream) is False

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("CAGENTIC_MOTION", "reduce")
    assert ui.supports_cursor_control(stream) is True
    assert ui.motion_enabled(stream) is False


def test_repeated_tool_calls_do_not_leak_cursor_escapes_to_logs(monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    state = _RenderState()
    for summary in ("one.py", "two.py"):
        render_event(Message("tool_call", {"name": "read_file", "summary": summary}), state)
        render_event(
            Message(
                "tool_result",
                {
                    "name": "read_file",
                    "result": f"OK: {summary} (1 line)",
                    "first_line": f"OK: {summary} (1 line)",
                    "ok": True,
                },
            ),
            state,
        )

    output = capsys.readouterr().out
    assert output.count("read_file") == 2
    assert "\x1b" not in output


def test_untrusted_terminal_controls_are_removed(monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    _set_terminal(monkeypatch, 40)

    ui.info("\x1b[2Jvisible\x00 text")
    ui.assistant("answer \x1b]0;spoofed title\x07done")

    output = capsys.readouterr().out
    assert "visible text" in output
    assert "answer done" in output
    assert "\x1b" not in output
    assert "\x00" not in output


@pytest.mark.parametrize("columns", [32, 48, 80])
@pytest.mark.parametrize("colored", [False, True])
def test_core_render_matrix_fits_viewport(monkeypatch, capsys, columns, colored):
    _set_terminal(monkeypatch, columns)
    if colored:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "xterm-256color")
        monkeypatch.setenv("CAGENTIC_COLOR", "always")
    else:
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setenv("CAGENTIC_COLOR", "auto")

    ui.banner("openai:gpt-4o", "/Users/example/project/with/a/long/path", True, "Alex")
    ui.assistant("## Summary\nA concise response with **emphasis** and a long/path/value.")
    ui.plan(["Inspect the current state", "Apply the update", "Verify the result"])
    ui.tool_call("read_file", "/Users/example/project/a/long/file.py")
    ui.tool_result("184 lines")
    print_help()

    output = capsys.readouterr().out
    _assert_fits(output, columns)
    assert ("\x1b[" in output) is colored
