"""OpenAI-compatible API client.

Works with OpenAI (api.openai.com), OpenRouter, Azure OpenAI, local OpenAI-
compatible servers (llama.cpp, vLLM, Ollama's /v1 endpoint), and any other
service that speaks the /chat/completions protocol.

Exposes the same interface as OllamaClient so the engine can use either
without modification.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Iterator

import requests

from .ollama_client import OllamaError


class OpenAIError(OllamaError):
    """Raised on OpenAI / compatible-API errors."""


_BUILTIN_MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-turbo",
    "gpt-3.5-turbo",
    "o1",
    "o1-mini",
    "o3-mini",
]


def _make_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:12]}"


class OpenAIClient:
    """HTTP wrapper around the OpenAI chat-completions API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 600.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Model listing
    # ------------------------------------------------------------------

    def list_models(self) -> list[str]:
        """Return available models, prefixed with 'openai:' for disambiguation."""
        try:
            r = self._session.get(
                f"{self.base_url}/models",
                timeout=10.0,
            )
            if r.ok:
                data = r.json().get("data") or []
                ids = sorted(m["id"] for m in data if isinstance(m, dict) and m.get("id"))
                if ids:
                    return [f"openai:{m}" for m in ids]
        except Exception:
            pass
        return [f"openai:{m}" for m in _BUILTIN_MODELS]

    # ------------------------------------------------------------------
    # Message normalisation
    # ------------------------------------------------------------------

    def _normalize(self, messages: list[dict]) -> list[dict]:
        """Rewrite Cagentic's internal message list for the OpenAI API.

        - Extract the system message and re-insert it at position 0.
        - Assign tool-call IDs to assistant tool_calls that lack them.
        - Match tool-result messages to preceding call IDs by tool name.
        - Merge back-to-back messages of the same role (OpenAI rejects them).
        """
        # ---- First pass: build the clean list --------------------------
        pending: dict[str, list[str]] = {}  # tool_name -> [call_id, ...]
        out: list[dict] = []

        for m in messages:
            role = m.get("role", "")

            if role == "system":
                out.append({"role": "system", "content": m.get("content") or ""})
                continue

            if role == "assistant":
                tcs = m.get("tool_calls") or []
                new_tcs = []
                for tc in tcs:
                    fn = tc.get("function") or {}
                    cid = tc.get("id") or _make_call_id()
                    name = fn.get("name", "")
                    args = fn.get("arguments", {})
                    args_str = (
                        json.dumps(args) if isinstance(args, dict) else str(args or "")
                    )
                    new_tcs.append(
                        {
                            "id": cid,
                            "type": "function",
                            "function": {"name": name, "arguments": args_str},
                        }
                    )
                    pending.setdefault(name, []).append(cid)
                out.append(
                    {
                        "role": "assistant",
                        "content": m.get("content") or "",
                        **({"tool_calls": new_tcs} if new_tcs else {}),
                    }
                )
                continue

            if role == "tool":
                name = m.get("name", "")
                queue = pending.get(name, [])
                cid = queue.pop(0) if queue else _make_call_id()
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": cid,
                        "content": str(m.get("content") or ""),
                    }
                )
                continue

            # user / other roles
            out.append({"role": role, "content": m.get("content") or ""})

        # ---- Second pass: merge consecutive same-role messages ---------
        merged: list[dict] = []
        for m in out:
            if (
                merged
                and merged[-1]["role"] == m["role"]
                and m["role"] not in ("assistant",)
                and "tool_calls" not in merged[-1]
                and "tool_calls" not in m
            ):
                merged[-1]["content"] = (
                    str(merged[-1].get("content") or "")
                    + "\n"
                    + str(m.get("content") or "")
                ).strip()
            else:
                merged.append(m)

        return merged

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
        norm = self._normalize(messages)
        body: dict[str, Any] = {"model": model, "messages": norm}
        if tools:
            body["tools"] = tools
        if options and "temperature" in options:
            body["temperature"] = options["temperature"]

        try:
            r = self._session.post(
                f"{self.base_url}/chat/completions",
                json=body,
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            raise OpenAIError(f"OpenAI API error: {e}") from e

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tcs = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args_raw = fn.get("arguments", "{}")
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except json.JSONDecodeError:
                args = {}
            tcs.append(
                {
                    "id": tc.get("id"),
                    "function": {"name": fn.get("name", ""), "arguments": args},
                }
            )
        return {
            "role": msg.get("role", "assistant"),
            "content": msg.get("content") or "",
            "tool_calls": tcs,
        }

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
        norm = self._normalize(messages)
        body: dict[str, Any] = {
            "model": model,
            "messages": norm,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
        if options and "temperature" in options:
            body["temperature"] = options["temperature"]

        try:
            r = self._session.post(
                f"{self.base_url}/chat/completions",
                json=body,
                stream=True,
                timeout=self.timeout,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            raise OpenAIError(f"OpenAI streaming error: {e}") from e

        full = ""
        tc_acc: dict[int, dict] = {}  # index -> partial call

        for raw_line in r.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode() if isinstance(raw_line, bytes) else raw_line
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue

            delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
            text = delta.get("content") or ""
            if text:
                full += text
                yield ("delta", text)

            for tc_delta in delta.get("tool_calls") or []:
                idx = tc_delta.get("index", 0)
                if idx not in tc_acc:
                    tc_acc[idx] = {"id": "", "function": {"name": "", "arguments": ""}}
                if tc_delta.get("id"):
                    tc_acc[idx]["id"] = tc_delta["id"]
                fn_d = tc_delta.get("function") or {}
                if fn_d.get("name"):
                    tc_acc[idx]["function"]["name"] += fn_d["name"]
                if fn_d.get("arguments"):
                    tc_acc[idx]["function"]["arguments"] += fn_d["arguments"]

        tcs = []
        for idx in sorted(tc_acc):
            tc = tc_acc[idx]
            args_str = tc["function"]["arguments"]
            try:
                args = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError:
                args = {}
            tcs.append(
                {
                    "id": tc.get("id") or _make_call_id(),
                    "function": {"name": tc["function"]["name"], "arguments": args},
                }
            )

        yield (
            "done",
            {
                "message": {
                    "role": "assistant",
                    "content": full,
                    "tool_calls": tcs,
                },
                "eval_count": 0,
                "prompt_eval_count": 0,
                "total_duration_ns": 0,
            },
        )

    def unload(self, model: str) -> None:
        """No-op — cloud providers manage their own resources."""
