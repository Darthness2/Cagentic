"""Provider-aware token counting with a deterministic fallback."""

from __future__ import annotations

import json

from .services.compact import approx_tokens


def count_messages(messages: list[dict], model_spec: str = "") -> int:
    """Count model input tokens; use tiktoken for OpenAI when available."""
    if str(model_spec).startswith("openai:"):
        try:
            import tiktoken

            model = model_spec.split(":", 1)[1]
            try:
                encoding = tiktoken.encoding_for_model(model)
            except KeyError:
                encoding = tiktoken.get_encoding("cl100k_base")
            total = 3
            for message in messages:
                total += 3
                total += len(encoding.encode(str(message.get("content") or "")))
                if message.get("tool_calls"):
                    total += len(encoding.encode(json.dumps(message["tool_calls"], default=str)))
            return total
        except (ImportError, ValueError):
            pass
    return approx_tokens(messages)
