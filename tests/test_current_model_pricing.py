"""Published model rates verified against provider documentation on 2026-09-29.

Sources:
https://platform.claude.com/docs/en/about-claude/pricing
https://developers.openai.com/api/docs/pricing
"""

import unittest

from claude_usage.pricing import (
    calc_cost, calc_cost_parts, get_pricing, is_estimated, is_long_context,
)


class TestCurrentModelPricing(unittest.TestCase):
    def test_mixed_token_requests_use_each_models_published_rates(self):
        # One request: 100k fresh input, 10k output, 50k cache hits, and
        # 20k cache writes split equally between the two TTL buckets.
        for model, expected in (
            ("claude-opus-5-5", 0.74),
            ("claude-sonnet-5-5", 0.375),
            ("claude-fable-5-1", 1.8375),
            ("claude-mythos-5-1", 1.8375),
            ("gpt-6-astra", 1.8),
            ("gpt-6-sol", 0.36),
            ("gpt-6-luna", 0.018),
        ):
            for suffix in ("", "-20260922", "-2026-09-22"):
                with self.subTest(model=model, suffix=suffix):
                    name = model + suffix
                    self.assertAlmostEqual(
                        calc_cost(name, 100_000, 10_000, 50_000, 20_000,
                                  10_000), expected, places=9)
                    self.assertFalse(is_estimated(name))

    def test_opus_5_5_preserves_both_cache_write_ttls(self):
        parts = calc_cost_parts("claude-opus-5-5", 0, 0, 100_000,
                               200_000, 50_000)
        self.assertAlmostEqual(parts["cache_read"], 0.02)
        self.assertAlmostEqual(parts["cache_creation"], 1.15)
        self.assertFalse(is_long_context("claude-opus-5-5", 900_000))

    def test_gpt_6_long_context_includes_cached_input_and_writes(self):
        for model, expected in (
            ("gpt-6-astra", 3.65),
            ("gpt-6-sol", 0.73),
            ("gpt-6-luna", 0.0365),
        ):
            with self.subTest(model=model):
                self.assertAlmostEqual(
                    calc_cost(model, 100_000, 10_000, 200_000, 20_000,
                              10_000), expected, places=9)
                self.assertFalse(is_long_context(model, 272_000))
                self.assertTrue(is_long_context(model, 272_001))
                self.assertFalse(is_long_context(model + "-custom", 272_001))

    def test_new_rates_do_not_reprice_older_model_ids(self):
        for model, field, expected in (
            ("claude-opus-5", "input", 5.0),
            ("claude-opus-5", "cache_read", 0.5),
            ("claude-fable-5", "cache_read", 1.0),
            ("claude-mythos-5", "cache_read", 1.0),
            ("gpt-5.6-sol", "input", 4.0),
            ("gpt-5.6-luna", "output", 1.2),
        ):
            with self.subTest(model=model, field=field):
                self.assertEqual(get_pricing(model)[field], expected)

    def test_unknown_gpt_6_ids_do_not_inherit_an_unrelated_price(self):
        self.assertIsNone(get_pricing("gpt-6-unlisted"))


if __name__ == "__main__":
    unittest.main()
