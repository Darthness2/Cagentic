"""Phase 4d regressions — sampling parameters and reasoning display.

Two live bugs, both found while investigating "should users see the model's
thinking":

1. Cagentic sets `temperature` on every request (QueryEngine defaults it to
   0.4) and `_build_body` forwarded it unconditionally. Anthropic REMOVED the
   sampling parameters on Opus 4.7/4.8/5, Sonnet 5, Fable 5 and Mythos 5 —
   sending `temperature` there is a 400, so those models could not be used at
   all. Every turn failed before it started.
2. The engine has emitted a `thinking` event since day one and the terminal has
   rendered it as dim italic — but the gateway never mapped it, and `app.js`
   never stripped `<think>` tags, so on the web a local reasoning model's
   markup was escaped and rendered into the answer body.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from cagentic.anthropic_client import AnthropicClient, _accepts_sampling_params

_APP_JS = Path(__file__).resolve().parents[1] / "cagentic" / "gateway_assets" / "app.js"


class TestSamplingParamGate(unittest.TestCase):
    """Anthropic removed temperature/top_p/top_k on the newer families."""

    def test_models_that_reject_sampling_params(self) -> None:
        for name in (
            "claude-opus-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-sonnet-5",
            "claude-fable-5",
            "claude-mythos-5",
        ):
            self.assertFalse(_accepts_sampling_params(name), name)

    def test_models_that_still_accept_them(self) -> None:
        for name in (
            "claude-opus-4-6",
            "claude-opus-4-5",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
            "claude-3-5-sonnet-20241022",
        ):
            self.assertTrue(_accepts_sampling_params(name), name)

    def test_a_provider_prefix_is_tolerated(self) -> None:
        self.assertFalse(_accepts_sampling_params("anthropic:claude-opus-5"))
        self.assertTrue(_accepts_sampling_params("anthropic:claude-opus-4-6"))

    def test_an_unknown_model_is_assumed_to_reject(self) -> None:
        """The two mistakes aren't symmetric: omitting the parameter costs a
        default sampling setting, sending it to a model that rejects it costs
        the whole request. So an unrecognised (likely newer) model gets none."""
        self.assertFalse(_accepts_sampling_params("claude-something-new"))
        self.assertFalse(_accepts_sampling_params(""))


class TestRequestBody(unittest.TestCase):
    def _client(self) -> AnthropicClient:
        return AnthropicClient(api_key="test-key")

    def _body(self, model: str) -> dict:
        return self._client()._build_body(
            model,
            [{"role": "user", "content": "hi"}],
            None,
            {"temperature": 0.4},
            stream=False,
        )

    def test_temperature_is_withheld_from_current_models(self) -> None:
        """The whole bug: this is what 400s on every single turn."""
        for name in ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-8"):
            self.assertNotIn("temperature", self._body(name), name)

    def test_temperature_still_reaches_older_models(self) -> None:
        body = self._body("claude-opus-4-6")
        self.assertEqual(body["temperature"], 0.4)

    def test_the_rest_of_the_body_is_unaffected(self) -> None:
        body = self._body("claude-opus-5")
        self.assertEqual(body["model"], "claude-opus-5")
        self.assertTrue(body["messages"])
        self.assertIn("max_tokens", body)

    def test_no_temperature_option_is_still_fine(self) -> None:
        body = self._client()._build_body(
            "claude-opus-4-6", [{"role": "user", "content": "hi"}], None, {}, stream=False
        )
        self.assertNotIn("temperature", body)


class TestWebUIReasoning(unittest.TestCase):
    """The terminal has rendered reasoning since day one; the browser showed
    raw markup. Checked against the shipped asset because there is no JS test
    runner in this repo — the assertions target behaviour, not formatting."""

    def setUp(self) -> None:
        self.js = _APP_JS.read_text(encoding="utf-8")

    def test_the_thinking_event_is_handled(self) -> None:
        self.assertIn("k==='thinking'", self.js)
        self.assertIn("addThinkingBlock", self.js)

    def test_think_tags_are_stripped_from_every_answer_render(self) -> None:
        """One missed call site puts raw <think> markup back in the answer."""
        self.assertNotIn("md(stripHud(", self.js)
        self.assertIn("stripThink", self.js)

    def test_streamed_reasoning_updates_in_place(self) -> None:
        """Appending per delta would produce one block per token."""
        self.assertIn("syncThinking", self.js)
        self.assertIn("live.thinkBox", self.js)

    def test_the_live_block_is_reset_between_turns(self) -> None:
        """Otherwise turn 2's reasoning is written into turn 1's block."""
        self.assertNotIn(
            "live={body:null,raw:'',toolRow:null,thinking:null,turnStart:null", self.js
        )
        self.assertGreaterEqual(self.js.count("thinkBox:null"), 3)

    def test_reasoning_is_escaped_not_injected(self) -> None:
        """Model output is untrusted; the renderer is careful elsewhere and
        this path must match."""
        self.assertIn("esc(text)+'</div>'", self.js)

    def test_the_reasoning_block_has_styling(self) -> None:
        css = (_APP_JS.parent / "app.css").read_text(encoding="utf-8")
        self.assertIn(".think-box", css)
        self.assertIn(".think-body", css)


class TestTerminalReasoningUnchanged(unittest.TestCase):
    """The terminal path already worked — this phase must not regress it."""

    def test_the_stream_renderer_still_dims_think_blocks(self) -> None:
        from cagentic.ui import StreamMarkdown

        modes = {open_tag: mode for open_tag, _close, mode in StreamMarkdown.SUPPRESS_PAIRS}
        self.assertEqual(modes.get("<think>"), "dim")
        self.assertEqual(modes.get("<thinking>"), "dim")

    def test_plan_blocks_are_still_elided_rather_than_dimmed(self) -> None:
        """Plans render as their own panel; showing both would duplicate."""
        from cagentic.ui import StreamMarkdown

        modes = {open_tag: mode for open_tag, _close, mode in StreamMarkdown.SUPPRESS_PAIRS}
        self.assertEqual(modes.get("<plan>"), "elide")

    def test_the_thinking_event_kind_still_exists(self) -> None:
        import inspect

        from cagentic import agent as agent_mod

        self.assertIn('k == "thinking"', inspect.getsource(agent_mod.render_event))


if __name__ == "__main__":
    unittest.main()
