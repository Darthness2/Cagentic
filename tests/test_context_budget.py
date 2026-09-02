"""Model-aware context window regressions.

The window used to come from `ollama.num_ctx` (default 8192) for every
provider, and the compaction threshold was a flat 32000. That combination was
wrong in both directions: a 200k Claude model compacted at 16% full and threw
away context it never needed to lose, while an 8k local model never reached
32000 before overflowing its own window.
"""

from __future__ import annotations

import unittest

from cagentic import command_utils
from cagentic.config import _DEFAULTS, set_model_capability
from cagentic.engine import COMPACT_FRACTION, COMPACT_MIN_TOKENS
from cagentic.providers import context_window_for
from cagentic.tools import TOOL_GROUPS

_ALL_GROUPS = set(TOOL_GROUPS)


def _cfg(**over):
    import copy

    cfg = copy.deepcopy(_DEFAULTS)
    cfg.update(over)
    return cfg


class TestContextWindowFor(unittest.TestCase):
    def test_cloud_models_do_not_inherit_the_ollama_setting(self) -> None:
        cfg = _cfg()
        cfg["ollama"]["num_ctx"] = 8192
        self.assertEqual(context_window_for("anthropic:claude-sonnet-4-6", cfg), 200_000)
        self.assertEqual(context_window_for("openai:gpt-4o", cfg), 128_000)

    def test_ollama_tags_use_num_ctx(self) -> None:
        cfg = _cfg()
        cfg["ollama"]["num_ctx"] = 16384
        # A bare tag with a colon is an Ollama model, not a provider prefix.
        self.assertEqual(context_window_for("llama3:8b", cfg), 16384)
        self.assertEqual(context_window_for("ollama:qwen2.5-coder:14b", cfg), 16384)

    def test_longest_prefix_wins(self) -> None:
        cfg = _cfg()
        # gpt-4 alone is 8k, but gpt-4o must not match it.
        self.assertEqual(context_window_for("openai:gpt-4", cfg), 8_192)
        self.assertEqual(context_window_for("openai:gpt-4o", cfg), 128_000)
        self.assertEqual(context_window_for("openai:gpt-4-turbo", cfg), 128_000)

    def test_unknown_cloud_model_falls_back_per_provider(self) -> None:
        cfg = _cfg()
        self.assertEqual(context_window_for("anthropic:some-future-model", cfg), 200_000)
        self.assertEqual(context_window_for("openai:some-future-model", cfg), 128_000)

    def test_explicit_override_wins(self) -> None:
        cfg = _cfg()
        set_model_capability(cfg, "anthropic:claude-sonnet-4-6", "context_window", 1_000_000)
        self.assertEqual(context_window_for("anthropic:claude-sonnet-4-6", cfg), 1_000_000)

    def test_malformed_override_is_ignored_not_fatal(self) -> None:
        cfg = _cfg()
        set_model_capability(cfg, "anthropic:claude-sonnet-4-6", "context_window", "lots")
        self.assertEqual(context_window_for("anthropic:claude-sonnet-4-6", cfg), 200_000)

    def test_empty_spec_returns_the_default(self) -> None:
        self.assertEqual(context_window_for("", _cfg(), default=4096), 4096)


class TestCommandUtilsBridge(unittest.TestCase):
    def test_uses_the_configured_model_when_none_is_passed(self) -> None:
        cfg = _cfg(model="anthropic:claude-opus-4-8")
        cfg["ollama"]["num_ctx"] = 8192
        self.assertEqual(command_utils.context_window(cfg), 200_000)

    def test_explicit_model_spec_overrides_the_configured_one(self) -> None:
        cfg = _cfg(model="anthropic:claude-opus-4-8")
        cfg["ollama"]["num_ctx"] = 8192
        self.assertEqual(command_utils.context_window(cfg, model_spec="llama3:8b"), 8192)

    def test_no_model_configured_still_reads_num_ctx(self) -> None:
        cfg = _cfg(model=None)
        cfg["ollama"]["num_ctx"] = 32768
        self.assertEqual(command_utils.context_window(cfg), 32768)

    def test_malformed_ollama_section_does_not_raise(self) -> None:
        cfg = _cfg(model=None)
        cfg["ollama"] = "not a dict"
        self.assertEqual(command_utils.context_window(cfg, default=4096), 4096)


class TestCompactThreshold(unittest.TestCase):
    """The threshold has to track the model, not a module constant."""

    def _engine(self, model_spec: str, cfg: dict):
        from pathlib import Path

        from cagentic.engine import QueryEngine
        from cagentic.state import AppState

        class _NullClient:
            def chat(self, *a, **k):  # pragma: no cover - never called
                raise AssertionError("no network in tests")

        state = AppState(workspace=Path("."), home=Path.home())
        state.active_model_spec = model_spec
        return QueryEngine(client=_NullClient(), state=state, model=model_spec, config=cfg)

    def test_large_window_gets_a_large_budget(self) -> None:
        engine = self._engine("anthropic:claude-sonnet-4-6", _cfg())
        self.assertEqual(engine.context_window(), 200_000)
        self.assertEqual(engine.compact_threshold(), int(200_000 * COMPACT_FRACTION))

    def test_small_local_window_compacts_below_its_own_limit(self) -> None:
        cfg = _cfg()
        cfg["ollama"]["num_ctx"] = 8192
        engine = self._engine("llama3:8b", cfg)
        threshold = engine.compact_threshold()
        self.assertLess(threshold, 8192, "would overflow the model's own window")
        self.assertGreaterEqual(threshold, COMPACT_MIN_TOKENS)

    def test_tiny_window_does_not_produce_a_thrashing_threshold(self) -> None:
        cfg = _cfg()
        cfg["ollama"]["num_ctx"] = 512
        engine = self._engine("llama3:8b", cfg)
        self.assertEqual(engine.compact_threshold(), COMPACT_MIN_TOKENS)


class ShippedDefaultsLeaveRoomToWork(unittest.TestCase):
    """The floor every request carries has to fit the window we ship.

    Tool definitions and the system prompt are sent on every call and no
    compaction strategy touches either, so if the two together approach the
    context window there is nothing the agent can do at runtime — turns simply
    start failing. The shipped defaults were once *past 100%* of the configured
    window: 99 tool schemas (~7.6k tokens) plus a ~2.7k-token system prompt
    against `num_ctx` 8192. A fresh install could not hold a conversation.

    These tests pin the relationship, not the numbers, so retuning either side
    stays free as long as the floor keeps its room.
    """

    def _floor(self, groups=None) -> int:
        from pathlib import Path

        from cagentic.engine import fetch_system_prompt_parts
        from cagentic.services.compact import approx_schema_tokens, approx_tokens
        from cagentic.state import AppState
        from cagentic.tools import all_tool_schemas

        state = AppState(workspace=Path.cwd(), home=Path.home())
        system = fetch_system_prompt_parts(state)
        return approx_schema_tokens(all_tool_schemas(groups)) + approx_tokens(
            [{"role": "system", "content": system}]
        )

    def test_the_default_floor_fits_the_default_window(self) -> None:
        from cagentic.tools import DEFAULT_GROUPS

        window = _DEFAULTS["ollama"]["num_ctx"]
        floor = self._floor(DEFAULT_GROUPS)
        self.assertLess(
            floor / window,
            0.35,
            f"fixed floor is {floor:,} tokens of a {window:,}-token window — "
            "a fresh install has no room left to work in",
        )

    def test_enabling_the_assistant_groups_still_leaves_room(self) -> None:
        from cagentic.tools import ASSISTANT_GROUPS, DEFAULT_GROUPS

        window = _DEFAULTS["ollama"]["num_ctx"]
        floor = self._floor(set(DEFAULT_GROUPS) | set(ASSISTANT_GROUPS))
        self.assertLess(
            floor / window,
            0.55,
            "`/groups enable life browser notes reminders` must stay usable",
        )

    def test_doctor_reports_a_floor_that_does_not_fit(self) -> None:
        from cagentic import diagnostics

        report = diagnostics.run(
            {
                "model": "ollama:llama3.2",
                "ollama": {"num_ctx": 2048},
                "tool_groups": sorted(_ALL_GROUPS),
            }
        )
        check = next(c for c in report["checks"] if c["name"] == "context_floor")
        self.assertFalse(check["ok"], "an unusable floor must be a failing check")

    def test_doctor_passes_on_the_shipped_defaults(self) -> None:
        from cagentic import diagnostics

        report = diagnostics.run({"model": "ollama:llama3.2", "ollama": dict(_DEFAULTS["ollama"])})
        check = next(c for c in report["checks"] if c["name"] == "context_floor")
        self.assertTrue(check["ok"], check["detail"])


if __name__ == "__main__":
    unittest.main()
