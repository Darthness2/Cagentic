"""Anthropic prompt-caching regressions.

Before this, `_build_body` emitted no `cache_control` at all: every step of
every agent turn re-billed the system prompt, ~120 tool schemas and the whole
transcript at full input price. These tests pin the breakpoint placement, since
a marker that silently moves or disappears turns into a quiet 10x cost
regression that nothing else in the suite would catch.
"""

from __future__ import annotations

import unittest

from cagentic.anthropic_client import _MAX_CACHE_BREAKPOINTS, AnthropicClient


def _breakpoints(body: dict) -> int:
    """Count cache_control markers anywhere in a request body."""
    count = 0
    for tool in body.get("tools") or []:
        count += "cache_control" in tool
    system = body.get("system")
    if isinstance(system, list):
        count += sum("cache_control" in b for b in system)
    for m in body.get("messages") or []:
        content = m.get("content")
        if isinstance(content, list):
            count += sum(isinstance(b, dict) and "cache_control" in b for b in content)
    return count


_TOOLS = [
    {"function": {"name": "read_file", "description": "read", "parameters": {}}},
    {"function": {"name": "run_bash", "description": "run", "parameters": {}}},
]


class TestCacheBreakpoints(unittest.TestCase):
    def setUp(self) -> None:
        self.client = AnthropicClient(api_key="test-key")

    def _body(self, messages, tools=_TOOLS):
        return self.client._build_body("claude-sonnet-4-6", messages, tools, None, stream=False)

    def test_tools_and_system_are_cached(self) -> None:
        body = self._body(
            [
                {"role": "system", "content": "you are cagentic"},
                {"role": "user", "content": "hello"},
            ]
        )
        # Last tool schema carries the marker — it covers every tool before it.
        self.assertNotIn("cache_control", body["tools"][0])
        self.assertIn("cache_control", body["tools"][-1])
        # A str system prompt is promoted to blocks so it can carry one.
        self.assertIsInstance(body["system"], list)
        self.assertIn("cache_control", body["system"][-1])

    def test_never_exceeds_the_api_budget(self) -> None:
        messages = [{"role": "system", "content": "sys"}]
        for i in range(20):
            messages.append({"role": "user", "content": f"q{i}"})
            messages.append({"role": "assistant", "content": f"a{i}"})
        body = self._body(messages)
        self.assertLessEqual(_breakpoints(body), _MAX_CACHE_BREAKPOINTS)

    def test_tail_markers_roll_forward_with_the_conversation(self) -> None:
        """The point of the tail pair: request N+1 reads what N wrote.

        The marked position must track the end of the conversation, otherwise
        an agent loop appending tool results never gets an incremental hit.
        """
        base = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
        ]
        first = self._body(list(base))
        grown = self._body(base + [{"role": "user", "content": "second"}])

        self.assertIn("cache_control", first["messages"][-1]["content"][-1])
        self.assertIn("cache_control", grown["messages"][-1]["content"][-1])
        # And the newly-appended message is the one now carrying it.
        self.assertEqual(grown["messages"][-1]["content"][-1]["text"], "second")

    def test_prefix_is_byte_stable_across_turns(self) -> None:
        """A cache read only happens if the prefix is *identical*.

        Anything volatile in the earlier messages (a timestamp, a re-ordered
        dict) breaks every downstream hit, so compare the serialized prefix.
        """
        import json

        base = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
        ]
        first = self._body(list(base))
        grown = self._body(base + [{"role": "user", "content": "second"}])

        # Everything before the newly-added message must be unchanged, ignoring
        # the rolling markers themselves.
        def strip(msgs):
            out = []
            for m in msgs:
                blocks = [
                    {k: v for k, v in b.items() if k != "cache_control"}
                    for b in m["content"]
                    if isinstance(b, dict)
                ]
                out.append({"role": m["role"], "content": blocks})
            return json.dumps(out, sort_keys=True)

        self.assertEqual(
            strip(first["messages"]), strip(grown["messages"][: len(first["messages"])])
        )

    def test_all_content_is_normalised_to_blocks(self) -> None:
        """Uniform shape is what makes the prefix byte-stable — if only the
        marked messages became block lists, a message ageing out of the rolling
        window would change the prefix and void every cache read after it."""
        body = self._body(
            [{"role": "system", "content": "sys"}]
            + [{"role": "user", "content": f"q{i}"} for i in range(6)]
        )
        for m in body["messages"]:
            self.assertIsInstance(m["content"], list, m)

    def test_empty_message_yields_its_slot_to_an_earlier_one(self) -> None:
        """An empty tail message can't carry a marker; the budget must not be
        silently burned on it."""
        body = self._body(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "real content"},
                {"role": "assistant", "content": ""},
            ]
        )
        marked = [m for m in body["messages"] if any("cache_control" in b for b in m["content"])]
        self.assertTrue(marked, "no message carried a breakpoint")
        self.assertEqual(marked[-1]["content"][-1]["text"], "real content")

    def test_caching_can_be_disabled(self) -> None:
        client = AnthropicClient(api_key="test-key", prompt_cache=False)
        body = client._build_body(
            "claude-sonnet-4-6",
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            _TOOLS,
            None,
            stream=False,
        )
        self.assertEqual(_breakpoints(body), 0)
        # And the system prompt stays a plain string when untouched.
        self.assertIsInstance(body["system"], str)


class TestCacheUsageReporting(unittest.TestCase):
    def test_parse_response_surfaces_cache_tokens(self) -> None:
        msg = AnthropicClient._parse_response(
            {
                "content": [{"type": "text", "text": "hi"}],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 4000,
                    "cache_creation_input_tokens": 0,
                },
            }
        )
        self.assertEqual(msg["usage"]["cache_read"], 4000)
        self.assertEqual(msg["usage"]["cache_write"], 0)
        self.assertEqual(msg["usage"]["input"], 10)


if __name__ == "__main__":
    unittest.main()
