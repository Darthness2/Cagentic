"""Type-ahead: let the user type while the model is generating.

The REPL is otherwise single-threaded and serial — while ``Agent.turn`` iterates
the streaming generator, the main thread is blocked and nothing reads stdin, so
the user cannot compose the next message until generation finishes. This module
adds a background key reader, active only while a turn runs, that mirrors what
the user types onto a pinned row above the status bar.

Semantics (the "Both / key choice" the user picked):

  * Plain ``Enter``  → queue the typed message for the NEXT turn. The in-progress
                      reply is preserved and completes normally; the queued
                      message runs immediately after, without re-prompting.
  * ``Ctrl-C``       → interrupt the CURRENT turn (the in-progress reply is
                      discarded) and send the typed text as a fresh turn.

Live echo (full): the typed text is shown on its own row (the row just above the
status bar) with Backspace and a ``✉`` marker once a message is queued, so the
user can see and edit what they're composing mid-generation.

Threading / terminal model
---------------------------
The reader is a daemon thread that polls a key backend with a short timeout so
``stop()`` (which sets the ``_stop`` event) rejoins quickly — the same pattern
Spinner/StatusBar use, and required on Windows where an unbounded wait would
block SIGINT (see ``cli.py`` park-loop comment).

  * Windows: ``msvcrt.kbhit()`` (non-blocking) + ``msvcrt.getwch()`` (one key,
    no echo, **no console-mode change**). Ctrl-C does NOT arrive here — Windows
    delivers it as a ``KeyboardInterrupt`` on the main thread, which ``Agent.turn``
    already catches. So on Windows the interrupt+send message is captured from
    THIS object's buffer inside that existing handler (see ``capture_interrupt``),
    and the per-message ``interrupt_check`` hook is effectively unused.
  * POSIX: a guarded ``termios`` backend sets the input fd to char-at-a-time,
    no-echo, no-SIG (so Ctrl-C arrives as ``\\x03`` to the reader). We deliberately
    leave output processing (``OPOST``) intact so streaming output's ``\\n`` still
    maps to CRLF. If ``termios``/``select`` is unavailable, the backend is "none"
    and every method is a no-op — the REPL behaves exactly as before.

The echo row lives at ``rows-1`` and the status bar at ``rows``; both are pinned
outside the scroll region, which the ``StatusBar`` reserves via DECSTBM. This
module NEVER touches the scroll region — ``StatusBar.start`` owns it (with
``extra_reserved_rows=1`` when type-ahead is active), and ``start_after_bar`` is
called only after the bar has reserved both rows. All cursor writes go through
``ui.sync_write`` (the shared ``_PAINT_LOCK``) bracketed in DECSC/DECRC, so a
StatusBar repaint can't slip between the write and its cursor restore.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading

from . import ui

# A reverse-video space, i.e. exactly what a block cursor looks like. The
# hardware cursor can't be parked on the echo row (see `_paint`), so without a
# drawn stand-in there is no visible insertion point while you type mid-turn.
_CURSOR = "\033[7m \033[0m"


def _visible_tail(text: str, width: int) -> str:
    """Keep the END of a too-long line, marked with a leading ellipsis.

    ``ui.truncate`` keeps the head, which is right for a label and wrong for an
    input line — you type at the end, so the end is the part you need to see.
    """
    if width <= 0:
        return ""
    if ui._vlen(text) <= width:
        return text
    # Wide chars mean columns != characters, so trim by measurement. Slice to a
    # generous bound first so a pasted paragraph doesn't make this quadratic.
    cut = text[-(width + 8) :]
    while cut and ui._vlen(cut) > width - 1:
        cut = cut[1:]
    return "…" + cut


class TypeAhead:
    """Background type-ahead line reader + live echo, active only during a turn.

    Constructed once by the REPL and reused across turns. ``Agent.turn`` calls
    ``start_after_bar(bar)`` after the StatusBar has reserved its rows, and
    ``stop()`` in its ``finally``. Between turns every method is a no-op.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._bar = None  # the StatusBar whose rows we echo above
        self._active = False

        # Per-turn state (reset in start_after_bar).
        self._buf = ""  # text typed this turn, not yet Enter'd
        self._pending: str | None = None  # message queued by Enter for next turn
        self._interrupted = False  # Ctrl-C requested (POSIX reader path)
        self._interrupt_msg: str | None = None  # message to send fresh on Ctrl-C
        self._consumed = False  # consume_interrupt once-guard

        # Pick a key backend. "none" → disabled, every method no-ops.
        self._backend = "none"
        if os.name == "nt" or sys.platform == "win32":
            try:
                import msvcrt  # noqa: F401

                self._backend = "msvcrt"
            except Exception:
                pass
        else:
            try:
                import select  # noqa: F401
                import termios  # noqa: F401
                import tty  # noqa: F401

                self._backend = "posix"
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Capability / gating
    # ------------------------------------------------------------------

    @property
    def can_run(self) -> bool:
        """True iff a live key backend exists AND stdin/stdout are ttys AND the
        status bar isn't disabled (the bar owns the pinned-row scroll region we
        echo into). When False, all public methods are no-ops."""
        return (
            self._backend != "none"
            and sys.stdin.isatty()
            and sys.stdout.isatty()
            # Same precedence StatusBar.start() uses — checking only the
            # legacy name meant CAGENTIC_STATUS_BAR=off disabled the bar
            # while type-ahead still believed it could paint above it.
            and os.environ.get("CAGENTIC_STATUS_BAR", os.environ.get("COLLAMA_STATUS_BAR", "on"))
            .strip()
            .lower()
            not in {"off", "0", "false", "no"}
        )

    def make_interrupt_check(self):
        """Return a cheap callable() -> bool polled once per streamed message
        inside ``Agent.turn``'s loop. True means "Ctrl-C was received, break
        the loop now." Only ever fires on the POSIX backend (Windows Ctrl-C
        arrives as KeyboardInterrupt instead, which breaks the loop directly).
        Returns None when type-ahead is disabled."""
        if not self.can_run:
            return None

        def _check() -> bool:
            with self._lock:
                return self._interrupted

        return _check

    # ------------------------------------------------------------------
    # Lifecycle (called from Agent.turn)
    # ------------------------------------------------------------------

    def start_after_bar(self, bar) -> None:
        """Begin accepting type-ahead. ``bar`` is the already-started StatusBar
        that has reserved the bottom rows via DECSTBM; we only start the reader
        thread — we never touch the scroll region (the bar owns it). If the bar
        isn't actually active (e.g. non-tty / disabled), do nothing."""
        if not self.can_run:
            return
        if bar is None or not getattr(bar, "_active", False):
            return
        with self._lock:
            self._buf = ""
            self._pending = None
            self._interrupted = False
            self._interrupt_msg = None
            self._consumed = False
        self._bar = bar
        self._active = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="cagentic-typeahead")
        self._thread.start()
        # Show the input affordance immediately so the user knows they can type
        # while the model works.
        self._paint()

    def stop(self) -> None:
        """Stop the reader and clear the echo row. Called in ``Agent.turn``'s
        finally BEFORE ``bar.stop()`` so our row is cleared while the scroll
        region is still reserved, then the bar resets the region."""
        if not self._active:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._active = False
        self._bar = None
        self._clear_row()

    # ------------------------------------------------------------------
    # Results consumed by the REPL after a turn
    # ------------------------------------------------------------------

    def take_pending(self) -> str | None:
        """Return the message queued by Enter for the next turn, then clear it.
        Called by the REPL after a turn that completed normally."""
        if not self.can_run:
            return None
        with self._lock:
            p = self._pending
            self._pending = None
            return p

    def consume_interrupt(self) -> str | None:
        """Return the Ctrl-C interrupt message once, then None. Called by the
        REPL after a turn to decide whether to run a fresh turn with the typed
        text (in-progress reply already discarded by ``gen.close``)."""
        if not self.can_run:
            return None
        with self._lock:
            if self._interrupt_msg is not None and not self._consumed:
                self._consumed = True
                msg = self._interrupt_msg
                return msg if msg else None
            return None

    def capture_interrupt(self) -> bool:
        """Signal that an interrupt is happening and snapshot the message to
        send fresh. Called from the POSIX reader on ``\\x03`` AND from
        ``Agent.turn``'s ``except KeyboardInterrupt`` (the Windows path).
        Idempotent — only the first call captures. Returns True if there's a
        non-empty message to send (typed buffer, or an already-queued message)."""
        if not self.can_run:
            return False
        with self._lock:
            if self._interrupt_msg is None:
                self._interrupt_msg = self._buf or self._pending or ""
                self._buf = ""
                self._pending = None
            self._interrupted = True
            return bool(self._interrupt_msg)

    def partial_buffer(self) -> str:
        """Return text the user was mid-typing when the turn ended (no Enter,
        no Ctrl-C) so the next prompt can pre-fill it and not lose their typing.
        Does not clear — ``reset_partial`` does, once the prompt consumes it."""
        if not self.can_run:
            return ""
        with self._lock:
            return self._buf

    def reset_partial(self) -> None:
        """Clear the partial buffer once the idle prompt has consumed it."""
        with self._lock:
            self._buf = ""

    # ------------------------------------------------------------------
    # Reader thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            if self._backend == "msvcrt":
                self._run_msvcrt()
            elif self._backend == "posix":
                self._run_posix()
        except Exception:
            # Never let the reader thread kill the process — a broken reader
            # just means type-ahead silently stops; the turn still runs.
            pass

    def _run_msvcrt(self) -> None:
        import msvcrt

        while not self._stop.is_set():
            if msvcrt.kbhit():  # type: ignore[attr-defined]
                try:
                    ch = msvcrt.getwch()  # type: ignore[attr-defined]
                except Exception:
                    ch = ""
                if ch:
                    self._handle_char(ch)
            else:
                if self._stop.wait(0.02):
                    return

    def _run_posix(self) -> None:
        import select
        import termios

        fd = sys.stdin.fileno()
        try:
            old = termios.tcgetattr(fd)
        except Exception:
            return
        try:
            new = termios.tcgetattr(fd)
            # c_lflag (index 3): disable canonical mode, echo, and signal
            # generation so Ctrl-C is delivered as a byte, not a SIGINT.
            new[3] &= ~termios.ECHO
            new[3] &= ~termios.ICANON
            new[3] &= ~termios.ISIG
            # c_cc (index 6): block until at least one byte is available.
            new[6][termios.VMIN] = 1
            new[6][termios.VTIME] = 0
            # Leave c_oflag (index 1) intact — OPOST must stay on so the
            # streaming output's "\n" still maps to CRLF.
            termios.tcsetattr(fd, termios.TCSANOW, new)
            while not self._stop.is_set():
                r, _, _ = select.select([fd], [], [], 0.05)
                if not r:
                    continue
                try:
                    data = os.read(fd, 256)
                except OSError:
                    return
                if not data:
                    return
                try:
                    text = data.decode("utf-8", "replace")
                except Exception:
                    text = data.decode("latin-1", "replace")
                for ch in text:
                    if self._stop.is_set():
                        return
                    self._handle_char(ch)
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Key handling (shared by both backends)
    # ------------------------------------------------------------------

    def _handle_char(self, ch: str) -> None:
        o = ord(ch)
        if o in (0x00, 0xE0):
            # Windows extended-key prefix (arrows, F-keys, etc.) — consume and
            # discard the scan code that follows; never append either byte.
            if self._backend == "msvcrt":
                try:
                    import msvcrt

                    msvcrt.getwch()  # type: ignore[attr-defined]
                except Exception:
                    pass
            return
        if ch in ("\r", "\n"):
            self._submit()
        elif ch in ("\x08", "\x7f"):
            self._backspace()
        elif ch == "\x03":
            # Ctrl-C. On POSIX (raw, no ISIG) it arrives here; on Windows it
            # normally raises KeyboardInterrupt instead — but handle either way.
            self.capture_interrupt()
            self._paint()
        elif ch == "\x15":
            # Ctrl-U: clear the line buffer (common readline convention).
            with self._lock:
                self._buf = ""
            self._paint()
        elif o >= 0x20:
            self._append(ch)
        # other control chars (Tab, etc.): ignore

    def _append(self, ch: str) -> None:
        with self._lock:
            self._buf += ch
        self._paint()

    def _backspace(self) -> None:
        with self._lock:
            if self._buf:
                self._buf = self._buf[:-1]
        self._paint()

    def _submit(self) -> None:
        """Enter: queue the typed line for the next turn (replacing any earlier
        queued message). Empty input is ignored (no empty-message queue)."""
        with self._lock:
            if self._buf.strip():
                self._pending = self._buf
                self._buf = ""
        self._paint()

    # ------------------------------------------------------------------
    # Echo row painting
    # ------------------------------------------------------------------

    def _echo_row(self) -> int:
        return shutil.get_terminal_size((80, 24)).lines - 1

    def _paint(self) -> None:
        if not self._active or not sys.stdout.isatty():
            return
        prefix = ui.prompt_prefix()  # "│ ✦ " — matches the framed input rail
        avail = ui.width() - ui._vlen(prefix)
        if avail < 1:
            return
        with self._lock:
            buf = self._buf
            pending = self._pending
        # The block cursor costs a column, so reserve it before laying out text.
        avail -= 1
        if avail < 1:
            return
        if buf:
            tag = ui.color(" ✉", ui.GOLD) if pending else ""
            content = prefix + _visible_tail(buf, avail - ui._vlen(tag)) + _CURSOR + tag
        elif pending:
            mark = ui.color("✉ ", ui.GOLD)
            body = _visible_tail(pending, max(1, avail - ui._vlen(mark)))
            content = prefix + mark + body + _CURSOR
        else:
            hint = ui.color("type to queue · Ctrl-C to interrupt", ui.SOFT)
            content = prefix + _CURSOR + ui.truncate(hint, avail)
        row = self._echo_row()
        # Move to the echo row, clear it, write the content, restore the cursor
        # — all under the shared paint lock so a StatusBar frame can't interleave.
        #
        # The real cursor is deliberately restored rather than parked here: it
        # has to stay in the scroll region, because streamed output is written
        # at wherever it happens to be. `_CURSOR` is a drawn stand-in, which is
        # why it can sit on a row the hardware cursor must never rest on.
        ui.sync_write(f"\0337\033[{row};1H\033[2K{content}\0338")

    def _clear_row(self) -> None:
        if not sys.stdout.isatty():
            return
        row = self._echo_row()
        ui.sync_write(f"\0337\033[{row};1H\033[2K\0338")
