"""Client factory — resolves 'provider:model' strings to the right API client.

Used by both the CLI (cli.py) and the web gateway (gateway.py) so the same
provider-switching logic isn't duplicated.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from . import config as _config


def parse_model(model_str: str) -> tuple[str, str]:
    """Split 'provider:model' into (provider, model_name).

    Plain model names (no colon, or starting with http) are treated as Ollama.
    Ollama model tags like 'llama3:8b' are also handled — only known provider
    prefixes (ollama, openai, anthropic) are split off; everything else is
    treated as an Ollama model name.
    """
    _KNOWN_PROVIDERS = {"ollama", "openai", "anthropic"}
    if ":" in model_str and not model_str.startswith("http"):
        provider, _, name = model_str.partition(":")
        if provider.lower() in _KNOWN_PROVIDERS:
            return provider.lower(), name
    return "ollama", model_str


def build_client(cfg: dict, provider: str = "ollama") -> Any:
    """Return an instantiated client for *provider*.

    Raises RuntimeError with a user-readable message if credentials are
    missing or the provider name is unknown.
    """
    if provider == "openai":
        from .openai_client import OpenAIClient  # noqa: F401

        api_key = os.environ.get("OPENAI_API_KEY") or _config.get_value(
            cfg, "providers.openai.api_key"
        )
        if not api_key:
            raise RuntimeError(
                "OpenAI API key not set.\n"
                "  Option 1: export OPENAI_API_KEY=sk-...\n"
                "  Option 2: /login openai sk-..."
            )
        base_url = _config.get_value(cfg, "providers.openai.base_url", "https://api.openai.com/v1")
        return OpenAIClient(api_key=api_key, base_url=base_url)

    if provider == "anthropic":
        from .anthropic_client import AnthropicClient  # noqa: F401

        api_key = os.environ.get("ANTHROPIC_API_KEY") or _config.get_value(
            cfg, "providers.anthropic.api_key"
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

        raw_host = os.environ.get("OLLAMA_HOST") or cfg.get("host", "http://localhost:11434")
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

    raise RuntimeError(f"Unknown provider '{provider}'. Supported: ollama, openai, anthropic.")


_MODEL_CACHE: dict[str, list[str]] = {}
_MODEL_CACHE_AT = 0.0
_MODEL_CACHE_LOCK = threading.Lock()
_MODEL_WARMING = False


def list_all_models(
    cfg: dict, *, cached_only: bool = False, max_age: float = 60.0
) -> dict[str, list[str]]:
    """Return a dict of provider → [model, …] for every configured provider.

    Ollama models are always included (if Ollama is reachable).
    Cloud providers are included when their API key is set.
    """
    global _MODEL_CACHE, _MODEL_CACHE_AT
    with _MODEL_CACHE_LOCK:
        if _MODEL_CACHE and (cached_only or time.monotonic() - _MODEL_CACHE_AT < max_age):
            return {key: list(value) for key, value in _MODEL_CACHE.items()}
    if cached_only:
        return {"ollama": []}
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

    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE = {key: list(value) for key, value in result.items()}
        _MODEL_CACHE_AT = time.monotonic()
    return result


def warm_model_cache(cfg: dict) -> threading.Thread:
    """Refresh provider model discovery without delaying CLI/gateway startup."""
    global _MODEL_WARMING
    with _MODEL_CACHE_LOCK:
        if _MODEL_WARMING or (_MODEL_CACHE and time.monotonic() - _MODEL_CACHE_AT < 60):
            return threading.Thread(name="cagentic-model-cache-noop")
        _MODEL_WARMING = True

    def refresh() -> None:
        global _MODEL_WARMING
        try:
            list_all_models(cfg, max_age=0)
        finally:
            with _MODEL_CACHE_LOCK:
                _MODEL_WARMING = False

    thread = threading.Thread(
        target=refresh,
        name="cagentic-model-discovery",
        daemon=True,
    )
    thread.start()
    return thread
