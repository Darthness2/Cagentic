"""Scriptable installation and connectivity diagnostics."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import command_utils, config, sessions
from .providers import build_client, context_window_for, parse_model
from .storage import database_path

# How much of the window the uncompactable floor may take before this is a
# finding rather than a note. Above roughly a third, compaction is fighting for
# what little is left and long turns start failing for reasons no amount of
# trimming can fix.
_FLOOR_LIMIT = 0.35


def run(cfg: dict) -> dict:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    cfg_dir = config.config_dir()
    try:
        cfg_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg_dir / ".doctor-write"
        probe.write_text("ok")
        probe.unlink()
        add("config", True, str(cfg_dir))
    except OSError as exc:
        add("config", False, str(exc))

    model_spec = str(cfg.get("model") or "")
    if model_spec:
        provider, model = parse_model(model_spec)
        try:
            client = build_client(cfg, provider)
            available = client.list_models()
            add("provider", True, f"{provider}:{model}; {len(available)} model(s) visible")
        except Exception as exc:
            add("provider", False, f"{provider}: {exc}")
    else:
        add("provider", False, "no model configured")

    # What every request costs before a single word of conversation: the tool
    # definitions plus the system prompt. Neither can be compacted away, so when
    # this floor approaches the model's window there is nothing the agent can do
    # at runtime to recover — turns just start failing. The shipped defaults were
    # once past 100% of the configured window, which is the kind of thing that
    # should be a line in `--doctor` rather than a mystery.
    if model_spec:
        try:
            from .engine import fetch_system_prompt_parts
            from .services.compact import approx_schema_tokens, approx_tokens
            from .state import AppState
            from .tools import all_tool_schemas

            groups_raw = cfg.get("tool_groups")
            groups = set(groups_raw) if isinstance(groups_raw, list) else None
            schemas = all_tool_schemas(groups)
            probe_state = AppState(workspace=Path.cwd(), home=Path.home())
            floor = approx_schema_tokens(schemas) + approx_tokens(
                [{"role": "system", "content": fetch_system_prompt_parts(probe_state)}]
            )
            window = context_window_for(model_spec, cfg)
            share = (floor / window) if window else 0.0
            detail = (
                f"~{floor:,} tokens before any conversation "
                f"({len(schemas)} tools + system prompt) of a {window:,}-token window"
            )
            if share >= _FLOOR_LIMIT:
                detail += f" — {share:.0%}. Raise the context size or turn off tool groups."
            add("context_floor", share < _FLOOR_LIMIT, detail)
        except Exception as exc:  # a diagnostic must never be the thing that breaks
            add("context_floor", True, f"not measured ({type(exc).__name__}: {exc})")

    roots = os.environ.get("CAGENTIC_WORKSPACE_ROOTS") or str(Path.cwd())
    add("workspace_roots", True, roots)
    add("sqlite", True, str(database_path()))
    add("sessions", True, f"{len(sessions.list_all())} saved")
    add("git", shutil.which("git") is not None, shutil.which("git") or "not installed")
    raw_browser = cfg.get("browser")
    browser = raw_browser if isinstance(raw_browser, dict) else {}
    browser_enabled = command_utils.boolean_value(browser.get("enabled", True), True)
    add("browser", browser_enabled, "enabled in config")

    # Report the sandbox explicitly: shell commands are confined by default, so
    # a missing backend (or a deliberate shell.sandbox=off) changes what an
    # approval actually grants, and that shouldn't be something you discover by
    # accident when a command fails to reach the network.
    from . import sandbox as _sandbox

    raw_shell = cfg.get("shell")
    shell = raw_shell if isinstance(raw_shell, dict) else {}
    sandbox_setting = str(shell.get("sandbox", "auto")).lower()
    network_setting = str(shell.get("network", "deny")).lower()
    if sandbox_setting == "off":
        add("shell_sandbox", False, "disabled by config (shell.sandbox=off)")
    else:
        kind = _sandbox.backend()
        add(
            "shell_sandbox",
            kind != "none",
            f"{_sandbox.describe()}; network {network_setting}",
        )
    return {"ok": all(item["ok"] for item in checks), "checks": checks}
