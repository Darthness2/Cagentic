"""Terminal UI primitives for a quiet, responsive Cagentic CLI.

The module intentionally has no rendering dependency.  Every public helper
must remain useful in a narrow terminal, when color is disabled, and when its
output is redirected to a file.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
import unicodedata

# ----- Windows ANSI passthrough --------------------------------------------
# Older Windows shells don't process ANSI escape sequences by default; the
# escapes leak as raw text (`^[[38;5;49m`). Enable Virtual Terminal Processing
# on stdout/stderr so our color/cursor codes work in cmd.exe / PowerShell /
# Windows Terminal without needing colorama.


def _enable_windows_ansi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = getattr(ctypes, "windll").kernel32
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(handle_id)
            if not handle or handle == ctypes.c_void_p(-1).value:
                continue
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(
                    handle,
                    mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING,
                )
    except Exception:
        pass


def _ensure_utf8_output() -> None:
    """Make stdout/stderr able to carry the UI's glyphs (✦ ◦ ↳ ✓ ─ …).

    Attached to a real console, Python encodes output as UTF-16 and everything
    works. Redirected to a pipe or a file it falls back to the locale encoding
    — cp1252 on most Windows installs — and the very first banner line dies
    with UnicodeEncodeError. `cagentic -p "hi" > out.txt` crashed for that
    reason alone. Re-encode as UTF-8, and never let an unmappable glyph raise.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # not a TextIOWrapper (test capture, etc.)
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # Already-detached or non-reconfigurable stream — leave it be.
            pass


_enable_windows_ansi()
_ensure_utf8_output()


# ANSI base
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ITALIC = "\033[3m"

# Standard 16-color codes (kept for fallbacks)
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
GRAY = "\033[90m"

# Semantic 256-color palette.  The graphite/indigo base is deliberately
# restrained: color communicates hierarchy and state instead of decorating
# every line.  The historical names stay public because other modules import
# them, but each now maps to one semantic role.
DUSK = "\033[38;5;111m"  # primary blue
GLOW = "\033[38;5;147m"  # bright indigo; brand and prompt
PLUM = "\033[38;5;60m"  # structural slate
GOLD = "\033[38;5;180m"  # focused accent
SURFACE = "\033[38;5;252m"  # primary text
MUTED = "\033[38;5;245m"  # secondary text
SOFT = "\033[38;5;242m"  # tertiary text and rules
WARN = "\033[38;5;214m"  # warning amber
ERR = "\033[38;5;203m"  # error red
OK = "\033[38;5;114m"  # success green


def _supports_color(stream=None) -> bool:
    """Honor common color controls and only style an interactive stream."""
    mode = os.environ.get("CAGENTIC_COLOR", "auto").strip().lower()
    if mode in {"never", "0", "false", "off"}:
        return False
    if os.environ.get("NO_COLOR") is not None or os.environ.get("CLICOLOR") == "0":
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    if mode in {"always", "1", "true", "on"} or os.environ.get("CLICOLOR_FORCE"):
        return True
    target = stream or sys.stdout
    return bool(getattr(target, "isatty", lambda: False)())


def supports_cursor_control(stream=None) -> bool:
    """Return whether transient cursor painting is safe for ``stream``.

    Color and cursor movement are separate capabilities: a user may disable
    color but still have a capable TTY, while ``TERM=dumb`` and redirected
    output must never receive erase-line or cursor-up escape sequences.
    """
    target = stream or sys.stdout
    if not bool(getattr(target, "isatty", lambda: False)()):
        return False
    if os.environ.get("TERM", "").strip().lower() == "dumb":
        return False
    configured = os.environ.get("CAGENTIC_CURSOR_CONTROL", "auto").strip().lower()
    return configured not in {"never", "off", "0", "false", "no"}


def motion_enabled(stream=None) -> bool:
    """Honor a terminal-friendly reduced-motion preference."""
    configured = os.environ.get("CAGENTIC_MOTION", "auto").strip().lower()
    if configured in {"reduce", "reduced", "never", "off", "0", "false", "no"}:
        return False
    return supports_cursor_control(stream)


def color(text: str, c: str, stream=None) -> str:
    if not _supports_color(stream):
        return text
    return f"{c}{text}{RESET}"


# ---------- screen / sizing ----------


def clear_screen() -> None:
    if not supports_cursor_control():
        return
    sys.stdout.write("\033[3J\033[2J\033[H")
    sys.stdout.flush()


def width() -> int:
    try:
        return max(1, min(shutil.get_terminal_size((80, 24)).columns, 120))
    except OSError:
        return 80


_ANSI_RX = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[()][A-Z0-9]|[0-~])")


def _strip_ansi(s: str) -> str:
    return _ANSI_RX.sub("", s)


def sanitize(text: object) -> str:
    """Remove terminal control sequences from untrusted display text."""
    stripped = _strip_ansi(str(text)).expandtabs(4)
    return "".join(
        char for char in stripped if char == "\n" or (ord(char) >= 32 and ord(char) != 127)
    )


def single_line(text: object) -> str:
    """Sanitize and collapse a label or identifier onto one display line."""
    return " ".join(sanitize(text).split())


def _char_width(char: str) -> int:
    if not char or unicodedata.combining(char):
        return 0
    if unicodedata.category(char) in {"Cc", "Cf"}:
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _vlen(s: str) -> int:
    return sum(_char_width(char) for char in _strip_ansi(s))


def _pad(s: str, w: int) -> str:
    return s + " " * max(0, w - _vlen(s))


def truncate(text: str, max_width: int, suffix: str = "…") -> str:
    """Truncate ANSI-styled text to visible columns without leaking styles."""
    if max_width <= 0:
        return ""
    if _vlen(text) <= max_width:
        return text
    suffix_width = _vlen(suffix)
    budget = max(0, max_width - suffix_width)
    out: list[str] = []
    visible = 0
    pos = 0
    had_ansi = False
    while pos < len(text):
        match = _ANSI_RX.match(text, pos)
        if match:
            out.append(match.group(0))
            had_ansi = True
            pos = match.end()
            continue
        char = text[pos]
        char_width = _char_width(char)
        if visible + char_width > budget:
            break
        out.append(char)
        visible += char_width
        pos += 1
    out.append(suffix if suffix_width <= max_width else "")
    if had_ansi:
        out.append(RESET)
    return "".join(out)


def _split_visible(text: str, max_width: int) -> list[str]:
    """Hard-wrap one unbroken token while retaining simple SGR styling."""
    if max_width <= 0:
        return [""]
    pieces: list[str] = []
    current: list[str] = []
    current_width = 0
    active_style = ""
    pos = 0
    while pos < len(text):
        match = _ANSI_RX.match(text, pos)
        if match:
            sequence = match.group(0)
            current.append(sequence)
            if sequence.endswith("m"):
                if sequence == RESET or re.search(r"(?:\[|;)0(?:;|m)", sequence):
                    active_style = ""
                else:
                    active_style += sequence
            pos = match.end()
            continue
        char = text[pos]
        char_width = _char_width(char)
        if current_width and current_width + char_width > max_width:
            piece = "".join(current)
            if active_style:
                piece += RESET
            pieces.append(piece)
            current = [active_style] if active_style else []
            current_width = 0
        current.append(char)
        current_width += char_width
        pos += 1
    if current or not pieces:
        pieces.append("".join(current))
    return pieces


# ---------- markdown rendering ----------

_MD_FENCE_RX = re.compile(r"```([A-Za-z0-9_+\-]*)\n(.*?)```", re.DOTALL)
_MD_INLINE_CODE_RX = re.compile(r"`([^`\n]+)`")
_MD_BOLD_RX = re.compile(r"\*\*([^*\n]+)\*\*")
_MD_ITALIC_AST_RX = re.compile(r"(?<![*\w])\*([^*\n]+?)\*(?!\w)")
_MD_ITALIC_UND_RX = re.compile(r"(?<!\w)_([^_\n]+?)_(?!\w)")
_MD_HEADER_RX = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_MD_BULLET_RX = re.compile(r"^(\s*)[-*]\s+", re.MULTILINE)
# Same two rules for text that CONTINUES a line already on screen: they must
# still fire after an embedded newline, but never at offset 0. A streamed
# fragment starting "* — I can look up…" (the tail of "**Watch something**")
# is not a bullet, and rewriting it to "• — …" corrupts the line.
_MD_HEADER_CONT_RX = re.compile(r"(?<=\n)(#{1,6})\s+(.*)$", re.MULTILINE)
_MD_BULLET_CONT_RX = re.compile(r"(?<=\n)(\s*)[-*]\s+")
# A line start that has not yet revealed whether it opens with a bullet or a
# header — the marker and the whitespace that confirms it may straddle two
# streamed chunks. Nothing may be emitted while this matches.
_MD_PENDING_MARKER_RX = re.compile(r"^[ \t]*(?:[-*]|#{1,6})?[ \t]*$")
# The complete leading marker, once it has arrived. It is one indivisible
# token: '-' emitted without its trailing space is just a dash, and the rest
# of the line then arrives as a continuation where the bullet rule no longer
# applies, so the bullet is lost.
_MD_MARKER_PREFIX_RX = re.compile(r"[ \t]*(?:[-*]|#{1,6})[ \t]+")
# <step N> markers the model emits to signal progress through its plan —
# rendered as a visible 'on step N' header so the user sees what's happening.
_MD_STEP_RX = re.compile(r"<step\s+(\d+)(?:\s*/\s*(\d+))?\s*>", re.IGNORECASE)
# Same pattern used by StatusBar to extract step markers from raw delta chunks.
_BAR_STEP_RX = _MD_STEP_RX
_MD_LINK_RX = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
# An SGR colour sequence. Escapes injected by one rule must be hidden from the
# next: every one of them contains '[' and ends in 'm', which the link pattern
# and the italic lookbehinds would otherwise read as ordinary text.
_SGR_RX = re.compile(r"\x1b\[[0-9;]*m")


def render_markdown(text: str, *, line_start: bool = True) -> str:
    """Render a readable CommonMark subset with or without terminal color.

    Plain output is a real rendering path, not an early return.  This matters
    for ``NO_COLOR``, logs, snapshots, and piped output: users should see clean
    headings and code instead of raw formatting punctuation.

    ``line_start=False`` says `text` continues a line already on screen, so the
    rules anchored to a line start (bullets, headers) must not fire at offset
    0.  The streaming renderer hands over mid-line fragments, and column 0 of a
    fragment is not column 0 of the terminal.
    """
    text = sanitize(text)
    styled = _supports_color()

    # Fenced code blocks first — pull them out and replace with placeholders
    # so we don't rewrite their interior with bold/italic rules.
    placeholders: list[str] = []

    def _stash_block(m: re.Match) -> str:
        body = m.group(2)
        rendered_lines: list[str] = []
        for line in body.splitlines():
            rail = color("│ ", PLUM) if styled else "│ "
            code = color(line, GOLD) if styled else line
            rendered_lines.append(rail + code)
        rendered = "\n".join(rendered_lines)
        placeholders.append(rendered)
        return f"\x00BLOCK{len(placeholders) - 1}\x00"

    out = _MD_FENCE_RX.sub(_stash_block, text)

    def _stash_inline(m: re.Match) -> str:
        rendered = color(m.group(1), GOLD + BOLD) if styled else m.group(1)
        placeholders.append(rendered)
        return f"\x00INL{len(placeholders) - 1}\x00"

    out = _MD_INLINE_CODE_RX.sub(_stash_inline, out)

    # Inline links: [text](url) → text (url, dimmed). Stashed like code, and
    # stashed HERE, before any rule injects an escape sequence: every SGR code
    # is 'ESC [ …' and the link pattern's '[' would happily match the one
    # inside it, swallowing a whole styled line into a bogus link label. It
    # also keeps a URL's underscores away from the italic rule.
    def _stash_link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        if label == url:
            rendered = color(url, GLOW) if styled else url
        elif styled:
            rendered = color(label, GLOW) + color(f" ({url})", SOFT)
        else:
            rendered = f"{label} ({url})"
        placeholders.append(rendered)
        return f"\x00INL{len(placeholders) - 1}\x00"

    out = _MD_LINK_RX.sub(_stash_link, out)

    # Headers (one per line).
    def _header(m: re.Match) -> str:
        level = len(m.group(1))
        body = m.group(2)
        if not styled:
            return body
        if level == 1:
            return color(body, GLOW + BOLD)
        if level == 2:
            return color(body, DUSK + BOLD)
        return color(body, SURFACE + BOLD)

    out = (_MD_HEADER_RX if line_start else _MD_HEADER_CONT_RX).sub(_header, out)

    # Bullets.
    bullet = color("• ", DUSK) if styled else "• "
    out = (_MD_BULLET_RX if line_start else _MD_BULLET_CONT_RX).sub(
        lambda m: m.group(1) + bullet, out
    )

    # Step markers: '<step 2>' becomes a styled '→ step 2', '<step 2/4>'
    # becomes '→ step 2 of 4'. Wrapped in newlines so they always read as
    # their own visible line even if the model puts them inline.
    def _step(m: "re.Match[str]") -> str:
        n, total = m.group(1), m.group(2)
        label = f"Step {n} of {total}" if total else f"Step {n}"
        rendered = color(label, GOLD + BOLD) if styled else label
        return "\n" + rendered + "\n"

    out = _MD_STEP_RX.sub(_step, out)

    # Hide every escape injected above before running the inline rules. An SGR
    # sequence ends in 'm' — a word character — so a bullet rendered right
    # before '*italic*' makes the italic pattern's (?<![*\w]) fail and the
    # asterisks show raw. Whether that happens depended on whether the bullet
    # and the span landed in the same streamed fragment, which is exactly the
    # "sometimes the markdown doesn't work" report.
    def _stash_sgr(m: re.Match) -> str:
        placeholders.append(m.group(0))
        return f"\x00INL{len(placeholders) - 1}\x00"

    out = _SGR_RX.sub(_stash_sgr, out)

    # Bold and italic. (Order matters — bold first.)
    out = _MD_BOLD_RX.sub(lambda m: color(m.group(1), BOLD) if styled else m.group(1), out)
    out = _MD_ITALIC_AST_RX.sub(lambda m: color(m.group(1), ITALIC) if styled else m.group(1), out)
    out = _MD_ITALIC_UND_RX.sub(lambda m: color(m.group(1), ITALIC) if styled else m.group(1), out)

    # Restore stashed code and links.
    def _restore(m: re.Match) -> str:
        return placeholders[int(m.group(1))]

    out = re.sub(r"\x00BLOCK(\d+)\x00", _restore, out)
    out = re.sub(r"\x00INL(\d+)\x00", _restore, out)
    return out


# How much text may be held back waiting for an inline span to close. Past
# this the span is implausible — an unmatched '`' or '**' must not stall the
# stream for a whole paragraph — so we cut anyway and render it literally.
_MAX_INLINE_HOLD = 200


def _span_end(s: str, i: int) -> int:
    """End index of the inline span opening at `s[i]`, ``-1`` if it hasn't
    closed yet, or ``0`` if nothing opens there."""
    c = s[i]
    if c == "`":
        j = s.find("`", i + 1)
        return j + 1 if j >= 0 else -1
    if s.startswith("**", i):
        j = s.find("**", i + 2)
        return j + 2 if j >= 0 else -1
    if c == "[":
        label = s.find("](", i + 1)
        if label < 0:
            # Still a candidate while the '](' could yet arrive: that is either
            # no ']' at all, or a ']' sitting at the very end of what we have.
            end = s.find("]", i + 1)
            return 0 if 0 <= end < len(s) - 1 else -1
        j = s.find(")", label + 2)
        return j + 1 if j >= 0 else -1
    if c in "*_":
        # Mirror the italic patterns' guards so arithmetic ('2 * 3'), globs and
        # snake_case don't read as spans: an opener is preceded by a non-word
        # and followed by a non-space.
        if i and (s[i - 1].isalnum() or s[i - 1] in "*_"):
            return 0
        if i + 1 >= len(s) or s[i + 1].isspace():
            return 0
        j = s.find(c, i + 1)
        while 0 <= j and s[j - 1].isspace():
            j = s.find(c, j + 1)
        return j + 1 if j >= 0 else -1
    return 0


def _safe_split(s: str, cut: int, *, line_start: bool) -> int:
    """Largest index ``<= cut`` that does not fall inside a markdown token.

    ``render_markdown`` renders exactly the string it is handed, so a streamed
    fragment cut in the middle of ``**bold**`` shows the literal asterisks and
    drops the other half into the next fragment — which is how a clean answer
    reaches the terminal as ``**Watch something*• — …``.  Pulling the cut back
    keeps every token whole; the tail waits for the next chunk, or for the
    newline that flushes the line entire.

    Three things are indivisible:

    * the leading bullet/header marker, when `line_start` — a ``-`` emitted
      without its trailing space is just a dash, and the remainder of the line
      then arrives as a continuation where the rule no longer applies;
    * an inline span — both while it is still open and, once closed, through
      its middle;
    * a word. Cutting mid-word would also fabricate a boundary that the
      renderer's lookbehinds trust: ``some_function_name`` split after
      ``some`` leaves a fragment starting ``_function_name``, whose ``_`` now
      satisfies ``(?<!\\w)`` and renders as italics, eating the underscores.
      Cutting only after whitespace makes those lookbehinds true by
      construction.
    """
    if line_start:
        marker = _MD_MARKER_PREFIX_RX.match(s)
        if marker is not None and cut < marker.end():
            return 0

    # The two rules feed each other — backing out of a word can land inside a
    # span, and backing out of a span can land mid-word — so run them to a
    # fixed point. `safe` only ever decreases, so this terminates.
    safe = cut
    previous = -1
    while safe != previous:
        previous = safe
        while safe > 0 and not s[safe - 1].isspace():
            safe -= 1
        i = 0
        while i < min(len(s), safe):
            end = _span_end(s, i)
            if end == 0:
                i += 1
                continue
            if end < 0 or end > safe:
                safe = i
                break
            i = end

    # An unmatched '`' or '**', or one implausibly long word, must not stall
    # the stream for a whole paragraph. Past the cap, cut and render literally.
    if len(s) - safe > _MAX_INLINE_HOLD:
        return cut
    return safe


class StreamMarkdown:
    """Render streamed tokens with markdown styling, line-by-line.

    Two ways of handling tagged blocks:
      • mode='elide' (e.g. <plan>): drop the contents — the engine
        renders the block as a panel later, showing both would duplicate.
      • mode='dim' (e.g. <think>): keep the contents but render dim
        italic with a '◦' prefix so the model's internal reasoning is
        visible LIVE, clearly distinguished from the final answer
        (which uses '●'). Each thinking line gets its own visual line.
    """

    SUPPRESS_PAIRS = [
        ("<plan>", "</plan>", "elide"),
        ("<think>", "</think>", "dim"),
        ("<thinking>", "</thinking>", "dim"),
    ]

    def __init__(
        self,
        emit,
        first_prefix: str = "",
        cont_prefix: str = "",
        dim_first_prefix: str = "",
        dim_cont_prefix: str = "",
    ):
        self.emit = emit
        self.first_prefix = first_prefix
        self.cont_prefix = cont_prefix
        self.dim_first_prefix = dim_first_prefix or first_prefix
        self.dim_cont_prefix = dim_cont_prefix or cont_prefix
        self.buf = ""
        self.opened = False
        self.mid_line = False
        self.in_dim = False  # currently inside a <think> block
        self.dim_opened = False  # have we emitted any dim line in current block?
        self.suppress_until: str | None = None  # close marker if currently eliding
        self._max_open = max(len(o) for o, _, _ in self.SUPPRESS_PAIRS)
        # True iff we've emitted at least one non-dim, non-empty line.
        # The renderer falls back to the static assistant panel when this
        # stays False — covers the case where the model wraps its entire
        # response in <plan>/<think> tags and the stream looks empty.
        self.visible_emitted = False

    def _emit_line(self, line: str, terminator: str) -> None:
        if not self.in_dim and line.strip():
            self.visible_emitted = True
        if self.in_dim:
            # Dim block — italic gray, '◦' prefix for the first line of the
            # block, indented continuation after.
            if self.mid_line:
                prefix = ""
            elif not self.dim_opened:
                prefix = self.dim_first_prefix
                self.dim_opened = True
            else:
                prefix = self.dim_cont_prefix
            # MUTED (246) is more legible than SOFT (240) on dark terminals;
            # the model's reasoning is worth reading, not squinting at.
            styled = color(line, MUTED + ITALIC) if line else ""
            self.emit(prefix + styled + terminator)
        else:
            if self.mid_line:
                prefix = ""
            elif not self.opened:
                prefix = self.first_prefix
            else:
                prefix = self.cont_prefix
            self.opened = True
            # mid_line means this fragment continues a line already painted,
            # so its offset 0 is not a line start — see render_markdown.
            self.emit(prefix + render_markdown(line, line_start=not self.mid_line) + terminator)
        self.mid_line = terminator == ""

    def feed(self, text: str) -> None:
        if not text:
            return
        self.buf += text
        self._drain(final=False)

    def flush(self) -> None:
        self._drain(final=True)
        if self.buf and not self.suppress_until:
            self._emit_line(self.buf, "")
        self.buf = ""

    def _drain(self, *, final: bool) -> None:
        # Loop until no more progress can be made on the buffer.
        while True:
            if self.suppress_until and not self.in_dim:
                # ELIDE mode: drop everything up to and including the close.
                close = self.suppress_until
                idx = self.buf.find(close)
                if idx < 0:
                    keep = len(close) - 1
                    if final:
                        self.buf = ""
                        self.suppress_until = None
                        return
                    if len(self.buf) > keep:
                        self.buf = self.buf[-keep:]
                    return
                self.buf = self.buf[idx + len(close) :]
                self.suppress_until = None
                continue

            if self.in_dim:
                # DIM mode: keep emitting lines as dim/italic until we hit
                # the close marker. Treat the close marker as a line break.
                close = self.suppress_until or ""
                idx = self.buf.find(close) if close else -1
                if idx < 0:
                    # No close yet — emit completed lines, hold a small tail
                    # so we don't split the close marker across emits.
                    if "\n" in self.buf:
                        last_nl = self.buf.rfind("\n")
                        head = self.buf[: last_nl + 1]
                        self.buf = self.buf[last_nl + 1 :]
                        for line in head.split("\n")[:-1]:
                            self._emit_line(line, "\n")
                        continue
                    if final:
                        if self.buf:
                            self._emit_line(self.buf, "")
                            self.buf = ""
                        return
                    keep = len(close) - 1 if close else 0
                    if len(self.buf) > keep:
                        emit_now = self.buf[:-keep] if keep else self.buf
                        self.buf = self.buf[-keep:] if keep else ""
                        if emit_now:
                            self._emit_line(emit_now, "")
                    return
                # Close marker found — emit any content before it (with a
                # newline so the next normal line starts fresh), then exit
                # dim mode.
                before = self.buf[:idx]
                self.buf = self.buf[idx + len(close) :]
                if before:
                    parts = before.split("\n")
                    for line in parts[:-1]:
                        self._emit_line(line, "\n")
                    if parts[-1]:
                        self._emit_line(parts[-1], "\n")  # force newline at end of dim block
                elif self.mid_line:
                    self.emit("\n")
                    self.mid_line = False
                self.in_dim = False
                self.dim_opened = False
                self.suppress_until = None
                continue

            # Not suppressing — look for the earliest open marker in the buf.
            earliest = -1
            earliest_close: str | None = None
            earliest_open_len = 0
            earliest_mode = "elide"
            for o, c, mode in self.SUPPRESS_PAIRS:
                i = self.buf.find(o)
                if i >= 0 and (earliest < 0 or i < earliest):
                    earliest = i
                    earliest_close = c
                    earliest_open_len = len(o)
                    earliest_mode = mode

            if earliest >= 0:
                # Emit text before the marker as normal lines.
                before = self.buf[:earliest]
                self.buf = self.buf[earliest + earliest_open_len :]
                self.suppress_until = earliest_close
                if before:
                    parts = before.split("\n")
                    for line in parts[:-1]:
                        self._emit_line(line, "\n")
                    if parts[-1]:
                        # Force newline so dim content starts on its own line.
                        self._emit_line(parts[-1], "\n" if earliest_mode == "dim" else "")
                if earliest_mode == "dim":
                    if self.mid_line:
                        self.emit("\n")
                        self.mid_line = False
                    self.in_dim = True
                    self.dim_opened = False
                continue

            # No open marker; emit completed lines but hold a small tail to
            # catch open markers split across feed() calls.
            if "\n" in self.buf:
                last_nl = self.buf.rfind("\n")
                head = self.buf[: last_nl + 1]
                self.buf = self.buf[last_nl + 1 :]
                for line in head.split("\n")[:-1]:
                    self._emit_line(line, "\n")
                continue

            # Single line, no newline. Hold back enough chars to detect a
            # partial open marker. On final flush, emit everything.
            if final:
                if self.buf:
                    self._emit_line(self.buf, "")
                    self.buf = ""
                return
            if not self.mid_line and _MD_PENDING_MARKER_RX.match(self.buf):
                # At a real line start whose leading token could still turn out
                # to be a bullet or a header. Emitting the bare '-' now would
                # split it from the space that makes it a bullet, and the rest
                # of the line arrives as a continuation where the rule no
                # longer applies — the marker is lost and '-' is printed raw.
                return
            keep = self._max_open - 1
            if len(self.buf) > keep:
                # The prefix can't contain a complete open marker that hasn't
                # been found above; safe to emit as a line continuation.
                cut = len(self.buf) - keep
                # …but never through the middle of an inline span, or the two
                # halves of a '**bold**' land in different fragments and
                # neither one renders.
                cut = _safe_split(self.buf, cut, line_start=not self.mid_line)
                if cut > 0:
                    emit_now = self.buf[:cut]
                    self.buf = self.buf[cut:]
                    # Treat as a partial line (no newline yet).
                    self._emit_line(emit_now, "")
            return


def _wrap_visible(text: str, width: int) -> list[str]:
    """Wrap `text` to `width` *visible* columns, leaving ANSI escapes intact."""
    width = max(1, width)
    out: list[str] = []
    for raw in text.splitlines() or [""]:
        if not raw.strip():
            out.append("")
            continue
        if _vlen(raw) <= width:
            out.append(raw)
            continue
        # Greedy word-wrap with a hard-wrap fallback for paths and URLs.
        indent_width = min(len(raw) - len(raw.lstrip(" ")), max(0, width - 1))
        indent = " " * indent_width
        words = raw.strip().split()
        line = indent
        for word in words:
            word_width = _vlen(word)
            separator = "" if not line.strip() else " "
            if _vlen(line) + len(separator) + word_width <= width:
                line += separator + word
                continue
            if line.strip():
                out.append(line.rstrip())
                line = indent
            available = max(1, width - _vlen(indent))
            pieces = _split_visible(word, available)
            for piece in pieces[:-1]:
                out.append(indent + piece)
            line = indent + pieces[-1]
        if line:
            out.append(line.rstrip())
    return out


# ---------- panels / boxes ----------

# Box characters
_BX = {
    "single": ("┌", "┐", "└", "┘", "─", "│"),
    "double": ("╔", "╗", "╚", "╝", "═", "║"),
    "round": ("╭", "╮", "╰", "╯", "─", "│"),
    "thick": ("┏", "┓", "┗", "┛", "━", "┃"),
}


def panel(
    body: str | list[str],
    title: str = "",
    style: str = "round",
    color_c: str = PLUM,
    title_c: str = GLOW,
    inner_pad: int = 1,
    markdown: bool = False,
) -> None:
    """Print a width-safe bordered panel."""
    tl, tr, bl, br, h, v = _BX[style]
    w = max(4, width())
    inner_pad = max(0, min(inner_pad, max(0, (w - 3) // 2)))
    inner = max(1, w - 2 - inner_pad * 2)

    # body lines (allow either a string or a list of pre-formatted lines)
    if isinstance(body, str):
        rendered = render_markdown(body) if markdown else sanitize(body)
        lines = _wrap_visible(rendered, inner)
    else:
        lines = []
        for raw in body:
            lines.extend(_wrap_visible(raw, inner))

    # title bar
    if title:
        title_text = truncate(single_line(title), max(1, w - 5))
        title_visible = f" {title_text} "
        bar_len = max(0, w - 3 - _vlen(title_visible))
        top = (
            color(tl + h, color_c)
            + color(title_visible, title_c)
            + color(h * bar_len + tr, color_c)
        )
    else:
        top = color(tl + h * (w - 2) + tr, color_c)

    bottom = color(bl + h * (w - 2) + br, color_c)

    print(top)
    for line in lines:
        body_text = " " * inner_pad + _pad(line, inner) + " " * inner_pad
        print(color(v, color_c) + body_text + color(v, color_c))
    print(bottom)


def hr(char: str = "─", c: str = PLUM) -> None:
    columns = width()
    unit_width = max(1, _vlen(char))
    print(color(char * max(1, columns // unit_width), c))


# ---------- spinner ----------

# A compact braille spinner is the default.  The older spark animation remains
# available through CAGENTIC_SPINNER=spark, and terminals that advertise
# limited capabilities receive an ASCII fallback.
_SPARK_FRAMES = (
    "·  ",
    "✦  ",
    "✦· ",
    "✦✶ ",
    "✦✶✦",
    " ✶✦",
    "  ✦",
    "  ·",
    "   ",
    "·  ",
)

_BRAILLE_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_ASCII_FRAMES = ("-", "\\", "|", "/")


def _default_frames() -> tuple[str, ...]:
    style = os.environ.get("CAGENTIC_SPINNER", "braille").strip().lower()
    if style in {"off", "none", "0"}:
        return ()
    if style == "spark":
        return _SPARK_FRAMES
    if style == "ascii" or os.environ.get("TERM", "").lower() == "dumb":
        return _ASCII_FRAMES
    return _BRAILLE_FRAMES


# Track any live spinner so we can force-stop it before reading user input.
_active_spinners: list["Spinner"] = []

# Serializes all terminal writes that involve cursor save/restore so the
# spinner thread and the status-bar thread never interleave their escape
# sequences and leave the cursor on the wrong row.
_PAINT_LOCK = threading.Lock()


def sync_write(s: str) -> None:
    """Write `s` to stdout under the shared paint lock, then flush.

    The StatusBar and Spinner paint from background threads, bracketing each
    frame in a cursor save/restore. Main-thread output that streams tokens or
    moves the cursor must take the SAME lock — otherwise a paint frame can
    land between the write and its flush and the two fight over the cursor,
    so the model's text visibly writes over itself. Plain prints elsewhere
    are cursor-neutral against a paint frame and don't need this.
    """
    with _PAINT_LOCK:
        sys.stdout.write(s)
        sys.stdout.flush()


def stop_all_spinners() -> None:
    for s in list(_active_spinners):
        try:
            s.stop()
        except Exception:
            pass


def prepare_for_input() -> None:
    """Call right before reading user input: stop spinners, show cursor, flush."""
    stop_all_spinners()
    if supports_cursor_control():
        sys.stdout.write("\033[?25h")  # show cursor
        sys.stdout.flush()


class Spinner:
    """Tiny non-blocking status spinner. Use as a context manager.

    Renders as:    ⠋ thinking…   (0.4s)
    On stop, clears the line so the next print is clean.
    """

    def __init__(
        self,
        label: str = "thinking",
        color_c: str = GOLD,
        escalations: list[tuple[float, str]] | None = None,
    ) -> None:
        """`escalations` is an optional list of (after_seconds, label) pairs
        applied as time passes — used by the engine to tell the user WHY
        the agent has been thinking for a while (large prompt, model loading,
        etc.) instead of just sitting on the original label."""
        self.label = label
        self.color_c = color_c
        # Sort ascending so the loop just picks the highest matching tier.
        self.escalations = sorted(escalations or [], key=lambda x: x[0])
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = 0.0
        self._frames = _default_frames()

    def set_label(self, label: str) -> None:
        self.label = label

    def _current_label(self, elapsed: float) -> str:
        label = self.label
        for after, lbl in self.escalations:
            if elapsed >= after:
                label = lbl
            else:
                break
        return label

    def start(self) -> None:
        if not motion_enabled() or self._thread is not None or not self._frames:
            return
        self._stop.clear()
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        _active_spinners.append(self)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None
        if self in _active_spinners:
            _active_spinners.remove(self)
        with _PAINT_LOCK:
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()

    def _run(self) -> None:
        # 150ms grace period — if the work finishes fast (most tool dispatches
        # are sub-100ms), we never draw a frame and there's no visual flash.
        if self._stop.wait(0.15):
            return
        i = 0
        while not self._stop.is_set():
            frame = self._frames[i % len(self._frames)]
            elapsed = time.monotonic() - self._t0
            timer = f"({elapsed:0.1f}s)"
            # If the label would push the line past the terminal width, the
            # row wraps and `\r\033[2K` only clears the LAST visual row,
            # leaving the wrapped fragment behind. Truncate the label so
            # the whole line always fits on one row.
            term_w = width()
            label_text = self._current_label(elapsed) + "…"
            fixed = _vlen(frame) + _vlen(timer) + 6  # spaces + padding
            avail = max(1, term_w - fixed)
            label_text = truncate(label_text, avail)
            line = (
                "  "
                + color(frame, self.color_c)
                + " "
                + color(label_text, MUTED)
                + "  "
                + color(timer, SOFT)
            )
            with _PAINT_LOCK:
                sys.stdout.write("\r\033[2K" + line)
                sys.stdout.flush()
            i += 1
            # Wait in small increments so .stop() reacts quickly.
            self._stop.wait(0.08)

    def __enter__(self) -> "Spinner":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


class SilenceWatchdog:
    """Background watchdog that prints a dim 'still receiving' breadcrumb
    when a streaming response has gone quiet for a while.

    Why this exists: Qwen-class reasoning models can stop emitting tokens
    for a long time while they think (<think> happens model-side, BEFORE
    any tokens are sent). Without feedback the user can't tell the
    difference between 'thinking hard' and 'Ollama died' and reaches for
    Ctrl+C — usually right before the answer would have started.

    Design notes:
    - Writes to stderr, not stdout, so the streaming text buffer is never
      overwritten mid-line. Output may visually land below an unfinished
      line; that's intentional — accuracy over prettiness.
    - Escalation tiers are spaced wide (25s, 60s, 180s, 600s) so we never
      spam the scrollback. Each tier prints once per silence stretch.
    - .ping() resets the silence counter AND the escalation index, so a
      single token between two long stalls produces two breadcrumb runs
      rather than skipping past tiers.
    """

    DEFAULT_TIERS = (25.0, 60.0, 180.0, 600.0)

    def __init__(
        self,
        label: str = "still receiving",
        tiers: tuple[float, ...] = DEFAULT_TIERS,
    ) -> None:
        self.label = label
        self.tiers = tiers
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last = 0.0
        self._lock = threading.Lock()
        self._tier_idx = 0

    def start(self) -> None:
        if not sys.stderr.isatty() or self._thread is not None:
            return
        self._last = time.monotonic()
        self._tier_idx = 0
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def ping(self) -> None:
        # Called on every received token.
        with self._lock:
            self._last = time.monotonic()
            self._tier_idx = 0

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            with self._lock:
                silent = time.monotonic() - self._last
                idx = self._tier_idx
            if idx >= len(self.tiers):
                continue
            if silent < self.tiers[idx]:
                continue
            # Dim italic line on stderr, prefixed with \n so it lands on a
            # fresh row even if stdout was mid-line.
            sys.stderr.write(
                "\n"
                + color(
                    f"  · {self.label} — {silent:0.0f}s since last token…",
                    MUTED + ITALIC,
                    sys.stderr,
                )
                + "\n"
            )
            sys.stderr.flush()
            with self._lock:
                self._tier_idx += 1


# ---------- log helpers ----------
#
# Marker vocabulary, kept consistent so the transcript reads at a glance:
#   ●  Cagentic speaking
#   ·  quiet context or progress
#   →  a tool call
#   ✓ / ×  success or failure
#   !  something needs attention


def _message(marker: str, msg: str, marker_c: str, text_c: str, *, stream=None) -> None:
    """Print a semantic message with aligned, width-safe continuation lines."""
    target = stream or sys.stdout
    prefix = f"  {marker} "
    continuation = " " * _vlen(prefix)
    available = max(1, width() - _vlen(prefix))
    lines = _wrap_visible(sanitize(msg), available) or [""]
    for index, line in enumerate(lines):
        lead = color(prefix, marker_c, target) if index == 0 else continuation
        print(lead + color(line, text_c, target), file=target)


def heading(label: str) -> None:
    """Print a compact section heading used by command output."""
    print("  " + color(truncate(single_line(label).upper(), max(1, width() - 2)), DUSK + BOLD))


def field(
    label: str,
    value: object,
    *,
    warning: bool = False,
    label_width: int | None = None,
) -> None:
    """Print one responsive key/value row for diagnostics and configuration."""
    label_text = f"{single_line(label)}:"
    value_text = sanitize(value)
    columns = width()
    value_c = WARN if warning else SURFACE
    label_width = label_width or min(14, max(9, len(label_text) + 1))
    if columns >= 40:
        prefix = "  " + label_text.ljust(label_width)
        lines = _wrap_visible(value_text, max(1, columns - _vlen(prefix))) or [""]
        for index, line in enumerate(lines):
            lead = (
                "  " + color(label_text.ljust(label_width), MUTED)
                if index == 0
                else " " * _vlen(prefix)
            )
            print(lead + color(line, value_c))
        return
    print("  " + color(truncate(label_text, max(1, columns - 2)), MUTED))
    for line in _wrap_visible(value_text, max(1, columns - 4)) or [""]:
        print("    " + color(line, value_c))


def list_item(
    text: object,
    *,
    detail: object | None = None,
    marker: str = "•",
    active: bool = True,
) -> None:
    """Print a compact list row with an optional secondary description."""
    marker_c = {
        "✓": OK,
        "×": ERR,
        "!": WARN,
        "→": DUSK,
        "●": GLOW,
    }.get(marker, DUSK if active else SOFT)
    text_c = SURFACE if active else MUTED
    _message(marker, sanitize(text), marker_c, text_c)
    if detail is not None:
        for line in _wrap_visible(sanitize(detail), max(1, width() - 4)) or [""]:
            print("    " + color(line, MUTED if active else SOFT))


def code_block(text: str) -> None:
    """Print preformatted command/config output with a subtle left rail."""
    prefix = "  │ "
    for raw in sanitize(text).splitlines() or [""]:
        lines = _wrap_visible(raw, max(1, width() - _vlen(prefix))) or [""]
        for line in lines:
            print(color(prefix, PLUM) + color(line, MUTED))


def input_prompt(label: str, default: str | None = None) -> str:
    """Read one setup value using the same prompt language as the REPL."""
    clean_label = single_line(label)
    clean_default = single_line(default) if default else ""
    suffix = f" [{clean_default}]" if clean_default else ""
    prompt = color("  › ", GLOW + BOLD) + color(f"{clean_label}{suffix}: ", SURFACE)
    return input(prompt)


def prompt_prefix() -> str:
    return color("› ", GLOW + BOLD)


def info(msg: str) -> None:
    _message("·", msg, DUSK, SURFACE)


def meta(msg: str) -> None:
    """Print low-priority progress or usage metadata."""
    _message("·", msg, SOFT, MUTED)


def warn(msg: str) -> None:
    _message("!", msg, WARN, WARN)


def error(msg: str) -> None:
    _message("×", msg, ERR, ERR, stream=sys.stderr)


def assistant(msg: str) -> None:
    """Print the assistant's answer as clean indented text — markdown-styled,
    no box. (Streaming answers are already shown live; this is the
    non-streaming render and the /resume replay.)"""
    rendered = render_markdown(msg)
    prefix = "  ● "
    lines = _wrap_visible(rendered, max(1, width() - _vlen(prefix)))
    for i, line in enumerate(lines or [""]):
        lead = color(prefix, GLOW) if i == 0 else " " * _vlen(prefix)
        print(lead + line)


def thinking(msg: str) -> None:
    """Print <think>…</think> content as faint italic indented text — no box."""
    prefix = "  · "
    lines = _wrap_visible(sanitize(msg), max(1, width() - _vlen(prefix)))
    for i, line in enumerate(lines or [""]):
        lead = color(prefix, SOFT) if i == 0 else " " * _vlen(prefix)
        print(lead + color(line, MUTED + ITALIC))


def plan(items: list[str]) -> None:
    """Render a compact numbered plan without consuming the full viewport."""
    if not items:
        return
    print()
    heading("Plan")
    for i, step in enumerate(items, 1):
        prefix = f"  {i:>2}  "
        lines = _wrap_visible(sanitize(step), max(1, width() - _vlen(prefix)))
        for line_index, line in enumerate(lines or [""]):
            lead = color(prefix, GOLD) if line_index == 0 else " " * _vlen(prefix)
            print(lead + color(line, SURFACE))
    print()


def tool_call(name: str, summary: str) -> None:
    detail = f"{name}  {summary}" if summary else name
    _message("→", detail, DUSK, MUTED)


def tool_result(summary: str, ok: bool = True) -> None:
    _message("✓" if ok else "×", summary, OK if ok else ERR, MUTED if ok else ERR)


# ---------- banner ----------


def banner(
    model: str,
    cwd: str,
    tools_enabled: bool = True,
    user_name: str | None = None,
    *,
    version: str | None = None,
    plan_mode: bool = False,
    yolo: bool = False,
    dry_run: bool = False,
) -> None:
    """Print a compact, responsive identity and runtime summary.

    Startup no longer clears scrollback.  Users can opt into the old behavior
    with ``CAGENTIC_CLEAR_SCREEN=1``.
    """
    if os.environ.get("CAGENTIC_CLEAR_SCREEN", "").lower() in {"1", "true", "on"}:
        clear_screen()

    columns = width()
    print()
    brand = color("Cagentic", GLOW + BOLD)
    if version:
        brand += color(f" {single_line(version)}", SOFT)
    if user_name:
        brand += color(f" · {single_line(user_name)}", MUTED)
    header_width = max(1, columns - 4)
    ready = color("ready", OK + BOLD)
    if _vlen(brand) + _vlen(ready) + 3 <= header_width:
        gap = " " * (header_width - _vlen(brand) - _vlen(ready))
        print("  " + brand + gap + ready)
    else:
        print("  " + truncate(brand, max(1, columns - 2)))
        print("  " + ready)

    print("  " + color("─" * max(1, columns - 4), PLUM))
    field("model", single_line(model), label_width=12)
    field("workspace", _short_path(single_line(cwd)), label_width=12)

    if dry_run:
        mode = "dry run · no changes"
    elif plan_mode:
        mode = "plan · read only"
    elif yolo:
        mode = "act · auto approve changes"
    else:
        mode = "act · ask before changes"
    mode += " · " + ("tools on" if tools_enabled else "tools off")
    if columns < 48:
        mode = mode.replace("ask before changes", "ask").replace(
            "auto approve changes", "auto approve"
        )
    field(
        "mode",
        mode,
        warning=dry_run or yolo or not tools_enabled,
        label_width=12,
    )

    available = max(1, columns - 4)
    hint = (
        "Type / for commands · @ to attach files · Esc+Enter for newline"
        if columns >= 48
        else "/ commands · @ files · Esc+Enter newline"
    )
    for line in _wrap_visible(hint, available):
        print("    " + color(line, SOFT))
    print()


def _short_path(p: str) -> str:
    """Render a path with ~ for home — shorter, friendlier in the status row."""
    try:
        home = str(_os_home())
        if p == home:
            return "~"
        if p.startswith(home + os.sep):
            return "~" + p[len(home) :]
    except Exception:
        pass
    return p


def _os_home() -> str:
    from pathlib import Path

    return str(Path.home())


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------


def _reserve_bottom_row_seq(rows: int, *, region_active: bool, reserve: int = 1) -> str:
    """Escapes that reserve row `rows` for the status bar and leave the cursor
    INSIDE the resulting scroll region.

    DECSTBM (`ESC [ t;b r`) homes the cursor to (1,1) per the VT spec, so it
    must be bracketed in DECSC/DECRC (`ESC 7` / `ESC 8`) — otherwise the cursor
    jumps to the top of the screen and the turn's first streamed tokens write
    right over the banner and previous output.

    The bracket alone is not enough: it faithfully restores a cursor that was
    already on the LAST row, which is the row we just reserved. Row N sits
    below the scroll region, where LF no longer scrolls — every line of the
    turn overwrites that one row and the next paint frame erases it, so the
    whole answer is written and wiped and the REPL looks like it returned
    nothing. That is the REPL's steady state once the screen has scrolled
    full, because prompt_toolkit leaves the cursor on the bottom row. (Seen
    live under ConPTY: every paint frame restored to `ESC [ 24;22 H` on a
    24-row screen.)

    So free the rows first: IND (`ESC D`) moves down one, scrolling only if we
    are already at the bottom, and CUU (`ESC [ nA`) steps back up without ever
    scrolling. Both preserve the column, so a half-written streamed line is
    never split — when the screen does scroll, the partial line rides up with
    it and the cursor stays right after it. Mid-screen the pair is a no-op; on
    the last row it scrolls and leaves the cursor above the reserved rows.

    The IND count must equal `reserve`. It was hard-coded to one back when the
    bar reserved exactly one row, and when type-ahead started asking for a
    second (its echo line) that left the cursor one row BELOW the new region —
    reintroducing the exact wipe described above, on Windows, where
    prompt_toolkit reliably parks the cursor on the bottom row.

    `region_active` says whether a scroll region is already in effect (the
    resize path, re-reserving mid-turn). It matters because IND scrolls
    *within* the current region: with a stale region still set, the make-room
    step could scroll the region's contents and drag the cursor off the line
    being streamed. Resetting to the full screen first — bracketed, since that
    escape homes too — makes IND behave exactly as it does at start().
    """
    reserve = max(1, int(reserve))
    prelude = ""
    if region_active:
        prelude = (
            "\0337"  # ESC 7: save cursor (DECSC)
            + "\033[r"  # reset scroll region to the full screen (homes)
            + "\0338"  # ESC 8: restore cursor (DECRC)
        )
    return (
        prelude
        # One IND per reserved row: each scrolls only if we're at the bottom,
        # so together they guarantee `reserve` rows of room below the cursor.
        + "\033D" * reserve
        + f"\033[{reserve}A"  # CUU: back up the same number of rows (never scrolls)
        + "\0337"  # ESC 7: save cursor (DECSC)
        + f"\033[1;{rows - reserve}r"  # DECSTBM — reserve the bottom row(s) (homes)
        + "\0338"  # ESC 8: restore cursor (DECRC)
    )


# Seconds a new terminal size must hold still before we re-reserve the bar
# row. A window drag emits dozens of sizes; acting on each one is what made
# resizing mid-turn spray duplicate status lines into the scrollback.
_RESIZE_SETTLE = 0.35


class StatusBar:
    """One-row status bar pinned to the terminal's last line via DECSTBM.

    The scroll region is set to rows 1..(N-1) so normal output scrolls
    naturally above the bar without overwriting it.  The bar itself lives
    on row N and is painted every 200 ms from a daemon thread.

    Disabled automatically for redirected/small terminals or when
    CAGENTIC_STATUS_BAR=off is set.  COLLAMA_STATUS_BAR remains a compatibility
    alias for existing installations.

    Lifecycle (called from agent.turn()):
        bar = StatusBar(ctx_tokens=pre_turn_ctx)
        bar.start()
        # … on each delta event:  bar.on_delta(text)
        # … on each done  event:  bar.on_done(post_turn_ctx)
        bar.stop()   # always in a finally block
    """

    def __init__(
        self,
        ctx_tokens: int = 0,
        extra_reserved_rows: int = 0,
        ctx_limit: int = 0,
        compact_at: int = 0,
    ) -> None:
        # The model's input window and the token count at which older history
        # gets compacted. A bare "context ~12,345" has no denominator, so it
        # said nothing about how close auto-compact was — which made compaction
        # arrive as a surprise mid-task.
        self._ctx_limit = max(0, int(ctx_limit))
        self._compact_at = max(0, int(compact_at))
        # Rows kept below the scroll region IN ADDITION to the bar's own. The
        # type-ahead echo lives on one of them; without the extra reservation
        # streamed output would scroll straight over it.
        self._reserve = 1 + max(0, int(extra_reserved_rows))
        self._t0 = time.monotonic()
        self._tok = 0  # output chars seen this turn ÷ 4 ≈ tokens
        self._ctx = ctx_tokens  # running context estimate, updated on done
        self._step_n: int | None = None
        self._step_m: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._active = False
        self._last_rows = 0  # track height so resize re-reserves the row
        # Resize debounce: a window drag emits sizes continuously, and each
        # re-reserve is destructive, so act only once the size holds still.
        self._pending_rows = 0
        self._resize_at = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        configured = (
            os.environ.get("CAGENTIC_STATUS_BAR", os.environ.get("COLLAMA_STATUS_BAR", "on"))
            .strip()
            .lower()
        )
        rows = shutil.get_terminal_size((80, 24)).lines
        if (
            not motion_enabled()
            or configured in {"off", "0", "false", "no"}
            or rows < 3 + self._reserve
        ):
            return
        self._last_rows = rows
        self._pending_rows = rows
        with _PAINT_LOCK:
            sys.stdout.write(
                _reserve_bottom_row_seq(rows, region_active=False, reserve=self._reserve)
            )
            sys.stdout.flush()
        self._active = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def on_delta(self, text: str) -> None:
        if not self._active:
            return
        with self._lock:
            self._tok += len(text) // 4
            m = _BAR_STEP_RX.search(text)
            if m:
                self._step_n = int(m.group(1))
                if m.group(2):
                    self._step_m = int(m.group(2))

    def on_done(self, ctx_tokens: int) -> None:
        if not self._active:
            return
        with self._lock:
            self._ctx = ctx_tokens

    def stop(self) -> None:
        if not self._active or self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None
        self._active = False
        rows = shutil.get_terminal_size((80, 24)).lines
        # Reset the scroll region and clear the bar row, but leave the cursor
        # where the turn's output ended. Both DECSTBM reset (ESC [ r) and the
        # move-to-bar-row escape disturb the cursor, so they MUST sit between
        # the save and restore — emitting ESC [ r *after* the restore (the old
        # bug) homed the cursor to (1,1), so the next prompt drew over the
        # whole screen.
        with _PAINT_LOCK:
            sys.stdout.write(
                "\0337"  # ESC 7: save cursor (DECSC)
                + "\033[r"  # reset scroll region (homes cursor)
                + f"\033[{rows};1H"  # move to bar row
                + "\033[2K"  # clear it
                + "\0338"  # ESC 8: restore cursor (DECRC)
            )
            sys.stdout.flush()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _context_segment(self, ctx: int) -> str:
        """Context usage as pressure, not a bare number.

        Without a window the old absolute count is all we can honestly show.
        With one, the percentage is what tells the user whether they're about
        to lose history — and the "compact at N%" marker only appears once
        it's near enough to matter, so a fresh session stays quiet.
        """
        if self._ctx_limit <= 0:
            return f"context ~{ctx:,}"
        pct = ctx / self._ctx_limit
        shown = round(pct * 100)
        if self._compact_at <= 0:
            return f"context {shown}%"
        trigger = self._compact_at / self._ctx_limit
        trigger_shown = round(trigger * 100)
        if pct >= trigger:
            # Past the line: compaction fires on the next turn.
            return color(f"context {shown}% · compacting", GOLD)
        if shown >= trigger_shown:
            # Rounds to the trigger without having crossed it — printing
            # "60% · compact at 60%" looks like a stuck bar rather than a
            # near miss.
            return color(f"context {shown}% · compact soon", GOLD)
        if pct >= trigger * 0.6:
            return f"context {shown}% · compact at {trigger_shown}%"
        return f"context {shown}%"

    def _compose(self) -> str:
        with self._lock:
            elapsed = time.monotonic() - self._t0
            tok = self._tok
            ctx = self._ctx
            step_n = self._step_n
            step_m = self._step_m

        if elapsed < 60:
            time_str = f"working {elapsed:.1f}s"
        else:
            mins = int(elapsed) // 60
            secs = int(elapsed) % 60
            time_str = f"working {mins}m {secs:02d}s"

        parts = [time_str]
        if step_n is not None:
            parts.append(f"step {step_n}/{step_m}" if step_m else f"step {step_n}")
        if tok > 0:
            parts.append(f"output ~{tok:,}")
        if ctx > 0:
            parts.append(self._context_segment(ctx))

        row = "  " + " · ".join(parts)
        cols = shutil.get_terminal_size((80, 24)).columns
        return truncate(row, max(1, cols - 1))

    def _paint(self) -> None:
        text = self._compose()
        rows = shutil.get_terminal_size((80, 24)).lines
        now = time.monotonic()
        with _PAINT_LOCK:
            if rows != self._last_rows:
                # A resize is in flight. Dragging a window emits a continuous
                # stream of new sizes, and re-reserving on each one costs a
                # scrolled line *and* strands the previous bar text in the
                # scrollback — which is what produced a ladder of duplicated
                # "working …" lines every time the window was resized. So wait
                # for the geometry to settle, and paint nothing meanwhile:
                # painting against a size we're about to redo is what leaves
                # the stale copies behind.
                if rows != self._pending_rows:
                    self._pending_rows = rows
                    self._resize_at = now
                    return
                if now - self._resize_at < _RESIZE_SETTLE:
                    return
                # Growing leaves the old bar row mid-screen holding stale text;
                # clear it before reserving the new one. (Shrinking pushes it
                # into scrollback, which no escape can reach — hence the
                # settle-first approach above, which keeps it to one line.)
                if 0 < self._last_rows <= rows:
                    sys.stdout.write(
                        "\0337"  # ESC 7: save cursor
                        + "\033[r"  # full-screen region so the row is addressable
                        + f"\033[{self._last_rows};1H"
                        + "\033[2K"  # clear the old bar row
                        + "\0338"  # ESC 8: restore cursor
                    )
                # Re-reserve the bottom row against the new height — with the
                # same make-room guard start() uses. Shrinking is the dangerous
                # direction: the terminal clamps the cursor into the new,
                # shorter screen, so it can land on the row we are about to
                # reserve and every subsequent line of the turn gets painted
                # over. Emitted as its own statement rather than folded into
                # the paint bracket below, because DECSC/DECRC is a single save
                # slot on most terminals and nesting one pair inside another
                # clobbers it.
                sys.stdout.write(
                    _reserve_bottom_row_seq(rows, region_active=True, reserve=self._reserve)
                )
                self._last_rows = rows
                self._pending_rows = rows
            sys.stdout.write(
                "\0337"  # ESC 7: save cursor
                + f"\033[{rows};1H"  # move to bottom row
                + "\033[2K"  # clear line
                + color(text, MUTED)
                + "\0338"  # ESC 8: restore cursor
            )
            sys.stdout.flush()

    def _run(self) -> None:
        while not self._stop.wait(0.2):
            try:
                self._paint()
            except Exception:
                pass
