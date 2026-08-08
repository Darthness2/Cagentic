#!/usr/bin/env python3
"""Cagentic daemon — start the gateway web UI and stay alive forever.

Unlike `run.py`, this never enters the REPL, so the process keeps running
without an interactive terminal. Use this when you want the gateway
(http://localhost:8700) available without an attached console.

    python daemon.py
"""
from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def main() -> int:
    # Re-use the same bootstrap deps-check as run.py.
    try:
        for pkg in ("requests", "prompt_toolkit", "pypdf"):
            __import__(pkg)
    except ImportError:
        print("Some dependencies are missing. Run: python run.py --install")
        return 1

    from cagentic.cli import main as cli_main

    # cli_main returns from inside repl(); we want to short-circuit before
    # that. The easiest path: copy the setup, then sleep forever.
    # We do that by monkey-patching cli.repl with a sleep-loop.
    from cagentic import cli

    def _idle_forever(agent, cfg, gateway_holder=None):
        print("Cagentic gateway daemon is running.")
        print("Open http://localhost:8700 in your browser.")
        print("Press Ctrl+C to stop.")
        # Keep main thread alive; gateway runs in a daemon thread.
        stop = {"flag": False}
        def _sig(*_):
            stop["flag"] = True
        for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, _sig)
            except (ValueError, OSError):
                pass
        try:
            while not stop["flag"]:
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        return 0

    cli.repl = _idle_forever
    return cli_main([])


if __name__ == "__main__":
    sys.exit(main())
