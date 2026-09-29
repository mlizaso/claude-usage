"""Rates a user supplies, and the tier that resolves a dated model id.

`CLAUDE_USAGE_RATES` is documented in README.md and in the CHANGELOG, and until
these tests existed nothing anywhere in the suite mentioned it — so "your rates
are used" was an unverified claim on both surfaces that quote a price. Three
separate defects lived in that gap:

* the terminal reports never loaded the file at all (only `dashboard.py` did, and
  `cli.py` imports it lazily inside `cmd_dashboard`/`cmd_url`);
* an override for a model that is not an exact `PRICING` key based every field
  the user did NOT supply on `gpt-5.6-sol`, so "my local model is free" billed
  its output at the most expensive Codex tier;
* the `startswith` tier returned the first matching key in table order rather
  than the most specific one, so a dated `gpt-5.4-mini-*` billed at `gpt-5.4`.

Each test below is written against the *cost that gets printed*, not only
against the resolved dict, because a rate table that is right and a report that
ignores it are indistinguishable to a user.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import pricing
from pricing import (
    ESTIMATED_RATE_MODELS,
    PRICING,
    RATE_FIELDS,
    calc_cost,
    get_pricing,
    is_estimated,
    load_rate_overrides,
)
from scanner import get_db, init_db, insert_turns, upsert_sessions

REPO_ROOT = Path(__file__).resolve().parent.parent
MILLION = 1_000_000


class _PricingTableRestored(unittest.TestCase):
    """`load_rate_overrides` mutates module globals; put them back afterwards.

    Restores the original inner dict *objects*, not copies of them: `is_estimated`
    answers by object identity (`resolved is PRICING[name]`), so handing the rest
    of the suite equal-but-distinct dicts would be a subtle way to break it.
    """

    def setUp(self):
        self._order = list(pricing.PRICING)
        self._objects = dict(pricing.PRICING)
        self._values = {k: dict(v) for k, v in pricing.PRICING.items()}
        self._estimated = set(pricing.ESTIMATED_RATE_MODELS)
        self._override_rate_objects = dict(pricing._OVERRIDE_RATE_OBJECTS)

    def tearDown(self):
        for key, value in self._values.items():
            self._objects[key].clear()
            self._objects[key].update(value)
        pricing.PRICING.clear()
        for key in self._order:
            pricing.PRICING[key] = self._objects[key]
        pricing.ESTIMATED_RATE_MODELS.clear()
        pricing.ESTIMATED_RATE_MODELS.update(self._estimated)
        pricing._OVERRIDE_RATE_OBJECTS.clear()
        pricing._OVERRIDE_RATE_OBJECTS.update(self._override_rate_objects)

    def apply_overrides(self, table):
        """Write `table` to a temp file and load it, returning the applied set."""
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        try:
            json.dump(table, handle)
            handle.close()
            return load_rate_overrides(path=handle.name)
        finally:
            os.unlink(handle.name)


class TestDatedModelIdsResolveToTheMostSpecificKey(_PricingTableRestored):
    """The `startswith` tier exists for date-suffixed ids; it must pick the
    longest matching key, not the first one the table happens to list."""

    def test_dated_mini_is_not_billed_at_the_parent_tier(self):
        self.assertIs(get_pricing("gpt-5.4-mini-2026-04-01"), PRICING["gpt-5.4-mini"])
        # In dollars: 1M input tokens is $0.75 at mini's rate and $2.50 at
        # gpt-5.4's — 3.3x, silently, with no error and no `n/a`.
        self.assertAlmostEqual(
            calc_cost("gpt-5.4-mini-2026-04-01", MILLION, 0, 0, 0), 0.75, places=6)

    def test_dated_nano_is_not_billed_at_the_parent_tier(self):
        self.assertIs(get_pricing("gpt-5.4-nano-2026-04-01"), PRICING["gpt-5.4-nano"])
        self.assertAlmostEqual(
            calc_cost("gpt-5.4-nano-2026-04-01", MILLION, 0, 0, 0), 0.20, places=6)

    def test_dated_spark_keeps_its_estimated_provenance(self):
        """`gpt-5.3-codex-spark` is one of the two ids OpenAI does not publish.

        Resolving a dated one onto the published `gpt-5.3-codex` entry made
        `is_estimated` answer "published" for a number that is still a guess.
        """
        self.assertIs(get_pricing("gpt-5.3-codex-spark-2026-04-01"),
                      PRICING["gpt-5.3-codex-spark"])
        self.assertTrue(is_estimated("gpt-5.3-codex-spark-2026-04-01"))

    def test_every_priced_id_survives_a_date_suffix(self):
        """The general rule, so a future table entry cannot reintroduce this."""
        for key in list(PRICING):
            with self.subTest(model=key):
                self.assertIs(get_pricing(key + "-20260401"), PRICING[key])

    def test_unrelated_ids_still_fall_through_to_nothing(self):
        """Longest-match must not widen what the prefix tier captures."""
        self.assertIsNone(get_pricing("gemma-3-27b"))
        self.assertIsNone(get_pricing("glm-4-9b"))
        self.assertIsNone(get_pricing(""))


class TestOverrideBaseline(_PricingTableRestored):
    """What an unspecified field falls back to when a rate file is loaded."""

    def test_unpriced_model_is_not_promoted_to_another_vendors_rates(self):
        """"My local model is free" must not start billing at $30.00/M output."""
        self.assertIsNone(get_pricing("my-local-llama-70b"))
        applied = self.apply_overrides({"my-local-llama-70b": {"input": 0.0}})
        self.assertEqual(applied, {"my-local-llama-70b"})
        rates = get_pricing("my-local-llama-70b")
        self.assertIsNotNone(rates)
        for field in RATE_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(rates[field], 0.0)
        self.assertEqual(
            calc_cost("my-local-llama-70b", MILLION, MILLION, MILLION, MILLION), 0.0)

    def test_partial_override_of_an_unpriced_model_keeps_the_rest_at_zero(self):
        self.apply_overrides({"my-local-llama-70b": {"output": 2.0}})
        self.assertAlmostEqual(
            calc_cost("my-local-llama-70b", MILLION, MILLION, MILLION, MILLION),
            2.0, places=6)

    def test_dated_id_keeps_the_rates_its_own_tier_resolves_to(self):
        """Correcting one field must not silently reprice the other four.

        `claude-opus-4-8-20260215` is not an exact key, so the old code based
        every unlisted field on `gpt-5.6-sol` — raising output from $25.00 to
        $30.00 because the user corrected the input rate.
        """
        self.apply_overrides({"claude-opus-4-8-20260215": {"input": 1.0}})
        rates = get_pricing("claude-opus-4-8-20260215")
        self.assertEqual(rates["input"], 1.0)
        for field in ("output", "cache_read", "cache_write", "cache_write_1h"):
            with self.subTest(field=field):
                self.assertEqual(rates[field], PRICING["claude-opus-4-8"][field])

    def test_overriding_a_dated_id_does_not_mutate_the_entry_it_resolved_from(self):
        before = dict(PRICING["claude-opus-4-8"])
        self.apply_overrides({"claude-opus-4-8-20260215": {"input": 1.0}})
        self.assertEqual(PRICING["claude-opus-4-8"], before)

    def test_exact_key_override_still_keeps_unspecified_built_in_fields(self):
        """The documented behaviour, pinned so the fix above cannot regress it."""
        before = dict(PRICING["gpt-5.6-sol"])
        self.apply_overrides({"gpt-5.6-sol": {"output": 10.0}})
        after = PRICING["gpt-5.6-sol"]
        self.assertEqual(after["output"], 10.0)
        for field in ("input", "cache_read", "cache_write", "cache_write_1h"):
            with self.subTest(field=field):
                self.assertEqual(after[field], before[field])

    def test_supplied_rate_drops_the_estimated_label(self):
        self.assertTrue(is_estimated("gpt-5.3-codex-spark"))
        self.apply_overrides({"gpt-5.3-codex-spark": {"input": 1.0}})
        self.assertFalse(is_estimated("gpt-5.3-codex-spark"))

    def test_malformed_entries_are_ignored_rather_than_half_applied(self):
        before = dict(PRICING["gpt-5.6-sol"])
        applied = self.apply_overrides({
            "gpt-5.6-sol": {"input": "free"},
            "another": {"input": -1},
            "third": {"input": True},
            "fourth": "not-a-dict",
        })
        self.assertEqual(applied, set())
        self.assertEqual(PRICING["gpt-5.6-sol"], before)
        self.assertIsNone(get_pricing("another"))

    @unittest.skipUnless(os.name == "posix", "FIFO semantics are POSIX")
    def test_a_fifo_override_cannot_block_process_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rates.json"
            os.mkfifo(path)
            result = []
            finished = threading.Event()

            def load():
                result.append(load_rate_overrides(path=path))
                finished.set()

            worker = threading.Thread(target=load, daemon=True)
            worker.start()
            returned_without_writer = finished.wait(0.25)
            if not returned_without_writer:
                writer = os.open(
                    path, os.O_WRONLY | getattr(os, "O_NONBLOCK", 0))
                os.close(writer)
            worker.join(2)

            self.assertTrue(
                returned_without_writer,
                "loading a pricing override blocked on a FIFO",
            )
            self.assertEqual(result, [set()])


class TestRateOverridesReachTheTerminalReports(unittest.TestCase):
    """`python cli.py stats` must quote the user's rates, not the built-ins.

    Driven as a subprocess deliberately: the defect was that `cli.py` never
    called the loader on any path that does not import `dashboard`, and only a
    real end-to-end invocation can show that.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "usage.db"
        ts = "2026-08-01T12:00:00Z"
        conn = get_db(cls.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-1", "project_name": "user/proj",
            "first_timestamp": ts, "last_timestamp": ts,
            "git_branch": "main", "model": "gpt-5.6-sol",
            "total_input_tokens": MILLION, "total_output_tokens": 0,
            "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 1,
        }])
        insert_turns(conn, [{
            "session_id": "sess-1", "timestamp": ts, "model": "gpt-5.6-sol",
            "input_tokens": MILLION, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_name": None, "cwd": None, "message_id": "m-1",
            "is_subagent": 0, "agent_id": None,
        }])
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _stats(self, rates=None):
        env = dict(os.environ)
        # Pin BOTH ends of the pipe. Without encoding= the parent decodes with
        # locale.getencoding() — cp1252 on windows-latest — and without
        # PYTHONIOENCODING the child would encode its stdout with that same
        # codepage, so decoding it as UTF-8 here would be the mismatch that
        # UnicodeDecodeError is made of. No errors= : a mismatch should fail
        # loudly rather than smuggle mojibake into an assertion.
        env["PYTHONIOENCODING"] = "utf-8"
        env["CLAUDE_USAGE_DB"] = str(self.db_path)
        env.pop("CLAUDE_USAGE_RATES", None)
        rate_file = None
        if rates is not None:
            handle = tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8")
            json.dump(rates, handle)
            handle.close()
            rate_file = handle.name
            env["CLAUDE_USAGE_RATES"] = rate_file
        try:
            done = subprocess.run(
                [sys.executable, "cli.py", "stats"],
                cwd=REPO_ROOT, capture_output=True, text=True,
                encoding="utf-8", env=env,
            )
        finally:
            if rate_file:
                os.unlink(rate_file)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_built_in_rate_without_an_override_file(self):
        # 1M input tokens on gpt-5.6-sol crosses the long-context threshold,
        # The fixture is dated before the 2026-08-22 verification boundary, so
        # the prior $5.00/M estimate is doubled for this long-context request.
        self.assertIn("Est. total cost:  $10.0000", self._stats())

    def test_override_changes_the_printed_cost(self):
        out = self._stats({"gpt-5.6-sol": {"input": 1.0}})
        self.assertIn("Est. total cost:  $2.0000", out)
        self.assertNotIn("$8.0000", out)

    def test_unreadable_override_file_leaves_the_built_ins_alone(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"  # see _stats: pin both ends
        env["CLAUDE_USAGE_DB"] = str(self.db_path)
        env["CLAUDE_USAGE_RATES"] = str(Path(self.tmp.name) / "nope.json")
        done = subprocess.run(
            [sys.executable, "cli.py", "stats"],
            cwd=REPO_ROOT, capture_output=True, text=True,
            encoding="utf-8", env=env,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Est. total cost:  $10.0000", done.stdout)


if __name__ == "__main__":
    unittest.main()
