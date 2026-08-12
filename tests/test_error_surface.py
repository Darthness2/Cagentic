"""Regressions for two things that leaked implementation detail at the user.

1. Resizing the terminal mid-turn sprayed duplicated "working …" status lines
   into the scrollback: a window drag emits a continuous stream of sizes, and
   the bar re-reserved its row (a destructive, scrolling operation) on every
   one of them.

2. A failed `web_fetch` printed sixty lines of urllib3 traceback into the
   middle of the assistant's reply. Nothing configured a logging handler, so
   Python's lastResort handler sent every `exc_info=True` record to stderr.
"""

from __future__ import annotations

import io
import logging
import sys
import tempfile
import unittest
from pathlib import Path


class TestRequestErrorMessages(unittest.TestCase):
    """The model reads these strings and decides what to do next, so they have
    to name the cause and the fix — not restate the stack."""

    def setUp(self) -> None:
        import requests

        from cagentic.tools import describe_request_error

        self.requests = requests
        self.describe = describe_request_error

    def test_ssl_error_names_the_cause_and_the_switch(self) -> None:
        exc = self.requests.exceptions.SSLError(
            "HTTPSConnectionPool(host='openai.com', port=443): Max retries exceeded "
            "(Caused by SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] "
            "certificate verify failed: self-signed certificate in certificate chain')))"
        )
        out = self.describe(exc, "https://openai.com/pricing")
        self.assertEqual(len(out.splitlines()), 1, "must be one line, not a traceback")
        self.assertIn("openai.com", out)
        self.assertIn("intercepting HTTPS", out)
        self.assertIn("insecure_ssl", out)
        # Steer the model away from a pointless retry loop.
        self.assertIn("Do NOT retry", out)
        for noise in ("Traceback", "urllib3", "_ssl.c", 'File "'):
            self.assertNotIn(noise, out)

    def test_insecure_ssl_is_a_real_settable_key(self) -> None:
        """The SSL message tells the user to run `/set insecure_ssl true`; if
        that key ever stops being valid the advice becomes a lie."""
        from cagentic.command_utils import _BOOLEAN_SETTINGS, validate_config_value

        self.assertIn("insecure_ssl", _BOOLEAN_SETTINGS)
        self.assertIsNone(validate_config_value("insecure_ssl", True))

    def test_each_failure_mode_gets_its_own_sentence(self) -> None:
        cases = [
            (self.requests.exceptions.ConnectTimeout("x"), "timed out connecting"),
            (self.requests.exceptions.ReadTimeout("x"), "sent no response in time"),
            (self.requests.exceptions.TooManyRedirects("x"), "too many redirects"),
            (self.requests.exceptions.ProxyError("x"), "proxy refused"),
            (
                self.requests.exceptions.ConnectionError("Connection refused"),
                "refused the connection",
            ),
            (
                self.requests.exceptions.ConnectionError("Name or service not known"),
                "could not resolve",
            ),
        ]
        for exc, expected in cases:
            out = self.describe(exc, "https://example.test/a")
            self.assertIn(expected, out, type(exc).__name__)
            self.assertEqual(len(out.splitlines()), 1, type(exc).__name__)

    def test_unknown_error_is_still_one_bounded_line(self) -> None:
        exc = self.requests.exceptions.RequestException("boom\n" + "x" * 5000)
        out = self.describe(exc, "https://example.test/a")
        self.assertEqual(len(out.splitlines()), 1)
        self.assertLess(len(out), 400)


class TestLoggingStaysOffTheTranscript(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        import os

        os.environ["XDG_CONFIG_HOME"] = self._tmp.name
        # Reset the module's one-shot guard so setup() runs against the temp dir.
        from cagentic import logs

        logs._CONFIGURED = False
        self.logs = logs
        self.logger = logging.getLogger("cagentic")
        self._old_handlers = list(self.logger.handlers)
        self.logger.handlers.clear()

    def tearDown(self) -> None:
        self.logger.handlers.clear()
        self.logger.handlers.extend(self._old_handlers)
        self.logs._CONFIGURED = False
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_a_traceback_goes_to_the_file_and_not_to_stderr(self) -> None:
        self.logs.setup(debug=False)
        captured = io.StringIO()
        old, sys.stderr = sys.stderr, captured
        try:
            try:
                raise ValueError("something went wrong deep in a library")
            except ValueError:
                logging.getLogger("cagentic.tools").warning("fetch failed", exc_info=True)
        finally:
            sys.stderr = old

        self.assertEqual(captured.getvalue(), "", "diagnostics must not reach the transcript")
        text = Path(self.logs.log_path()).read_text(encoding="utf-8")
        self.assertIn("something went wrong", text)
        self.assertIn("Traceback", text)

    def test_propagation_is_off_so_lastresort_never_fires(self) -> None:
        """lastResort is what wrote to stderr; it only runs when a record
        reaches the root logger with no handlers anywhere."""
        self.logs.setup(debug=False)
        self.assertFalse(self.logger.propagate)
        self.assertTrue(self.logger.handlers)

    def test_debug_mirrors_to_stderr(self) -> None:
        self.logs.setup(debug=True)
        kinds = {type(h).__name__ for h in self.logger.handlers}
        self.assertIn("StreamHandler", kinds)
        self.assertEqual(self.logger.level, logging.DEBUG)


class TestStatusBarResize(unittest.TestCase):
    """The bar must not touch the scroll region until the size settles."""

    def _bar(self, rows: int):
        from cagentic import ui

        bar = ui.StatusBar(ctx_tokens=10)
        bar._active = True
        bar._last_rows = rows
        bar._pending_rows = rows
        return bar

    def _paint_to(self, bar, rows: int, at: float) -> str:
        """Run one _paint() with a stubbed terminal size and clock."""
        import shutil

        from cagentic import ui

        out = io.StringIO()
        old_size, old_stdout, old_time = shutil.get_terminal_size, sys.stdout, ui.time.monotonic
        shutil.get_terminal_size = lambda fallback=(80, 24): type(
            "S", (), {"lines": rows, "columns": 80}
        )()
        sys.stdout = out
        ui.time.monotonic = lambda: at
        try:
            bar._paint()
        finally:
            shutil.get_terminal_size = old_size
            sys.stdout = old_stdout
            ui.time.monotonic = old_time
        return out.getvalue()

    def test_steady_size_paints_without_touching_the_region(self) -> None:
        bar = self._bar(24)
        out = self._paint_to(bar, 24, 100.0)
        self.assertNotIn("\033[1;", out, "no DECSTBM on an unchanged size")
        self.assertIn("\033[24;1H", out, "still paints the bar row")

    def test_first_sight_of_a_new_size_paints_nothing(self) -> None:
        """Painting against a geometry we're about to redo is what stranded
        stale 'working …' lines in the scrollback."""
        bar = self._bar(24)
        self.assertEqual(self._paint_to(bar, 30, 100.0), "")

    def test_a_drag_does_not_re_reserve_on_every_tick(self) -> None:
        from cagentic import ui

        bar = self._bar(24)
        emitted = [
            self._paint_to(bar, rows, 100.0 + i * 0.05)
            for i, rows in enumerate([30, 31, 32, 33, 34, 35])
        ]
        self.assertEqual([o for o in emitted if o], [], "a drag must produce no output at all")
        # Once it holds still past the settle window, exactly one re-reserve.
        settled = self._paint_to(bar, 35, 100.0 + ui._RESIZE_SETTLE + 1.0)
        self.assertEqual(settled.count("\033[1;34r"), 1)
        self.assertEqual(bar._last_rows, 35)

    def test_growing_clears_the_old_bar_row(self) -> None:
        """It's now mid-screen holding stale text; leaving it there is the
        duplicated-line symptom."""
        from cagentic import ui

        bar = self._bar(24)
        self._paint_to(bar, 40, 100.0)
        out = self._paint_to(bar, 40, 100.0 + ui._RESIZE_SETTLE + 1.0)
        self.assertIn("\033[24;1H\033[2K", out, "old row must be cleared")

    def test_shrinking_does_not_address_a_row_that_no_longer_exists(self) -> None:
        from cagentic import ui

        bar = self._bar(40)
        self._paint_to(bar, 24, 100.0)
        out = self._paint_to(bar, 24, 100.0 + ui._RESIZE_SETTLE + 1.0)
        self.assertNotIn("\033[40;1H", out)


if __name__ == "__main__":
    unittest.main()
