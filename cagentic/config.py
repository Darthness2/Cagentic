"""Persistent JSON config for Cagentic.

Stored at $XDG_CONFIG_HOME/cagentic/config.json (default ~/.config/cagentic/config.json).
File is chmod 600 since it can hold API tokens (GitHub PAT, MCP secrets, etc.).
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "cagentic"


def config_path() -> Path:
    return config_dir() / "config.json"


_DEFAULTS: dict[str, Any] = {
    "model": None,
    "host": "http://localhost:11434",
    "temperature": 0.4,          # personal-assistant chat reads better with a little warmth
    "yolo": False,
    "user_name": None,           # how the assistant addresses you
    "tool_groups": None,         # None = cagentic.tools.DEFAULT_GROUPS
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
    "mcp": {"servers": {}},      # {name: {"command": [...], "env": {...}, "enabled": bool}}
    "browser": {"enabled": True, "port": 8765},   # companion Chrome extension bridge
    "gateway": {"port": 8700},   # /gateway web UI
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
    p = config_path()
    if not p.exists():
        return dict(_DEFAULTS)
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULTS)
    if not isinstance(data, dict):
        return dict(_DEFAULTS)
    return _merge(_DEFAULTS, data)


def save(cfg: dict) -> None:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = config_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(p)
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


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
