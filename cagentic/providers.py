"""Client factory — resolves 'provider:model' strings to the right API client.

Used by both the CLI (cli.py) and the web gateway (gateway.py) so the same
provider-switching logic isn't duplicated.
"""
from __future__ import annotations

import os
from typing import Any

from . import config as _config


def parse_model(model_str: str) -> tuple[str, str]:
    """Split 'provider:model' into (provider, model_name).

    Plain model names (no colon, or starting with http) are treated as Ollama.
    """
    if ":" in model_str and not model_str.startswith("http"):
        provider, _, name = model_str.partition(":")
        return provider.lower(), name
    return "ollama", model_str


def build_client(cfg: dict, provider: str = "ollama") -> Any:
    """Return an instantiated client for *provider*.

    Raises RuntimeError with a user-readable message if credentials are
    missing or the provider name is unknown.
    """
    if provider == "openai":
        from .openai_client import OpenAIClient  # noqa: F401
        api_key = (
            os.environ.get("OPENAI_API_KEY")
            or _config.get_value(cfg, "providers.openai.api_key")
        )
        if not api_key:
            raise RuntimeError(
                "OpenAI API key not set.\n"
                "  Option 1: export OPENAI_API_KEY=sk-...\n"
                "  Option 2: /login openai sk-..."
            )
        base_url = _config.get_value(
            cfg, "providers.openai.base_url", "https://api.openai.com/v1"
        )
        return OpenAIClient(api_key=api_key, base_url=base_url)

    if provider == "anthropic":
        from .anthropic_client import AnthropicClient  # noqa: F401
        api_key = (
            os.environ.get("ANTHROPIC_API_KEY")
            or _config.get_value(cfg, "providers.anthropic.api_key")
        )
        if not api_key:
            raise RuntimeError(
                "Anthropic API key not set.\n"
                "  Option 1: export ANTHROPIC_API_KEY=sk-ant-...\n"
                "  Option 2: /login anthropic sk-ant-..."
            )
        return AnthropicClient(api_key=api_key)

    if provider == "ollama":
        from .ollama_client import OllamaClient
        raw_host = (
            os.environ.get("OLLAMA_HOST")
            or cfg.get("host", "http://localhost:11434")
        )
        return OllamaClient(
            host=raw_host,
            connect_timeout=float(_config.get_value(cfg, "ollama.connect_timeout", 15.0)),
            read_timeout=float(_config.get_value(cfg, "ollama.read_timeout", 1800.0)),
            nonstream_read_timeout=float(
                _config.get_value(cfg, "ollama.nonstream_read_timeout", 1800.0)
            ),
            keep_alive=_config.get_value(cfg, "ollama.keep_alive", "30m"),
            num_ctx=_config.get_value(cfg, "ollama.num_ctx", 8192),
            num_predict=_config.get_value(cfg, "ollama.num_predict", -1),
        )

    raise RuntimeError(
        f"Unknown provider '{provider}'. Supported: ollama, openai, anthropic."
    )


def list_all_models(cfg: dict) -> dict[str, list[str]]:
    """Return a dict of provider → [model, …] for every configured provider.

    Ollama models are always included (if Ollama is reachable).
    Cloud providers are included when their API key is set.
    """
    result: dict[str, list[str]] = {}

    # Ollama
    try:
        client = build_client(cfg, "ollama")
        result["ollama"] = client.list_models()
    except Exception:
        result["ollama"] = []

    # OpenAI
    if os.environ.get("OPENAI_API_KEY") or _config.get_value(cfg, "providers.openai.api_key"):
        try:
            client = build_client(cfg, "openai")
            result["openai"] = client.list_models()
        except Exception:
            result["openai"] = []

    # Anthropic
    if os.environ.get("ANTHROPIC_API_KEY") or _config.get_value(cfg, "providers.anthropic.api_key"):
        try:
            client = build_client(cfg, "anthropic")
            result["anthropic"] = client.list_models()
        except Exception:
            result["anthropic"] = []

    return result
