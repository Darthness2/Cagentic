#!/usr/bin/env python3
"""Cagentic bootstrap runner.

Usage:
    python run.py              # start Cagentic (auto-installs deps on first run)
    python run.py --install    # force install / upgrade dependencies only
    python run.py --check      # check if dependencies are satisfied

Any additional arguments are forwarded to Cagentic, e.g.:
    python run.py --model llama3.2
    python run.py -p "hello"
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
VENV_DIR  = REPO_ROOT / ".venv"


# ---------------------------------------------------------------------------
# Venv bootstrap — if we're not already inside the project venv, create it
# and re-exec this script under it so every subsequent action uses the right
# Python.  This handles PEP 668 "externally-managed" environments (Homebrew,
# Debian/Ubuntu system Python, etc.) transparently.
# ---------------------------------------------------------------------------

def _venv_python() -> Path:
    """Return the path to the venv's Python binary."""
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _in_project_venv() -> bool:
    """True when we're already running inside the project's .venv."""
    try:
        return Path(sys.prefix).resolve() == VENV_DIR.resolve()
    except Exception:
        return False


def _ensure_venv() -> None:
    """Create the project venv if it doesn't exist yet."""
    if not VENV_DIR.exists():
        print(f"Creating virtual environment in {VENV_DIR} …")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
        print("Virtual environment ready.\n")


def _reexec_in_venv() -> None:
    """Re-run this script using the venv Python (replaces current process)."""
    venv_py = _venv_python()
    os.execv(str(venv_py), [str(venv_py)] + sys.argv)


# ---------------------------------------------------------------------------
# Dependency helpers — only used once we're inside the venv
# ---------------------------------------------------------------------------

def _deps_satisfied() -> bool:
    """Return True if all required packages are importable."""
    for pkg in ("requests", "prompt_toolkit", "pypdf"):
        try:
            __import__(pkg)
        except ImportError:
            return False
    return True


def _install_deps() -> None:
    """Install this package (with all dependencies) in editable mode."""
    print("Installing Cagentic dependencies…")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-e", str(REPO_ROOT), "--quiet"]
    )
    print("Dependencies installed.\n")


def _run_cagentic(args: list[str]) -> int:
    """Invoke the Cagentic CLI entry point."""
    from cagentic.cli import main
    return main(args)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    # --- venv gate ---
    if not _in_project_venv():
        _ensure_venv()
        _reexec_in_venv()
        # _reexec_in_venv() replaces the process; we never reach here.

    args = sys.argv[1:]

    if "--check" in args:
        if _deps_satisfied():
            print("All dependencies are satisfied.")
            return 0
        print("Some dependencies are missing.")
        return 1

    force_install = "--install" in args
    if force_install:
        args.remove("--install")

    if force_install or not _deps_satisfied():
        _install_deps()

    return _run_cagentic(args)


if __name__ == "__main__":
    sys.exit(main())
