"""Logging setup — keep diagnostics out of the user's transcript.

The package logs failures with ``logger.warning(..., exc_info=True)`` in ~70
places. That is the right thing for the *developer*; the problem was that
nothing ever configured a handler, so Python's ``lastResort`` handler took over
and wrote every one of those records — full traceback included — straight to
stderr, interleaved with the assistant's reply. A user asking about pricing got
sixty lines of urllib3 stack trace in the middle of their answer.

So: attach a rotating file handler and stop propagation to the root handler.
Warnings and tracebacks go to ``~/.config/cagentic/logs/cagentic.log``, where
they can be read after the fact; the terminal shows only what the UI decides to
show. ``--debug`` puts them back on stderr for development.

This module is imported for its side effect at CLI/gateway start-up, so it must
stay dependency-free (it sits below everything except config).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

_CONFIGURED = False

# One file, rotated, so a long-running gateway can't fill a disk.
MAX_BYTES = 1_000_000
BACKUPS = 3


def log_path() -> Path:
    """Where diagnostics land. Mirrors config_dir() without importing it, so
    this module stays at the very bottom of the import graph."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "cagentic" / "logs" / "cagentic.log"


def setup(debug: bool = False) -> None:
    """Install the package's log handlers. Safe to call more than once.

    `debug=True` also mirrors records to stderr with tracebacks — the
    behaviour `--debug` promises.
    """
    global _CONFIGURED
    logger = logging.getLogger("cagentic")
    if _CONFIGURED:
        logger.setLevel(logging.DEBUG if debug else logging.WARNING)
        return

    logger.setLevel(logging.DEBUG if debug else logging.WARNING)
    # The whole point: don't hand records to the root logger, whose absent
    # configuration is what triggers lastResort → stderr.
    logger.propagate = False

    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    except OSError:
        # A read-only or missing home shouldn't stop the assistant from
        # running; losing the log file is survivable, spewing to the
        # transcript is not.
        logger.addHandler(logging.NullHandler())

    if debug:
        stderr = logging.StreamHandler(sys.stderr)
        stderr.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(stderr)

    _CONFIGURED = True
