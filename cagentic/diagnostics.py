"""Scriptable installation and connectivity diagnostics."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import command_utils, config, sessions
from .providers import build_client, parse_model
from .storage import database_path


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
