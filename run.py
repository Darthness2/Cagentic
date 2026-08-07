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
VENV_DIR = REPO_ROOT / ".venv"


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
    """Create the project venv if it doesn't exist yet, ensuring pip is present."""
    if not VENV_DIR.exists():
        print(f"Creating virtual environment in {VENV_DIR} …")
        try:
            subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
        except subprocess.CalledProcessError as exc:
            raise SystemExit(
                f"ERROR: could not create {VENV_DIR} (exit {exc.returncode}). "
                "Install Python's venv support and try again."
            ) from None

    venv_python = _venv_python()
    if not venv_python.is_file():
        raise SystemExit(
            f"ERROR: {VENV_DIR} exists but is not a usable virtual environment "
            f"({venv_python} is missing). Rename or remove that directory and retry."
        )

    # On Debian/Ubuntu (and WSL), pip is not included in the venv by default.
    # Bootstrap it with ensurepip if it's missing.
    venv_py = str(venv_python)
    if (
        subprocess.call(
            [venv_py, "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        != 0
    ):
        result = subprocess.run(
            [venv_py, "-m", "ensurepip", "--upgrade"],
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            sys.exit(
                "\nERROR: pip is missing from the venv and ensurepip is unavailable.\n"
                "On Debian/Ubuntu/WSL, install the missing packages and retry:\n"
                "  sudo apt install python3-pip python3-venv python3-full\n"
                "Then delete .venv/ and re-run:\n"
                "  rm -rf .venv && python3 run.py\n"
            )


def _reexec_in_venv() -> None:
    """Re-run this script using the venv Python (replaces current process)."""
    venv_py = _venv_python()
    os.execv(str(venv_py), [str(venv_py)] + sys.argv)


# ---------------------------------------------------------------------------
# Dependency helpers — only used once we're inside the venv
# ---------------------------------------------------------------------------


def _deps_satisfied() -> bool:
    """Return True if all required packages are importable."""
    for pkg in ("click", "requests", "prompt_toolkit", "pypdf"):
        try:
            __import__(pkg)
        except ImportError:
            return False
    return True


def _install_deps() -> bool:
    """Install this package (with all dependencies) in editable mode."""
    print("Installing Cagentic dependencies…")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-e", str(REPO_ROOT), "--quiet"]
        )
    except subprocess.CalledProcessError as exc:
        print(
            f"ERROR: dependency installation failed (exit {exc.returncode}).",
            file=sys.stderr,
        )
        return False
    print("Dependencies installed.\n")
    return True


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

    if "--check" in args and "--install" in args:
        print("ERROR: --check and --install cannot be used together.", file=sys.stderr)
        return 2

    if "--check" in args:
        if len(args) != 1:
            print("ERROR: --check does not accept CLI arguments.", file=sys.stderr)
            return 2
        if _deps_satisfied():
            print("All dependencies are satisfied.")
            return 0
        print("Some dependencies are missing.")
        return 1

    force_install = "--install" in args
    if force_install:
        args = [arg for arg in args if arg != "--install"]

    if force_install or not _deps_satisfied():
        if not _install_deps():
            return 1
        if force_install and not args:
            return 0

    return _run_cagentic(args)


if __name__ == "__main__":
    sys.exit(main())
