"""Client factory — resolves 'provider:model' strings to the right API client.

Used by both the CLI (cli.py) and the web gateway (gateway.py) so the same
provider-switching logic isn't duplicated.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from typing import Any

from . import config as _config

_log = logging.getLogger(__name__)


def _positive_float(cfg: dict, key: str, default: float) -> float:
    raw = _config.get_value(cfg, key, default)
    try:
        value = float(raw) if not isinstance(raw, bool) else 0.0
    except (TypeError, ValueError):
        value = 0.0
    if value > 0 and math.isfinite(value):
        return value
    _log.warning("ignoring invalid %s=%r; using %s", key, raw, default)
    return default


def _integer(cfg: dict, key: str, default: int, *, positive: bool = False) -> int:
    raw = _config.get_value(cfg, key, default)
    try:
        if isinstance(raw, bool):
            raise ValueError
        value = int(raw)
    except (TypeError, ValueError):
        _log.warning("ignoring invalid %s=%r; using %s", key, raw, default)
        return default
    if positive and value <= 0:
        _log.warning("ignoring invalid %s=%r; using %s", key, raw, default)
        return default
    return value


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


# Context windows by model-name prefix, longest prefix wins. Only the input
# window matters here — this drives compaction thresholds and the /context
# readout, not max_tokens.
#
# Before this table the window came from `ollama.num_ctx` (default 8192) for
# EVERY provider, so a 200k Claude model was told it had 8k and compacted away
# context it never needed to lose, while the compaction threshold itself was a
# flat 32000 that an 8k local model could never reach before overflowing.
_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    # Anthropic
    ("claude-opus-4", 200_000),
    ("claude-sonnet-4", 200_000),
    ("claude-haiku-4", 200_000),
    ("claude-3-5", 200_000),
    ("claude-3-7", 200_000),
    ("claude-3", 200_000),
    ("claude", 200_000),
    # OpenAI
    ("gpt-4.1", 1_047_576),
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("gpt-4", 8_192),
    ("gpt-3.5", 16_385),
    ("o1", 200_000),
    ("o3", 200_000),
    ("o4", 200_000),
)

# Used when a cloud model isn't in the table — better to assume a modern
# window than to inherit Ollama's 8k and silently over-compact.
_PROVIDER_FALLBACK_WINDOW = {"anthropic": 200_000, "openai": 128_000}


def context_window_for(model_spec: str, cfg: dict, default: int = 8192) -> int:
    """Input context window, in tokens, for a 'provider:model' spec.

    Resolution order: explicit per-model config override → known-model table →
    provider fallback → for Ollama tags, `ollama.num_ctx` (the only place the
    number is genuinely a local runtime setting rather than a model property).
    """
    if not isinstance(model_spec, str) or not model_spec.strip():
        return default

    override = _config.get_model_capability(cfg, model_spec, "context_window")
    try:
        if override is not None and not isinstance(override, bool):
            value = int(override)
            if value > 0:
                return value
    except (TypeError, ValueError):
        _log.warning("ignoring invalid context_window override for %s: %r", model_spec, override)

    provider, name = parse_model(model_spec)

    if provider == "ollama":
        # A local model's usable window is whatever Ollama was told to
        # allocate, not whatever the weights theoretically support.
        return _integer(cfg, "ollama.num_ctx", default, positive=True)

    lowered = name.lower()
    best: tuple[int, int] | None = None  # (prefix length, window)
    for prefix, window in _CONTEXT_WINDOWS:
        if lowered.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), window)
    if best is not None:
        return best[1]
    return _PROVIDER_FALLBACK_WINDOW.get(provider, default)


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
        if not isinstance(api_key, str) or not api_key.strip():
            raise RuntimeError(
                "OpenAI API key not set.\n"
                "  Option 1: export OPENAI_API_KEY=sk-...\n"
                "  Option 2: run cagentic --login openai"
            )
        base_url = _config.get_value(cfg, "providers.openai.base_url", "https://api.openai.com/v1")
        if not isinstance(base_url, str) or not base_url.strip():
            _log.warning("ignoring invalid providers.openai.base_url=%r", base_url)
            base_url = "https://api.openai.com/v1"
        return OpenAIClient(api_key=api_key, base_url=base_url)

    if provider == "anthropic":
        from .anthropic_client import AnthropicClient  # noqa: F401

        api_key = os.environ.get("ANTHROPIC_API_KEY") or _config.get_value(
            cfg, "providers.anthropic.api_key"
        )
        if not isinstance(api_key, str) or not api_key.strip():
            raise RuntimeError(
                "Anthropic API key not set.\n"
                "  Option 1: export ANTHROPIC_API_KEY=sk-ant-...\n"
                "  Option 2: run cagentic --login anthropic"
            )
        cache = _config.get_value(cfg, "providers.anthropic.prompt_cache", True)
        return AnthropicClient(api_key=api_key, prompt_cache=cache is not False)

    if provider == "ollama":
        from .ollama_client import OllamaClient

        raw_host = os.environ.get("OLLAMA_HOST") or cfg.get("host", "http://localhost:11434")
        if not isinstance(raw_host, str):
            _log.warning("ignoring invalid host=%r; using localhost", raw_host)
            raw_host = "http://localhost:11434"
        keep_alive = _config.get_value(cfg, "ollama.keep_alive", "30m")
        if isinstance(keep_alive, bool) or not isinstance(
            keep_alive, (str, int, float, type(None))
        ):
            _log.warning("ignoring invalid ollama.keep_alive=%r; using 30m", keep_alive)
            keep_alive = "30m"
        return OllamaClient(
            host=raw_host,
            connect_timeout=_positive_float(cfg, "ollama.connect_timeout", 15.0),
            read_timeout=_positive_float(cfg, "ollama.read_timeout", 1800.0),
            nonstream_read_timeout=_positive_float(cfg, "ollama.nonstream_read_timeout", 1800.0),
            keep_alive=keep_alive,
            num_ctx=_integer(cfg, "ollama.num_ctx", 8192, positive=True),
            num_predict=_integer(cfg, "ollama.num_predict", -1),
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
