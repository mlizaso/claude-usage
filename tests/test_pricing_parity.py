"""Tests that pricing projections never drift from the Python source.

Costs are computed in Python and mirrored in the browser. The canonical table
and effective-date policy live in `codex_claude_usage.pricing`; the JavaScript and
Markdown tables are generated projections. A stale generated file would make
`cli.py stats`, the dashboard, and the published documentation disagree, so
the generator's check mode is part of this parity suite.

These tests parse the JS table out of the template and compare it to the Python
one, including the substring-fallback tiers in get_pricing / getPricing (the
family defaults for an unrecognised opus/sonnet/haiku/fable model id).
"""

import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import dashboard
import pricing
from cli import PRICING
from pricing import calc_cost, is_estimated
from scanner import get_db, init_db

from tests.test_dashboard_js import (NODE, _DOM_STUB, emit, extract_app_script,
                                     requires_node, run_js)
from tests.test_pricing_overrides import REPO_ROOT, _PricingTableRestored

# cache_write is the 5-minute write tier, cache_write_1h the 1-hour one. Both
# are billed, so both have to match across the two implementations.
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")


def _js_pricing_table():
    """Extract the PRICING object literal from the embedded dashboard JS."""
    m = re.search(r"^const PRICING = \{$(.*?)^\};$",
                  dashboard.HTML_TEMPLATE, re.MULTILINE | re.DOTALL)
    if m is None:
        raise AssertionError(
            "Could not find the `const PRICING = {...};` block in "
            "dashboard.HTML_TEMPLATE. If it was renamed or reformatted, update "
            "this test — do not delete it; it is the only guard against the "
            "CLI and the dashboard pricing the same turn differently."
        )
    table = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        entry = re.fullmatch(
            r"'([^']+)':\s*\{(.*?)\},?", line)
        if entry is None:
            raise AssertionError(f"Unparsed line in the JS PRICING table: {line!r}")
        model, body = entry.group(1), entry.group(2)
        prices = {}
        for field in re.finditer(r"(\w+):\s*([0-9.]+)", body):
            prices[field.group(1)] = float(field.group(2))
        table[model] = prices
    return table


def _js_get_pricing_body():
    """The body of the page's getPricing(), where the fallback tiers live."""
    body = re.search(r"function getPricing\(model\) \{(.*?)\n\}",
                     dashboard.HTML_TEMPLATE, re.DOTALL)
    if body is None:
        raise AssertionError(
            "getPricing() not found in the dashboard JS. If it was renamed, "
            "update this test — do not delete it; it is the only guard against "
            "the two fallback tiers resolving differently."
        )
    return body.group(1)


def _js_family_fallbacks():
    """Extract the substring-fallback family defaults from getPricing().

    Every keyword on a branch, not only the first. `gpt-5` and `codex` share one
    `||` statement, as do `fable` and `mythos`, and a pattern that captured just
    the leading keyword probed 5 of the 8 families — so splitting that shared
    statement and repricing half of it (24x on an unlisted gpt-5.x id) passed the
    whole suite. `\\w` also excludes `-` and `.`, which hid `codex-auto-review`
    and `gpt-5` even as leading keywords.
    """
    fallbacks = {}
    for keywords, model in re.findall(
        r"((?:m\.includes\('[^'\n]+'\)[ \t]*(?:\|\|[ \t]*)?)+)\)?[ \t]*"
        r"return PRICING\['([^']+)'\]", _js_get_pricing_body()
    ):
        for family in re.findall(r"m\.includes\('([^'\n]+)'\)", keywords):
            fallbacks.setdefault(family, model)
    return fallbacks


_STUB_APP_CONFIG = "APP_CONFIG: { version: 'test', surface: 'web' },"


def _run_page_with_app_config(config, expr):
    """Load the page's real JS with `config` as its APP_CONFIG; return `expr`.

    The shared harness in tests/test_dashboard_js.py hard-codes an APP_CONFIG of
    its own, so no test using it can see whether the page applies what the
    server injects — which is the whole of the rate-override wire. Same stub,
    same concatenation, one substituted line.
    """
    if _STUB_APP_CONFIG not in _DOM_STUB:
        raise AssertionError(
            "the JS harness no longer declares APP_CONFIG the way this test "
            "substitutes it. Update the marker — do not delete the test; it is "
            "the only thing that runs the page against a server-supplied config."
        )
    source = (_DOM_STUB.replace(_STUB_APP_CONFIG,
                                "APP_CONFIG: " + json.dumps(config) + ",")
              + "\n" + extract_app_script()
              + "\nconsole.log(JSON.stringify(" + expr + "));")
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.cjs"
        harness.write_text(source, encoding="utf-8")
        # node writes UTF-8; without encoding= the parent would decode with
        # locale.getencoding() — cp1252 on windows-latest, which turned an en
        # dash into mojibake and failed the assertions.
        proc = subprocess.run([NODE, str(harness)], capture_output=True,
                              text=True, encoding="utf-8", timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f"node exited {proc.returncode}:\n{proc.stderr[-4000:]}")
    return json.loads(proc.stdout)


def _py_family_keywords():
    """Every substring keyword pricing.get_pricing's fallback tier tests.

    Read out of the function's own source rather than listed here, for the same
    reason the dated-id probes are derived from PRICING: a family added tomorrow
    is covered on arrival instead of needing a second list updated. A
    hand-maintained list is exactly what left three of the eight branches
    unprobed.
    """
    keywords = set(re.findall(r'"([^"\n]+)"\s+in\s+m\b',
                              inspect.getsource(pricing.get_pricing)))
    if len(keywords) < 5:
        raise AssertionError(
            "Fewer than five family keywords found in pricing.get_pricing. The "
            "fallback tier was probably reformatted; fix this extraction rather "
            "than letting it silently probe nothing."
        )
    return keywords


class TestPricingParity(unittest.TestCase):
    def setUp(self):
        self.js = _js_pricing_table()

    def test_generated_browser_and_documentation_assets_are_current(self):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" /
                                "generate-pricing-assets.py"), "--check"],
            cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_same_models_are_priced(self):
        self.assertEqual(
            sorted(self.js), sorted(PRICING),
            "cli.PRICING and the dashboard's JS PRICING list different models. "
            "Both are used to bill the same turns; add the model to both.",
        )

    def test_same_rates_for_every_model(self):
        for model in sorted(PRICING):
            with self.subTest(model=model):
                py = PRICING[model]
                js = self.js.get(model, {})
                self.assertEqual(
                    {f: py[f] for f in PRICE_FIELDS},
                    {f: js.get(f) for f in PRICE_FIELDS},
                    f"Rates for {model} differ between cli.py and the dashboard.",
                )

    def test_every_model_declares_every_rate(self):
        for model, prices in sorted(PRICING.items()):
            with self.subTest(model=model):
                self.assertEqual(sorted(prices), sorted(PRICE_FIELDS))

    def test_family_fallbacks_match(self):
        """An unrecognised model id must fall back to the same family price."""
        from cli import get_pricing

        js_fallbacks = _js_family_fallbacks()
        self.assertTrue(js_fallbacks, "No substring fallbacks found in getPricing().")
        for family, js_model in sorted(js_fallbacks.items()):
            with self.subTest(family=family):
                py_prices = get_pricing(f"some-unknown-{family}-model-9")
                self.assertIsNotNone(
                    py_prices,
                    f"cli.get_pricing has no {family} fallback but the dashboard does.",
                )
                self.assertEqual(
                    {f: py_prices[f] for f in PRICE_FIELDS},
                    {f: self.js[js_model][f] for f in PRICE_FIELDS},
                    f"The {family} substring fallback resolves to different rates "
                    f"in cli.py than in the dashboard (JS uses {js_model}).",
                )

    def test_every_python_family_keyword_is_probed(self):
        """The guard must cover every branch, not the ones a regex happens to see.

        `gpt-5`, `codex-auto-review` and `mythos` were all invisible to the
        extraction above, so a divergence in any of them passed the suite — the
        worst kind of guard, because a reader trusts it. Deriving both sides
        means the next family is covered the day it is added.
        """
        self.assertEqual(
            sorted(_js_family_fallbacks()), sorted(_py_family_keywords()),
            "The family keywords in pricing.get_pricing and in the dashboard's "
            "getPricing differ (or one of the two extractions stopped seeing a "
            "branch). Both bill the same turns; add the family to both.",
        )

    def test_is_billable_derives_from_get_pricing(self):
        """`calcCost` is gated on `isBillable`, so the two must never disagree.

        `isBillable` used to carry its own list of family keywords ('opus',
        'sonnet', ...) and answer independently of PRICING. That agreed with
        `getPricing` only by luck — every priced model id happened to contain a
        keyword — and a future model with a new family name would have been
        charged by cli.py while the dashboard showed it as free. Deriving the
        answer from `getPricing` removes the second source of truth entirely,
        which is a stronger guarantee than any list this test could check.

        This asserts the structure (no independent keyword list). The executable
        proof that the two agree for every model lives in
        tests/test_dashboard_js.py::TestBillabilityMatchesPricing, which runs
        both functions under node; this one still runs where node is absent.
        """
        body = re.search(r"function isBillable\(model\) \{(.*?)\n\}",
                         dashboard.HTML_TEMPLATE, re.DOTALL)
        self.assertIsNotNone(body, "isBillable() not found in the dashboard JS.")
        source = body.group(1)
        self.assertIn(
            "getPricing(", source,
            "isBillable no longer defers to getPricing. If it grows its own "
            "notion of which models are billable, that second source of truth "
            "can disagree with PRICING and the dashboard will show a priced "
            "model as free.",
        )
        self.assertNotRegex(
            source, r"includes\('(opus|sonnet|haiku|fable|mythos)'\)",
            "isBillable has regrown a hard-coded family list alongside PRICING.",
        )

    def test_unpriced_models_are_free_in_both(self):
        """Local/3rd-party models must be billed at 0 (shown 'n/a'), not guessed.

        Deliberate behaviour (see AGENTS.md): a model matching no tier returns
        None so gemma/glm/etc. aren't charged at Sonnet rates.
        """
        from cli import calc_cost, get_pricing

        self.assertIsNone(get_pricing("gemma-3-27b"))
        self.assertEqual(calc_cost("gemma-3-27b", 1_000_000, 1_000_000, 0, 0), 0.0)
        self.assertNotIn("gemma", json.dumps(sorted(self.js)))


@requires_node
class TestFamilyFallbacksResolveIdentically(unittest.TestCase):
    """The text comparison above pairs each keyword with the table entry its own
    branch names. That cannot see *order*: moving the `codex` branch above
    `codex-auto-review` in one copy only leaves every pairing intact and still
    bills the review model at the wrong tier. Running the real getPricing under
    node is what settles it.
    """

    def test_each_family_probe_resolves_to_the_same_rates(self):
        probes = {family: f"some-unknown-{family}-model-9"
                  for family in sorted(_py_family_keywords())}
        resolved = run_js(emit("ids.map(id => getPricing(id))",
                               ids=[probes[f] for f in sorted(probes)]))
        for family, js_rates in zip(sorted(probes), resolved):
            with self.subTest(family=family):
                py_rates = pricing.get_pricing(probes[family])
                self.assertIsNotNone(
                    py_rates,
                    f"cli.get_pricing has no {family} fallback but the page does.")
                self.assertIsNotNone(
                    js_rates,
                    f"the page has no {family} fallback but cli.get_pricing does.")
                self.assertEqual({f: py_rates[f] for f in PRICE_FIELDS},
                                 {f: js_rates.get(f) for f in PRICE_FIELDS})


class TestEstimatedRateParity(unittest.TestCase):
    """Which rates are guesses must be one fact, not two.

    The Codex rates are the only estimated entries in either table — nothing
    under ~/.codex carries a price — and the page labels any figure derived from
    them. If the two lists drift, one copy shows an estimate as a published rate.
    """

    def _js_estimated(self):
        block = re.search(r"ESTIMATED_RATE_MODELS = Object\.freeze\(\[(.*?)\]\)",
                          dashboard.HTML_TEMPLATE, re.S)
        self.assertIsNotNone(block, "ESTIMATED_RATE_MODELS not found in the page")
        return sorted(match[0] or match[1] for match in re.findall(
            r"'([^']+)'|\"([^\"]+)\"", block.group(1)))

    def test_the_same_models_are_marked_estimated(self):
        from pricing import ESTIMATED_RATE_MODELS
        self.assertEqual(self._js_estimated(), sorted(ESTIMATED_RATE_MODELS))

    def test_every_estimated_model_is_actually_priced(self):
        """A marker for a model with no rate would label nothing."""
        from pricing import ESTIMATED_RATE_MODELS, PRICING
        for model in sorted(ESTIMATED_RATE_MODELS):
            with self.subTest(model=model):
                self.assertIn(model, PRICING)

    def test_anthropic_rates_are_not_marked_estimated(self):
        """They come from a published price list; saying otherwise would
        undersell them exactly as much as the reverse oversells Codex."""
        from pricing import ESTIMATED_RATE_MODELS, is_estimated
        for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5",
                      "claude-fable-5"):
            with self.subTest(model=model):
                self.assertNotIn(model, ESTIMATED_RATE_MODELS)
                self.assertFalse(is_estimated(model))

    def test_an_unpriced_model_is_not_an_estimate(self):
        """It has no rate at all, which is a different statement."""
        from pricing import is_estimated
        for model in ("gemma-3", "glm-5.1", "gpt-4o", ""):
            with self.subTest(model=model):
                self.assertFalse(is_estimated(model))


class TestResolvedRatesIsWhatThePageNeeds(unittest.TestCase):
    """`pricing.resolved_rates` is the server half of the override wire."""

    def test_it_yields_the_full_five_field_table_for_each_model(self):
        table = pricing.resolved_rates({"gpt-5.6-sol", "claude-opus-5"})
        self.assertEqual(sorted(table), ["claude-opus-5", "gpt-5.6-sol"])
        for model, rates in table.items():
            with self.subTest(model=model):
                self.assertEqual(sorted(rates), sorted(PRICE_FIELDS))
                self.assertEqual(rates, PRICING[model])

    def test_it_copies_rather_than_aliasing_the_table(self):
        """`is_estimated` answers by object identity, so the live dicts must not
        leave the module inside a payload a caller could edit."""
        table = pricing.resolved_rates({"gpt-5.6-sol"})
        self.assertIsNot(table["gpt-5.6-sol"], PRICING["gpt-5.6-sol"])

    def test_a_model_with_no_rates_is_left_out_rather_than_invented(self):
        self.assertEqual(pricing.resolved_rates({"gemma-3-27b"}), {})
        self.assertEqual(pricing.resolved_rates(set()), {})


class TestAnOverWideRateIsSkippedRatherThanFatal(_PricingTableRestored):
    """`load_rate_overrides` says "Never raises", and `cli.main` believes it.

    The validator ahead of the conversion rejects bools, non-numbers, negatives,
    NaN and both infinities — but a JSON integer has no width, and `float()` on
    one wider than a double raises. `cli.py` calls the loader unguarded before
    dispatching any command, so one over-wide figure anywhere in the user's own
    rates file killed `today` / `week` / `stats` / `scan` / `url` with a raw
    traceback and no report at all.

    Skipping the field is not a new rule — it is the one every other unusable
    value here already follows. What the raise added was an arbitrary cut: the
    models the loop had already reached kept their overrides and the ones behind
    it silently lost theirs, so which of a user's valid rates survived depended
    on the order they happen to appear in the file. `account._whole` bounds the
    identical hazard the identical way.
    """

    # json parses this at full width; float() cannot hold it (~4e399).
    WIDE = int("9" * 400)

    def test_an_over_wide_integer_does_not_raise(self):
        applied = self.apply_overrides({"gpt-5.6-sol": {"input": self.WIDE}})
        self.assertEqual(applied, set())

    def test_the_models_ahead_of_it_still_apply(self):
        """The rest of the file is not collateral damage."""
        sol = dict(PRICING["gpt-5.6-sol"])
        applied = self.apply_overrides({
            "claude-opus-4-8": {"input": 1.0, "output": 2.0},
            "gpt-5.6-sol": {"input": self.WIDE},
        })
        self.assertEqual(applied, {"claude-opus-4-8"})
        self.assertAlmostEqual(
            calc_cost("claude-opus-4-8", 1_000_000, 0, 0, 0), 1.0, places=9)
        self.assertEqual(PRICING["gpt-5.6-sol"], sol,
                         "the unusable rate must leave its model on the built-in "
                         "table, not on a partly-overwritten one")

    def test_the_models_behind_it_still_apply(self):
        """`applied` is built in file order; a raise took the tail with it."""
        applied = self.apply_overrides({
            "gpt-5.6-sol": {"input": self.WIDE},
            "claude-opus-4-8": {"input": 1.0},
        })
        self.assertEqual(applied, {"claude-opus-4-8"})

    def test_the_other_fields_of_the_same_model_still_apply(self):
        """Per-field, exactly as a negative or a NaN field is already treated."""
        before = dict(PRICING["gpt-5.6-sol"])
        applied = self.apply_overrides(
            {"gpt-5.6-sol": {"input": self.WIDE, "output": 10.0}})
        self.assertEqual(applied, {"gpt-5.6-sol"})
        self.assertEqual(PRICING["gpt-5.6-sol"]["output"], 10.0)
        self.assertEqual(PRICING["gpt-5.6-sol"]["input"], before["input"])

    def test_a_rate_a_double_can_hold_is_still_accepted(self):
        """The bound rejects what `float()` cannot represent, nothing narrower."""
        applied = self.apply_overrides(
            {"gpt-5.6-sol": {"input": int("1" + "0" * 300)}})
        self.assertEqual(applied, {"gpt-5.6-sol"})
        self.assertEqual(PRICING["gpt-5.6-sol"]["input"], 1e300)


class TestAnOverWideRateDoesNotKillTheCli(unittest.TestCase):
    """The observable half: `cli.main` loads the file before any command runs.

    Driven as a subprocess for the same reason the override tests in
    tests/test_pricing_overrides.py are: the promise being checked is that the
    report still prints, and only a real invocation can show that. Before the
    bound, `today` / `week` / `stats` / `scan` / `url` all exited 1 with the
    loader's traceback and printed nothing at all.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        conn = get_db(self.tmp / "usage.db")
        init_db(conn)
        conn.commit()
        conn.close()
        self.rates = self.tmp / "rates.json"
        self.rates.write_text(
            '{"gpt-5.6-sol": {"input": ' + "9" * 400 + "}}", encoding="utf-8")

    def test_stats_still_prints_its_report(self):
        env = dict(os.environ)
        # Pin both ends of the pipe: encoding= here, PYTHONIOENCODING in the
        # child. Without either, the pair falls back to locale.getencoding()
        # — cp1252 on windows-latest — and mixing the two raises
        # UnicodeDecodeError. No errors= : a mismatch should fail loudly.
        env["PYTHONIOENCODING"] = "utf-8"
        env["CODEX_CLAUDE_USAGE_DB"] = str(self.tmp / "usage.db")
        env["CODEX_CLAUDE_USAGE_RATES"] = str(self.rates)
        done = subprocess.run([sys.executable, "cli.py", "stats"], cwd=REPO_ROOT,
                              capture_output=True, text=True, encoding="utf-8",
                              env=env)
        self.assertNotIn("OverflowError", done.stderr)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("All-Time Statistics", done.stdout)


@requires_node
class TestUserSuppliedRatesReachThePage(_PricingTableRestored):
    """`CODEX_CLAUDE_USAGE_RATES` has to move the page's money, not only the CLI's.

    The dashboard computes every figure in the browser from its own copy of the
    table, so an override that reached `pricing.PRICING` and stopped there made
    `cli.py stats` print $1.0000 while the Est. Cost tile, the Cost by Model
    table and the CSV export showed $5.0000 for the identical turns — beneath a
    footer that tells the reader to set the variable that had just failed to
    work.

    These drive the page's real `applyRateOverrides` over exactly what
    `pricing.resolved_rates` produces from a real override file, so both ends of
    that wire are checked against each other rather than assumed.
    """

    def page(self, overrides, expr):
        """Apply `overrides` to the page's table, then evaluate `expr` in it."""
        return run_js(emit("(() => { applyRateOverrides(overrides); return "
                           + expr + "; })()", overrides=overrides))

    def load(self, table):
        """Load `table` as the user's rate file; return the server's injection."""
        return pricing.resolved_rates(self.apply_overrides(table))

    def test_the_page_bills_an_overridden_model_at_the_users_rate(self):
        injected = self.load({"gpt-5.6-sol": {"input": 1.0}})
        shown = self.page(injected, "calcCost('gpt-5.6-sol', 100000, 0, 0, 0)")
        self.assertAlmostEqual(
            shown, calc_cost("gpt-5.6-sol", 100_000, 0, 0, 0), places=9,
            msg="the page and the CLI quote different money for the same turn")
        self.assertAlmostEqual(shown, 0.1, places=9)

    def test_a_dated_id_of_an_overridden_model_follows_it(self):
        """The prefix tier has to see the injected key too — a dated id is the
        exact class of CLI/page divergence that has already shipped once."""
        injected = self.load({"gpt-5.6-sol": {"input": 1.0}})
        dated = "gpt-5.6-sol-20260215"
        shown = self.page(injected, "calcCost('" + dated + "', 100000, 0, 0, 0)")
        self.assertAlmostEqual(shown, calc_cost(dated, 100_000, 0, 0, 0),
                               places=9)
        self.assertAlmostEqual(shown, 0.1, places=9)

    def test_a_rate_the_user_supplied_is_no_longer_labelled_an_estimate(self):
        """Replacing the rates is not enough: `isEstimatedRate` matches by object
        identity, so the name has to leave ESTIMATED_RATE_MODELS exactly as it
        does in `load_rate_overrides`."""
        injected = self.load({"codex-auto-review": {"input": 2.0}})
        self.assertFalse(is_estimated("codex-auto-review"))
        self.assertEqual(
            self.page(injected,
                      "[isEstimatedRate('codex-auto-review'),"
                      " ESTIMATED_RATE_MODELS.slice()]"),
            [False, sorted(pricing.ESTIMATED_RATE_MODELS)])

    def test_an_overridden_local_model_becomes_billable_on_both_sides(self):
        """It read `n/a` in both before; a rate the user gave it must count."""
        injected = self.load({"my-local-llama-70b": {"output": 2.0}})
        shown = self.page(injected,
                          "[isBillable('my-local-llama-70b'),"
                          " calcCost('my-local-llama-70b', 1000000, 1000000, 0, 0)]")
        self.assertTrue(shown[0])
        self.assertAlmostEqual(
            shown[1], calc_cost("my-local-llama-70b", 1_000_000, 1_000_000, 0, 0),
            places=9)
        self.assertAlmostEqual(shown[1], 2.0, places=9)

    def test_the_untouched_models_keep_their_built_in_rates(self):
        injected = self.load({"gpt-5.6-sol": {"input": 1.0}})
        self.assertEqual(
            self.page(injected, "getPricing('claude-opus-5')"),
            {f: PRICING["claude-opus-5"][f] for f in PRICE_FIELDS})

    def test_no_override_leaves_the_page_exactly_as_it_was(self):
        """The overwhelmingly common case: nothing injected must be a no-op."""
        before = run_js(emit("[PRICING, ESTIMATED_RATE_MODELS.slice()]"))
        for empty in (None, {}, "not a table", []):
            with self.subTest(injected=empty):
                self.assertEqual(
                    self.page(empty, "[PRICING, ESTIMATED_RATE_MODELS.slice()]"),
                    before)

    def test_a_malformed_entry_is_ignored_rather_than_half_applied(self):
        """The same rule the file itself gets: a partly-read price table is a
        silently wrong bill, so an entry missing a field changes nothing."""
        before = run_js(emit("[PRICING, ESTIMATED_RATE_MODELS.slice()]"))
        for bad in ({"gpt-5.6-sol": {"input": 1.0}},          # four fields short
                    {"gpt-5.6-sol": {f: -1 for f in PRICE_FIELDS}},
                    {"gpt-5.6-sol": None},
                    {"codex-auto-review": {f: "1" for f in PRICE_FIELDS}}):
            with self.subTest(injected=bad):
                self.assertEqual(
                    self.page(bad, "[PRICING, ESTIMATED_RATE_MODELS.slice()]"),
                    before)

    def test_the_five_rate_fields_are_named_the_same_on_both_sides(self):
        self.assertEqual(run_js(emit("RATE_FIELDS.slice()")),
                         list(pricing.RATE_FIELDS))

    def test_a_magic_property_name_is_still_a_model_not_a_prototype(self):
        injected = self.load({"__proto__": {"input": 1.0}})
        encoded = json.dumps(json.dumps(injected))
        shown = run_js(
            "const specialRates = JSON.parse(" + encoded + ");\n"
            "applyRateOverrides(specialRates);\n"
            "console.log(JSON.stringify(["
            "Object.prototype.hasOwnProperty.call(PRICING, '__proto__'),"
            "Object.getPrototypeOf(PRICING) === null,"
            "calcCost('__proto__', 1000000, 0, 0, 0)]));"
        )
        self.assertEqual(shown[:2], [True, True])
        self.assertAlmostEqual(shown[2], 1.0, places=9)


@requires_node
class TestThePageAppliesWhatTheServerInjects(unittest.TestCase):
    """The last link: a page loaded with rates on APP_CONFIG must bill at them.

    Everything above calls `applyRateOverrides` by hand, which proves the
    function works and not that the page ever runs it — and the shared JS
    harness hard-codes its own APP_CONFIG, so nothing else can tell the
    difference. Deleting or commenting out the one call at the foot of
    10-pricing.js leaves every other test in this file green.
    """

    def test_a_page_loaded_with_injected_rates_bills_at_them(self):
        self.assertAlmostEqual(
            _run_page_with_app_config(
                {"version": "test", "surface": "web",
                 "rate_overrides": pricing.resolved_rates({"gpt-5.6-sol"})
                                   | {"gpt-5.6-sol": dict(PRICING["gpt-5.6-sol"],
                                                          input=1.0)}},
                "calcCost('gpt-5.6-sol', 100000, 0, 0, 0)"),
            0.1, places=9,
            msg="the page ignored the rates the server injected, so "
                "CODEX_CLAUDE_USAGE_RATES moves the CLI's money and not the page's")

    def test_a_page_loaded_without_them_bills_at_the_built_in_rate(self):
        self.assertAlmostEqual(
            _run_page_with_app_config({"version": "test", "surface": "web"},
                                      "calcCost('gpt-5.6-sol', 100000, 0, 0, 0)"),
            PRICING["gpt-5.6-sol"]["input"] / 10, places=9)

    def test_entry_transport_preserves_a_magic_model_name(self):
        rates = {field: (1.0 if field == "input" else 0.0)
                 for field in pricing.RATE_FIELDS}
        shown = _run_page_with_app_config(
            {"version": "test", "surface": "web",
             "rate_overrides": [["__proto__", rates]]},
            "[Object.prototype.hasOwnProperty.call(PRICING, '__proto__'),"
            " calcCost('__proto__', 1000000, 0, 0, 0)]",
        )
        self.assertEqual(shown[0], True)
        self.assertAlmostEqual(shown[1], 1.0, places=9)


class TestTheInjectedRatesAreAppliedAtLoad(unittest.TestCase):
    """The same wiring, checked as text so it still guards where node is absent.
    Anchored at the start of a line on purpose: commenting the call out is the
    cheap way to lose it, and an unanchored pattern still matches `// call();`.
    """

    def test_the_page_calls_apply_rate_overrides_at_load(self):
        self.assertRegex(
            dashboard.HTML_TEMPLATE,
            r"(?m)^applyRateOverrides\(APP_CONFIG\.rate_overrides\);$",
            "the page defines applyRateOverrides but never calls it with the "
            "rates the server injects, so CODEX_CLAUDE_USAGE_RATES moves the CLI's "
            "money and not the page's.",
        )


class TestOpenAiRateShape(unittest.TestCase):
    """Pin the published OpenAI pricing relationships as executable policy.

    The pricing page and Prompt Caching guide were checked 2026-09-29. Cached
    input is 10% of input. GPT-5.6+ cache writes cost 1.25x; earlier models have
    no additional write fee, so a disjoint write bucket costs ordinary input.
    """

    def _openai_models(self):
        return {name: rates for name, rates in PRICING.items()
                if name.startswith(("gpt-", "codex-"))}

    def test_the_table_actually_contains_openai_models(self):
        self.assertGreaterEqual(len(self._openai_models()), 8)

    def test_cached_input_is_a_tenth_of_input(self):
        for name, rates in sorted(self._openai_models().items()):
            with self.subTest(model=name):
                self.assertAlmostEqual(rates["cache_read"], rates["input"] * 0.10,
                                       places=6)

    def test_current_families_cache_writes_are_1_25x_input(self):
        for name, rates in sorted(self._openai_models().items()):
            if not name.startswith(("gpt-5.6-", "gpt-6-")):
                continue
            with self.subTest(model=name):
                self.assertAlmostEqual(rates["cache_write"], rates["input"] * 1.25,
                                       places=6)
                self.assertEqual(rates["cache_write_1h"], rates["cache_write"])

    def test_earlier_models_add_no_cache_write_surcharge(self):
        for name, rates in sorted(self._openai_models().items()):
            if name.startswith(("gpt-5.6-", "gpt-6-")):
                continue
            with self.subTest(model=name):
                self.assertAlmostEqual(rates["cache_write"], rates["input"],
                                       places=6)
                self.assertAlmostEqual(rates["cache_write_1h"], rates["input"],
                                       places=6)

    def test_output_costs_more_than_input(self):
        for name, rates in sorted(self._openai_models().items()):
            with self.subTest(model=name):
                self.assertGreater(rates["output"], rates["input"])

    def test_the_headline_rates_match_the_published_figures(self):
        """Pinned so a later edit cannot quietly drift them back to a guess."""
        for model, inp, cached, out in (
            ("gpt-6-astra",  10.00, 1.00,  50.00),
            ("gpt-6-sol",     2.00, 0.20,  10.00),
            ("gpt-6-luna",    0.10, 0.01,   0.50),
            ("gpt-5.6-sol",   4.00, 0.40,  20.00),
            ("gpt-5.6-terra", 2.00, 0.20,  12.00),
            ("gpt-5.6-luna",  0.20, 0.02,   1.20),
            ("gpt-5.4-mini",  0.75, 0.075,  4.50),
            ("gpt-5.3-codex", 1.75, 0.175, 14.00),
        ):
            with self.subTest(model=model):
                rates = PRICING[model]
                self.assertEqual(rates["input"], inp)
                self.assertEqual(rates["cache_read"], cached)
                self.assertEqual(rates["output"], out)

    def test_sol_policy_starts_at_the_verified_date_without_a_speculative_end(self):
        current = pricing.PRICING["gpt-5.6-sol"]
        self.assertEqual(
            pricing.get_pricing("gpt-5.6-sol", "2026-08-21T23:59:59Z"),
            {"input": 5.00, "output": 30.00, "cache_read": 0.50,
             "cache_write": 6.25, "cache_write_1h": 6.25},
        )
        self.assertEqual(
            pricing.get_pricing("gpt-5.6-sol", "2026-08-22T00:00:00Z"),
            current,
        )
        self.assertEqual(
            pricing.get_pricing("gpt-5.6-sol", "2026-11-22T00:00:00Z"),
            current,
        )

    def test_terra_and_luna_use_the_published_july_transition(self):
        for model, before in (
            ("gpt-5.6-terra", {
                "input": 2.50, "output": 15.00, "cache_read": 0.25,
                "cache_write": 3.125, "cache_write_1h": 3.125,
            }),
            ("gpt-5.6-luna", {
                "input": 1.00, "output": 6.00, "cache_read": 0.10,
                "cache_write": 1.25, "cache_write_1h": 1.25,
            }),
        ):
            with self.subTest(model=model):
                self.assertEqual(
                    pricing.get_pricing(model, "2026-07-29T23:59:59Z"),
                    before,
                )
                self.assertEqual(
                    pricing.get_pricing(model, "2026-07-30T00:00:00Z"),
                    pricing.PRICING[model],
                )
if __name__ == "__main__":
    unittest.main()
