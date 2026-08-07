"""Lifecycle hooks — config-declared command handlers fired on agent events.

Mirrors the Claude Code model: each event maps to a list of handlers in
config.json under ``"hooks"``. ``command`` handlers receive the event
payload as JSON on stdin; exit codes drive allow/deny for the gating
events (``PreToolUse``, ``UserPromptSubmit``):

    0           allow   (stdout, if any, is surfaced to the model as context)
    1           allow   (stdout surfaced to the model as a warning note)
    2           deny    (stderr shown to the user; the tool call / prompt is blocked)
    other / timeout / failure   allow   (hooks must never break the agent loop)

Only ``command`` handlers are implemented now; ``prompt`` and ``http``
handler types are reserved for future use.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

# Events whose handlers can gate execution via exit code 2.
GATING_EVENTS = {"PreToolUse", "UserPromptSubmit"}

# Every recognised event — used by /hooks listing and config defaults.
EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "Notification",
)


def _handlers(cfg: dict | None, event: str) -> list[dict]:
    hooks = (cfg or {}).get("hooks") or {}
    if not isinstance(hooks, dict):
        return []
    entries = hooks.get(event) or []
    if not isinstance(entries, list):
        return []
    return [h for h in entries if isinstance(h, dict) and h.get("command")]


def _run_command(command: str, payload: dict, timeout: float) -> dict:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(os.environ),
        )
        return {
            "exit": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as e:
        stdout = (
            e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else e.stdout or ""
        )
        stderr = (
            e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else e.stderr or ""
        )
        return {
            "exit": -1,
            "stdout": stdout,
            "stderr": stderr + f"\nhook timed out after {timeout}s",
            "timed_out": True,
        }
    except Exception as e:  # never raise out of the agent loop
        logger.warning("hook command failed: %s", e, exc_info=True)
        return {"exit": -1, "stdout": "", "stderr": str(e), "timed_out": False}


def run_event(event: str, payload: dict, cfg: dict | None) -> list[dict]:
    """Run every command handler for ``event``. Returns one result dict per
    handler (never raises)."""
    return [
        _run_command(h["command"], payload, float(h.get("timeout", 10)))
        for h in _handlers(cfg, event)
    ]


def decision(event: str, payload: dict, cfg: dict | None) -> tuple[str, str]:
    """For gating events: returns ``(decision, message)``.

    ``decision`` is ``"allow"``, ``"warn"``, or ``"deny"``. If any handler
    denies (exit 2) we deny; else if any warns (exit 1) we warn; else
    allow. ``message`` is the first handler's stdout (allow/warn) or
    stderr (deny) — surfaced to the model or user. Non-gating events
    always allow.
    """
    if event not in GATING_EVENTS:
        return "allow", ""
    deny_msg = warn_msg = allow_msg = ""
    for r in run_event(event, payload, cfg):
        if r["exit"] == 2:
            deny_msg = deny_msg or (r["stderr"].strip() or "blocked by hook")
        elif r["exit"] == 1:
            warn_msg = warn_msg or r["stdout"].strip()
        elif r["exit"] == 0:
            allow_msg = allow_msg or r["stdout"].strip()
    if deny_msg:
        return "deny", deny_msg
    if warn_msg:
        return "warn", warn_msg
    return "allow", allow_msg


def emit(event: str, payload: dict, cfg: dict | None) -> None:
    """Fire-and-forget for non-gating events (SessionStart, PostToolUse,
    Stop, Notification). Swallows all errors."""
    try:
        run_event(event, payload, cfg)
    except Exception:  # pragma: no cover
        logger.warning("hook event %s failed", event, exc_info=True)


def describe(cfg: dict | None) -> list[tuple[str, int]]:
    """``[(event, handler_count), ...]`` for every recognised event —
    used by the ``/hooks`` slash command."""
    return [(e, len(_handlers(cfg, e))) for e in EVENTS]
