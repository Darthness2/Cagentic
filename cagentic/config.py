"""Persistent JSON config for Cagentic.

Stored at $XDG_CONFIG_HOME/cagentic/config.json (default ~/.config/cagentic/config.json).
File is chmod 600 since it can hold API tokens (GitHub PAT, MCP secrets, etc.).
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

from .storage import atomic_write_json, read_json


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "cagentic"


def config_path() -> Path:
    return config_dir() / "config.json"


_DEFAULTS: dict[str, Any] = {
    "model": None,
    "host": "http://localhost:11434",
    "temperature": 0.4,  # personal-assistant chat reads better with a little warmth
    "yolo": False,
    "user_name": None,  # how the assistant addresses you
    "tool_groups": None,  # None = cagentic.tools.DEFAULT_GROUPS
    "ollama": {
        "num_ctx": 8192,
        "keep_alive": "30m",
        "stream": True,
    },
    # Cloud provider API keys and settings.
    # Models are addressed as "openai:<model>" or "anthropic:<model>".
    # Keys can also be set via OPENAI_API_KEY / ANTHROPIC_API_KEY env vars.
    "providers": {
        "openai": {
            "api_key": None,
            "base_url": "https://api.openai.com/v1",
        },
        "anthropic": {
            "api_key": None,
        },
    },
    "github": {"token": None},
    "mcp": {"servers": {}},  # {name: {"command": [...], "env": {...}, "enabled": bool}}
    "browser": {"enabled": True, "port": 8765},  # companion Chrome extension bridge
    "gateway": {
        "port": 8700,
        "auto_port": True,  # try the next 20 ports when the preferred one is occupied
        "workspace_roots": None,
    },  # /gateway web UI
    "proactive": {
        "enabled": True,
        "interval": 60,
        "desktop_notifications": True,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    data = read_json(config_path(), None)
    if not isinstance(data, dict):
        return copy.deepcopy(_DEFAULTS)
    return _merge(_DEFAULTS, data)


def save(cfg: dict) -> None:
    """Persist the config. The file can hold API tokens, so it is written
    privately (0600 before any bytes land) and replaced atomically."""
    atomic_write_json(config_path(), cfg, private=True)


def set_value(cfg: dict, dotted_key: str, value: Any) -> dict:
    parts = dotted_key.split(".")
    cur = cfg
    for k in parts[:-1]:
        if not isinstance(cur.get(k), dict):
            cur[k] = {}
        cur = cur[k]
    cur[parts[-1]] = value
    return cfg


def get_value(cfg: dict, dotted_key: str, default: Any = None) -> Any:
    cur: Any = cfg
    for k in dotted_key.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _mask(value: Any) -> str:
    """Mask a secret value for display, keeping a short hint of its shape."""
    s = str(value)
    if len(s) > 8:
        return s[:4] + "…" + s[-4:]
    return "••••"


def redact_secrets(cfg: dict) -> dict:
    """Return a deep copy of `cfg` with every secret-bearing value masked.

    Masks: github.token, providers.<name>.api_key, SMTP/email password,
    and MCP server env secrets. Safe to print (e.g. /config) or expose to
    the gateway. Never mutates the input.
    """
    out = copy.deepcopy(cfg)

    # GitHub token
    gh = out.get("github")
    if isinstance(gh, dict) and gh.get("token"):
        gh["token"] = _mask(gh["token"])

    # Cloud provider API keys
    providers = out.get("providers")
    if isinstance(providers, dict):
        for spec in providers.values():
            if isinstance(spec, dict) and spec.get("api_key"):
                spec["api_key"] = _mask(spec["api_key"])

    # SMTP / email password
    email = out.get("email")
    if isinstance(email, dict):
        for k in list(email.keys()):
            if "password" in k.lower() and email.get(k):
                email[k] = _mask(email[k])

    # MCP server env secrets
    mcp = out.get("mcp")
    if isinstance(mcp, dict):
        servers = mcp.get("servers")
        if isinstance(servers, dict):
            for spec in servers.values():
                if not isinstance(spec, dict):
                    continue
                env = spec.get("env")
                if not isinstance(env, dict):
                    continue
                for k, v in list(env.items()):
                    if any(s in k.lower() for s in ("token", "secret", "key", "password")):
                        env[k] = _mask(v) if v else "••••"

    return out
