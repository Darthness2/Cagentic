"""Phase 4a regressions — token spend and cost reporting.

`engine._usage` tracked tokens from the start but nothing surfaced it: no
`/cost`, no per-turn line, and — once Phase 1a added prompt caching — no way to
tell whether the caching was doing anything.

Two bugs fell out of building it. `_usage` accumulates for the whole *session*,
so the "tokens N in" footer was a running total that read like a per-turn cost
(on both the terminal and the web UI). And an unpriced model needs to report
*nothing* rather than $0.00 — a fabricated zero is worse than an honest gap.
"""

from __future__ import annotations

import unittest

from cagentic.fmt import fmt_cost, fmt_tokens, fmt_usage_line
from cagentic.providers import (
    CACHE_READ_RATE,
    CACHE_WRITE_RATE,
    cost_without_cache,
    estimate_cost,
    price_for,
)

# Anthropic's published rate for this model, in USD per million tokens.
_OPUS = "anthropic:claude-opus-5"
_OPUS_IN, _OPUS_OUT = 5.00, 25.00


class TestPriceTable(unittest.TestCase):
    def test_known_anthropic_models_resolve(self) -> None:
        self.assertEqual(price_for(_OPUS, {}), (_OPUS_IN, _OPUS_OUT))
        self.assertEqual(price_for("anthropic:claude-sonnet-5", {}), (3.00, 15.00))
        self.assertEqual(price_for("anthropic:claude-haiku-4-5", {}), (1.00, 5.00))

    def test_longest_prefix_wins(self) -> None:
        """`claude-opus-5` and `claude-opus-4` both prefix-match some names;
        picking the shorter one would misprice a whole family."""
        self.assertEqual(price_for("anthropic:claude-opus-4-8", {}), (5.00, 25.00))

    def test_local_models_are_explicitly_free(self) -> None:
        """Zero, not None — `/cost` should say $0.00 for a local model rather
        than 'unknown rate'."""
        self.assertEqual(price_for("ollama:qwen2.5:7b", {}), (0.0, 0.0))
        self.assertEqual(price_for("llama3:8b", {}), (0.0, 0.0))

    def test_an_unlisted_cloud_model_has_no_price(self) -> None:
        """A confidently wrong price is worse than no price."""
        self.assertIsNone(price_for("openai:some-future-model", {}))

    def test_blank_specs_do_not_crash(self) -> None:
        for spec in ("", "   ", None):
            self.assertIsNone(price_for(spec, {}))  # type: ignore[arg-type]


class TestPricingOverride(unittest.TestCase):
    def _cfg(self, value) -> dict:
        return {"models": {"openai:gpt-4o": {"pricing": value}}}

    def test_a_list_override_is_honoured(self) -> None:
        self.assertEqual(price_for("openai:gpt-4o", self._cfg([2.5, 10.0])), (2.5, 10.0))

    def test_a_comma_string_is_honoured(self) -> None:
        """`/set` writes a string; rejecting it would silently ignore a rate
        the user believes they configured."""
        self.assertEqual(price_for("openai:gpt-4o", self._cfg("2.50,10.00")), (2.5, 10.0))

    def test_a_space_separated_string_is_honoured(self) -> None:
        self.assertEqual(price_for("openai:gpt-4o", self._cfg("2.50 10.00")), (2.5, 10.0))

    def test_garbage_falls_back_instead_of_crashing(self) -> None:
        for bad in ("abc", "1,2,3", [], {"in": 1}, 5):
            self.assertIsNone(price_for("openai:gpt-4o", self._cfg(bad)), bad)

    def test_negative_rates_are_rejected(self) -> None:
        self.assertIsNone(price_for("openai:gpt-4o", self._cfg("-1,10")))

    def test_an_override_beats_the_table(self) -> None:
        cfg = {"models": {_OPUS: {"pricing": "1,2"}}}
        self.assertEqual(price_for(_OPUS, cfg), (1.0, 2.0))


class TestCostMath(unittest.TestCase):
    def test_plain_input_and_output(self) -> None:
        usage = {"input": 1_000_000, "output": 1_000_000}
        self.assertAlmostEqual(estimate_cost(usage, _OPUS, {}), _OPUS_IN + _OPUS_OUT)

    def test_cache_reads_bill_at_a_fraction_of_input(self) -> None:
        usage = {"input": 0, "output": 0, "cache_read": 1_000_000}
        self.assertAlmostEqual(estimate_cost(usage, _OPUS, {}), _OPUS_IN * CACHE_READ_RATE)

    def test_cache_writes_carry_a_premium(self) -> None:
        usage = {"input": 0, "output": 0, "cache_write": 1_000_000}
        self.assertAlmostEqual(estimate_cost(usage, _OPUS, {}), _OPUS_IN * CACHE_WRITE_RATE)

    def test_cached_tokens_are_not_double_counted_as_input(self) -> None:
        """Providers report `input` as the *uncached remainder*; adding the
        cached tokens back in would inflate every cached turn."""
        cached = {"input": 100, "output": 0, "cache_read": 1_000_000}
        plain = {"input": 100, "output": 0}
        delta = estimate_cost(cached, _OPUS, {}) - estimate_cost(plain, _OPUS, {})
        self.assertAlmostEqual(delta, _OPUS_IN * CACHE_READ_RATE)

    def test_caching_actually_saves_money(self) -> None:
        """The whole point of Phase 1a — if this ever inverts, caching is
        costing the user money."""
        usage = {"input": 2_000, "output": 800, "cache_read": 40_000, "cache_write": 12_000}
        with_cache = estimate_cost(usage, _OPUS, {})
        without = cost_without_cache(usage, _OPUS, {})
        self.assertLess(with_cache, without)

    def test_the_counterfactual_prices_every_token_at_full_rate(self) -> None:
        usage = {"input": 1_000, "output": 0, "cache_read": 2_000, "cache_write": 3_000}
        self.assertAlmostEqual(cost_without_cache(usage, _OPUS, {}), 6_000 * _OPUS_IN / 1e6)

    def test_a_local_model_costs_nothing(self) -> None:
        usage = {"input": 500_000, "output": 500_000}
        self.assertEqual(estimate_cost(usage, "ollama:qwen2.5:7b", {}), 0.0)

    def test_an_unpriced_model_returns_none_not_zero(self) -> None:
        """None means 'unknown'; 0.0 would render as a free turn."""
        usage = {"input": 500_000, "output": 500_000}
        self.assertIsNone(estimate_cost(usage, "openai:unknown-model", {}))
        self.assertIsNone(cost_without_cache(usage, "openai:unknown-model", {}))

    def test_missing_and_none_fields_are_treated_as_zero(self) -> None:
        self.assertEqual(estimate_cost({}, _OPUS, {}), 0.0)
        self.assertEqual(estimate_cost({"input": None, "output": None}, _OPUS, {}), 0.0)


class TestPerTurnDelta(unittest.TestCase):
    """`_usage` is a session running total, so a per-turn figure has to be a
    delta — otherwise the footer grows every turn while reading per-turn."""

    def _engine(self):
        import tempfile
        from pathlib import Path

        from cagentic.agent import Agent

        self._tmp = tempfile.TemporaryDirectory()
        self._cfg = tempfile.TemporaryDirectory()
        import os

        os.environ["XDG_CONFIG_HOME"] = self._cfg.name

        class _NullClient:
            pass

        return Agent(_NullClient(), "test-model", Path(self._tmp.name)).engine

    def tearDown(self) -> None:
        for name in ("_tmp", "_cfg"):
            d = getattr(self, name, None)
            if d is not None:
                try:
                    d.cleanup()
                except (OSError, PermissionError):
                    pass

    def test_the_delta_excludes_earlier_turns(self) -> None:
        eng = self._engine()
        eng._usage.update({"input": 1000, "output": 200})
        eng._usage_at_turn_start = dict(eng._usage)  # turn boundary
        eng._usage.update({"input": 1300, "output": 260})
        self.assertEqual(eng.turn_usage()["input"], 300)
        self.assertEqual(eng.turn_usage()["output"], 60)

    def test_the_first_turn_reports_its_own_spend(self) -> None:
        eng = self._engine()
        eng._usage.update({"input": 900, "output": 120})
        self.assertEqual(eng.turn_usage()["input"], 900)

    def test_a_turn_with_no_calls_is_all_zeroes(self) -> None:
        eng = self._engine()
        eng._usage.update({"input": 500})
        eng._usage_at_turn_start = dict(eng._usage)
        self.assertTrue(all(v == 0 for v in eng.turn_usage().values()))

    def test_cost_report_marks_an_unpriced_cloud_model(self) -> None:
        eng = self._engine()
        eng.state.update(active_model_spec="openai:some-future-model")
        eng._usage.update({"input": 1000, "output": 100})
        report = eng.cost_report()
        self.assertIsNone(report["spent"])
        self.assertIsNone(report["saved"])

    def test_a_bare_model_name_is_an_ollama_tag_and_therefore_free(self) -> None:
        """`providers.parse_model` treats anything without a known provider
        prefix as a local Ollama tag, so it prices at zero rather than unknown."""
        eng = self._engine()
        eng._usage.update({"input": 1000, "output": 100})
        self.assertEqual(eng.cost_report()["spent"], 0.0)

    def test_cost_report_prices_a_known_model(self) -> None:
        eng = self._engine()
        eng.state.update(active_model_spec=_OPUS)
        eng._usage.update({"input": 1_000_000, "output": 0, "cache_read": 1_000_000})
        report = eng.cost_report()
        self.assertAlmostEqual(report["spent"], _OPUS_IN + _OPUS_IN * CACHE_READ_RATE)
        self.assertGreater(report["saved"], 0)


class TestFormatting(unittest.TestCase):
    def test_token_magnitudes(self) -> None:
        self.assertEqual(fmt_tokens(340), "340")
        self.assertEqual(fmt_tokens(1200), "1.2k")
        self.assertEqual(fmt_tokens(2000), "2k")
        self.assertEqual(fmt_tokens(1_500_000), "1.5M")

    def test_sub_cent_costs_stay_visible(self) -> None:
        """Cached traffic makes these the common case; '$0.00' would hide the
        entire point of the caching work."""
        self.assertEqual(fmt_cost(0.0011), "$0.0011")
        self.assertEqual(fmt_cost(0.0), "$0.00")
        self.assertEqual(fmt_cost(1.5), "$1.50")

    def test_no_cost_renders_as_empty(self) -> None:
        self.assertEqual(fmt_cost(None), "")

    def test_the_usage_line_reads_as_intended(self) -> None:
        line = fmt_usage_line(
            {"input": 1200, "output": 340, "cache_read": 11_100, "ms": 4200},
            {"spent": 0.0113, "saved": 0.031},
        )
        self.assertEqual(line, "↑1.2k ↓340 · 11.1k cached · $0.01 (saved $0.03) · 4s")

    def test_cache_gets_its_own_segment(self) -> None:
        """Folded into the input count the cache hit would be invisible."""
        self.assertIn("cached", fmt_usage_line({"input": 1, "output": 1, "cache_read": 900}))

    def test_no_cache_means_no_cache_segment(self) -> None:
        self.assertNotIn("cached", fmt_usage_line({"input": 100, "output": 20}))

    def test_an_unpriced_model_shows_tokens_and_no_money(self) -> None:
        line = fmt_usage_line({"input": 100, "output": 20}, {"spent": None})
        self.assertIn("↑100", line)
        self.assertNotIn("$", line)

    def test_a_trivial_saving_is_not_advertised(self) -> None:
        """Rounding noise presented as a saving reads as spin."""
        self.assertNotIn("saved", fmt_usage_line({"input": 1}, {"spent": 0.02, "saved": 0.0001}))

    def test_sub_second_turns_omit_the_timing(self) -> None:
        """fmt_duration renders '0s' below a second, which is noise."""
        self.assertNotIn("0s", fmt_usage_line({"input": 100, "output": 5, "ms": 900}))


class TestSurfacesAreWired(unittest.TestCase):
    def test_the_done_event_carries_the_per_turn_delta_and_cost(self) -> None:
        import inspect

        from cagentic import engine as engine_mod

        source = inspect.getsource(engine_mod.QueryEngine.submit_message)
        self.assertIn('"turn_usage"', source)
        self.assertIn('"cost"', source)

    def test_the_terminal_prefers_the_delta_over_the_session_total(self) -> None:
        import inspect

        from cagentic import agent as agent_mod

        source = inspect.getsource(agent_mod.render_event)
        self.assertIn('d.get("turn_usage")', source)

    def test_the_web_ui_prefers_the_delta_too(self) -> None:
        """Both front ends had the same session-total bug; fixing one would
        leave the other quietly wrong."""
        from pathlib import Path

        js = (
            Path(__file__).resolve().parents[1] / "cagentic" / "gateway_assets" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn("d.turn_usage||d.usage", js)
        self.assertIn("d.cost.spent!=null", js)

    def test_cost_is_registered_in_the_command_catalog(self) -> None:
        from cagentic.prompt import COMMAND_GROUPS

        names = {name for _s, entries in COMMAND_GROUPS for name, _a, _h in entries}
        self.assertIn("/cost", names)


if __name__ == "__main__":
    unittest.main()
