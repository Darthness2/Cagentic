"""Anthropic Claude API client.

Exposes the same interface as OllamaClient so the engine can drive any
Claude model (claude-opus-4-8, claude-sonnet-4-6, claude-haiku-4-5, …)
without modification.

Message-format translation (Ollama ↔ Anthropic):
- system   : top-level ``system`` field, not inside the message list
- tool call: assistant content block  {"type":"tool_use","id":…,"name":…,"input":{…}}
- tool result: user content block     {"type":"tool_result","tool_use_id":…,"content":…}
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Iterator

import requests

from .ollama_client import OllamaError

_log = logging.getLogger(__name__)

# Anthropic requires an explicit max_tokens. 8192 was hard-coded and far below
# what current Claude models can emit, silently truncating long answers. Use a
# generous default and let callers override via options/config.
_DEFAULT_MAX_TOKENS = 32000


class AnthropicError(OllamaError):
    """Raised on Anthropic API errors."""


_BUILTIN_MODELS = [
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"


def _make_tool_id() -> str:
    return f"toolu_{uuid.uuid4().hex[:12]}"


def _merge_consecutive(messages: list[dict]) -> list[dict]:
    """Merge back-to-back messages of the same role.

    Anthropic rejects requests where two ``user`` messages appear in a row
    (or two ``assistant`` messages).  We collapse consecutive same-role
    messages by concatenating their content blocks.
    """
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        content = m["content"]
        if out and out[-1]["role"] == role:
            prev = out[-1]["content"]
            # Normalise both sides to lists of blocks
            if isinstance(prev, str):
                prev = [{"type": "text", "text": prev}] if prev else []
            if isinstance(content, str):
                content = [{"type": "text", "text": content}] if content else []
            out[-1]["content"] = prev + content
        else:
            out.append({"role": role, "content": content})
    return out


class AnthropicClient:
    """HTTP wrapper around the Anthropic Messages API."""

    def __init__(self, api_key: str, timeout: float = 600.0) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "x-api-key": api_key,
                "anthropic-version": _API_VERSION,
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Model listing
    # ------------------------------------------------------------------

    def list_models(self) -> list[str]:
        return [f"anthropic:{m}" for m in _BUILTIN_MODELS]

    # ------------------------------------------------------------------
    # Message / tool format conversion
    # ------------------------------------------------------------------

    def _convert_messages(
        self, messages: list[dict]
    ) -> tuple[str, list[dict]]:
        """Convert Cagentic's OpenAI-style messages to Anthropic format.

        Returns (system_text, anthropic_messages).
        """
        system = ""
        # pending_ids maps tool name → [call_id, ...] FIFO so we can match
        # tool-result messages back to the corresponding tool_use id.
        pending: dict[str, list[str]] = {}
        raw: list[dict] = []

        for m in messages:
            role = m.get("role", "")
            content = m.get("content") or ""

            if role == "system":
                system = content
                continue

            if role == "assistant":
                tcs = m.get("tool_calls") or []
                if tcs:
                    blocks: list[dict] = []
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for tc in tcs:
                        fn = tc.get("function") or {}
                        cid = tc.get("id") or _make_tool_id()
                        name = fn.get("name", "")
                        args = fn.get("arguments", {})
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {}
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": cid,
                                "name": name,
                                "input": args,
                            }
                        )
                        pending.setdefault(name, []).append(cid)
                    raw.append({"role": "assistant", "content": blocks})
                else:
                    raw.append({"role": "assistant", "content": content})
                continue

            if role == "tool":
                name = m.get("name", "")
                queue = pending.get(name, [])
                if not queue:
                    # Orphan tool result — its originating tool_use was dropped
                    # during compaction. Fabricating an id here references a
                    # tool_use that doesn't exist, which Anthropic rejects with
                    # a 400. Skip the orphan instead.
                    _log.warning(
                        "dropping orphan tool result for %r with no matching tool_use", name
                    )
                    continue
                cid = queue.pop(0)
                raw.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": cid,
                                "content": str(content),
                            }
                        ],
                    }
                )
                continue

            # user / other
            raw.append({"role": "user", "content": content})

        return system, _merge_consecutive(raw)

    @staticmethod
    def _convert_tools(tools: list[dict]) -> list[dict]:
        """Convert OpenAI tool schemas to Anthropic tool schemas."""
        result = []
        for t in tools:
            fn = t.get("function") or {}
            params = fn.get("parameters") or {"type": "object", "properties": {}}
            result.append(
                {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": params,
                }
            )
        return result

    @staticmethod
    def _parse_response(data: dict) -> dict:
        """Build an Ollama-compatible message dict from an Anthropic response."""
        blocks = data.get("content") or []
        text = ""
        tcs = []
        for block in blocks:
            btype = block.get("type")
            if btype == "text":
                text += block.get("text", "")
            elif btype == "tool_use":
                tcs.append(
                    {
                        "id": block.get("id"),
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": block.get("input", {}),
                        },
                    }
                )
        msg = {"role": "assistant", "content": text, "tool_calls": tcs}
        if data.get("stop_reason") == "max_tokens":
            # Response hit the output cap — surface so the engine warns and the
            # user knows the answer is cut short.
            msg["truncated"] = True
        return msg

    @staticmethod
    def _resolve_max_tokens(options: dict | None) -> int:
        if options:
            for key in ("max_tokens", "max_completion_tokens", "num_predict"):
                val = options.get(key)
                if isinstance(val, int) and val > 0:
                    return val
        return _DEFAULT_MAX_TOKENS

    def _build_body(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        options: dict | None,
        stream: bool,
    ) -> dict:
        system, norm = self._convert_messages(messages)
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": self._resolve_max_tokens(options),
            "messages": norm,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = self._convert_tools(tools)
        if options and options.get("temperature") is not None:
            body["temperature"] = options["temperature"]
        if stream:
            body["stream"] = True
        return body

    # ------------------------------------------------------------------
    # Non-streaming chat
    # ------------------------------------------------------------------

    def chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
    ) -> dict:
        body = self._build_body(model, messages, tools, options, stream=False)
        try:
            r = self._session.post(_API_URL, json=body, timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            raise AnthropicError(f"Anthropic API error: {e}") from e
        return self._parse_response(r.json())

    # ------------------------------------------------------------------
    # Streaming chat
    # ------------------------------------------------------------------

    def chat_stream_assembled(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
    ) -> Iterator[tuple[str, Any]]:
        body = self._build_body(model, messages, tools, options, stream=True)

        full = ""
        tool_blocks: dict[str, dict] = {}  # tool_use_id -> {name, input_str}
        current_id: str | None = None
        in_tokens = 0
        out_tokens = 0
        stop_reason: str | None = None
        saw_message_stop = False

        try:
            with self._session.post(
                _API_URL, json=body, stream=True, timeout=self.timeout
            ) as r:
                try:
                    r.raise_for_status()
                except requests.RequestException as e:
                    raise AnthropicError(f"Anthropic streaming error: {e}") from e

                for raw_line in r.iter_lines():
                    if not raw_line:
                        continue
                    line = raw_line.decode() if isinstance(raw_line, bytes) else raw_line

                    if line.startswith("event: "):
                        continue  # event-type lines — handled by the data line below

                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type")

                    if etype == "content_block_start":
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            current_id = block["id"]
                            tool_blocks[current_id] = {
                                "name": block.get("name", ""),
                                "input_str": "",
                            }

                    elif etype == "content_block_delta":
                        delta = event.get("delta") or {}
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            text = delta.get("text", "")
                            full += text
                            yield ("delta", text)
                        elif dtype == "input_json_delta" and current_id:
                            tool_blocks[current_id]["input_str"] += delta.get(
                                "partial_json", ""
                            )

                    elif etype == "content_block_stop":
                        current_id = None

                    elif etype == "message_delta":
                        usage = event.get("usage") or {}
                        out_tokens = usage.get("output_tokens", 0)
                        sr = (event.get("delta") or {}).get("stop_reason")
                        if sr:
                            stop_reason = sr

                    elif etype == "message_start":
                        usage = (event.get("message") or {}).get("usage") or {}
                        in_tokens = usage.get("input_tokens", 0)

                    elif etype == "message_stop":
                        saw_message_stop = True
        except requests.RequestException as e:
            raise AnthropicError(f"Anthropic streaming error: {e}") from e

        tcs = []
        for cid, tb in tool_blocks.items():
            try:
                args = json.loads(tb["input_str"]) if tb["input_str"] else {}
            except json.JSONDecodeError:
                args = {}
            tcs.append(
                {
                    "id": cid,
                    "function": {"name": tb["name"], "arguments": args},
                }
            )

        # Mark truncated when the model hit its output cap OR the stream ended
        # without a proper message_stop sentinel (connection died mid-response).
        truncated = stop_reason == "max_tokens" or not saw_message_stop

        done: dict[str, Any] = {
            "message": {
                "role": "assistant",
                "content": full,
                "tool_calls": tcs,
            },
            "eval_count": out_tokens,
            "prompt_eval_count": in_tokens,
            "total_duration_ns": 0,
        }
        if truncated:
            done["truncated"] = True
        yield ("done", done)

    def unload(self, model: str) -> None:
        """No-op — cloud providers manage their own resources."""
