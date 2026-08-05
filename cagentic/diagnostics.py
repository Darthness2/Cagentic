"""Scriptable installation and connectivity diagnostics."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import config, sessions
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
    add("browser", bool((cfg.get("browser") or {}).get("enabled", True)), "enabled in config")
    return {"ok": all(item["ok"] for item in checks), "checks": checks}
