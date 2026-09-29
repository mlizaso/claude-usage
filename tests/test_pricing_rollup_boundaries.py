"""Boundary tests for dated pricing, long-context tiers, and source joins."""

import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from unittest.mock import patch

import reports
import rollups
from db import init_db
from pricing import (
    LONG_CONTEXT_THRESHOLD,
    calc_cost,
    calc_cost_parts,
    calc_cost_parts_tiered,
    get_pricing,
    is_long_context,
    is_long_context_model,
    load_rate_overrides,
)
from scanner import insert_turns, upsert_sessions

from tests.test_dashboard_js import daily_dom, daily_state, emit, requires_node, run_js


def _turn(message_id, model, timestamp, *, session_id="same", source="claude",
          input_tokens=0, output_tokens=0, cache_read_tokens=0,
          cache_creation_tokens=0, cache_creation_1h_tokens=0,
          is_subagent=0, agent_id=None):
    return {
        "session_id": session_id,
        "timestamp": timestamp,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_creation_1h_tokens": cache_creation_1h_tokens,
        "tool_name": None,
        "cwd": None,
        "message_id": message_id,
        "is_subagent": is_subagent,
        "agent_id": agent_id,
        "source": source,
    }


def _session(session_id, source, project, first, last, model="claude-opus-5",
             input_tokens=0, turns=1):
    return {
        "session_id": session_id,
        "source": source,
        "project_name": project,
        "first_timestamp": first,
        "last_timestamp": last,
        "git_branch": "main",
        "model": model,
        "total_input_tokens": input_tokens,
        "total_output_tokens": 0,
        "total_cache_read": 0,
        "total_cache_creation": 0,
        "total_cache_creation_1h": 0,
        "turn_count": turns,
    }


class TestLongContextPricingBoundaries(unittest.TestCase):
    def test_unrepresentable_utc_dates_keep_current_rates_without_raising(self):
        for raw in ("0001-01-01T00:00:00+14:00",
                    "9999-12-31T23:59:59-14:00"):
            for value in (raw, datetime.fromisoformat(raw)):
                with self.subTest(timestamp=value):
                    self.assertEqual(get_pricing("gpt-5.6-sol", value),
                                     get_pricing("gpt-5.6-sol"))

    def test_threshold_is_strict_and_counts_all_input_side_tokens(self):
        at = calc_cost_parts("gpt-5.4", LONG_CONTEXT_THRESHOLD, 1_000, 0, 0)
        above = calc_cost_parts("gpt-5.4", LONG_CONTEXT_THRESHOLD + 1,
                                1_000, 0, 0)
        self.assertFalse(is_long_context(
            "gpt-5.4", LONG_CONTEXT_THRESHOLD, 0, 0))
        self.assertTrue(is_long_context(
            "gpt-5.4", LONG_CONTEXT_THRESHOLD + 1, 0, 0))
        self.assertAlmostEqual(at["input"], 0.68, places=9)
        self.assertAlmostEqual(at["output"], 0.015, places=9)
        self.assertAlmostEqual(above["input"], 1.360005, places=9)
        self.assertAlmostEqual(above["output"], 0.0225, places=9)

    def test_cached_input_and_writes_count_toward_the_prompt_threshold(self):
        normal = calc_cost_parts("gpt-5.4", 1, 1_000, LONG_CONTEXT_THRESHOLD - 1, 0)
        long = calc_cost_parts("gpt-5.4", 1, 1_000, LONG_CONTEXT_THRESHOLD, 1)
        self.assertFalse(is_long_context(
            "gpt-5.4", 1, LONG_CONTEXT_THRESHOLD - 1, 0))
        self.assertTrue(is_long_context("gpt-5.4", 1, LONG_CONTEXT_THRESHOLD,
                                       1))
        self.assertAlmostEqual(normal["cache_read"],
                               (LONG_CONTEXT_THRESHOLD - 1) * 0.25 / 1e6,
                               places=9)
        self.assertAlmostEqual(long["cache_read"],
                               LONG_CONTEXT_THRESHOLD * 0.25 * 2 / 1e6,
                               places=9)
        self.assertAlmostEqual(long["cache_creation"], 2 / 1e6 * 2.50,
                               places=9)

    def test_two_short_turns_are_not_promoted_by_aggregate_tokens(self):
        normal = calc_cost_parts_tiered(
            "gpt-5.4", 400_000, 2_000, 0, 0,
            long_input=0, long_output=0, long_cache_read=0,
            long_cache_creation=0, long_cache_creation_1h=0)
        one_long = calc_cost_parts_tiered(
            "gpt-5.4", 400_000, 2_000, 0, 0,
            long_input=400_000, long_output=2_000, long_cache_read=0,
            long_cache_creation=0, long_cache_creation_1h=0)
        self.assertAlmostEqual(sum(normal.values()), 1.03, places=9)
        self.assertAlmostEqual(sum(one_long.values()), 2.045, places=9)
        self.assertLess(sum(normal.values()), sum(one_long.values()))

    def test_only_exact_or_date_stamped_eligible_families_get_the_tier(self):
        for model in (
            "gpt-6-astra", "gpt-6-astra-20260904", "gpt-6-astra-2026-09-04",
            "gpt-5.6-sol", "gpt-5.6-sol-20260215", "gpt-5.6-sol-2026-02-15",
            "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4",
            "gpt-5.4-20260215", "gpt-5.4-2026-02-15",
        ):
            with self.subTest(model=model):
                self.assertTrue(is_long_context_model(model))
        for model in (
            "gpt-5.4-mini", "gpt-5.4-mini-20260215",
            "gpt-5.4-mini-2026-02-15", "gpt-5.4-nano",
            "gpt-5.4-nano-20260215", "gpt-5.4-nano-2026-02-15",
        ):
            with self.subTest(model=model):
                self.assertFalse(is_long_context_model(model))

    def test_sonnet_5_uses_the_current_standard_rate_for_each_request(self):
        before = calc_cost("claude-sonnet-5", 1_000_000, 0, 0, 0,
                           timestamp="2026-08-31T23:59:59Z")
        after = calc_cost("claude-sonnet-5", 1_000_000, 0, 0, 0,
                          timestamp="2026-09-01T00:00:00Z")
        self.assertEqual(before, 2.0)
        self.assertEqual(after, 2.0)

    def test_sol_uses_the_verified_promotion_without_a_speculative_end(self):
        before = calc_cost("gpt-5.6-sol", 100_000, 0, 0, 0,
                           timestamp="2026-08-21T23:59:59Z")
        current = calc_cost("gpt-5.6-sol", 100_000, 0, 0, 0,
                            timestamp="2026-08-22T00:00:00Z")
        future = calc_cost("gpt-5.6-sol", 100_000, 0, 0, 0,
                           timestamp="2026-11-22T00:00:00Z")
        self.assertEqual(before, 0.5)
        self.assertEqual(current, 0.4)
        self.assertEqual(future, 0.4)

    def test_terra_and_luna_use_their_published_transition_date(self):
        for model, old_cost, new_cost in (
            ("gpt-5.6-terra", 0.25, 0.20),
            ("gpt-5.6-luna", 0.10, 0.02),
        ):
            with self.subTest(model=model):
                before = calc_cost(
                    model, 100_000, 0, 0, 0,
                    timestamp="2026-07-29T23:59:59Z",
                )
                current = calc_cost(
                    model, 100_000, 0, 0, 0,
                    timestamp="2026-07-30T00:00:00Z",
                )
                self.assertEqual(before, old_cost)
                self.assertEqual(current, new_cost)

    def test_an_equal_value_override_stays_authoritative_for_dated_lookups(self):
        import pricing

        original = pricing.PRICING["claude-sonnet-5"]
        original_values = dict(original)
        original_markers = dict(pricing._OVERRIDE_RATE_OBJECTS)
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        try:
            json.dump({"claude-sonnet-5": original_values}, handle)
            handle.close()
            self.assertEqual(load_rate_overrides(path=handle.name),
                             {"claude-sonnet-5"})
            self.assertEqual(
                get_pricing("claude-sonnet-5", "2026-09-01T00:00:00Z")["input"],
                original_values["input"],
            )
        finally:
            os.unlink(handle.name)
            pricing.PRICING["claude-sonnet-5"] = original
            original.clear()
            original.update(original_values)
            pricing._OVERRIDE_RATE_OBJECTS.clear()
            pricing._OVERRIDE_RATE_OBJECTS.update(original_markers)

    def test_sol_override_wins_before_and_after_the_verified_boundary(self):
        import pricing

        original = pricing.PRICING["gpt-5.6-sol"]
        original_values = dict(original)
        original_markers = dict(pricing._OVERRIDE_RATE_OBJECTS)
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        try:
            json.dump({"gpt-5.6-sol": {"input": 1.0}}, handle)
            handle.close()
            self.assertEqual(load_rate_overrides(path=handle.name),
                             {"gpt-5.6-sol"})
            for timestamp in ("2026-08-21T23:59:59Z",
                              "2026-08-22T00:00:00Z",
                              "2026-11-22T00:00:00Z"):
                with self.subTest(timestamp=timestamp):
                    self.assertEqual(
                        get_pricing("gpt-5.6-sol", timestamp)["input"], 1.0)
        finally:
            os.unlink(handle.name)
            pricing.PRICING["gpt-5.6-sol"] = original
            original.clear()
            original.update(original_values)
            pricing._OVERRIDE_RATE_OBJECTS.clear()
            pricing._OVERRIDE_RATE_OBJECTS.update(original_markers)


@unittest.skipUnless(hasattr(time, "tzset"), "requires time.tzset")
class TestRollupPricingAndTimestampBoundaries(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def _set_tz(self, name):
        old = os.environ.get("TZ")
        os.environ["TZ"] = name
        time.tzset()
        self.addCleanup(self._restore_tz, old)

    def _restore_tz(self, old):
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()

    def test_current_rate_survives_one_viewer_local_day_bucket(self):
        self._set_tz("America/New_York")
        ts_before = "2026-08-31T23:30:00Z"
        ts_after = "2026-09-01T00:30:00Z"
        upsert_sessions(self.conn, [_session(
            "sonnet", "claude", "user/project", ts_before, ts_after,
            model="claude-sonnet-5", input_tokens=200_000, turns=2)])
        insert_turns(self.conn, [
            _turn("s-before", "claude-sonnet-5", ts_before, session_id="sonnet",
                  input_tokens=100_000),
            _turn("s-after", "claude-sonnet-5", ts_after, session_id="sonnet",
                  input_tokens=100_000),
        ])
        self.conn.commit()

        rows = rollups.daily_by_model(self.conn, source="claude")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["day"], "2026-08-31")
        self.assertAlmostEqual(rows[0]["cost"], 0.4, places=9)
        self.assertAlmostEqual(rows[0]["cost_parts"]["input"], 0.4,
                               places=9)

    def test_same_display_session_id_stays_source_scoped_in_rollups_and_report(self):
        ts = "2026-08-22T12:00:00Z"
        upsert_sessions(self.conn, [
            _session("same", "claude", "user/claude", ts, ts,
                     model="claude-opus-5", input_tokens=100),
            _session("same", "codex", "user/codex", ts, ts,
                     model="gpt-5.6-sol", input_tokens=100),
        ])
        insert_turns(self.conn, [
            _turn("claude-turn", "claude-opus-5", ts, session_id="same",
                  source="claude", input_tokens=100),
            _turn("codex-turn", "gpt-5.6-sol", ts, session_id="same",
                  source="codex", input_tokens=100),
        ])
        self.conn.commit()

        sessions = rollups.sessions_all(self.conn)
        self.assertEqual({(row["source"], row["session_id"], row["project"])
                          for row in sessions}, {
                              ("claude", "same", "user/claude"),
                              ("codex", "same", "user/codex"),
                          })
        projects = rollups.project_by_day_model(self.conn)
        self.assertEqual({(row["source"], row["project"]) for row in projects},
                         {("claude", "user/claude"), ("codex", "user/codex")})

        output = io.StringIO()
        with redirect_stdout(output):
            reports._cmd_stats(self.conn, source=None)
        text = output.getvalue()
        self.assertIn("Total sessions:   2", text)
        self.assertIn("user/claude", text)
        self.assertIn("user/codex", text)

    def test_stats_counts_model_sessions_across_hidden_pricing_days(self):
        first = "2026-08-21T12:00:00Z"
        second = "2026-08-22T12:00:00Z"
        upsert_sessions(self.conn, [
            _session("first", "claude", "user/first", first, first,
                     model="claude-opus-5"),
            _session("second", "claude", "user/second", second, second,
                     model="claude-opus-5"),
        ])
        insert_turns(self.conn, [
            _turn("first-turn", "claude-opus-5", first, session_id="first"),
            _turn("second-turn", "claude-opus-5", second, session_id="second"),
        ])
        self.conn.commit()

        output = io.StringIO()
        with redirect_stdout(output):
            reports._cmd_stats(self.conn, source="claude")
        self.assertRegex(output.getvalue(),
                         r"claude-opus-5\s+sessions=2\s+turns=2")

    def test_stats_counts_one_session_spanning_pricing_days_once(self):
        first = "2026-08-21T12:00:00Z"
        second = "2026-08-22T12:00:00Z"
        upsert_sessions(self.conn, [
            _session("spanning", "claude", "user/spanning", first, second,
                     model="claude-opus-5", turns=2),
        ])
        insert_turns(self.conn, [
            _turn("span-first", "claude-opus-5", first,
                  session_id="spanning"),
            _turn("span-second", "claude-opus-5", second,
                  session_id="spanning"),
        ])
        self.conn.commit()

        output = io.StringIO()
        with redirect_stdout(output):
            reports._cmd_stats(self.conn, source="claude")
        self.assertRegex(output.getvalue(),
                         r"claude-opus-5\s+sessions=1\s+turns=2")

    def test_sessions_and_dispatches_order_and_start_by_instant(self):
        self._set_tz("UTC")
        # Lexical order says the +14 value is later; by instant it is the
        # earlier endpoint. The -14 value has the opposite local day.
        early = "2026-08-02T00:00:00+14:00"
        late = "2026-08-01T23:00:00-14:00"
        upsert_sessions(self.conn, [
            _session("early", "claude", "user/early", early, early,
                     input_tokens=1),
            _session("late", "claude", "user/late", late, late,
                     input_tokens=1),
        ])
        insert_turns(self.conn, [
            _turn("early-turn", "claude-opus-5", early, session_id="early",
                  input_tokens=1),
            _turn("late-turn", "claude-opus-5", late, session_id="late",
                  input_tokens=1),
            _turn("dispatch-early", "claude-opus-5", early, session_id="early",
                  input_tokens=1, is_subagent=1, agent_id="agent-1"),
            _turn("dispatch-late", "claude-opus-5", late, session_id="late",
                  input_tokens=1, is_subagent=1, agent_id="agent-1"),
        ])
        self.conn.commit()

        sessions = rollups.sessions_all(self.conn)
        self.assertEqual([row["session_id"] for row in sessions], ["late", "early"])
        dispatch = rollups.top_dispatches(self.conn)[0]
        self.assertEqual(dispatch["start_date"], "2026-08-01")
        self.assertIn("2026-08-01", dispatch["start"])

    def test_stats_period_uses_instant_bounds_for_mixed_offsets(self):
        self._set_tz("UTC")
        early = "2026-08-02T00:00:00+14:00"  # Aug 1 UTC
        late = "2026-08-01T23:00:00-14:00"   # Aug 2 UTC
        upsert_sessions(self.conn, [
            _session("early", "claude", "user/early", early, early),
            _session("late", "claude", "user/late", late, late),
        ])
        self.conn.commit()
        output = io.StringIO()
        with redirect_stdout(output):
            reports._cmd_stats(self.conn, source="claude")
        self.assertIn("Period:           2026-08-01 to 2026-08-02",
                      output.getvalue())


class TestCurrentModelRollupCosts(unittest.TestCase):
    def test_current_models_keep_per_request_tiers_in_daily_rollups(self):
        with tempfile.TemporaryDirectory() as root:
            conn = sqlite3.connect(os.path.join(root, "usage.db"))
            conn.row_factory = sqlite3.Row
            try:
                init_db(conn)
                timestamp = "2026-09-29T12:00:00Z"
                expected = {
                    "gpt-6-astra": 5.45,
                    "gpt-6-sol": 1.09,
                    "gpt-6-luna": 0.0545,
                    "claude-opus-5-5": 1.51,
                }
                for model in expected:
                    source = "claude" if model.startswith("claude-") else "codex"
                    upsert_sessions(conn, [_session(
                        model, source, "example/project", timestamp, timestamp,
                        model=model, input_tokens=200_000, turns=2)])
                    insert_turns(conn, [
                        _turn(model + suffix, model, timestamp,
                              session_id=model, source=source,
                              input_tokens=100_000, output_tokens=10_000,
                              cache_read_tokens=cached,
                              cache_creation_tokens=20_000,
                              cache_creation_1h_tokens=10_000)
                        for suffix, cached in (("-short", 50_000),
                                               ("-long", 200_000))
                    ])
                conn.commit()
                rows = rollups.daily_by_model(conn)
                self.assertEqual({row["model"] for row in rows}, set(expected))
                for row in rows:
                    with self.subTest(model=row["model"]):
                        self.assertAlmostEqual(row["cost"], expected[row["model"]],
                                               places=9)
                output = io.StringIO()
                with redirect_stdout(output):
                    reports._cmd_stats(conn, source=None)
                for model in expected:
                    self.assertIn(model, output.getvalue())
            finally:
                conn.close()


@requires_node
class TestBrowserPricingBoundaries(unittest.TestCase):
    def test_new_models_reach_browser_costs_and_default_selection(self):
        for model, normal, long in (
            ("gpt-6-sol", 0.36, 0.73),
            ("gpt-6-luna", 0.018, 0.0365),
            ("claude-opus-5-5", 0.74, 0.77),
        ):
            for suffix in ("", "-2026-09-22"):
                with self.subTest(model=model, suffix=suffix):
                    name = model + suffix
                    result = run_js(emit("""({
                      normal: costParts(model, 100000, 10000, 50000, 20000, 10000),
                      long: costParts(model, 100000, 10000, 200000, 20000, 10000),
                      selected: Array.from(defaultModelSelection([model])),
                      estimated: isEstimatedRate(model)
                    })""", model=name))
                    self.assertAlmostEqual(sum(result["normal"].values()), normal,
                                           places=9)
                    self.assertAlmostEqual(sum(result["long"].values()), long,
                                           places=9)
                    self.assertIn(name, result["selected"])
                    self.assertFalse(result["estimated"])

    def test_astra_prices_all_token_buckets_and_the_long_context_boundary(self):
        for model in ("gpt-6-astra", "gpt-6-astra-20260904",
                      "gpt-6-astra-2026-09-04"):
            with self.subTest(model=model):
                result = run_js(emit("""({
                  normal: costParts(model, 200000, 50, 71000, 1000),
                  long: costParts(model, 200001, 50, 71000, 1000),
                  selected: Array.from(defaultModelSelection([model, 'gpt-5.6-sol'])),
                  estimated: isEstimatedRate(model)
                })""", model=model))
                expected = {"input": 2, "output": 0.0025,
                            "cache_read": 0.071, "cache_creation": 0.0125}
                expected_long = {"input": 4.00002, "output": 0.00375,
                                 "cache_read": 0.142, "cache_creation": 0.025}
                for name, values in (("normal", expected), ("long", expected_long)):
                    self.assertIsNotNone(result[name])
                    for field, value in values.items():
                        self.assertAlmostEqual(result[name][field], value, places=9)
                self.assertIn(model, result["selected"])
                self.assertFalse(result["estimated"])

    def test_browser_matches_python_at_threshold_and_current_rate(self):
        got = run_js(emit("""({
          exact: costParts('gpt-5.4', 272000, 1000, 0, 0),
          above: costParts('gpt-5.4', 272001, 1000, 0, 0),
          cached: costParts('gpt-5.4', 1, 1000, 272000, 1),
          solBefore: costParts('gpt-5.6-sol', 100000, 0, 0, 0, 0,
                               '2026-08-21T23:59:59Z'),
          solPromo: costParts('gpt-5.6-sol', 100000, 0, 0, 0, 0,
                              '2026-08-22T00:00:00Z'),
          solFuture: costParts('gpt-5.6-sol', 100000, 0, 0, 0, 0,
                               '2026-11-22T00:00:00Z'),
          before: costParts('claude-sonnet-5', 1000000, 0, 0, 0, 0,
                           '2026-08-31T23:59:59Z'),
          after: costParts('claude-sonnet-5', 1000000, 0, 0, 0, 0,
                              '2026-09-01T00:00:00Z')
        })"""))
        self.assertAlmostEqual(got["exact"]["input"], 0.68, places=9)
        self.assertAlmostEqual(got["above"]["input"], 1.360005, places=9)
        self.assertAlmostEqual(got["above"]["output"], 0.0225, places=9)
        self.assertAlmostEqual(got["cached"]["cache_read"],
                               272000 * 0.25 * 2 / 1e6, places=9)
        self.assertEqual(got["solBefore"]["input"], 0.5)
        self.assertEqual(got["solPromo"]["input"], 0.4)
        self.assertEqual(got["solFuture"]["input"], 0.4)
        self.assertEqual(got["before"]["input"], 2)
        self.assertEqual(got["after"]["input"], 2)

    def test_unpriced_rows_remain_na_after_filtered_aggregation(self):
        day = time.strftime("%Y-%m-%d")
        rows = [
            {"day": day, "source": "codex", "model": "local-llama",
             "input": 100, "output": 0, "cache_read": 0,
             "cache_creation": 0, "cache_creation_1h": 0,
             "turns": 1, "cost_parts": None},
            {"day": day, "source": "codex", "model": "gpt-5.6-sol",
             "input": 100, "output": 0, "cache_read": 0,
             "cache_creation": 0, "cache_creation_1h": 0,
             "turns": 1, "cost_parts": {"input": 0.0005, "output": 0,
                                          "cache_read": 0, "cache_creation": 0}},
        ]
        got = run_js(
            daily_dom(1024, 558) + daily_state(
                rows, ["local-llama", "gpt-5.6-sol"], source="codex")
            + "applyFilter();\n"
            + emit("""({
                local: lastByModel.find(r => r.model === 'local-llama'),
                html: elFor('model-cost-body').innerHTML,
                total: capturedTotals
              })"""))
        self.assertEqual(got["local"]["cost"], 0)
        self.assertIn("n/a", got["html"])
        self.assertAlmostEqual(got["total"]["cost"], 0.0005, places=9)


if __name__ == "__main__":
    unittest.main()
