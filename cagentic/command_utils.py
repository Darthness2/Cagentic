"""Shared parsing and validation for terminal and gateway commands."""

from __future__ import annotations

import json
import math
from typing import Any

_BOOLEAN_SETTINGS = {
    "browser.enabled",
    "gateway.auto_port",
    "gateway.auto_reload",
    "gateway.auto_start",
    "gateway.lan",
    "insecure_ssl",
    "ollama.stream",
    "proactive.desktop_notifications",
    "proactive.enabled",
    "yolo",
}
_PORT_SETTINGS = {"browser.port", "gateway.port"}


def boolean_value(value: Any, default: bool) -> bool:
    """Return only genuine booleans; config strings such as ``"false"`` are invalid."""
    return value if isinstance(value, bool) else default


def full_argument(arg1: str, arg2: str) -> str:
    """Reassemble the text after a slash command without losing spaces."""
    return (arg1 + (" " + arg2 if arg2 else "")).strip()


def parse_config_value(text: str) -> Any:
    """Parse JSON scalars/containers while leaving ordinary text as text.

    This gives both command surfaces the same behavior for values such as
    ``false``, ``8192``, ``null``, and ``["files", "web"]``. Unquoted model
    names, URLs, and durations remain strings.
    """
    value = text.strip()

    def reject_constant(constant: str) -> None:
        raise ValueError(f"non-finite JSON value: {constant}")

    try:
        return json.loads(value, parse_constant=reject_constant)
    except (json.JSONDecodeError, TypeError, ValueError):
        return value


def validate_config_key(key: str) -> str | None:
    """Return an actionable error for malformed dotted config keys."""
    parts = key.split(".")
    if not key or any(not part or any(ch.isspace() for ch in part) for part in parts):
        return "config key must use non-empty dot-separated names without spaces"
    return None


def validate_config_value(key: str, value: Any) -> str | None:
    """Validate settings whose runtime contract Cagentic knows."""
    if key in _BOOLEAN_SETTINGS and not isinstance(value, bool):
        return f"{key} must be true or false"
    if key == "temperature":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "temperature must be a number between 0 and 2"
        if not math.isfinite(float(value)) or not 0 <= float(value) <= 2:
            return "temperature must be a number between 0 and 2"
    if key in _PORT_SETTINGS:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            return f"{key} must be an integer between 1 and 65535"
    if key == "ollama.num_ctx":
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return "ollama.num_ctx must be a positive integer"
    if key in {
        "ollama.connect_timeout",
        "ollama.read_timeout",
        "ollama.nonstream_read_timeout",
    }:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
        ):
            return f"{key} must be a positive number"
    if key == "ollama.num_predict" and (isinstance(value, bool) or not isinstance(value, int)):
        return "ollama.num_predict must be an integer"
    if key == "ollama.keep_alive" and not (
        value is None or isinstance(value, (str, int, float)) and not isinstance(value, bool)
    ):
        return "ollama.keep_alive must be text, a number, or null"
    if key == "proactive.interval":
        if isinstance(value, bool) or not isinstance(value, int) or not 30 <= value <= 3600:
            return "proactive.interval must be an integer between 30 and 3600 seconds"
    if key == "effort" and value not in {"low", "medium", "high"}:
        return "effort must be low, medium, or high"
    if key == "shell.sandbox" and value not in {"auto", "off"}:
        return "shell.sandbox must be auto or off"
    if key == "shell.network" and value not in {"deny", "allow"}:
        return "shell.network must be deny or allow"
    if key == "shell.session" and value not in {"auto", "off"}:
        return "shell.session must be auto or off"
    if key == "tool_groups" and not (
        value is None
        or (
            isinstance(value, list)
            and all(isinstance(item, str) and item.strip() for item in value)
        )
    ):
        return "tool_groups must be null or a JSON list of group names"
    if key in {"host", "model", "providers.openai.base_url", "user_name"} and not (
        value is None or isinstance(value, str)
    ):
        return f"{key} must be text or null"
    return None


def apply_runtime_setting(state: Any, engine: Any, key: str, value: Any) -> bool:
    """Apply a validated setting to one live engine when possible."""
    if key == "temperature":
        engine.temperature = float(value)
        return True
    if key == "ollama.num_ctx" and hasattr(engine.client, "num_ctx"):
        engine.client.num_ctx = int(value)
        return True
    timeout_attributes = {
        "ollama.connect_timeout": "connect_timeout",
        "ollama.read_timeout": "read_timeout",
        "ollama.nonstream_read_timeout": "nonstream_read_timeout",
    }
    if key in timeout_attributes and hasattr(engine.client, timeout_attributes[key]):
        setattr(engine.client, timeout_attributes[key], float(value))
        return True
    if key == "ollama.num_predict" and hasattr(engine.client, "num_predict"):
        engine.client.num_predict = value
        return True
    if key == "ollama.stream":
        engine.stream = value
        return True
    if key == "ollama.keep_alive" and hasattr(engine.client, "keep_alive"):
        engine.client.keep_alive = value
        return True
    if key == "yolo":
        state.update(yolo=value)
        return True
    if key == "insecure_ssl":
        state.update(insecure_ssl=value)
        return True
    if key == "user_name":
        state.update(user_name=value.strip() if isinstance(value, str) and value.strip() else None)
        engine.refresh_system_prompt()
        return True
    if key == "effort":
        state.update(effort=value)
        engine.refresh_system_prompt()
        return True
    if key == "tool_groups":
        state.update(tool_groups=None if value is None else set(value))
        engine.refresh_system_prompt()
        return True
    if key == "github.token":
        state.update(github_token=value)
        return True
    return False


def switch_value(raw: str, current: bool) -> bool | None:
    """Parse a friendly on/off value; an omitted value toggles."""
    value = raw.strip().lower()
    if not value:
        return not current
    if value in {"on", "true", "1", "yes"}:
        return True
    if value in {"off", "false", "0", "no"}:
        return False
    return None


def context_window(config: dict, default: int = 8192, model_spec: str | None = None) -> int:
    """Context window for *model_spec*, or for the configured active model.

    Callers that don't track a model still get the right answer, because the
    persisted `model` is the one the session is running. Previously this read
    `ollama.num_ctx` unconditionally, so `/context` reported 8192 while talking
    to a 200k Claude model.
    """
    spec = model_spec or config.get("model")
    if isinstance(spec, str) and spec.strip():
        from .providers import context_window_for

        return context_window_for(spec, config, default=default)

    ollama = config.get("ollama")
    raw = ollama.get("num_ctx", default) if isinstance(ollama, dict) else default
    try:
        value = 0 if isinstance(raw, bool) else int(raw)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else default


def mcp_server_config(config: dict) -> dict:
    """Return the MCP server map without trusting user-edited JSON shapes."""
    mcp = config.get("mcp")
    servers = mcp.get("servers") if isinstance(mcp, dict) else None
    return servers if isinstance(servers, dict) else {}
