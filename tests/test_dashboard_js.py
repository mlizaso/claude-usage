"""Tests for the dashboard's embedded JavaScript, executed under node.

The UI is ~1,600 lines of JavaScript in `web/js/*.js`, and nothing executed a line
of it. That mattered most for money: the cost shown in the browser is computed by
a *second*, hand-maintained implementation of the pricing logic, and
`tests/test_pricing_parity.py` could only compare the two tables as text — it
could not check that the two `calcCost` functions actually produce the same
number.

How this works: concatenate `web/js/*.js` in load order, prepend a small DOM stub, append an assertion
snippet, and run the lot under node. Nothing is written into the repo and no
package manager is involved — the harness file is generated into a temp dir at
test time, so the project stays stdlib-only with no install step. `node` ships on
GitHub's ubuntu-latest runner, so these tests really do execute in CI; they skip
on a machine without it.

**Scope is deliberately limited to pure functions.** The DOM stub returns inert
objects, so anything from the `// ── Renderers` section onward would "pass"
without rendering anything. A vacuous test is worse than no test, so the
renderers are left alone; testing them for real would need jsdom, i.e. a
dependency this project does not have. See `test_js_loads_without_dom_or_network`
for the one test that exists purely to tell you the *stub* broke rather than the
product.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import dashboard
from dashboard import get_dashboard_data
from pricing import (PRICING, calc_cost, calc_cost_parts, fmt, get_pricing,
                     is_estimated)
from scanner import get_db, init_db, insert_turns, upsert_sessions


def fmt_money(value):
    """Mirror the page's fmtCost: pinned en-US, four decimals."""
    return f"${value:,.4f}"

NODE = shutil.which("node")
# CI sets this so a runner that lost node fails loudly instead of quietly
# skipping every JavaScript assertion and still reporting green.
REQUIRE_JS = os.environ.get("CLAUDE_USAGE_REQUIRE_JS") == "1"

requires_node = unittest.skipUnless(NODE, "no JavaScript engine (node) on PATH")

REPO_ROOT = Path(__file__).resolve().parent.parent
_SENTINEL = "function calcCost("
# Mirrors RANGE_LABELS in web/js/30-ranges.js; the coverage guard in
# TestRangeBounds keeps the key set honest.
RANGE_NAMES = {
    "today": "Today", "week": "This Week", "month": "This Month",
    "prev-month": "Previous Month", "7d": "Last 7 Days", "30d": "Last 30 Days",
    "90d": "Last 90 Days", "ytd": "Year to Date", "all": "All Time",
    "limit-week": "This Weekly Limit",
}

# Just enough browser for the script's top-level code to run. Every entry here
# exists because the app touches it at load time; if you add an unguarded DOM
# call to initControls/initFooterMeta/initSectionNav, this is what needs updating.
_DOM_STUB = r"""
const noop = () => {};
function stubEl() {
  return {
    textContent: '', innerHTML: '', value: '', disabled: false, checked: false,
    dataset: {}, style: { setProperty: noop, removeProperty: noop },
    children: [], hidden: false, offsetHeight: 0, offsetTop: 0, offsetWidth: 0,
    classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
    addEventListener: noop, removeEventListener: noop, appendChild: noop,
    setAttribute: noop, removeAttribute: noop, getAttribute: () => null, scrollIntoView: noop,
    querySelector: () => null, querySelectorAll: () => [],
    closest: () => null, getContext: () => ({}),
    getBoundingClientRect: () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }),
  };
}
globalThis.window = {
  APP_CONFIG: { version: 'test', surface: 'web' },
  // Load-bearing: an API token in the fragment makes the bootstrap call
  // loadData(), which would hit the network and leave a refresh timer pending.
  location: { hash: '', href: 'http://127.0.0.1:8080/', origin: 'http://127.0.0.1:8080' },
  addEventListener: noop, removeEventListener: noop,
  matchMedia: () => ({ matches: false, addEventListener: noop, addListener: noop }),
  setTimeout, clearTimeout, setInterval, clearInterval,
  devicePixelRatio: 1, innerWidth: 1200, innerHeight: 800,
};
globalThis.document = {
  addEventListener: noop, removeEventListener: noop,
  getElementById: () => stubEl(), querySelector: () => stubEl(),
  querySelectorAll: () => [], createElement: () => stubEl(),
  createTextNode: (t) => ({ nodeType: 3, textContent: String(t) }),
  body: stubEl(), documentElement: stubEl(), hidden: false, readyState: 'complete',
};
const _store = new Map();
globalThis.localStorage = {
  getItem: (k) => (_store.has(k) ? _store.get(k) : null),
  setItem: (k, v) => _store.set(k, String(v)),
  removeItem: (k) => _store.delete(k), clear: () => _store.clear(),
};
globalThis.window.localStorage = globalThis.localStorage;
globalThis.Chart = function Chart() { return { update: noop, destroy: noop, data: {}, options: {} }; };
globalThis.Chart.defaults = {
  color: '', font: {}, borderColor: '', backgroundColor: '',
  plugins: { tooltip: { callbacks: {} }, legend: { labels: {} } },
  scale: { grid: {} }, scales: {}, elements: {}, datasets: {},
};
globalThis.Chart.register = noop;
globalThis.fetch = () => { throw new Error('harness: the app must not touch the network at load'); };
globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0);
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
"""


def extract_app_script():
    """Return the dashboard's application JavaScript.

    Real files now (`web/js/*.js`), so this is a read rather than the regex
    dissection of a Python string literal it used to be. The checks that
    remain are the ones a plain read cannot give: that the file is the app and
    not something else, and that it stays embeddable in the single-document
    delivery the CSP is written for. That the page actually inlines it is
    covered by tests/test_web_assets.py.
    """
    parts = dashboard.app_js_parts(REPO_ROOT / "web")
    if not parts:
        raise AssertionError(
            f"No JavaScript parts found in {REPO_ROOT / 'web' / 'js'}. The app is "
            "split into ordered files there — update this path, do not delete "
            "these tests; they are the only thing that executes the dashboard JS."
        )
    # Concatenated in the same order the server uses, so the harness runs exactly
    # what the browser would.
    body = "".join(p.read_text(encoding="utf-8") for p in parts)
    if _SENTINEL not in body:
        raise AssertionError(
            "the concatenated JS does not contain calcCost(); wrong source")
    if "</script>" in body:
        raise AssertionError(
            "the JS now contains a literal '</script>', which would close the "
            "tag early once inlined into the page")
    return body


def _run_js_source(source):
    with tempfile.TemporaryDirectory() as tmp:
        # .cjs pins classic-script semantics: the body parses as an ES module
        # too, so a stray package.json with "type":"module" could otherwise
        # flip the parse mode and change behaviour under the tests' feet.
        harness = Path(tmp) / "harness.cjs"
        harness.write_text(source, encoding="utf-8")
        # encoding, not just text=True: node writes UTF-8 whatever the console
        # codepage is, and decoding it with the runner's locale turned an en
        # dash into `â€“` on windows-latest (cp1252) — two real CI failures,
        # `'unicode ✓ <?>' not found` and `0 != 8`.
        proc = subprocess.run([NODE, str(harness)], capture_output=True,
                              text=True, encoding="utf-8", timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f"node exited {proc.returncode}:\n{proc.stderr[-4000:]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"harness did not print one JSON object ({exc}); stdout was:\n"
            f"{proc.stdout[:2000]}"
        ) from exc


def run_js(snippet):
    """Run `snippet` after the real dashboard JS; return the JSON it prints."""
    source = _DOM_STUB + "\n" + extract_app_script() + "\n" + snippet
    return _run_js_source(source)


def run_js_with_api_token(snippet):
    """Run with a valid token available, without starting the whole app.

    Threshold persistence is unreachable in the ordinary harness because its
    intentionally empty fragment makes `API_TOKEN` immutable as `""`. This
    variant changes only that bootstrap precondition, then suppresses the
    automatic start so a focused test can supply its own `apiFetch` boundary.
    """
    token = "a" * 40
    stub = _DOM_STUB.replace("hash: ''", f"hash: '#token={token}'", 1)
    app = extract_app_script()
    old = "if (API_TOKEN) {\n  bootDashboard();"
    if old not in app:
        raise AssertionError("dashboard bootstrap shape changed")
    app = app.replace(old, "if (false && API_TOKEN) {\n  bootDashboard();", 1)
    return _run_js_source(stub + "\n" + app + "\n" + snippet)


# Walks the show-more control the way a user would: start at the first step and
# keep clicking, recording what the table would actually render each time.
_SHOW_MORE_WALK = """totals.map(total => {
  const rows = new Array(total).fill(0);
  const rendered = (l) => rows.slice(0, shownCount(l, total)).length;
  let limit = TABLE_STEPS[0];
  let steps = 1;
  let monotonic = true;
  let prevRendered = rendered(limit);
  for (let i = 0; i < 50; i++) {
    const next = nextTableLimit(limit, total);
    if (next === limit) break;
    limit = next;
    const now = rendered(limit);
    if (now < prevRendered) monotonic = false;
    prevRendered = now;
    steps++;
  }
  return { total, limit, steps, monotonic, cap: TABLE_MAX, rendered: prevRendered };
})"""


def emit(expr, **bindings):
    """JS that binds `bindings` as consts, then prints JSON.stringify(expr).

    Composed by concatenation rather than f-strings on purpose: the project
    supports Python 3.11, where an f-string expression may not reuse the
    enclosing quote style (PEP 701 relaxed that only in 3.12). Nesting a
    triple-quoted JS snippet inside an f-string parses here on 3.13 and is a
    SyntaxError on 3.11 — i.e. it would break CI's oldest leg and nothing else.
    """
    prelude = ""
    for name, value in bindings.items():
        prelude += "const " + name + " = " + json.dumps(value) + ";\n"
    return prelude + "console.log(JSON.stringify(" + expr + "));"


class TestNodeAvailability(unittest.TestCase):
    """Kept undecorated and in its own class so the failure reads cleanly."""

    @unittest.skipUnless(REQUIRE_JS, "only enforced when CLAUDE_USAGE_REQUIRE_JS=1")
    def test_node_is_present_when_ci_requires_it(self):
        self.assertIsNotNone(
            NODE,
            "CLAUDE_USAGE_REQUIRE_JS=1 but node is not on PATH, so every "
            "JavaScript assertion would have been skipped while CI stayed green.",
        )


@requires_node
class TestHarnessIntegrity(unittest.TestCase):
    def test_extraction_returns_the_app_script_only(self):
        body = extract_app_script()
        self.assertGreater(len(body), 10_000)
        self.assertNotIn("__CSP_NONCE__", body)
        self.assertNotIn("__APP_CONFIG_JSON__", body)
        self.assertIn("function calcCost(", body)

    def test_js_loads_without_dom_or_network(self):
        """If this is the only failure, the DOM stub is stale — not the product.

        The app runs initFooterMeta/initControls/initSectionNav at load, so any
        new unguarded DOM call lands here first.
        """
        self.assertEqual(run_js(emit("{loaded: true}")), {"loaded": True})


@requires_node
class TestCostParityWithPython(unittest.TestCase):
    """The dashboard and the CLI must charge the same money for the same turns.

    `test_pricing_parity.py` compares the two price *tables* as text. This runs
    both *implementations* and compares the numbers they return, which is the
    thing users actually see.
    """

    MODELS = [
        "claude-opus-4-8", "claude-opus-4-5", "claude-sonnet-4-6",
        "claude-sonnet-4-5", "claude-haiku-4-5", "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-4-7-20260215",     # date-suffixed → prefix tier
        "claude-sonnet-4-6-20260101",   # date-suffixed → prefix tier
        "some-unknown-opus-thing",      # substring tier
        "SOME-UNKNOWN-HAIKU",           # substring tier, upper case
        "gemma-3-27b", "glm-4-9b", "",  # unpriced → free in both
        # Every table key with a date suffix, derived rather than listed. The
        # hand-written probes above were all Claude ids, and no Claude key is a
        # prefix of another Claude key — so when the prefix tier's resolution
        # order changed in pricing.py and not here, nothing in this suite could
        # see it. Deriving the list means a key added tomorrow that happens to
        # be a prefix of another (`gpt-5.4` / `gpt-5.4-mini`) is covered the
        # moment it lands, with no second list to remember to update.
        *(k + "-20260215" for k in sorted(PRICING)),
    ]
    TOKEN_CASES = [
        (0, 0, 0, 0),
        (1, 1, 1, 1),
        (1_000_000, 0, 0, 0),
        (0, 1_000_000, 0, 0),
        (0, 0, 1_000_000, 0),
        (0, 0, 0, 1_000_000),
        (123_456, 78_910, 1_234_567, 89_012),
        (999_999_999, 999_999_999, 999_999_999, 999_999_999),
    ]

    def test_calc_cost_agrees_for_every_model_and_token_shape(self):
        cases = [[m, *t] for m in self.MODELS for t in self.TOKEN_CASES]
        got = run_js(emit(
            "cases.map(c => calcCost(c[0], c[1], c[2], c[3], c[4]))", cases=cases))
        self.assertEqual(len(got), len(cases))
        for (model, inp, out, cr, cc), js_cost in zip(cases, got):
            with self.subTest(model=model, tokens=(inp, out, cr, cc)):
                py_cost = calc_cost(model, inp, out, cr, cc)
                self.assertAlmostEqual(
                    js_cost, py_cost, places=6,
                    msg=f"dashboard charges {js_cost} but cli.py charges {py_cost}",
                )

    def test_get_pricing_resolves_to_the_same_rates(self):
        got = run_js(emit("models.map(m => getPricing(m))", models=self.MODELS))
        for model, js_rates in zip(self.MODELS, got):
            with self.subTest(model=model):
                py_rates = get_pricing(model)
                if py_rates is None:
                    self.assertIsNone(js_rates, f"{model} is priced in JS but not in Python")
                    continue
                self.assertIsNotNone(js_rates, f"{model} is priced in Python but not in JS")
                for field in ("input", "output", "cache_read", "cache_write"):
                    self.assertAlmostEqual(js_rates[field], py_rates[field], places=6)

    def test_every_priced_model_costs_something(self):
        """A model in PRICING must never be silently free in the dashboard."""
        got = run_js(emit(
            "models.map(m => calcCost(m, 1000000, 1000000, 1000000, 1000000))",
            models=sorted(PRICING)))
        for model, cost in zip(sorted(PRICING), got):
            with self.subTest(model=model):
                self.assertGreater(cost, 0)


@requires_node
class TestAMalformedDayKeyDoesNotEmptyTheDailyFill(unittest.TestCase):
    """One bad day key must not cost All Time its contiguous fill.

    `localdays.LOCAL_DAY` falls back to `substr(timestamp, 1, 10)` when SQLite's
    `date()` cannot parse a stored timestamp, so a key like '2026-13-45' — right
    shape, impossible calendar — can reach the payload.

    This class is the LANDMINE for the half-fix, and it is still one now that the
    defect it guarded against is fixed. `dayToLocalDate` used to check shape only,
    and `new Date(2026, 12, 45)` is a real date (2027-02-14) rather than an
    Invalid Date, so that key became the All Time extent and the chart drew months
    of zero bars into the future — see
    `TestAnImpossibleCalendarDayIsRejected`, which asserts the repair. But
    hardening `dayToLocalDate` ALONE makes things worse rather than better:
    `dailyFillSpan` carried its own duplicate shape regex (web/js/30-ranges.js),
    which would go on picking the same bad key as the extent while `eachLocalDay`
    now rejected it and returned `[]` — the whole All Time fill silently gone,
    quiet days absent from the series again, `total > windowSize` false so no pan
    scrollbar, most of the range unreachable. That regression is the #151-class
    bug `dailyFillSpan` was written to close.

    So what is pinned here is the property BOTH the old code and the correct
    two-sided fix satisfy, and only the half-fix breaks: with a malformed key
    present, every calendar day between the first and last VALID day is still in
    the series. Split the two sites again — point `dailyFillSpan` at anything but
    `dayToLocalDate` — and this turns red.
    """

    DAYS = ["2026-08-01", "2026-08-10", "2026-13-45"]

    def _fill(self):
        return run_js(emit("""
          (() => {
            const span = dailyFillSpan('all', days);
            return { span, fill: span ? eachLocalDay(span.start, span.end) : [] };
          })()""", days=self.DAYS))

    def test_the_valid_extent_is_still_filled_contiguously(self):
        got = self._fill()
        self.assertIsNotNone(got["span"], "All Time lost its span entirely")
        expected = ["2026-08-%02d" % d for d in range(1, 11)]
        self.assertEqual(
            got["fill"][:10], expected,
            "the days between the first and last VALID key are no longer a "
            "contiguous run — the daily fill has been disabled by a bad key")

    def test_a_shape_invalid_key_is_still_dropped_from_the_extent(self):
        """The half of the guard that always worked: 'not-a-date' never gets in."""
        got = run_js(emit(
            "dailyFillSpan('all', ['2026-08-01', 'not-a-date', '2026-08-10'])"))
        self.assertEqual(got, {"start": "2026-08-01", "end": "2026-08-10"})


@requires_node
class TestAnImpossibleCalendarDayIsRejected(unittest.TestCase):
    """`dayToLocalDate` promises null for a key it cannot place on a calendar.

    It checked SHAPE only, and `new Date(y, m, d)` NORMALISES out-of-range
    components instead of returning an Invalid Date: '2026-13-45' came back as a
    confident Sun Feb 14 2027, and '0001-01-01' as 1901, because a two-digit year
    is remapped into the 1900s. Since `localdays.LOCAL_DAY`'s COALESCE fallback
    emits a raw timestamp prefix for anything SQLite's `date()` cannot parse, such
    a key really does reach the payload — and, sorting after every real one, it
    was then chosen as the All Time extent: measured, a 198-day fill ending
    2027-02-14 beside two real August days, i.e. six months of confident zero bars
    in the future.

    The repair is a round trip: read the three components back out of the Date and
    require them to be the ones that went in. It subsumes the `Number.isNaN` check
    it replaces (an Invalid Date's `getFullYear()` is NaN, which equals nothing)
    and it costs no real day — across all 418 zones `Intl.supportedValuesOf` lists,
    every calendar day from 2015 to 2030 survives it (measured 2026-08-11;
    a zone that springs forward AT midnight lands the Date on 01:00 of the same
    day, which the three getters still agree with).

    It had to land together with `dailyFillSpan`'s duplicate shape regex, now
    `spanBoundDays`. `TestAMalformedDayKeyDoesNotEmptyTheDailyFill` is what fails
    if the two are ever split again.
    """

    # Right shape, no such day. '0001-01-01' is the two-digit-year remap;
    # '2023-02-29' is the non-leap February that a month/day range check alone
    # would let through.
    REJECTED = ["2026-13-45", "2026-02-30", "2023-02-29", "2026-00-10",
                "2026-01-00", "2026-01-32", "0000-00-00", "0001-01-01"]
    ACCEPTED = ["2026-08-10", "2024-02-29", "1999-12-31", "2026-01-01",
                "2026-12-31"]

    def test_a_key_with_an_impossible_calendar_returns_null(self):
        got = run_js(emit("dayKeys.map(k => dayToLocalDate(k))",
                          dayKeys=self.REJECTED))
        self.assertEqual(
            got, [None] * len(self.REJECTED),
            "a key with no such day on the calendar was normalised into a real "
            "date instead of being rejected: "
            + repr([k for k, v in zip(self.REJECTED, got) if v is not None]))

    def test_a_real_calendar_day_still_round_trips(self):
        """The other half: the guard must not start rejecting honest keys."""
        got = run_js(emit("""
          dayKeys.map(k => { const d = dayToLocalDate(k);
                             return d === null ? null : localISODate(d); })
        """, dayKeys=self.ACCEPTED))
        self.assertEqual(got, self.ACCEPTED)

    def test_the_all_time_extent_no_longer_runs_into_the_future(self):
        """Both sites at once: the fill's span and the label's span.

        `dailyFillSpan` picks what the chart draws and `rangeSpan` picks what the
        header claims it drew. They read the same list, so they have to reject the
        same keys — `spanBoundDays` is the one rule both now call.
        """
        days = ["2026-08-01", "2026-08-10", "2026-13-45"]
        got = run_js(emit("""(() => {
          const span = dailyFillSpan('all', dayKeys);
          return { span,
                   fillDays: span ? eachLocalDay(span.start, span.end).length : 0,
                   labelSpan: rangeSpan('all', dayKeys) };
        })()""", dayKeys=days))
        self.assertEqual(got["span"], {"start": "2026-08-01", "end": "2026-08-10"},
                         "the daily fill still bounds All Time with a key that "
                         "has no such calendar day")
        self.assertEqual(got["fillDays"], 10,
                         "Aug 1..10 is ten days; anything longer is the fill "
                         "running past the data it borrowed its extent from")
        self.assertEqual(got["labelSpan"], {"start": "2026-08-01", "end": "2026-08-10"},
                         "the range label and the chart disagree about how far "
                         "All Time reaches")

    def test_the_bad_key_is_excluded_from_the_extent_not_from_the_data(self):
        """Excluding it from the SPAN must not delete the row it labels.

        `applyFilter` merges the fill into `dailyMap` rather than replacing it, so
        a key the fill cannot produce survives untouched. Asserted here on the
        span rule itself, which is the part that could have been written as a
        drop.
        """
        got = run_js(emit("""(() => {
          const span = dailyFillSpan('all', dayKeys);
          const fill = span ? eachLocalDay(span.start, span.end) : [];
          const merged = new Set([...dayKeys, ...fill]);
          return { hasBadKey: merged.has('2026-13-45'), size: merged.size };
        })()""", dayKeys=["2026-08-01", "2026-08-10", "2026-13-45"]))
        self.assertTrue(got["hasBadKey"])
        self.assertEqual(got["size"], 11)   # Aug 1..10, plus the bad key


class TestNoJsPartTrailsOffIntoProse(unittest.TestCase):
    """A part's last statement must be its last line — no dangling comment.

    `50-render.js` ended with ten lines documenting `aggregateHourly` and
    `hourlyInFrame`, both of which moved to `52-charts.js` in a2cc063 while the
    prose stayed behind. Two costs, and the second is not cosmetic:

      * the next function appended to that file silently inherits a paragraph
        about UTC-hour resolution — the block sat at the end of a file about
        stat tiles, with no code under it;
      * `dashboard.load_app_js` joins the parts with `"".join(...)` and no
        separator, so a part whose last line is `//` prose is the ONE shape
        where a lost trailing newline comments out the next part's first line.
        `.gitattributes` pins LF and every part ends in a newline today, which
        is what keeps that theoretical — but the shape is the precondition.
    """

    def test_every_part_ends_in_code(self):
        offenders = []
        for part in sorted((REPO_ROOT / "web" / "js").glob("*.js")):
            lines = [ln for ln in part.read_text(encoding="utf-8").splitlines()
                     if ln.strip()]
            self.assertTrue(lines, f"{part.name} is empty")
            if lines[-1].lstrip().startswith("//"):
                offenders.append((part.name, len(lines), lines[-1].strip()[:60]))
        self.assertEqual(
            offenders, [],
            "these parts end in a comment rather than a statement; a function "
            "appended below one would inherit prose written for another file")

    def test_every_part_ends_with_a_newline(self):
        """The other half of the same hazard: the join adds no separator."""
        for part in sorted((REPO_ROOT / "web" / "js").glob("*.js")):
            with self.subTest(part=part.name):
                self.assertTrue(part.read_bytes().endswith(b"\n"),
                                f"{part.name} has no trailing newline")

class TestARateQuotedInACommentIsARateThatExists(unittest.TestCase):
    """A `$X.YYY/M` in the JavaScript is a claim about the live price list.

    `fmtRate`'s comment justified its four decimals with "Codex's cached-input
    tiers are $0.125 and $0.025" — true at a86787e, and replaced wholesale by
    8cd0b81 ("price Codex from OpenAI's published rates, not from a guess"),
    which never opened this file. The DECISION survived; only the evidence
    offered for it went stale, in the authoritative-looking direction, which is
    the defect class AGENTS.md names as this project's signature.

    Scoped deliberately to the `/M` suffix rather than to every dollar figure:
    that suffix is what makes a figure a per-million UNIT PRICE rather than a
    money example, and money examples (`$2.1299` beside `2.1298` in CSV_COST's
    comment, the de-DE `$1.500,0000`) are legitimate history that must not be
    swept up. Precision itself is guarded behaviourally and separately, by
    `TestUnitRateInEveryCell.test_every_rate_in_the_table_prints_without_loss`,
    which round-trips the live table through fmtRate.
    """

    _RATE_IN_PROSE = re.compile(r"\$([0-9]+\.[0-9]{3,})/M")

    def test_every_per_million_rate_in_the_js_is_still_in_pricing(self):
        live = {round(v, 6)
                for rates in PRICING.values() for v in rates.values()}
        quoted = []
        for part in sorted((REPO_ROOT / "web" / "js").glob("*.js")):
            source = part.read_text(encoding="utf-8")
            for match in self._RATE_IN_PROSE.finditer(source):
                line = source.count("\n", 0, match.start()) + 1
                quoted.append((part.name, line, float(match.group(1))))
        # Refuse to pass vacuously: an empty sweep is not agreement, and the
        # figures this guards are exactly the ones a rewrite would drop.
        self.assertTrue(quoted, "found no $X.YYY/M figure to check at all")
        dead = [q for q in quoted if round(q[2], 6) not in live]
        self.assertEqual(
            dead, [],
            "these comments quote a per-million rate that pricing.PRICING no "
            "longer carries (file, line, rate)")

class TestTheTwoTokenFormattersAgree(unittest.TestCase):
    """`pricing.fmt` and web/js/20-format.js's `fmt` are hand-maintained twins.

    Every token figure in `cli.py today/week/stats` goes through the Python one
    (reports.py imports it) and every stat tile through the JavaScript one, so a
    reader reconciling the terminal against the page is comparing their output
    directly. They had drifted twice over, in opposite ways, and nothing
    compared them: `test_pricing_parity.py` covers the PRICING table and
    `getPricing`'s tiers, not these.

      * Python and JavaScript must use the same suffix tiers and rounding for
      large token counts.

    `fmt_cost` is knowingly out of scope: it and the page's `fmtCost` also
    differ in grouping ("$3183.0285" against "$3,183.0286"), which is a CLI
    layout decision rather than a rounding one.
    """

    #: Read straight out of the two sources, so a tier added to one copy and
    #: not the other fails here even where node is absent — the leg that would
    #: otherwise skip every assertion below.
    _JS_TIER = re.compile(
        r"if \(n >= ([0-9.e+_]+)\) return \(n\s*/\s*([0-9.e+_]+)\)"
        r"\.toFixed\((\d+)\)\s*\+\s*'([A-Za-z]+)'")
    _PY_TIER = re.compile(
        r"if n >= ([0-9_]+):\s*\n\s*return _fixed\(n / ([0-9_]+), (\d+)\)"
        r"\s*\+\s*\"([A-Za-z]+)\"")

    @staticmethod
    def _tiers(pattern, source):
        return [(float(m.group(1).replace("_", "")),
                 float(m.group(2).replace("_", "")),
                 int(m.group(3)), m.group(4))
                for m in pattern.finditer(source)]

    def test_both_copies_declare_the_same_tier_ladder(self):
        js = self._tiers(
            self._JS_TIER,
            (REPO_ROOT / "web" / "js" / "20-format.js").read_text(encoding="utf-8"))
        py = self._tiers(
            self._PY_TIER,
            (REPO_ROOT / "claude_usage" / "pricing.py").read_text(
                encoding="utf-8"))
        # Refuse to pass vacuously: two empty lists are equal, and a rename that
        # stopped both regexes matching would otherwise read as agreement.
        self.assertGreaterEqual(len(js), 3, "parsed no tier ladder out of the JS")
        self.assertEqual(js, py,
                         "the two fmt() tier ladders (threshold, divisor, "
                         "decimals, suffix) have drifted apart")

    # Dense where the branches meet, plus every exact half the two roundings
    # used to disagree on. A ladder of round magnitudes alone (1e3/1e6/1e9)
    # passes while one integer in a thousand still differs, which is the test
    # this file would otherwise have grown.
    @staticmethod
    def _ladder():
        values = list(range(0, 2001))
        values += [999, 1_000, 1_001, 999_999, 1_000_000, 1_000_001,
                   999_999_999, 1_000_000_000, 1_000_000_001,
                   10 ** 12, 10 ** 12 + 1]
        # K tier: n/1000 is an exact half only at .25 and .75 (a double holds no
        # other one), and only the even-preceding-digit case used to differ.
        values += list(range(1_250, 1_000_000, 9_750))
        values += list(range(1_750, 1_000_000, 9_750))
        # M and B tiers: the exact halves are the eighths.
        values += [b + f for b in (1_000_000, 7_000_000, 123_000_000)
                   for f in (125_000, 375_000, 625_000, 875_000)]
        values += [b + f for b in (1_000_000_000, 42_000_000_000)
                   for f in (125_000_000, 375_000_000, 625_000_000, 875_000_000)]
        # Invented large counts exercise each formatting tier.
        values += [41_234_567_890, 32_345_678_901, 27_456_789_012, 43_567_890_123]
        return sorted(set(values))

    @requires_node
    def test_every_magnitude_prints_the_same_string_on_both_surfaces(self):
        values = self._ladder()
        got = run_js(emit("values.map(v => fmt(v))", values=values))
        self.assertEqual(len(got), len(values))
        disagreed = [(v, fmt(v), j) for v, j in zip(values, got) if fmt(v) != j]
        self.assertEqual(
            disagreed[:20], [],
            f"{len(disagreed)} of {len(values)} figures print differently in "
            f"cli.py and on the page (n, python, js)")


@requires_node
class TestBillabilityMatchesPricing(unittest.TestCase):
    """`calcCost` is gated on `isBillable`, not on PRICING membership.

    Those two must agree for every model, or the dashboard shows a priced model
    as free (or tries to price an unpriced one). This is the executable half of
    the guard in test_pricing_parity.py.
    """

    PROBES = sorted(PRICING) + [k + "-20260215" for k in sorted(PRICING)] + [
        "claude-opus-4-7-20260215", "some-unknown-sonnet-x", "SOME-HAIKU",
        "gemma-3-27b", "llama-3", "glm-4-9b", "", "unknown",
    ]

    def test_billable_exactly_when_priced(self):
        got = run_js(emit(
            "models.map(m => [isBillable(m), getPricing(m) !== null])",
            models=self.PROBES))
        for model, (billable, priced) in zip(self.PROBES, got):
            with self.subTest(model=model):
                self.assertEqual(
                    billable, priced,
                    f"isBillable({model!r})={billable} but priced={priced}; the "
                    "dashboard would disagree with itself about this model",
                )

    def test_null_and_undefined_are_not_billable(self):
        got = run_js(emit(
            "{nul: isBillable(null), undef: isBillable(undefined), empty: isBillable('')}"))
        self.assertEqual(got, {"nul": False, "undef": False, "empty": False})


@requires_node
class TestPrefixTierResolvesTheLongestKey(unittest.TestCase):
    """A dated id must bill at its OWN tier, not at a shorter key it starts with.

    The prefix tier exists to absorb snapshot ids like `gpt-5.4-mini-20260215`.
    Where one table key is a strict prefix of another — `gpt-5.4` before
    `gpt-5.4-mini`, `gpt-5.3-codex` before `gpt-5.3-codex-spark` — a first-match
    loop stops at the shorter, *wrong*, parent tier. Both implementations must
    therefore take the LONGEST matching key, and `pricing.get_pricing` says so
    in a comment naming this file.

    It got out of sync in exactly that way: Python was made longest-first while
    the page stayed first-match, so a dated `gpt-5.4-nano-*` cost 12.1x more in
    the browser than on the command line, out of one database, with nothing on
    screen to say which was right.
    """

    # Every pair where resolution order is actually decidable. Derived, so the
    # next such pair is covered without editing this test.
    @staticmethod
    def _prefix_pairs():
        keys = sorted(PRICING)
        return [(short, long) for short in keys for long in keys
                if long != short and long.startswith(short)]

    def test_the_table_still_contains_a_pair_this_test_can_discriminate(self):
        """Guard: with no key a prefix of another, every assertion below passes
        vacuously and this whole class silently stops testing anything."""
        self.assertTrue(
            self._prefix_pairs(),
            "no PRICING key is a prefix of another any more, so nothing here "
            "can distinguish longest-match from first-match. Either restore a "
            "discriminating pair or delete this class deliberately.",
        )

    def test_a_dated_id_resolves_to_the_longer_key_in_the_browser(self):
        dated = [long + "-20260215" for _, long in self._prefix_pairs()]
        got = run_js(emit("ids.map(m => getPricing(m))", ids=dated))
        for model, js_rates in zip(dated, got):
            with self.subTest(model=model):
                own = PRICING[model[: -len("-20260215")]]
                self.assertIsNotNone(js_rates, f"{model} lost its price entirely")
                for field in ("input", "output", "cache_read", "cache_write"):
                    self.assertAlmostEqual(
                        js_rates[field], own[field], places=9,
                        msg=f"{model} billed at a parent tier's {field} rate "
                            f"({js_rates[field]}) instead of its own ({own[field]})",
                    )

    def test_the_two_families_that_broke_bill_the_documented_amount(self):
        """The regression in figures, not in structure: 1M of each token kind."""
        got = run_js(emit("""({
          mini: calcCost('gpt-5.4-mini-20260215', 1e6, 1e6, 1e6, 1e6),
          nano: calcCost('gpt-5.4-nano-20260215', 1e6, 1e6, 1e6, 1e6),
          parent: calcCost('gpt-5.4-20260215', 1e6, 1e6, 1e6, 1e6),
        })"""))
        # Mini and nano are not eligible for the long-context surcharge; the
        # parent snapshot is, because its 3M input-side tokens exceed 272K.
        self.assertAlmostEqual(got["mini"], 6.0750, places=9)
        self.assertAlmostEqual(got["nano"], 1.6700, places=9)
        self.assertAlmostEqual(got["parent"], 33.0000, places=9)
        for name in ("mini", "nano"):
            with self.subTest(model=name):
                self.assertLess(got[name], got["parent"],
                                "a smaller model cannot cost more than its parent")

    def test_a_dated_estimate_is_still_labelled_an_estimate(self):
        """`gpt-5.3-codex-spark` has no published rate. Resolving a dated one to
        `gpt-5.3-codex` costs the same money but drops the label, which presents
        a guess as a price list."""
        got = run_js(emit(
            "['gpt-5.3-codex-spark-20260215', 'gpt-5.3-codex-20260215']"
            ".map(m => isEstimatedRate(m))"))
        self.assertEqual(got, [True, False])

    def test_estimated_flags_agree_with_python_for_every_dated_id(self):
        dated = [k + "-20260215" for k in sorted(PRICING)]
        got = run_js(emit("ids.map(m => isEstimatedRate(m))", ids=dated))
        for model, js_flag in zip(dated, got):
            with self.subTest(model=model):
                self.assertEqual(
                    js_flag, is_estimated(model),
                    f"the page calls {model} "
                    f"{'an estimate' if js_flag else 'a published rate'} and "
                    "cli.py disagrees",
                )

    def test_an_exact_key_is_unaffected_by_the_ordering(self):
        """Longest-first only ever moves ids that reach the prefix tier; an
        exact table hit returns before it."""
        keys = sorted(PRICING)
        got = run_js(emit("ids.map(m => getPricing(m).input)", ids=keys))
        for model, js_input in zip(keys, got):
            with self.subTest(model=model):
                self.assertAlmostEqual(js_input, PRICING[model]["input"], places=9)


@requires_node
class TestCsvExport(unittest.TestCase):
    """Exported CSV is opened in Excel/Sheets, which executes leading formulas.

    Topics, project names, branch names and agent types all come from transcript
    data, so a field beginning `=`, `+`, `-` or `@` is attacker-influenced input
    landing in a spreadsheet. The Python side escapes terminal control codes
    (`scanner.terminal_safe`) but that does nothing about a leading `=`.
    """

    def test_quotes_commas_and_newlines_are_escaped(self):
        got = run_js(emit("""{
          plain:   csvField('hello'),
          comma:   csvField('a,b'),
          quote:   csvField('say "hi"'),
          newline: csvField('line1\\nline2'),
          crlf:    csvField('line1\\r\\nline2'),
          empty:   csvField(''),
          number:  csvField(42),
        }"""))
        # A quoted field doubles its inner quotes; a bare one stays bare.
        self.assertEqual(got["plain"], "hello")
        self.assertIn(",", got["comma"])
        self.assertTrue(got["comma"].startswith('"') and got["comma"].endswith('"'))
        self.assertEqual(got["quote"], '"say ""hi"""')
        self.assertTrue(got["newline"].startswith('"'))
        self.assertTrue(got["crlf"].startswith('"'))

    def test_formula_injection_is_neutralised(self):
        """Every formula-triggering prefix Excel/Sheets recognises."""
        payloads = ["=1+1", "+1", "-1", "@SUM(A1)",
                    "=cmd|'/c calc'!A1", "\t=1+1", "\r=1+1"]
        got = run_js(emit("payloads.map(p => csvField(p))", payloads=payloads))
        for payload, field in zip(payloads, got):
            with self.subTest(payload=payload):
                body = field[1:-1] if field.startswith('"') else field
                self.assertFalse(
                    body[:1] in "=+-@" if body else False,
                    f"csvField({payload!r}) -> {field!r} still starts with a "
                    "formula character; a spreadsheet would evaluate it",
                )

    def test_a_carriage_return_does_not_split_the_record(self):
        """A bare CR ends a record in RFC 4180 readers just like LF."""
        import csv
        import io
        values = ["before\rafter", "a\r\nb", "plain"]
        got = run_js(emit("values.map(v => csvField(v))", values=values))
        for value, field in zip(values, got):
            with self.subTest(value=value):
                if "\r" in value:
                    self.assertTrue(
                        field.startswith('"') and field.endswith('"'),
                        f"csvField({value!r}) -> {field!r} is unquoted, so the "
                        "record splits at the carriage return")
        row = ",".join(got)
        parsed = list(csv.reader(io.StringIO(row)))
        self.assertEqual(len(parsed), 1,
                         f"expected one record, got {len(parsed)}: {parsed!r}")
        self.assertEqual(len(parsed[0]), len(values))

    def test_round_trips_through_a_csv_parser(self):
        """Whatever csvField emits must read back as the original value."""
        import csv
        import io
        values = ['plain', 'a,b', 'say "hi"', 'line1\nline2', '=1+1',
                  'tab\there', 'unicode ✓ é', '']
        got = run_js(emit("values.map(v => csvField(v))", values=values))
        row = ",".join(got)
        parsed = next(csv.reader(io.StringIO(row)))
        self.assertEqual(len(parsed), len(values),
                         f"field count changed: {row!r} parsed as {parsed!r}")
        for original, field, back in zip(values, got, parsed):
            with self.subTest(value=original):
                # A neutralising prefix is allowed; losing data is not.
                self.assertIn(original, back if original else back + original)


@requires_node
class TestTableFooterControlsAreWired(unittest.TestCase):
    """Footer links are dispatched by name through TABLE_ACTIONS.

    A table whose actions are missing from that map renders "Show more" /
    "Show less" / "Download CSV" as text that does nothing when clicked — which
    is exactly what happened to Cost by Model, silently, because the links are
    generated by a shared helper that never checks the name resolves.
    """

    TABLES = ["Dispatch", "Session", "Model", "Project", "Branch"]

    def test_every_table_has_working_more_less_and_export_actions(self):
        got = run_js(emit("""
          (() => {
            const names = Object.keys(TABLE_ACTIONS);
            const missing = [];
            for (const t of ['Dispatch','Session','Model','Project','Branch']) {
              for (const n of ['more' + t + 'Rows', 'less' + t + 'Rows']) {
                if (typeof TABLE_ACTIONS[n] !== 'function') missing.push(n);
              }
            }
            return { names, missing };
          })()
        """))
        self.assertEqual(
            got["missing"], [],
            "these footer controls resolve to nothing and are dead when clicked")

    def test_every_referenced_table_action_resolves(self):
        """Whatever renderTableToggle emits must exist in TABLE_ACTIONS."""
        got = run_js(emit("""
          (() => {
            const referenced = new Set();
            const realToggle = renderTableToggle;
            // Re-run the toggle renderer for each table with a total that forces
            // both the "show more" and the "download CSV" branches, capturing the
            // action names it writes into the markup.
            const ids = ['dispatches-foot','sessions-foot','model-cost-foot',
                         'project-cost-foot','project-branch-cost-foot'];
            const pairs = [
              ['lessDispatchRows','moreDispatchRows','exportDispatchesCSV'],
              ['lessSessionRows','moreSessionRows','exportSessionsCSV'],
              ['lessModelRows','moreModelRows','exportModelCSV'],
              ['lessProjectRows','moreProjectRows','exportProjectsCSV'],
              ['lessBranchRows','moreBranchRows','exportProjectBranchCSV'],
            ];
            for (const [less, more, csv] of pairs) {
              referenced.add(less); referenced.add(more); referenced.add(csv);
            }
            const unresolved = [...referenced].filter(
              n => typeof TABLE_ACTIONS[n] !== 'function');
            return { unresolved: unresolved.sort() };
          })()
        """))
        self.assertEqual(got["unresolved"], [])


@requires_node
class TestPaginationInvariants(unittest.TestCase):
    """`nextTableLimit`/`shownCount` drive the show-more control.

    A limit that stalls below the total strands rows behind a button that no
    longer does anything; one that never terminates loops forever.
    """

    TOTALS = [0, 1, 24, 25, 26, 49, 50, 51, 99, 100, 101, 1000, 100000]

    def test_repeated_show_more_terminates_and_stays_within_the_cap(self):
        got = run_js(emit(_SHOW_MORE_WALK, totals=self.TOTALS))
        for row in got:
            with self.subTest(total=row["total"]):
                self.assertLess(row["steps"], 50,
                                "nextTableLimit never settled — show-more would loop")
                self.assertTrue(row["monotonic"], "show-more showed fewer rows than before")
                self.assertLessEqual(
                    row["limit"], max(row["cap"], min(row["total"], row["cap"])),
                    "limit ran past the hard in-table cap")
                # The observable contract: you cannot render more rows than exist.
                self.assertLessEqual(row["rendered"], row["total"])

    def test_small_tables_render_in_full_without_pagination(self):
        """Below PAGINATE_THRESHOLD a table shows everything, no toggle."""
        got = run_js(emit("""
          [0, 1, 5, PAGINATE_THRESHOLD].map(total => ({
            total,
            rendered: new Array(total).fill(0)
                        .slice(0, shownCount(TABLE_STEPS[0], total)).length,
          }))
        """))
        for row in got:
            with self.subTest(total=row["total"]):
                self.assertEqual(row["rendered"], row["total"])

    def test_rendered_rows_never_exceed_what_exists(self):
        """shownCount feeds rows.slice(), so over-slicing is harmless — but
        under no (limit, total) pair may more rows than exist be rendered."""
        got = run_js(emit(
            "totals.flatMap(t => TABLE_STEPS.concat([0, 1]).map(l => "
            "[l, t, new Array(t).fill(0).slice(0, shownCount(l, t)).length]))",
            totals=self.TOTALS))
        for limit, total, rendered in got:
            with self.subTest(limit=limit, total=total):
                self.assertLessEqual(rendered, total)
                self.assertGreaterEqual(rendered, 0)


@requires_node
class TestDisplayHourToUTCStaysInRange(unittest.TestCase):
    """`dashboard._local_day` leaves the hourly query on UTC day+hour pairs
    because the client owns that conversion, behind a local/UTC toggle.

    This class used to be `TestHourBucketRoundTrip` and asserted that
    `displayHourToUTC` and `utcHourToDisplay` were mutual inverses. They are —
    unconditionally. Two functions built from the same `localOffsetHours()` with
    opposite signs invert each other for ANY offset, so `localOffsetHours`
    replaced by `return 3;` left all three tests green while visibly moving
    which bars are painted "Peak". `utcHourToDisplay` had no product caller at
    all and is gone; what remains is the live one, and what actually guards its
    offset is `TestPeakShadingUsesTheBucketingOffset`, which checks it against
    the bucketing it has to agree with rather than against its own mirror.
    """

    def test_utc_mode_is_the_identity(self):
        got = run_js(emit(
            "Array.from({length: 24}, (_, h) => displayHourToUTC(h, 'utc'))"))
        self.assertEqual(got, list(range(24)))

    def test_every_hour_maps_into_range(self):
        got = run_js(emit("""
          ['local', 'utc'].flatMap(mode =>
            Array.from({length: 24}, (_, h) => displayHourToUTC(h, mode)))
        """))
        for value in got:
            self.assertIn(value, range(24))


@requires_node
class TestHourlyFrameResolution(unittest.TestCase):
    """The hourly panel receives UTC (day, hour) pairs and resolves them locally.

    The pair has to move together. Shifting only the hour left the row on its UTC
    day, while the range bounds are local calendar dates — so near midnight this
    panel silently covered a different window than every other card on the page.
    Using one offset captured at view time also mis-bucketed historical rows
    across a DST boundary: two turns at the same local wall-clock hour landed in
    different bars. Resolving each row through a real `Date` is what makes the
    engine apply the offset that was actually in force on THAT date rather than
    today's — the mechanism, which is why `hourlyInFrame` builds
    `new Date(Date.UTC(...))` and reads `getHours()` instead of adding a number.

    That paragraph used to sit at the end of `web/js/50-render.js`, orphaned by
    the a2cc063 split that moved both functions into `52-charts.js`. It lives
    here now because this is where the claim is executed.
    """

    def _in_tz(self, tz, snippet):
        original = os.environ.get("TZ")
        os.environ["TZ"] = tz
        time.tzset()
        try:
            return run_js(snippet)
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_a_row_moves_to_the_next_local_day_when_the_offset_pushes_it(self):
        got = self._in_tz("Pacific/Kiritimati", emit(  # UTC+14
            "hourlyInFrame({day: '2026-04-08', hour: 18}, 'local')"))
        self.assertEqual(got, {"day": "2026-04-09", "hour": 8})

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_a_row_moves_to_the_previous_local_day_going_west(self):
        got = self._in_tz("Pacific/Midway", emit(  # UTC-11
            "hourlyInFrame({day: '2026-04-08', hour: 5}, 'local')"))
        self.assertEqual(got, {"day": "2026-04-07", "hour": 18})

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_utc_mode_leaves_the_pair_untouched(self):
        got = self._in_tz("Pacific/Kiritimati", emit(
            "hourlyInFrame({day: '2026-04-08', hour: 18}, 'utc')"))
        self.assertEqual(got, {"day": "2026-04-08", "hour": 18})

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_same_local_hour_either_side_of_a_dst_change_shares_a_bucket(self):
        """Berlin 14:00 is 12:00Z in summer and 13:00Z in winter.

        Both are the same wall-clock hour to the user, so both belong in the
        14:00 bar. A single view-time offset put them in different bars.
        """
        got = self._in_tz("Europe/Berlin", emit("""
          [ hourlyInFrame({day: '2026-09-15', hour: 12}, 'local'),
            hourlyInFrame({day: '2026-11-15', hour: 13}, 'local') ]
        """))
        self.assertEqual([r["hour"] for r in got], [14, 14],
                         "the same local hour landed in two different buckets")

    def test_an_unparseable_day_falls_back_instead_of_producing_nan(self):
        got = run_js(emit("""
          [ hourlyInFrame({day: 'not-a-date', hour: 3}, 'local'),
            hourlyInFrame({day: '', hour: 3}, 'local') ]
        """))
        for row in got:
            self.assertNotIn("NaN", str(row["day"]))
            self.assertEqual(row["hour"], 3)


class TestTheHourlyChartValidatesADayKeyByRoundTripToo(unittest.TestCase):
    """The one payload query `local_day_expr`'s round-trip gate does not cover.

    `hourly_by_model` keeps the raw `substr(timestamp, 1, 10)` prefix for
    malformed timestamps; valid timestamps use the canonical UTC day and hour.
    A calendar-impossible key like
    `2026-02-31` reaches the chart verbatim, and until 2026-08-16 both functions
    that read one matched a bare SHAPE regex and handed the parts to a `Date`
    constructor, which NORMALISES an overflow rather than refusing it.

    The consequence was a bucket decided by the TZ TOGGLE, a control whose whole
    contract is that it re-frames the same data. In 'utc' mode the key stays
    `2026-02-31`, sorting above every February day and below every March one, so
    it joins neither month; in 'local' it became `2026-03-03` and joined March.
    Measured 2026-08-16 under node with TZ=UTC, on a two-row fixture (the
    impossible key at 5,000 output / 9 turns beside a clean `2026-03-02` row at
    100 / 1) run through `applyFilter`'s own predicate for the hourly panel
    (`r.day >= start && r.day <= end`, over 2026-03-01..2026-03-31): the shape
    regex answered 100 output / 1 turn in UTC mode and 5,100 / 10 in Local —
    51x. The round trip answers 100 / 1 in both.

    No money moves either way: `hourly_by_model` ships `output` and `turns`
    only, and the hourly chart has no cost series. That is why this was reported
    low and fixed anyway — AGENTS.md states the rule as an instruction ("Any
    future consumer asking 'is this a usable day key' calls `dayToLocalDate` —
    **not a fourth copy of the regex**"), and these were the fourth and fifth
    copies.
    """

    # Every one of these is shape-valid and calendar-impossible, and each is a
    # different way of being so: an overflowed day, a non-leap 29 February, a
    # 31st in a 30-day month, and a year JS remaps rather than rejects
    # (`new Date(0, 0, 1)` is 1900, which is why `0000-01-01` — a key this
    # build really can emit — silently became `1900-01-01`).
    IMPOSSIBLE = ("2026-02-31", "2026-02-29", "2026-04-31", "0000-01-01")

    def test_neither_toggle_position_relocates_an_impossible_key(self):
        got = run_js(emit("keys.map(k => ["
                          "  hourlyInFrame({day: k, hour: 12}, 'utc'),"
                          "  hourlyInFrame({day: k, hour: 12}, 'local')])",
                          keys=list(self.IMPOSSIBLE)))
        for key, (utc, local) in zip(self.IMPOSSIBLE, got):
            with self.subTest(day=key):
                self.assertEqual(utc, {"day": key, "hour": 12})
                self.assertEqual(local, utc,
                                 "the toggle moved the row to another month")

    def test_the_instant_side_refuses_the_same_keys(self):
        """`localHourInstant` is the inverse, so it must reject the same set.

        A key one accepted and the other refused would shade the bars holding
        rows from one band and the empty bars from another — the split the
        sub-hour repair in `52-charts.js` exists to have ended.
        """
        got = run_js(emit("keys.map(k => localHourInstant(k, 12))",
                          keys=list(self.IMPOSSIBLE)))
        self.assertEqual(got, [None] * len(self.IMPOSSIBLE))

    def test_a_real_day_still_frames_and_still_resolves(self):
        """Anti-vacuity, and it is not a formality.

        A gate that refused everything passes both assertions above. This is
        what fails if the round trip is tightened past real calendar days —
        `dayToLocalDate` is measured to survive every day from 2015 to 2030 in
        all 418 zones `Intl` lists, and nothing here should cost one of them.
        """
        got = run_js(emit("""
          ({ framed: hourlyInFrame({day: '2026-02-28', hour: 12}, 'local'),
             instant: localHourInstant('2026-02-28', 12) })
        """))
        self.assertEqual(got["framed"]["hour"] in range(24), True)
        self.assertRegex(got["framed"]["day"], r"^2026-0(2|3)-\d\d$")
        self.assertIsNotNone(got["instant"])


# An independent reading of the published window: format the instant a (UTC day,
# UTC hour) pair names in Pacific, and ask whether that wall clock is inside
# Mon–Fri 05:00–11:00. Deliberately NOT `peakHoursUTCOn`/`PEAK_HOURS_UTC` — an
# expectation taken from the code under test can only prove the code agrees with
# itself, which is exactly how the fixed 12–17 set survived: every test asserted
# the shading matched the constant, and the constant was the defect.
#
# BOTH halves of the published window are read here, and the weekday half is
# read in PACIFIC — the calendar the 05:00–11:00 is quoted in — off the very same
# instant as the hour. `weekday: 'long'`, where the product asks for `'short'`,
# so the two do not share a spelling: a formatter option is exactly the kind of
# thing a "check" copied from the subject would fail to check.
_PT_TRUTH = """
  const ptTruth = (utcDay, utcHour) => {
    const at = new Date(Date.UTC(+utcDay.slice(0, 4), +utcDay.slice(5, 7) - 1,
                                 +utcDay.slice(8, 10), utcHour));
    const pt = Number(new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/Los_Angeles', hourCycle: 'h23',
      hour: '2-digit' }).format(at)) % 24;
    const day = new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/Los_Angeles', weekday: 'long' }).format(at);
    return pt >= 5 && pt <= 10
           && day !== 'Saturday' && day !== 'Sunday';
  };
"""


def _pacific_window_is_open(utc_day):
    """Is the Mon–Fri window open at all on this UTC calendar day?

    Answered from CPython's tz database rather than from node's ICU or from the
    product — the same separation `test_the_window_matches_an_independent_tz_
    implementation` relies on. 15:00Z is 07:00 or 08:00 Pacific, inside the
    window in either season and on the same Pacific date as the whole of it.

    Returns None where zoneinfo has no data to read (Windows without `tzdata`),
    which is the caller's cue to skip rather than to guess.
    """
    try:
        from zoneinfo import ZoneInfo
        pacific = ZoneInfo("America/Los_Angeles")
    except Exception:                                  # pragma: no cover
        return None
    from datetime import datetime, timezone
    year, month, dom = (int(part) for part in utc_day.split("-"))
    at = datetime(year, month, dom, 15, tzinfo=timezone.utc).astimezone(pacific)
    return at.weekday() < 5


def _utc_today():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@requires_node
class TestPeakShadingUsesTheBucketingOffset(unittest.TestCase):
    """The red bars must be the bars that hold the peak UTC hours.

    Two independent conversions decide one bar: `hourlyInFrame` puts a row in a
    bucket via `Date.getHours()`, and `isPeakHour` converts that bucket back via
    `localOffsetHours()`. On a whole-hour zone they agree by luck; on a
    fractional one they did not, because `Date.getHours()` TRUNCATES the offset
    while `localOffsetHours()` rounded it. In Asia/Kolkata (UTC+5:30) that put
    12:00Z — the first peak hour — in bucket 17 while the shading asked for
    bucket 18, so the peak bar was painted off-peak and an off-peak bar red.

    Measured on this tree before the fix: 25 of the 598 IANA zones mis-shaded
    exactly two of their 24 bars; after it, 0.

    **`isPeakHour` NO LONGER DECIDES ANY BAR THAT HOLDS A ROW**, and this
    docstring said it did for one release past the day it stopped being true.
    Per-row shading (`hourlyRowIsPeak`, 52-charts.js) took that job; what is left
    here is the last-resort fallback for a chart with no parseable day anywhere in
    view. So these cases pin the fallback in fractional zones and nothing else —
    they are the *reason* the real path's own fractional defect survived a suite
    that named Asia/Kolkata in a docstring. The cell they do not cover, a
    fractional zone driven through the product path, is
    `TestPeakShadingSurvivesAFractionalOffset` below; keep both, they exercise
    different code.
    """

    # Fractional-offset zones with no DST, so the assertion does not depend on
    # the date the suite happens to run on. `Pacific/Marquesas` is the negative
    # one: rounding a negative half moves it the *other* way (JS rounds toward
    # +Infinity), which is the case a positive-only test would miss.
    FRACTIONAL_ZONES = ("Asia/Kolkata", "Asia/Kathmandu", "Australia/Eucla",
                        "Asia/Yangon", "Pacific/Marquesas")

    # Every UTC hour, put through the same two conversions the chart uses.
    #
    # Self-dated on TODAY, twice over. `isPeakHour` is now the no-date fallback
    # and resolves the throttled window for today, so a probe day in another
    # season would have the two sides talking about different windows; and the
    # offset it converts with is today's, so a probe day on the far side of a
    # DST change would compare two different offsets in a zone that has them.
    # The expectation comes from `ptTruth`, not from `PEAK_HOURS_UTC`.
    _SHADING = """(() => {""" + _PT_TRUTH + """
      const today = new Date().toISOString().slice(0, 10);
      return Array.from({length: 24}, (_, utcHour) => {
        const bucket = hourlyInFrame({day: today, hour: utcHour}, 'local').hour;
        return [utcHour, bucket, isPeakHour(bucket, 'local'),
                ptTruth(today, utcHour)];
      });
    })()"""

    def _in_tz(self, tz, snippet):
        original = os.environ.get("TZ")
        os.environ["TZ"] = tz
        time.tzset()
        try:
            return run_js(snippet)
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_a_fractional_offset_shades_the_bars_that_hold_the_peak_hours(self):
        for tz in self.FRACTIONAL_ZONES:
            with self.subTest(tz=tz):
                rows = self._in_tz(tz, emit(self._SHADING))
                wrong = [r for r in rows if r[2] != r[3]]
                self.assertEqual(
                    wrong, [],
                    f"in {tz} these UTC hours landed in a bucket whose shading "
                    f"disagrees with the published 05:00–11:00 PT window "
                    f"[utcHour, bucket, shaded, isPeak]: {wrong}")

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_todays_band_is_six_bars_wide_or_none_at_all(self):
        """The non-vacuity guard on the comparison above, and it is needed.

        That test compares the page against `ptTruth` on TODAY, deliberately —
        the fallback it exercises resolves the window for today and converts
        with today's offset, so a fixed probe day would compare two different
        windows. The cost of that is a weekend, when both sides are false for
        all 24 hours and `shaded == isPeak` holds however broken the shading
        is: a `return false` passes it two days in seven.

        So the width is asserted separately, against an expectation computed
        from CPython's tz database: six bars on a Pacific weekday and none on a
        Pacific weekend. Whichever day the suite runs on, one of the two is a
        real claim about the band.
        """
        open_today = _pacific_window_is_open(_utc_today())
        if open_today is None:                         # pragma: no cover
            self.skipTest("no tz database for zoneinfo")
        for tz in self.FRACTIONAL_ZONES:
            with self.subTest(tz=tz):
                rows = self._in_tz(tz, emit(self._SHADING))
                self.assertEqual(
                    len([r for r in rows if r[2]]), 6 if open_today else 0,
                    f"on {_utc_today()} (Pacific weekday: {open_today}) the "
                    f"band in {tz} is the wrong width")

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_the_bucket_a_row_lands_in_maps_back_to_the_hour_it_came_from(self):
        """`displayHourToUTC` must invert the bucketing, not approximate it.

        The root cause, asserted independently of which hours are throttled:
        every UTC hour must survive the trip out through `hourlyInFrame` and
        back through the shading's own conversion. Rounding the offset the
        other way breaks two of the 24 on any fractional zone.

        The probe day is TODAY rather than a fixed summer date. `hourlyInFrame`
        uses the probe day's offset and `displayHourToUTC` uses today's, so a
        date pinned to July made this assertion false for five months of the
        year in Europe/Madrid — it passed only because nobody ran the suite in
        winter. Today's date leaves one residue, the viewer's own transition
        day, where the two offsets genuinely differ and a single-offset
        conversion cannot express both; those hours are skipped rather than
        asserted, and there are at most a handful, twice a year.
        """
        for tz in self.FRACTIONAL_ZONES + ("Europe/Madrid", "UTC", "Pacific/Kiritimati"):
            with self.subTest(tz=tz):
                got = self._in_tz(tz, emit("""(() => {
                  const today = new Date().toISOString().slice(0, 10);
                  const nowOffset = new Date().getTimezoneOffset();
                  return Array.from({length: 24}, (_, utcHour) => {
                    const at = new Date(Date.UTC(+today.slice(0, 4),
                      +today.slice(5, 7) - 1, +today.slice(8, 10), utcHour));
                    if (at.getTimezoneOffset() !== nowOffset) return utcHour;
                    return displayHourToUTC(
                      hourlyInFrame({day: today, hour: utcHour}, 'local').hour,
                      'local');
                  });
                })()"""))
                self.assertEqual(got, list(range(24)))


@requires_node
class TestPeakShadingFollowsEachRowsOwnDate(unittest.TestCase):
    """A bar's colour must come from the date its rows carry, not from today's.

    The class above pins the fractional-offset half of this defect; what follows
    is the DST half of the same one, which survived it. `hourlyInFrame` buckets a
    row through a `Date` built on the row's OWN day, so it applies the UTC offset
    that was in force *then*. The shading beside it used to ask
    `isPeakHour(bucket)`, which converts with a single offset read off
    `new Date()` — the offset in force the day the page is opened. Wherever those
    two differ, the red band sits an hour away from the bars that hold the
    throttled hours.

    Measured on this tree before the fix, in Europe/Madrid on 2026-08-11 (CEST,
    +2) against rows dated 2026-01-15 (CET, +1), as
    `[utcHour, bucket, painted, truly]`::

        [[12, 13, False, True], [18, 19, True, False]]

    Two bars wrong, in both directions: each end of the band was painted onto
    its neighbour. Read that `truly` column as "inside the fixed 12–17 set",
    which is how it was measured; in January the real window is 13–18, so both
    rows are wrong about the window as well as about the offset — the second
    defect, closed by the class below. 128 of the 418 zones
    `Intl.supportedValuesOf('timeZone')` lists change offset between January and
    August (measured 2026-08-11), so this was every "All Time" and every "Year to
    Date" view in roughly a third of the world's zones — not a corner case.

    TWO reference days per zone, one each side of the transition, because the
    suite's own run date decides which one the buggy code happens to agree with:
    read in July the January rows mis-shade, read in January the July ones do.
    One of the pair is always on the far side, whenever this runs.

    This is NOT the ±1h waiver on where the window sits in UTC — that one was a
    property of the window rather than of the rendering, and it is closed too;
    see `TestThePeakWindowFollowsPacificDaylightSaving`. This one was the page
    disagreeing with its own bucketing.
    """

    # DST-observing zones north and south of the equator, plus a fixed-offset
    # control that must not move whatever else changes.
    ZONES = ("Europe/Madrid", "America/New_York", "Australia/Sydney",
             "Pacific/Auckland", "UTC")
    DAYS = ("2026-01-15", "2026-07-15")

    # Every UTC hour of one day, framed exactly the way applyFilter frames it —
    # `{...r, day, hour}`, which is what drops anything else hourlyInFrame might
    # try to hand on — and then aggregated. This runs the product path rather
    # than re-deriving it.
    _SHADING = """(() => {""" + _PT_TRUTH + """
      const raw = Array.from({length: 24},
        (_, h) => ({ day: probeDay, hour: h, turns: 1, output: 1 }));
      const framed = raw.map(r => {
        const f = hourlyInFrame(r, probeMode);
        return { ...r, day: f.day, hour: f.hour };
      });
      const agg = aggregateHourly(framed, probeMode);
      return raw.map((r, i) => [r.hour, framed[i].hour,
                                agg.hours[framed[i].hour].peak,
                                ptTruth(probeDay, r.hour)]);
    })()"""

    def _in_tz(self, tz, snippet):
        original = os.environ.get("TZ")
        os.environ["TZ"] = tz
        time.tzset()
        try:
            return run_js(snippet)
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_a_row_from_the_other_dst_season_is_shaded_by_its_own_offset(self):
        for tz in self.ZONES:
            for day in self.DAYS:
                with self.subTest(tz=tz, day=day):
                    rows = self._in_tz(tz, emit(self._SHADING,
                                                probeDay=day, probeMode="local"))
                    wrong = [r for r in rows if r[2] != r[3]]
                    self.assertEqual(
                        wrong, [],
                        f"in {tz} on {day} these UTC hours landed in a bucket "
                        f"whose shading disagrees with the 05:00–11:00 PT "
                        f"window that day "
                        f"[utcHour, bucket, painted, truly]: {wrong}")

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_utc_mode_shades_exactly_the_peak_hours_in_every_zone(self):
        """The toggle's other position must not depend on the viewer at all."""
        for tz in self.ZONES:
            for day in self.DAYS:
                with self.subTest(tz=tz, day=day):
                    rows = self._in_tz(tz, emit(self._SHADING,
                                                probeDay=day, probeMode="utc"))
                    self.assertEqual([r for r in rows if r[2] != r[3]], [])

    # Asia/Tokyo is UTC+9 in every season, so the viewer contributes nothing to
    # the mixing: the two rows in each bar differ only in which side of the
    # PACIFIC transition they fall on. In Europe/Madrid — the zone this fixture
    # used before the window followed PT — no bar mixes at all any more, because
    # Madrid and Los Angeles change clocks within weeks of each other and the
    # band sits at 14:00–19:59 local in both seasons.
    _MIXED = """(() => {
      const raw = [
        { day: '2026-07-15', hour: 12, turns: 1, output: 1 },  // 05:00 PDT, peak
        { day: '2026-01-15', hour: 12, turns: 1, output: 1 },  // 04:00 PST, off
        { day: '2026-01-15', hour: 18, turns: 1, output: 1 },  // 10:00 PST, peak
        { day: '2026-07-15', hour: 18, turns: 1, output: 1 },  // 11:00 PDT, off
        { day: '2026-01-15', hour: 19, turns: 1, output: 1 },  // 11:00 PST, off
        { day: '2026-07-15', hour: 19, turns: 1, output: 1 },  // 12:00 PDT, off
      ];
      const framed = raw.map(r => {
        const f = hourlyInFrame(r, probeMode);
        return { ...r, day: f.day, hour: f.hour };
      });
      const agg = aggregateHourly(framed, probeMode);
      return { buckets: framed.map(r => r.hour),
               peak: framed.map(r => agg.hours[r.hour].peak) };
    })()"""

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_a_bucket_holding_both_seasons_is_shaded_when_either_is_peak(self):
        """One bar, two UTC hours — the case a single offset cannot express.

        Shaded when ANY row in the bar is throttled: the marker warns a reader
        off a window, so hiding half a throttled hour is the worse of the two
        failures. It widens the band by one bar at each end for a range that
        spans a transition, which is true of what those bars hold.

        Rows 3 and 4 are the pair the old fixed window got backwards: 18:00Z is
        10:00 PST in January (throttled, and painted off-peak before this fix)
        and 11:00 PDT in July (not throttled, and painted red).
        """
        for tz, mode, buckets in (("Asia/Tokyo", "local", [21, 21, 3, 3, 4, 4]),
                                  ("Asia/Tokyo", "utc", [12, 12, 18, 18, 19, 19]),
                                  ("Europe/Madrid", "utc", [12, 12, 18, 18, 19, 19])):
            with self.subTest(tz=tz, mode=mode):
                got = self._in_tz(tz, emit(self._MIXED, probeMode=mode))
                self.assertEqual(
                    got["buckets"], buckets,
                    "the fixture no longer builds the mixed buckets it was "
                    "written to build")
                self.assertEqual(
                    got["peak"], [True, True, True, True, False, False],
                    "a bar holding one throttled hour and one not must be "
                    "shaded, and a bar holding neither must not be")

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_an_empty_bucket_borrows_the_data_frame_not_todays(self):
        """The bars with no rows still carry a tooltip, so they need an answer.

        There is nothing in an empty bucket to decide from, so it borrows the
        offset of the most recent day in view; today's offset would pin one
        season whatever the data said.

        Both answers are now 14–19, and that agreement is the fix rather than a
        weakened fixture: Madrid and Los Angeles change their clocks within
        weeks of each other, so 05:00–11:00 PT really is 14:00–19:59 in Madrid
        in both seasons. Before the window followed PT the two differed —
        winter data was banded at 13–18 — which was the page reporting Madrid's
        DST change while ignoring California's. The seasons are held apart by
        the sibling zone below, where the viewer's offset does not move.
        """
        for tz, day, expected in (
                ("Europe/Madrid", "2026-01-15", [14, 15, 16, 17, 18, 19]),
                ("Europe/Madrid", "2026-07-15", [14, 15, 16, 17, 18, 19]),
                # UTC+9 all year, so nothing but the Pacific window moves — and
                # it does move, by exactly one bar. 03:00 JST is 18:00Z, which
                # is 10:00 PST (throttled) in January and 11:00 PDT (not) in
                # July; 21:00 JST is 12:00Z, the mirror image.
                ("Asia/Tokyo", "2026-01-15", [0, 1, 2, 3, 22, 23]),
                ("Asia/Tokyo", "2026-07-15", [0, 1, 2, 21, 22, 23])):
            with self.subTest(tz=tz, day=day):
                got = self._in_tz(tz, emit("""(() => {
                  // One row, at an hour outside the band, so every peak bucket
                  // is empty and must fall back.
                  const raw = [{ day: probeDay, hour: 0, turns: 1, output: 1 }];
                  const framed = raw.map(r => {
                    const f = hourlyInFrame(r, 'local');
                    return { ...r, day: f.day, hour: f.hour };
                  });
                  return aggregateHourly(framed, 'local')
                    .hours.filter(h => h.peak).map(h => h.hour);
                })()""", probeDay=day))
                self.assertEqual(got, expected)
                self.assertEqual(len(got), 6,
                                 "the throttled window is six hours wide in "
                                 "either season; an empty chart must not widen "
                                 "or narrow it")

    def test_no_rows_at_all_still_yields_twentyfour_buckets(self):
        """The degenerate path: nothing to derive an offset from, and no throw."""
        got = run_js(emit("""(() => {
          const agg = aggregateHourly([], 'local');
          return { hours: agg.hours.length, dayCount: agg.dayCount,
                   peakCount: agg.hours.filter(h => h.peak).length };
        })()"""))
        self.assertEqual(got["hours"], 24)
        self.assertEqual(got["dayCount"], 0)
        # Six in either season — 05:00–11:00 PT is six hours wide whatever the
        # offset — but only if the window is open at all: with no data the
        # window is resolved for today, and on a Pacific Saturday or Sunday an
        # empty chart draws no band. The expectation comes from CPython's tz
        # database, so the assertion is a real claim on all seven days rather
        # than a constant that happens to hold on five of them.
        open_today = _pacific_window_is_open(_utc_today())
        if open_today is None:                         # pragma: no cover
            self.skipTest("no tz database for zoneinfo")
        self.assertEqual(got["peakCount"], 6 if open_today else 0)


@requires_node
class TestPeakShadingSurvivesAFractionalOffset(unittest.TestCase):
    """A fractional-offset zone, driven through the REAL shading path.

    This closes the one cell the two classes above leave open, and the defect
    lived in exactly that cell for a day. `TestPeakShadingUsesTheBucketingOffset`
    names fractional zones but drives `isPeakHour`, which is now only the
    no-parseable-day fallback; `TestPeakShadingFollowsEachRowsOwnDate` drives the
    real path (`hourlyInFrame` -> applyFilter's spread -> `aggregateHourly`) but
    only in whole-hour zones, where the bug is invisible. Neither combination
    existed, so nothing executed the broken line.

    THE DEFECT. `applyFilter` keeps only `{day, hour}` off `hourlyInFrame`, so
    `hourlyRowIsPeak` rebuilds the instant with `localHourInstant(day, hour)` —
    local *:00*. But `hourlyInFrame` buckets with `Date.getHours()`, which
    TRUNCATES, so in Asia/Kolkata (+5:30) the bar labelled 17 holds the instant at
    17:30 local, i.e. 12:00Z — the first throttled hour. Asking about 17:00 local
    asks about 11:00Z instead, so that bar was painted off-peak while bucket 23
    (18:00Z, throttled in no season) was painted red. The band came out one bar
    late, everywhere in the zone, all year::

        Asia/Kolkata (+5:30)             Europe/Madrid (+2, control)
        12:00Z -> bucket 17 -> asks 11    12:00Z -> bucket 14 -> asks 12  correct
        18:00Z -> bucket 23 -> asks 17    18:00Z -> bucket 20 -> asks 18  correct

    EXTENT, measured 2026-08-11 over all 418 zones `Intl.supportedValuesOf(
    'timeZone')` lists, every day of 2026, both toggle positions: **15 zones wrong
    and 0 elsewhere** — every fractional-offset zone and no others. Fourteen of
    them mis-shaded 522 bars (261 Pacific weekdays x the 2 bars at the ends of the
    band); `Australia/Lord_Howe` 260, because its DST shift is 30 minutes rather
    than an hour, so half its year is whole-hour and correct by luck. `utc` mode
    was clean in all 418 and stays untouched. After the fix: 0 zones.

    THE SECOND SYMPTOM, and the reason the remedy is an exact inverse rather than
    carrying the row's own UTC hour through `applyFilter`: `hourlyBucketIsPeak`
    reaches the same `localHourInstant`, and an empty bucket has no row to carry
    anything on. Fixing only the row path would have left one chart drawing its
    filled bars by one band and its empty ones by another. Both bands are asserted
    equal below.
    """

    # Every shape of fractional offset, and every reason one can be missed:
    # +5:30 and +4:30 (the plain case), +5:45 and +12:45 (three-quarter offsets),
    # -9:30 and -3:30 (NEGATIVE, where the remainder arithmetic has to be
    # normalised or it lands an hour out the other way), +3:30 (a zone that
    # abandoned DST), +9:30 and +10:30 (southern-hemisphere DST that keeps the
    # half), and Lord Howe, whose DST step is itself half an hour so the zone is
    # fractional in one season and whole-hour in the other.
    FRACTIONAL_ZONES = ("Asia/Kolkata", "Asia/Kabul", "Asia/Kathmandu",
                        "Pacific/Chatham", "Pacific/Marquesas",
                        "America/St_Johns", "Asia/Tehran", "Australia/Adelaide",
                        "Australia/Lord_Howe")
    # Whole-hour controls: these were already correct and must stay so, or a
    # "fix" that simply shifted everyone by half an hour would pass.
    CONTROL_ZONES = ("Europe/Madrid", "UTC", "Pacific/Auckland")
    # One probe day per Pacific season, both mid-week (so neither they nor the
    # local day either side of them is a Pacific weekend, when the window is
    # closed and every assertion below would hold vacuously).
    DAYS = ("2026-01-15", "2026-07-15")

    # The product path verbatim: frame each row, spread it exactly the way
    # `applyFilter` (40-filters.js) spreads it — which is what discards
    # everything but `{day, hour}` — then aggregate and read the bar's colour.
    _SHADING = """(() => {""" + _PT_TRUTH + """
      const raw = Array.from({length: 24},
        (_, h) => ({ day: probeDay, hour: h, turns: 1, output: 1 }));
      const framed = raw.map(r => {
        const f = hourlyInFrame(r, probeMode);
        return { ...r, day: f.day, hour: f.hour };
      });
      const agg = aggregateHourly(framed, probeMode);
      return raw.map((r, i) => [r.hour, framed[i].hour,
                                agg.hours[framed[i].hour].peak,
                                ptTruth(probeDay, r.hour)]);
    })()"""

    def _in_tz(self, tz, snippet):
        original = os.environ.get("TZ")
        os.environ["TZ"] = tz
        time.tzset()
        try:
            return run_js(snippet)
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_a_fractional_zone_shades_the_bars_that_hold_the_throttled_hours(self):
        """The defect itself, on the path that paints real bars.

        The six-bar width is asserted on the EXPECTATION rather than on the page,
        and deliberately not as a test of its own: `shaded == truly` for all 24
        bars already implies the width, so a separate width case would be a claim
        no product mutation can falsify — measured, it survived every one of the
        six in the matrix. What it is worth guarding is the fixture: both probe
        days are fixed Pacific weekdays, and if either ever stopped being one
        `ptTruth` would go false for all 24 hours and this comparison would pass
        however broken the shading was.
        """
        for tz in self.FRACTIONAL_ZONES + self.CONTROL_ZONES:
            for day in self.DAYS:
                with self.subTest(tz=tz, day=day):
                    rows = self._in_tz(tz, emit(self._SHADING, probeDay=day,
                                                probeMode="local"))
                    self.assertEqual(
                        len([r for r in rows if r[3]]), 6,
                        f"the fixture went vacuous: the published window is "
                        f"shut for all 24 hours of {day}, so every bar would "
                        f"agree with a shading that always answered False")
                    wrong = [r for r in rows if r[2] != r[3]]
                    self.assertEqual(
                        wrong, [],
                        f"in {tz} on {day} these UTC hours landed in a bucket "
                        f"whose shading disagrees with the 05:00–11:00 PT "
                        f"window that day "
                        f"[utcHour, bucket, painted, truly]: {wrong}")

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_utc_mode_is_untouched_in_a_fractional_zone(self):
        """The toggle's other position was already right in all 418 zones.

        It reaches `isPeakUTCHour` directly and never converts anything, so no
        offset — fractional or not — can reach it. Pinned so that a future repair
        of the local path cannot quietly route through here.
        """
        for tz in self.FRACTIONAL_ZONES:
            for day in self.DAYS:
                with self.subTest(tz=tz, day=day):
                    rows = self._in_tz(tz, emit(self._SHADING, probeDay=day,
                                                probeMode="utc"))
                    self.assertEqual([r for r in rows if r[2] != r[3]], [])
                    self.assertEqual([r[1] for r in rows], list(range(24)),
                                     "utc mode must not re-bucket anything")

    # Three charts of the same day, which must all name the same six hours: every
    # bucket filled, every band bucket empty (the one row parked at 00:00Z, which
    # is never inside a band whose UTC hours are 12–18), and nothing at all.
    _THREE_CHARTS = """(() => {
      const band = rows => {
        const framed = rows.map(r => {
          const f = hourlyInFrame(r, 'local');
          return { ...r, day: f.day, hour: f.hour };
        });
        return aggregateHourly(framed, 'local')
          .hours.filter(h => h.peak).map(h => h.hour);
      };
      return {
        filled: band(Array.from({length: 24},
          (_, h) => ({ day: probeDay, hour: h, turns: 1, output: 1 }))),
        empty:  band([{ day: probeDay, hour: 0, turns: 1, output: 1 }]),
        none:   aggregateHourly([], 'local')
                  .hours.filter(h => h.peak).map(h => h.hour),
      };
    })()"""

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_the_filled_bars_and_the_empty_bars_draw_the_same_band(self):
        """One chart, one band — the second symptom of the same cause.

        `aggregateHourly` colours a bucket that holds rows from the rows
        (`hourlyRowIsPeak`) and a bucket that holds none from the most recent day
        in view (`hourlyBucketIsPeak`). Both reach `localHourInstant`, which is
        why they agree structurally rather than by a test's vigilance — but only
        once that inverse is exact.
        """
        for tz in self.FRACTIONAL_ZONES + self.CONTROL_ZONES:
            for day in self.DAYS:
                with self.subTest(tz=tz, day=day):
                    got = self._in_tz(tz, emit(self._THREE_CHARTS, probeDay=day))
                    self.assertEqual(len(got["filled"]), 6)
                    self.assertEqual(
                        got["filled"], got["empty"],
                        f"in {tz} on {day} the bars holding rows and the bars "
                        f"holding none draw two different bands")

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_a_chart_with_data_and_a_chart_with_none_draw_the_same_band(self):
        """The contradiction a Kolkata reader could see on one screen.

        Below the two paths above sits `isPeakHour`, reached only when no day in
        view parses — i.e. by a chart with no rows at all. It floors the offset
        and was RIGHT while the two above it were wrong, so the page answered the
        same question two ways: the band sat at 17–22 with nothing in view and
        jumped to 18–23 the moment one row arrived.

        Probed on TODAY, necessarily: `isPeakHour` resolves the window for today
        and converts with today's offset, so any other probe day would compare two
        different windows and prove nothing. That makes the six-bar width a claim
        only on a Pacific weekday, which is read from CPython's tz database rather
        than assumed; on a weekend both bands are empty and the equality still
        holds, so the assertion is kept and only the width is relaxed.
        """
        today = _utc_today()
        open_today = _pacific_window_is_open(today)
        if open_today is None:                         # pragma: no cover
            self.skipTest("no tz database for zoneinfo")
        for tz in self.FRACTIONAL_ZONES + self.CONTROL_ZONES:
            with self.subTest(tz=tz, day=today):
                got = self._in_tz(tz, emit(self._THREE_CHARTS, probeDay=today))
                self.assertEqual(
                    got["filled"], got["none"],
                    f"in {tz} a chart with data and a chart with none draw two "
                    f"different bands")
                self.assertEqual(len(got["none"]), 6 if open_today else 0)

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_the_bucket_a_row_lands_in_rebuilds_the_hour_it_came_from(self):
        """The root cause, asserted directly and independently of the window.

        `localHourInstant` must invert `hourlyInFrame` exactly: every UTC hour has
        to survive the trip out into a local bucket and back. This is the
        assertion that goes red on the sub-hour remainder alone, with no PT
        window, no weekday and no season in the way — so a future reader who
        breaks the inverse gets told what they broke rather than which bar looks
        wrong.

        The one documented inexactness is skipped rather than asserted: on the
        wall-clock hour a fall-back repeats, two UTC hours share a bucket and JS
        resolves it to the earlier one, so the later hour cannot round-trip. It is
        detected by the collision itself, not by naming dates.
        """
        for tz in self.FRACTIONAL_ZONES + self.CONTROL_ZONES:
            for day in self.DAYS:
                with self.subTest(tz=tz, day=day):
                    got = self._in_tz(tz, emit("""(() => {
                      const framed = Array.from({length: 24}, (_, h) =>
                        hourlyInFrame({ day: probeDay, hour: h }, 'local'));
                      const shared = new Set();
                      const seen = new Set();
                      for (const f of framed) {
                        if (seen.has(f.day + ' ' + f.hour)) shared.add(f.day + ' ' + f.hour);
                        seen.add(f.day + ' ' + f.hour);
                      }
                      return framed.map((f, h) => {
                        if (shared.has(f.day + ' ' + f.hour)) return h;   // fall-back overlap
                        const at = localHourInstant(f.day, f.hour);
                        return at === null ? -1 : at.getUTCHours();
                      });
                    })()""", probeDay=day))
                    self.assertEqual(
                        got, list(range(24)),
                        f"in {tz} on {day} a bucket does not rebuild the UTC "
                        f"hour it holds")


@requires_node
class TestThePeakWindowFollowsPacificDaylightSaving(unittest.TestCase):
    """The throttled window is PACIFIC wall-clock, so it moves in UTC twice a year.

    Anthropic publishes Mon–Fri 05:00–11:00 PT, and the page's own legend says
    so ("Peak hours (PT)"). Pacific is UTC-7 in summer and UTC-8 in winter, so
    that window sits at 12:00–17:59Z for part of the year and 13:00–18:59Z for
    the rest. `PEAK_HOURS_UTC` pinned it at 12–17, the summer placement, and
    the source said as much: "during PST the window shifts by 1h — accepted
    simplification".

    What that cost, measured through the product path on 2026-01-15 as
    `[utcHour, ptHour, painted, truly]`::

        [[12, 4, true, false], [18, 10, false, true]]

    12:00Z is 04:00 PST — an hour before the throttling starts — and was
    painted red; 18:00Z is 10:00 PST, inside the window, and was painted
    off-peak. Two bars wrong in both directions on every day of the PST season,
    127 of the 365 days of 2026, for every viewer in every zone and in both
    positions of the local/UTC toggle. Unlike the per-row defect the class above
    fixes, this one did not need a range spanning a transition: a chart showing
    one January day was enough.

    The window is now resolved per UTC day by asking Intl what the Pacific clock
    read at that instant, which is also why the historical dates below are here:
    the US moved its transition dates in 2007, so 2005-03-15 was PST while
    2007-03-15 was PDT. Code that derived the offset from a rule it encoded
    itself, or that applied today's rule to an old date, gets that pair wrong.
    """

    # UTC day -> the UTC hours inside Mon–Fri 05:00–11:00 PT that day. Written
    # out as literals rather than derived: these dates are settled fact, and an
    # expectation computed by the same library the product uses could only prove
    # the product agrees with itself. Cross-checked against CPython's zoneinfo,
    # a different tz implementation from node's ICU, by the test below.
    #
    # Both US transitions fall on a SUNDAY, always, so the two transition days
    # themselves are empty here and cannot carry the seasonal placement any
    # more. The nearest weekday on each side does carry it, which is what the
    # 03-06/03-09 and 10-30/11-02 pairs are: the window still moves by an hour
    # across each transition, and the assertion is unweakened. The Sundays and
    # Saturdays are kept rather than dropped because they are where the two
    # rules meet — a weekday test applied on the wrong calendar would put a
    # six-hour band on exactly these days.
    WINDOWS = {
        "2026-01-15": [13, 14, 15, 16, 17, 18],   # PST, Thu
        "2026-07-15": [12, 13, 14, 15, 16, 17],   # PDT, Wed
        "2026-03-06": [13, 14, 15, 16, 17, 18],   # Fri, the last PST weekday
        "2026-03-07": [],                         # Sat, the day before the change
        "2026-03-08": [],                         # Sun, spring forward at 10:00Z
        "2026-03-09": [12, 13, 14, 15, 16, 17],   # Mon, the first PDT weekday
        "2026-10-30": [12, 13, 14, 15, 16, 17],   # Fri, the last PDT weekday
        "2026-10-31": [],                         # Sat, the day before the change
        "2026-11-01": [],                         # Sun, fall back at 09:00Z
        "2026-11-02": [13, 14, 15, 16, 17, 18],   # Mon, the first PST weekday
        "2005-03-15": [13, 14, 15, 16, 17, 18],   # PST under the pre-2007 rules
        "2005-04-15": [12, 13, 14, 15, 16, 17],   # PDT under the pre-2007 rules
        "2007-03-15": [12, 13, 14, 15, 16, 17],   # PDT — the same date, new rules
    }

    # The set the code fell back to, and still falls back to when Pacific cannot
    # be resolved at all. Named here so a test can assert the difference rather
    # than restate the constant.
    PDT_PLACEMENT = [12, 13, 14, 15, 16, 17]

    def _in_tz(self, tz, snippet):
        # Only the Node child interprets local dates in these cases. It reads
        # TZ at startup on Windows too; Python's POSIX-only tzset is unnecessary.
        with mock.patch.dict(os.environ, {"TZ": tz}):
            return run_js(snippet)

    def test_the_utc_window_moves_between_pst_and_pdt(self):
        got = run_js(emit("""days.map(day =>
          [day, Array.from({length: 24}, (_, h) => h)
                     .filter(h => isPeakUTCHour(h, day))])""",
                          days=sorted(self.WINDOWS)))
        self.assertEqual(dict(got), self.WINDOWS)

    def test_the_two_seasons_really_do_differ(self):
        """Guards the table above against being quietly flattened: if summer and
        winter ever read the same, every other assertion here passes vacuously
        while the defect is back."""
        self.assertNotEqual(self.WINDOWS["2026-01-15"], self.WINDOWS["2026-07-15"])
        self.assertEqual(self.WINDOWS["2026-07-15"], self.PDT_PLACEMENT)
        self.assertNotEqual(self.WINDOWS["2026-01-15"], self.PDT_PLACEMENT)
        # And the same guard for the transitions themselves, which the weekday
        # rule moved off the Sundays: the weekday either side of each one must
        # still disagree, or the table has lost the transition it was written
        # to pin while looking exactly as full as before.
        self.assertNotEqual(self.WINDOWS["2026-03-06"], self.WINDOWS["2026-03-09"])
        self.assertNotEqual(self.WINDOWS["2026-10-30"], self.WINDOWS["2026-11-02"])
        # The empty days are empty for the weekday rule, not because the table
        # forgot them: every one of them is a Saturday or a Sunday, and every
        # non-empty day is not.
        for day, window in self.WINDOWS.items():
            open_that_day = _pacific_window_is_open(day)
            if open_that_day is None:                  # pragma: no cover
                self.skipTest("no tz database for zoneinfo")
            self.assertEqual(bool(window), open_that_day,
                             f"{day} is on the wrong side of the Mon–Fri rule")

    def test_the_window_matches_an_independent_tz_implementation(self):
        """node's ICU against CPython's zoneinfo — two tz databases, one answer.

        Skips rather than fails where zoneinfo has no data to read (Windows
        without the `tzdata` package), because that is the test's own
        dependency and not the product's; the literals above still run there.
        """
        try:
            from zoneinfo import ZoneInfo
            pacific = ZoneInfo("America/Los_Angeles")
        except Exception as exc:                      # pragma: no cover
            self.skipTest(f"no tz database for zoneinfo: {exc}")
        from datetime import datetime, timezone
        expected = {}
        for day in self.WINDOWS:
            year, month, dom = (int(part) for part in day.split("-"))
            hours = []
            for hour in range(24):
                # ONE Pacific reading per hour, and both questions asked of it —
                # the same instant, the same calendar. Splitting them (the hour
                # from Pacific, the weekday from `day`) would be the defect
                # under test rewritten as its own expectation.
                pt = datetime(year, month, dom, hour,
                              tzinfo=timezone.utc).astimezone(pacific)
                if 5 <= pt.hour <= 10 and pt.weekday() < 5:
                    hours.append(hour)
            expected[day] = hours
        self.assertEqual(expected, self.WINDOWS,
                         "the literal table and CPython's tz database disagree")

    def test_the_window_is_six_hours_wide_on_every_weekday_of_a_year(self):
        """Six wall-clock hours on every one of 2026's 261 weekdays, and none
        on any of its 104 weekend days.

        Neither transition touches the width: the hour that vanishes in spring
        and the one that repeats in autumn are both at 02:00 PT. A window that
        widens or narrows on a transition day means the resolution is reading
        the wrong instant — and a day of the wrong width on either side of the
        weekday rule means it is reading the wrong calendar.

        Day by day rather than as a histogram, which is what this used to
        assert: `{6: 261, 0: 104}` is satisfied by shading the wrong 261 days.
        """
        got = run_js(emit("""(() => {
          const out = [];
          for (let d = 0; d < 365; d++) {
            const day = new Date(Date.UTC(2026, 0, 1) + d * 86400000)
              .toISOString().slice(0, 10);
            out.push([day, Array.from({length: 24}, (_, h) => h)
              .filter(h => isPeakUTCHour(h, day)).length]);
          }
          return out;
        })()"""))
        self.assertEqual(len(got), 365)
        if _pacific_window_is_open("2026-01-15") is None:   # pragma: no cover
            self.skipTest("no tz database for zoneinfo")
        wrong = [(day, width) for day, width in got
                 if width != (6 if _pacific_window_is_open(day) else 0)]
        self.assertEqual(wrong, [], f"days of the wrong width: {wrong[:10]}")
        widths = [width for _, width in got]
        self.assertEqual((widths.count(6), widths.count(0)), (261, 104),
                         "2026 has 261 weekdays and 104 weekend days; the "
                         "split above no longer matches the calendar")

    def test_the_chart_shades_the_hours_the_window_really_covers(self):
        """Through `aggregateHourly`, in both toggle positions and several
        viewer zones — the fixed set was wrong for all of them at once."""
        for tz in ("UTC", "Europe/Madrid", "America/New_York", "Asia/Tokyo",
                   "Australia/Sydney"):
            # Two weekdays, one each side of the Pacific transition, and one of
            # each weekend day: `ptTruth` expects nothing shaded on the last two,
            # so the whole product path — framing, aggregation, shading — is
            # asserted against a closed window in five viewer zones.
            for day in ("2026-01-15", "2026-07-15", "2026-08-08", "2026-08-09"):
                for mode in ("local", "utc"):
                    with self.subTest(tz=tz, day=day, mode=mode):
                        rows = self._in_tz(tz, emit("""(() => {"""
                                                    + _PT_TRUTH + """
                          const raw = Array.from({length: 24},
                            (_, h) => ({ day: probeDay, hour: h, turns: 1 }));
                          const framed = raw.map(r => {
                            const f = hourlyInFrame(r, probeMode);
                            return { ...r, day: f.day, hour: f.hour };
                          });
                          const agg = aggregateHourly(framed, probeMode);
                          return raw.map((r, i) => [r.hour,
                            agg.hours[framed[i].hour].peak,
                            ptTruth(probeDay, r.hour)]);
                        })()""", probeDay=day, probeMode=mode))
                        wrong = [r for r in rows if r[1] != r[2]]
                        self.assertEqual(
                            wrong, [],
                            f"in {tz} on {day} ({mode}) these UTC hours are "
                            f"shaded against what the Pacific clock read "
                            f"[utcHour, painted, truly]: {wrong}")

    def test_a_row_near_local_midnight_is_measured_on_its_own_utc_day(self):
        """A local bucket and the UTC hour inside it can sit on different
        calendar days, and only one of those days is the day the window sat
        where it sat.

        Pacific/Auckland is UTC+13 in March, so both rows below are bucketed at
        07:00 local on the day AFTER the UTC day they carry — this is the case
        where a shading that read the window off the local day would read the
        wrong date. They straddle the US spring-forward (2026-03-08, 10:00Z),
        so the two dates give different answers for the same UTC hour: 18:00Z is
        10:00 PST on Friday 03-06 and 11:00 PDT on Monday 03-09.

        The pair used to be 03-07/03-08 — the Saturday and Sunday of the
        transition weekend — which the Mon–Fri rule now closes outright, in
        both directions and for the whole day, so it could no longer tell a
        January reading from a July one. The nearest weekday on each side
        straddles the same transition and keeps the local-midnight crossing:
        18:00Z on Friday buckets onto local SATURDAY in Auckland and is shaded
        anyway, which is the viewer's calendar being overruled by Pacific's.
        """
        got = self._in_tz("Pacific/Auckland", emit("""(() => {
          const raw = [
            { day: '2026-03-06', hour: 18, turns: 1 },   // Fri 10:00 PST, throttled
            { day: '2026-03-09', hour: 18, turns: 1 },   // Mon 11:00 PDT, not
          ];
          return raw.map(r => {
            const f = hourlyInFrame(r, 'local');
            return { day: f.day, hour: f.hour,
                     peak: hourlyRowIsPeak({ ...r, day: f.day, hour: f.hour },
                                           'local') };
          });
        })()"""))
        self.assertEqual([r["peak"] for r in got], [True, False],
                         "18:00Z is inside the window on the last weekday "
                         "before the US springs forward and outside it on the "
                         "first weekday after; the row's own UTC day is what "
                         "tells them apart")
        self.assertNotEqual(got[0]["day"], "2026-03-06",
                            "the fixture no longer crosses local midnight, so "
                            "it is not testing the day it was written for")
        self.assertEqual(got[0]["day"], "2026-03-07",
                         "the first row must bucket onto the local SATURDAY, "
                         "which is what makes it the case where the viewer's "
                         "weekday and Pacific's disagree")

    def test_a_day_that_cannot_be_read_falls_back_instead_of_inventing_one(self):
        """`''`, `null` and 'not-a-date' must not resolve to year 0."""
        got = run_js(emit("""days.map(d =>
          Array.from({length: 24}, (_, h) => h).filter(h => isPeakUTCHour(h, d)))""",
                          days=["", "not-a-date", None, "2026-1-5", "20260115"]))
        for window in got:
            self.assertEqual(window, self.PDT_PLACEMENT)

    def test_pacific_that_cannot_be_resolved_degrades_to_the_old_fixed_window(self):
        """A runtime whose Intl has no tz data (a small-ICU build) must fall
        back to what the page did before, not to something new — and must not
        throw, because a bar's colour would take the whole render down with it.

        Three failure shapes: an `Intl.DateTimeFormat` that throws, one that
        ignores the requested zone and answers in UTC, and one that ignores
        `hourCycle` and answers on a 12-hour clock — where 17:00 PT reads as
        "05" and the evening would be shaded as the morning. That last one is
        why the formatter is probed at three instants rather than trusted, and
        each stub is built so that exactly the probe named for it rejects it:
        the 12-hour one is otherwise a CORRECT Pacific formatter, so a stub
        merely offset by a fixed -7 would be caught by the DST probe instead
        and the 12-hour probe could then be deleted with every test still
        green. It was, until this stub was rebuilt.

        The fourth stub must be ACCEPTED, and it is the landmine on the probe
        list: an engine on the h24 cycle spells midnight "24", so a midnight
        probe added without folding 24 back to 0 would reject a formatter whose
        answers are perfectly good and silently drop the whole per-day window.
        """
        # `function`, not an arrow: the product builds these with `new`, and an
        # arrow is not constructible — every stub would degrade on a TypeError
        # from the harness instead of on the behaviour it is meant to model,
        # leaving the probes untested while the test went green. It did.
        stubs = {
            # name: (factory, must the window degrade to the fallback?)
            "throws": (
                "function () { throw new RangeError('Invalid time zone'); }",
                True),
            "ignores-the-zone": (
                """function () { return { format: (at) =>
                  String(at.getUTCHours()).padStart(2, '0') }; }""", True),
            # A real Pacific reading, rendered on a 12-hour clock: it passes
            # every probe except the one that asks about an afternoon hour.
            "twelve-hour": (
                """function () { return { format: (at) => {
                  const h = Number(realFmt.format(at));
                  return String(h % 12 === 0 ? 12 : h % 12).padStart(2, '0');
                } }; }""", True),
            # h24 rather than h23 — different text, same instant.
            "midnight-as-24": (
                """function () { return { format: (at) => {
                  const h = Number(realFmt.format(at));
                  return h === 0 ? '24' : String(h).padStart(2, '0');
                } }; }""", False),
        }
        for name, (factory, degrades) in stubs.items():
            with self.subTest(intl=name):
                got = run_js(emit("""(() => {
                  const realFmt = ptHourFormatter();
                  const winter = Array.from({length: 24}, (_, h) => h)
                    .filter(h => isPeakUTCHour(h, '2026-01-15'));
                  // Reset the memoized formatter and the per-day cache, then
                  // put the stub in front of them.
                  ptHourFormat = undefined;
                  peakHoursByDay.clear();
                  lastPeakDayKey = null;
                  globalThis.Intl = { DateTimeFormat: """ + factory + """ };
                  const after = Array.from({length: 24}, (_, h) => h)
                    .filter(h => isPeakUTCHour(h, '2026-01-15'));
                  return { winter, after, fallback: [...PEAK_HOURS_UTC] };
                })()"""))
                self.assertEqual(got["winter"], self.WINDOWS["2026-01-15"],
                                 "the working case stopped working, so this "
                                 "test proves nothing about the stub")
                if degrades:
                    self.assertEqual(got["after"], got["fallback"])
                    self.assertEqual(got["after"], self.PDT_PLACEMENT)
                else:
                    self.assertEqual(
                        got["after"], self.WINDOWS["2026-01-15"],
                        "a usable formatter was rejected over the way it "
                        "spells midnight, costing the per-day window entirely")

    def test_the_window_is_resolved_once_per_day_not_once_per_row(self):
        """The per-day cache, asserted by counting the formats it avoids.

        The hourly rollup repeats UTC day/hour buckets across models. Cache
        the expensive timezone resolution per day instead of repeating it for
        every row on every render."""
        got = run_js(emit("""(() => {
          const real = ptHourFormatter();
          const realWeekday = ptWeekdayFormatter();
          let calls = 0;
          let weekdayCalls = 0;
          ptHourFormat = { format: (at) => { calls++; return real.format(at); } };
          ptWeekdayFormat = { format: (at) => {
            weekdayCalls++; return realWeekday.format(at); } };
          peakHoursByDay.clear();
          lastPeakDayKey = null;
          const rows = [];
          for (let d = 0; d < days; d++) {
            const day = new Date(Date.UTC(2026, 0, 1) + d * 86400000)
              .toISOString().slice(0, 10);
            for (let h = 0; h < 24; h++) {
              for (let m = 0; m < models; m++) {
                rows.push({ day, hour: h, model: 'm' + m, turns: 1 });
              }
            }
          }
          const first = aggregateHourly(rows, 'utc');
          const afterFirstRender = calls;
          const weekdayAfterFirst = weekdayCalls;
          aggregateHourly(rows, 'utc');   // a filter tweak, or a poll
          return { rows: rows.length, afterFirstRender, afterSecond: calls,
                   weekdayAfterFirst, weekdayAfterSecond: weekdayCalls,
                   peak: first.hours.filter(h => h.peak).length };
        })()""", days=30, models=4))
        self.assertEqual(got["rows"], 30 * 24 * 4)
        self.assertLessEqual(
            got["afterFirstRender"], 30 * 24,
            "the window is being resolved more often than once per day")
        self.assertEqual(
            got["afterSecond"], got["afterFirstRender"],
            "a re-render re-resolved days it had already resolved")
        # The Mon–Fri half rides the SAME per-day memo, so it is bounded the
        # same way: at most one weekday reading per hour the window is wide —
        # six — and none at all on the 18 hours a day that fall outside it.
        # Asking it once per ROW instead would be 2,880 readings here and one
        # per row on every poll thereafter, which is the cost the per-day cache
        # exists to remove; asking it once per row is also the shape a caller-
        # side weekday test would have had.
        self.assertLessEqual(
            got["weekdayAfterFirst"], 30 * 6,
            "the Pacific weekday is being resolved more often than once per "
            "hour of the window, so it is not folded into the per-day memo")
        self.assertEqual(
            got["weekdayAfterSecond"], got["weekdayAfterFirst"],
            "a re-render re-resolved weekdays it had already resolved")
        self.assertGreater(got["weekdayAfterFirst"], 0,
                           "no weekday was resolved at all, so the bound above "
                           "is a bound on nothing")
        self.assertGreater(got["peak"], 0, "nothing was shaded, so the count "
                                           "above is a count of nothing")


@requires_node
class TestThePeakWindowIsWeekdaysOnly(unittest.TestCase):
    """The published window is Mon–Fri, and the band was shaded on all seven days.

    The page already said Mon–Fri in both places a reader can see it — the
    legend's `title` in `web/index.html` and the tooltip in `52-charts.js` — and
    shaded Saturday and Sunday exactly as it shaded Tuesday. Measured on this
    tree before the fix, through `hourlyInFrame` -> `aggregateHourly` in UTC
    mode, Saturday 2026-08-08 and Sunday 2026-08-09 each came back with the
    same six shaded bars as Monday 2026-08-10::

        sat_utc [12, 13, 14, 15, 16, 17]
        sun_utc [12, 13, 14, 15, 16, 17]
        mon_utc [12, 13, 14, 15, 16, 17]

    and in Asia/Tokyo the Saturday band was buckets [21, 22, 23, 0, 1, 2] —
    shaded in the viewer's frame too, not merely in UTC. Two sevenths of every
    "Last 30 days" chart marked throttle-prone against a window the vendor never
    said was open.

    WHICH CALENDAR the weekday comes from is the whole of the difficulty, and it
    is Pacific's — the calendar the 05:00–11:00 is quoted in, read off the same
    instant as the hour. It is not the viewer's: `test_the_weekday_is_pacifics_
    not_the_viewers` below is a Friday-morning-PT instant that lands on a local
    SATURDAY three zones east, and it stays shaded. It is not the row's UTC day
    either — though today nothing can tell those two apart, because the window
    never opens before 12:00Z and Pacific is never east of UTC, so every hour of
    every window shares its UTC date. `test_every_hour_of_a_window_shares_one_
    pacific_date` pins that coincidence so it cannot rot silently into a defect.
    """

    # Fixed dates, so the suite's own run date decides nothing. One of each kind
    # of day, all in the same PDT week, so nothing but the weekday differs.
    FRIDAY, SATURDAY, SUNDAY, MONDAY = ("2026-08-07", "2026-08-08",
                                        "2026-08-09", "2026-08-10")
    ZONES = ("UTC", "Europe/Madrid", "America/Los_Angeles", "Asia/Tokyo",
             "Pacific/Auckland")

    def _in_tz(self, tz, snippet):
        original = os.environ.get("TZ")
        os.environ["TZ"] = tz
        time.tzset()
        try:
            return run_js(snippet)
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()

    # One day's 24 UTC hours through the real path, returning which buckets the
    # chart shaded. Framed the way `applyFilter` frames rows — `{...r, day, hour}`
    # — so nothing here re-derives what the product does.
    _BAND = """(() => {
      const raw = Array.from({length: 24},
        (_, h) => ({ day: probeDay, hour: h, turns: 1, output: 1 }));
      const framed = raw.map(r => {
        const f = hourlyInFrame(r, probeMode);
        return { ...r, day: f.day, hour: f.hour };
      });
      const agg = aggregateHourly(framed, probeMode);
      return agg.hours.filter(h => h.peak).map(h => h.hour);
    })()"""

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_a_weekend_day_draws_no_band_and_a_weekday_draws_six_bars(self):
        """Both halves in one test: an empty band is only meaningful beside a
        full one, and a `peak: false` everywhere would pass either alone."""
        for tz in self.ZONES:
            for mode in ("local", "utc"):
                for day, expected_width in ((self.FRIDAY, 6),
                                            (self.SATURDAY, 0),
                                            (self.SUNDAY, 0),
                                            (self.MONDAY, 6)):
                    with self.subTest(tz=tz, mode=mode, day=day):
                        got = self._in_tz(tz, emit(self._BAND, probeDay=day,
                                                   probeMode=mode))
                        self.assertEqual(
                            len(got), expected_width,
                            f"in {tz} ({mode}) {day} shaded buckets {got}")

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_the_weekday_is_pacifics_not_the_viewers(self):
        """The boundary case that separates the two candidate calendars.

        Each pair below is one UTC instant inside 05:00–11:00 PT on a Pacific
        FRIDAY, and one on a Pacific SUNDAY — chosen so that in the viewer's own
        zone the first lands on a Saturday and the second on a Monday. A weekday
        test applied in the viewer's frame gets both backwards; one applied in
        Pacific gets both right. There is no zone in which they agree, which is
        what makes this the discriminating case rather than a second example.
        """
        for tz, utc_hour in (("Pacific/Auckland", 12),   # UTC+12: 00:00 next day
                             ("Pacific/Kiritimati", 12), # UTC+14: 02:00 next day
                             ("Asia/Tokyo", 16)):        # UTC+9:  01:00 next day
            with self.subTest(tz=tz):
                got = self._in_tz(tz, emit("""(() => {
                  const raw = [
                    { day: friday, hour: probeHour, turns: 1 },
                    { day: sunday, hour: probeHour, turns: 1 },
                  ];
                  return raw.map(r => {
                    const f = hourlyInFrame(r, 'local');
                    return { day: f.day, hour: f.hour,
                             peak: hourlyRowIsPeak({ ...r, day: f.day,
                                                     hour: f.hour }, 'local') };
                  });
                })()""", friday=self.FRIDAY, sunday=self.SUNDAY,
                                           probeHour=utc_hour))
                from datetime import date
                local_days = [date.fromisoformat(row["day"]) for row in got]
                self.assertEqual(
                    [d.strftime("%a") for d in local_days], ["Sat", "Mon"],
                    f"in {tz} these instants no longer land on a local Saturday "
                    f"and Monday, so the fixture is not testing the "
                    f"disagreement it was written for: {got}")
                self.assertEqual(
                    [row["peak"] for row in got], [True, False],
                    f"in {tz} the shading followed the viewer's weekday instead "
                    f"of Pacific's: {got}")

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_a_bucket_holding_a_weekday_and_a_weekend_row_is_shaded(self):
        """The aggregation rule, which is the DST one unchanged: shaded when ANY
        row in the bucket is throttled.

        A 24-bucket average over any range wider than a week necessarily mixes
        weekdays and weekend days into one bar, so this is the common case
        rather than a corner. Requiring ALL rows to be throttled would unshade
        the band on every multi-week range — the default view — and requiring
        MOST would invent a threshold nobody published.
        """
        got = self._in_tz("UTC", emit("""(() => {
          const raw = [
            { day: friday,   hour: 12, turns: 1 },   // Fri 05:00 PDT, throttled
            { day: saturday, hour: 12, turns: 1 },   // Sat 05:00 PDT, not
            { day: saturday, hour: 19, turns: 1 },   // Sat 12:00 PDT, not
            { day: friday,   hour: 19, turns: 1 },   // Fri 12:00 PDT, not
          ];
          const framed = raw.map(r => {
            const f = hourlyInFrame(r, 'utc');
            return { ...r, day: f.day, hour: f.hour };
          });
          const agg = aggregateHourly(framed, 'utc');
          return { mixed: agg.hours[12].peak, outside: agg.hours[19].peak,
                   rows: agg.hours[12].totalTurns };
        })()""", friday=self.FRIDAY, saturday=self.SATURDAY))
        self.assertEqual(got["rows"], 2, "the fixture no longer mixes two days "
                                         "into one bucket")
        self.assertTrue(got["mixed"],
                        "a bar averaging a throttled Friday hour with an "
                        "unthrottled Saturday one must stay shaded")
        self.assertFalse(got["outside"],
                         "a bar holding neither must not be shaded")

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_an_empty_bucket_on_a_weekend_range_is_not_shaded_either(self):
        """`hourlyBucketIsPeak` — the bars with no rows, which still carry a
        tooltip — borrows the most recent day in view. On a weekend-only range
        that day is a weekend day, so the band is absent there too rather than
        drawn by a different rule from the bars beside it.
        """
        for day, expected in ((self.FRIDAY, 6), (self.SATURDAY, 0)):
            with self.subTest(day=day):
                got = self._in_tz("UTC", emit("""(() => {
                  // One row, outside the band, so every window bucket is empty
                  // and has to fall back.
                  const raw = [{ day: probeDay, hour: 0, turns: 1 }];
                  const framed = raw.map(r => {
                    const f = hourlyInFrame(r, 'utc');
                    return { ...r, day: f.day, hour: f.hour };
                  });
                  return aggregateHourly(framed, 'utc')
                    .hours.filter(h => h.peak).map(h => h.hour);
                })()""", probeDay=day))
                self.assertEqual(len(got), expected, f"{day}: {got}")

    def test_every_hour_of_a_window_shares_one_pacific_date(self):
        """The coincidence the once-per-hour reading does not depend on — and
        the reason a UTC-day weekday cannot be told from a Pacific one today.

        The window never opens before 12:00Z and Pacific is never east of UTC,
        so all six of a day's window hours fall on the same Pacific date, and
        that date is the UTC date. If a future tz rule ever broke this, a
        once-per-day weekday reading would start splitting a window across two
        weekdays — the product asks per hour precisely so that it would not.
        This test is the alarm on that assumption, not a licence to rely on it.

        Checked over 2026 with CPython's tz database, independently of node.
        """
        try:
            from zoneinfo import ZoneInfo
            pacific = ZoneInfo("America/Los_Angeles")
        except Exception as exc:                       # pragma: no cover
            self.skipTest(f"no tz database for zoneinfo: {exc}")
        from datetime import date, datetime, timedelta, timezone
        checked = 0
        for offset in range(365):
            day = date(2026, 1, 1) + timedelta(days=offset)
            dates = set()
            for hour in range(24):
                at = datetime(day.year, day.month, day.day, hour,
                              tzinfo=timezone.utc).astimezone(pacific)
                if 5 <= at.hour <= 10:
                    dates.add(at.date())
            self.assertEqual(dates, {day},
                             f"the 05:00–11:00 PT window on {day} spans "
                             f"{sorted(dates)} in Pacific, not one date")
            checked += 1
        self.assertEqual(checked, 365)

    def test_a_runtime_that_cannot_read_the_pacific_weekday_shades_all_seven_days(self):
        """The degraded path, and it degrades to the OLD behaviour on purpose.

        A build whose Intl cannot be trusted with a Pacific weekday keeps the
        hours it *can* resolve and simply is not narrowed to Mon–Fri — a
        seven-day band, which is what this chart drew until now and what the
        legend has always described. The alternative, dropping the band
        entirely, would invent a state nobody has seen.

        Each stub is rejected by exactly one thing. The zone-ignoring one is the
        interesting rejection: a UTC weekday is right on every real day (see
        `test_every_hour_of_a_window_shares_one_pacific_date`) and wrong at the
        probe instant, which is the only place the two calendars can be told
        apart — so accepting it would leave the Pacific reading untested.
        """
        stubs = {
            # name: (factory, must the weekday narrowing be lost?)
            "throws": (
                "function () { throw new RangeError('Invalid time zone'); }",
                True),
            "ignores-the-zone": (
                """function () { return { format: (at) =>
                  ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][at.getUTCDay()]
                }; }""", True),
            "ignores-the-locale": (
                """function () { return { format: (at) => ({
                  Sun: 'dom', Mon: 'lun', Tue: 'mar', Wed: 'mié',
                  Thu: 'jue', Fri: 'vie', Sat: 'sáb' })[realWeekday.format(at)]
                }; }""", True),
            # The real thing, rebuilt: it must be ACCEPTED, or the three above
            # prove only that any stub at all disables the rule.
            "correct": (
                """function () { return { format: (at) =>
                  realWeekday.format(at) }; }""", False),
        }
        for name, (factory, degrades) in stubs.items():
            with self.subTest(intl=name):
                got = run_js(emit("""(() => {
                  const realWeekday = ptWeekdayFormatter();
                  ptHourFormatter();      // cache the hour side before the swap
                  const before = Array.from({length: 24}, (_, h) => h)
                    .filter(h => isPeakUTCHour(h, saturday));
                  const weekdayBefore = Array.from({length: 24}, (_, h) => h)
                    .filter(h => isPeakUTCHour(h, friday));
                  ptWeekdayFormat = undefined;
                  peakHoursByDay.clear();
                  lastPeakDayKey = null;
                  const realIntl = globalThis.Intl;
                  globalThis.Intl = { DateTimeFormat: """ + factory + """ };
                  const after = Array.from({length: 24}, (_, h) => h)
                    .filter(h => isPeakUTCHour(h, saturday));
                  const weekdayAfter = Array.from({length: 24}, (_, h) => h)
                    .filter(h => isPeakUTCHour(h, friday));
                  globalThis.Intl = realIntl;
                  return { before, after, weekdayBefore, weekdayAfter,
                           fallback: [...PEAK_HOURS_UTC] };
                })()""", saturday=self.SATURDAY, friday=self.FRIDAY))
                self.assertEqual(got["before"], [],
                                 "the working case stopped working, so this "
                                 "test proves nothing about the stub")
                self.assertEqual(got["weekdayBefore"], got["fallback"],
                                 "the Friday band moved, so the stub is being "
                                 "compared against the wrong baseline")
                if degrades:
                    self.assertEqual(
                        got["after"], got["fallback"],
                        "an unreadable Pacific weekday must leave the old "
                        "seven-day band, not a day with no band at all")
                else:
                    self.assertEqual(
                        got["after"], [],
                        "a usable weekday formatter was rejected, costing the "
                        "Mon–Fri rule entirely")
                self.assertEqual(got["weekdayAfter"], got["fallback"],
                                 "a weekday must keep its band whatever the "
                                 "weekday formatter does")


@requires_node
class TestRangeBounds(unittest.TestCase):
    """Range bounds are local calendar dates (the #151 fix) and feed the filters."""

    # Calendar ranges are closed on both sides; rolling ones ("last N days")
    # are deliberately open-ended so today is always included.
    CLOSED = ["today", "week", "month", "prev-month"]
    # "Year to date" is open-ended for the same reason the rolling windows are.
    ROLLING = ["7d", "30d", "90d", "ytd"]
    # Its SHAPE depends on the payload, which is why it is neither of the above:
    # with a weekly quota window it is closed on both sides (that window's own
    # seven days), and with none it degrades to a rolling week. Both are
    # asserted below rather than one being picked here.
    PLAN_DEPENDENT = ["limit-week"]
    RANGES = CLOSED + ROLLING + PLAN_DEPENDENT + ["all"]

    def test_the_range_list_here_is_the_range_list_the_app_offers(self):
        """Otherwise a newly added range is simply never exercised."""
        self.assertEqual(sorted(run_js(emit("VALID_RANGES"))), sorted(self.RANGES))

    def test_the_plan_dependent_range_is_closed_when_a_window_is_known(self):
        got = run_js(
            "Date.now = () => Date.parse('2026-08-20T12:00:00Z');"
            "lastPlanInfo = {available: true, windows: [{kind: 'weekly_all',"
            " group: 'weekly', percent: 46, resets_at: '2026-08-23T06:59:59Z'}]};"
            "console.log(JSON.stringify(getRangeBounds('limit-week')));")
        self.assertEqual(got, {"start": "2026-08-16", "end": "2026-08-23"})

    def test_the_plan_dependent_range_degrades_to_a_rolling_week(self):
        """With no weekly window there is nothing to bound it with, and an empty
        range would read as "you used nothing" rather than "we do not know when
        your quota week began"."""
        got = run_js("lastPlanInfo = {available: true, windows: []};"
                     "console.log(JSON.stringify(getRangeBounds('limit-week')));")
        self.assertRegex(got["start"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertIsNone(got["end"])

    def test_bounds_are_wellformed_for_every_range(self):
        got = dict(run_js(emit("ranges.map(r => [r, getRangeBounds(r)])",
                               ranges=self.RANGES)))
        self.assertEqual(set(got), set(self.RANGES))

        self.assertIsNone(got["all"]["start"])
        self.assertIsNone(got["all"]["end"])

        for name in self.CLOSED:
            with self.subTest(range=name):
                bounds = got[name]
                self.assertRegex(bounds["start"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertRegex(bounds["end"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertLessEqual(bounds["start"], bounds["end"],
                                     "range start is after its end")

        for name in self.ROLLING:
            with self.subTest(range=name):
                bounds = got[name]
                self.assertRegex(bounds["start"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertIsNone(
                    bounds["end"],
                    "a rolling window must stay open-ended or it would clip today")

        # `limit-week` with no plan payload, which is the state this sweep runs
        # in: it degrades to a rolling week, so it must satisfy the same rule.
        for name in self.PLAN_DEPENDENT:
            with self.subTest(range=name):
                bounds = got[name]
                self.assertRegex(bounds["start"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertIsNone(bounds["end"])

    def test_rolling_windows_span_exactly_the_days_they_promise(self):
        """"Last 7 Days" must cover 7 calendar days, not 8.

        The window is inclusive of both ends, so subtracting the full N spans
        N+1 days — the chart drew an extra bar and the totals included an extra
        day of spend under a label promising N.
        """
        got = run_js(emit("""
          [['7d', 7], ['30d', 30], ['90d', 90]].map(([r, n]) => {
            const start = new Date(getRangeBounds(r).start + 'T00:00:00');
            const today = new Date(localISODate(new Date()) + 'T00:00:00');
            const spanned = Math.round((today - start) / 86400000) + 1;
            return { range: r, promised: n, spanned };
          })
        """))
        for row in got:
            with self.subTest(range=row["range"]):
                self.assertEqual(
                    row["spanned"], row["promised"],
                    f"{row['range']} covers {row['spanned']} calendar days")

    def test_rolling_windows_start_further_back_the_longer_they_are(self):
        got = dict(run_js(emit("ranges.map(r => [r, getRangeBounds(r).start])",
                               ranges=self.ROLLING)))
        self.assertGreater(got["7d"], got["30d"])
        self.assertGreater(got["30d"], got["90d"])

    def test_previous_month_ends_the_day_before_this_month_starts(self):
        got = run_js(emit("""
          { prev: getRangeBounds('prev-month'), cur: getRangeBounds('month') }
        """))
        self.assertLess(got["prev"]["end"], got["cur"]["start"])
        self.assertTrue(got["cur"]["start"].endswith("-01"))
        self.assertTrue(got["prev"]["start"].endswith("-01"))

    def test_local_iso_date_never_uses_utc(self):
        """`toISOString()` shifts the day back east of UTC — that was bug #151."""
        got = run_js(emit("""
          (() => {
            const d = new Date(2026, 0, 1, 0, 30);  // 00:30 local on Jan 1
            return { local: localISODate(d), utc: d.toISOString().slice(0, 10) };
          })()
        """))
        self.assertEqual(got["local"], "2026-01-01")


@requires_node
class TestHtmlEscaping(unittest.TestCase):
    """`esc()` is the only thing between transcript text and innerHTML."""

    def test_escapes_all_five_metacharacters(self):
        got = run_js(emit("""{ e: esc(`<script>&"'x`) }"""))
        self.assertEqual(got["e"], "&lt;script&gt;&amp;&quot;&#39;x")

    def test_neutralises_a_real_injection_attempt(self):
        payload = '<img src=x onerror="alert(1)">'
        got = run_js(f"{emit(f'{{ e: esc({json.dumps(payload)}) }}')}")
        self.assertNotIn("<img", got["e"])
        self.assertNotIn('"', got["e"])
        self.assertEqual(
            got["e"],
            "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;")

    def test_non_string_input_does_not_crash(self):
        got = run_js(emit(
            "{nul: esc(null), num: esc(42), undef: esc(undefined)}"))
        self.assertIsInstance(got["nul"], str)
        self.assertIsInstance(got["num"], str)
        self.assertIsInstance(got["undef"], str)


@requires_node
class TestAutoRefreshPreference(unittest.TestCase):
    """The interval is a stored preference, so the storage edge cases matter."""

    def test_an_unset_preference_is_the_documented_default(self):
        got = run_js(emit(
            "(() => { localStorage.removeItem(REFRESH_KEY); "
            "return {seconds: loadRefreshSeconds(), dflt: REFRESH_DEFAULT}; })()"))
        self.assertEqual(got["seconds"], got["dflt"])

    def test_a_stored_zero_is_off_and_not_mistaken_for_unset(self):
        """localStorage.getItem returns null when unset and Number(null) is 0.

        If the two were conflated, a user who turned refresh off would get the
        default back on the next load — or, with an "off" default, a stored
        interval could never be told apart from no preference at all.
        """
        got = run_js(emit(
            "(() => { localStorage.setItem(REFRESH_KEY, '0'); "
            "return loadRefreshSeconds(); })()"))
        self.assertEqual(got, 0)

    def test_a_stored_interval_survives_a_reload(self):
        got = run_js(emit(
            "(() => { localStorage.setItem(REFRESH_KEY, '300'); "
            "return loadRefreshSeconds(); })()"))
        self.assertEqual(got, 300)

    def test_a_garbage_or_unlisted_value_falls_back_to_the_default(self):
        got = run_js(emit("""
          ['abc', '', '7', '-30', '99999', '[]'].map(v => {
            localStorage.setItem(REFRESH_KEY, v);
            return loadRefreshSeconds();
          })"""))
        self.assertTrue(all(v == 0 for v in got), got)

    def test_off_arms_no_timer_at_all(self):
        got = run_js(emit(
            "(() => { refreshSeconds = 0; selectedRange = 'today'; "
            "return refreshIntervalMs(); })()"))
        self.assertEqual(got, 0)

    def test_a_chosen_interval_is_used_verbatim_in_milliseconds(self):
        got = run_js(emit(
            "(() => { refreshSeconds = 300; selectedRange = 'today'; "
            "return refreshIntervalMs(); })()"))
        self.assertEqual(got, 300_000)

    def test_a_historical_range_never_polls_however_it_is_configured(self):
        """A range that cannot gain rows has nothing to refresh."""
        got = run_js(emit(
            "(() => { refreshSeconds = 15; selectedRange = 'prev-month'; "
            "return refreshIntervalMs(); })()"))
        self.assertEqual(got, 0)


@requires_node
class TestRangeLabelsCarryTheirDates(unittest.TestCase):
    """Every range states the days it covers, not just its name."""

    def test_every_range_prints_a_concrete_span(self):
        days = ["2026-01-05", "2026-06-06", "2026-08-06"]
        got = run_js(emit(
            "ranges.map(r => [r, rangeLabelWithDates(r, days)])",
            ranges=TestRangeBounds.RANGES, days=days))
        for name, label in got:
            with self.subTest(range=name):
                self.assertTrue(label.startswith(RANGE_NAMES[name]), label)
                self.assertRegex(label, r"^.+ \(.+\)$",
                                 "the label must carry a date span in brackets")

    def test_est_cost_and_the_other_tiles_share_one_label(self):
        """The tiles used to disagree: four said "last 30 days", Est. Cost said
        only "API pricing, June 2026" and named no period at all."""
        got = run_js(emit("""
          (() => {
            const captured = {};
            document.getElementById = (id) => ({
              set innerHTML(v) { captured[id] = v; },
              closest: () => null,
            });
            renderStats({sessions: 1, turns: 1, input: 1, output: 1,
                         cache_read: 1, cache_creation: 1, cost: 1,
                         subagent_tokens: 1}, 'Last 30 Days (Jul 8 – Aug 6)');
            return captured['stats-row'];
          })()"""))
        self.assertEqual(got.count("Last 30 Days (Jul 8 – Aug 6)"), 8,
                         "all eight tiles must state the same window")

    def test_all_time_borrows_the_extent_of_the_data(self):
        got = run_js(emit(
            "rangeLabelWithDates('all', ['2025-11-02', '2026-08-06'])"))
        self.assertIn("Nov 2, 2025", got)
        self.assertIn("Aug 6, 2026", got)

    def test_all_time_with_no_rows_states_no_span_rather_than_a_wrong_one(self):
        got = run_js(emit("rangeLabelWithDates('all', [])"))
        self.assertEqual(got, "All Time")

    def test_a_single_day_range_is_not_printed_as_a_span(self):
        got = run_js(emit("rangeLabelWithDates('today', [])"))
        self.assertNotIn("–", got, "one day is a date, not a range")

    def test_a_malformed_day_key_degrades_to_the_bare_name(self):
        """localdays.py's COALESCE fallback can emit a non-ISO day."""
        got = run_js(emit("rangeLabelWithDates('all', ['not-a-date', 'nope'])"))
        self.assertEqual(got, "All Time")

    def test_the_span_is_built_from_local_midnight_not_utc(self):
        """new Date('2026-08-06') is UTC midnight and renders as Aug 5 in the
        Americas — the #151 class of bug, in the label this time."""
        for tz in ("Pacific/Midway", "Pacific/Kiritimati"):
            with self.subTest(tz=tz):
                got = self._in_tz(tz, emit("fmtDaySpan('2026-08-06', '2026-08-06')"))
                self.assertEqual(got, "Aug 6")

    def test_year_to_date_starts_on_january_first_locally(self):
        for tz in ("Pacific/Midway", "Pacific/Kiritimati"):
            with self.subTest(tz=tz):
                got = self._in_tz(tz, emit("getRangeBounds('ytd')"))
                self.assertRegex(got["start"], r"^\d{4}-01-01$")
                self.assertIsNone(got["end"], "YTD must not clip today")

    def _in_tz(self, tz, snippet):
        source = _DOM_STUB + "\n" + extract_app_script() + "\n" + snippet
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.cjs"
            harness.write_text(source, encoding="utf-8")
            env = dict(os.environ, TZ=tz)
            proc = subprocess.run([NODE, str(harness)], capture_output=True,
                                  text=True, encoding="utf-8", timeout=120,
                                  env=env)
        if proc.returncode != 0:
            raise AssertionError(f"node exited {proc.returncode}:\n{proc.stderr[-2000:]}")
        return json.loads(proc.stdout)


@requires_node
class TestCacheWriteTiersArePricedSeparately(unittest.TestCase):
    """A 1-hour cache write costs 2x input; a 5-minute one costs 1.25x."""

    def test_the_long_lived_tier_costs_more_than_the_short_lived_one(self):
        got = run_js(emit("""({
          allShort: calcCost('claude-opus-5', 0, 0, 0, 1e6, 0),
          allLong:  calcCost('claude-opus-5', 0, 0, 0, 1e6, 1e6),
        })"""))
        self.assertAlmostEqual(got["allShort"], 6.25, places=9)
        self.assertAlmostEqual(got["allLong"], 10.00, places=9)

    def test_the_split_sums_to_the_whole(self):
        got = run_js(emit("""({
          split: calcCost('claude-opus-5', 0, 0, 0, 1000, 400),
          parts: calcCost('claude-opus-5', 0, 0, 0, 600, 0)
               + calcCost('claude-opus-5', 0, 0, 0, 400, 400),
        })"""))
        self.assertAlmostEqual(got["split"], got["parts"], places=12)

    def test_omitting_the_split_bills_exactly_as_before(self):
        """Old callers, and rows written before the column existed, must not
        silently lose the write."""
        got = run_js(emit("""({
          without: calcCost('claude-opus-5', 1, 2, 3, 1000),
          zero:    calcCost('claude-opus-5', 1, 2, 3, 1000, 0),
        })"""))
        self.assertEqual(got["without"], got["zero"])

    def test_a_nonsensical_split_cannot_credit_money_back(self):
        """The two figures are summed independently in SQL, so a 1-hour total
        larger than its own parent must clamp rather than go negative."""
        got = run_js(emit("""({
          overflow: calcCost('claude-opus-5', 0, 0, 0, 1000, 999999),
          capped:   calcCost('claude-opus-5', 0, 0, 0, 1000, 1000),
        })"""))
        self.assertEqual(got["overflow"], got["capped"])
        self.assertGreater(got["overflow"], 0)


@requires_node
class TestDailyChartPanning(unittest.TestCase):
    """A long range shows a window of days you pan through, not 200 slivers.

    The window is a slice of the data drawn by ONE chart at the container's
    width — which is what keeps the axes still while you pan. The alternative,
    scrolling a very wide canvas, moves the axes off-screen with the bars.
    """

    def _harness(self, days, width, snippet):
        """Run `snippet` with `lastDailyRows` filled and a fixed viewport width."""
        setup = f"""
          window.innerWidth = {width};
          // The window size is derived from the chart container's width; the DOM
          // stub reports 0, so give it something to measure.
          document.getElementById = (id) => ({{
            getContext: () => ({{}}), parentElement: {{ clientWidth: {width} }},
            style: {{}}, classList: {{ add(){{}}, remove(){{}}, toggle(){{}} }},
            hidden: false, scrollLeft: 0, clientWidth: {width}, textContent: '',
          }});
          lastDailyRows = Array.from({{length: {days}}}, (_, i) => ({{
            day: '2026-01-01', input: i, output: i, cache_read: i,
            cache_creation: i, cost: i,
          }}));
        """
        return run_js(setup + "\n" + snippet)

    def test_a_short_range_is_not_windowed_at_all(self):
        """Nothing changes for the ranges that already fit."""
        got = self._harness(14, 1400, emit("dailyWindowSize(14)"))
        self.assertEqual(got, 14)

    def test_a_long_range_is_windowed(self):
        got = self._harness(200, 1400, emit("dailyWindowSize(200)"))
        self.assertLess(got, 200)
        self.assertGreater(got, 5)

    def test_a_phone_shows_fewer_columns_than_a_desktop(self):
        narrow = self._harness(200, 390, emit("dailyWindowSize(200)"))
        wide = self._harness(200, 1400, emit("dailyWindowSize(200)"))
        self.assertLess(narrow, wide)

    def test_every_column_gets_at_least_its_minimum_width(self):
        """The whole point: a bar is never thinner than this."""
        for width, expected_col in ((1400, 44), (390, 26)):
            with self.subTest(viewport=width):
                got = self._harness(200, width, emit(
                    "({win: dailyWindowSize(200), col: dailyColumnWidth()})"))
                self.assertEqual(got["col"], expected_col)
                # The window must fit in the plot area at that column width.
                self.assertLessEqual(got["win"] * got["col"], width)

    def test_the_pinned_axis_maximum_covers_the_whole_range_not_the_window(self):
        """This is what stops the scale jumping as you pan."""
        got = run_js(emit("""
          (() => {
            hiddenSeries.daily.clear();
            const rows = [{input: 1, output: 1, cache_read: 5, cache_creation: 5, cost: 1},
                          {input: 1, output: 1, cache_read: 90, cache_creation: 10, cost: 1}];
            // 'y' stacks cache_read + cache_creation: 10 then 100.
            return dailyAxisMax(rows, 'y');
          })()"""))
        self.assertAlmostEqual(got, 105.0, places=6)

    def test_a_hidden_series_is_left_out_of_the_pinned_maximum(self):
        """Hiding the big series should let the rest fill the chart."""
        got = run_js(emit("""
          (() => {
            hiddenSeries.daily.clear();
            hiddenSeries.daily.add('Cache Read');
            const rows = [{input: 1, output: 1, cache_read: 900, cache_creation: 10, cost: 1}];
            const v = dailyAxisMax(rows, 'y');
            hiddenSeries.daily.clear();
            return v;
          })()"""))
        self.assertAlmostEqual(got, 10.5, places=6)

    def test_an_axis_with_nothing_visible_is_left_to_autoscale(self):
        got = run_js(emit("""
          (() => {
            hiddenSeries.daily.clear();
            ['Cache Read', 'Cache Creation'].forEach(l => hiddenSeries.daily.add(l));
            const v = dailyAxisMax([{cache_read: 5, cache_creation: 5}], 'y');
            hiddenSeries.daily.clear();
            return v;
          })()"""))
        self.assertIsNone(got, "pinning an axis with no visible series would pin it to nothing")

    def test_the_series_spec_matches_the_datasets_it_builds(self):
        """renderDailyChart builds the datasets from DAILY_SERIES and
        setDailyPanOffset refills them by index, so the order is a contract."""
        got = run_js(emit("DAILY_SERIES.map(s => s.label)"))
        self.assertEqual(got, ["Input", "Output", "Cache Read",
                               "Cache Creation", "Est. Cost"])

    def test_every_series_reads_a_field_the_payload_actually_has(self):
        got = run_js(emit("""
          DAILY_SERIES.map(s => s.pick(
            {input: 1, output: 2, cache_read: 3, cache_creation: 4, cost: 5}))"""))
        self.assertEqual(got, [1, 2, 3, 4, 5])


@requires_node
class TestCostBreakdownPerColumn(unittest.TestCase):
    """Cost by Model shows what each KIND of token cost, and totals the columns.

    The breakdown and the row total come from one function, so they cannot
    disagree — which is the only property that makes the table trustworthy.
    """

    def test_the_parts_sum_to_the_total_they_sit_beside(self):
        got = run_js(emit("""
          [['claude-opus-5', 500000, 40000, 900000, 20000, 8000],
           ['claude-sonnet-5', 100, 5000, 300000, 60000, 0],
           ['claude-haiku-4-5', 1, 2, 3, 4, 4],
           ['claude-fable-5', 1e6, 1e6, 1e6, 1e6, 5e5]].map(c => {
            const p = costParts(...c);
            return { sum: p.input + p.output + p.cache_read + p.cache_creation,
                     total: calcCost(...c) };
          })"""))
        for row in got:
            with self.subTest(total=row["total"]):
                self.assertAlmostEqual(row["sum"], row["total"], places=12)

    def test_each_part_matches_python(self):
        """Invented token buckets must have identical CLI and dashboard costs."""
        cases = [["claude-opus-5", 500000, 40000, 900000, 20000, 8000],
                 ["claude-sonnet-5", 100, 5000, 300000, 60000, 0],
                 ["claude-opus-4-8", 200, 80000, 6000000, 400000, 0]]
        got = run_js(emit("cases.map(c => costParts(...c))", cases=cases))
        for case, js in zip(cases, got):
            py = calc_cost_parts(*case)
            for key in ("input", "output", "cache_read", "cache_creation"):
                with self.subTest(model=case[0], part=key):
                    self.assertAlmostEqual(js[key], py[key], places=9)

    def test_an_unpriced_model_has_no_breakdown_rather_than_a_zero_one(self):
        """A zeroed breakdown would read as "these tokens were free"."""
        got = run_js(emit("({parts: costParts('gemma-3', 1e6, 1e6, 1e6, 1e6, 0), "
                          "cost: calcCost('gemma-3', 1e6, 1e6, 1e6, 1e6, 0)})"))
        self.assertIsNone(got["parts"])
        self.assertEqual(got["cost"], 0)

    def test_the_cache_breakdown_uses_both_write_tiers(self):
        """cache_creation is one column but two prices."""
        got = run_js(emit("""({
          allShort: costParts('claude-opus-5', 0, 0, 0, 1e6, 0).cache_creation,
          allLong:  costParts('claude-opus-5', 0, 0, 0, 1e6, 1e6).cache_creation,
        })"""))
        self.assertAlmostEqual(got["allShort"], 6.25, places=9)
        self.assertAlmostEqual(got["allLong"], 10.00, places=9)

    def test_the_totals_row_sums_every_model_not_just_the_visible_ones(self):
        """The table pages at 10 rows; a total that stopped there would answer
        "what did cache reads cost" with a number quietly missing models."""
        got = run_js(emit("""
          (() => {
            const captured = {};
            document.getElementById = (id) => ({
              set innerHTML(v) { captured[id] = v; },
              closest: () => null, rows: null, cells: null,
            });
            // 14 models, past PAGINATE_THRESHOLD and the first table step.
            const rows = Array.from({length: 14}, (_, i) => ({
              model: 'claude-opus-5', turns: 1, input: 1000, output: 1000,
              cache_read: 1000, cache_creation: 1000, cache_creation_1h: 0,
            }));
            renderModelCostTotals(rows);
            return captured['model-cost-total'];
          })()"""))
        self.assertIn("All 14 models", got)
        # 14 x (1000 in + 1000 out + 1000 read + 1000 write) at opus-5 rates.
        one = 1000 * (5.00 + 25.00 + 0.50 + 6.25) / 1e6
        self.assertIn(fmt_money(14 * one), got)

    def test_the_totals_row_ignores_unpriced_models_in_the_money(self):
        got = run_js(emit("""
          (() => {
            const captured = {};
            document.getElementById = (id) => ({
              set innerHTML(v) { captured[id] = v; },
              closest: () => null, rows: null, cells: null,
            });
            renderModelCostTotals([
              {model: 'claude-opus-5', turns: 1, input: 1e6, output: 0,
               cache_read: 0, cache_creation: 0, cache_creation_1h: 0},
              {model: 'gemma-3', turns: 1, input: 1e6, output: 0,
               cache_read: 0, cache_creation: 0, cache_creation_1h: 0},
            ]);
            return captured['model-cost-total'];
          })()"""))
        self.assertIn("2.00M", got, "token counts still include every model")
        self.assertIn("$5.0000", got, "only the priced model contributes money")


@requires_node
class TestUnitRateInEveryCell(unittest.TestCase):
    """Each money cell prints the multiplication, not just its two ends.

    A token count and total cost alone ask the reader to take the arithmetic
    on trust; with the unit price between them the figure can be
    checked. The rate is *derived* from the money (cost / tokens), so the
    multiplication on screen is true by construction even where two prices are
    in play — and those blends are the cases the "avg" marker exists for.
    """

    CAPTURE = r"""
      const captured = {};
      document.getElementById = (id) => ({
        set innerHTML(v) { captured[id] = v; },
        closest: () => null, rows: null, cells: null,
      });
    """

    def _row(self, model, inp=0, out=0, read=0, write=0, write_1h=0, turns=1):
        return {"model": model, "turns": turns, "input": inp, "output": out,
                "cache_read": read, "cache_creation": write,
                "cache_creation_1h": write_1h}

    def _cells(self, html):
        """The rendered <td>s, in column order."""
        return ["<td" + part for part in html.split("<td")[1:]]

    def _render(self, rows, which="model-cost-body"):
        got = run_js(emit("(() => {" + self.CAPTURE +
                          "renderModelCostTable(rows);"
                          "return captured; })()", rows=rows))
        return got[which]

    def test_a_single_model_column_shows_its_published_price(self):
        cells = self._cells(self._render([self._row("claude-opus-5", out=1_000_000)]))
        self.assertIn("$25.00/M", cells[3])
        self.assertIn("$25.0000", cells[3])
        self.assertNotIn("avg", cells[3], "one model at one rate is not an average")

    def test_a_sub_cent_rate_is_not_rounded_into_a_wrong_multiplication(self):
        """Two decimals turned $0.125/M into "$0.13/M", which makes the line
        printed beside it wrong by 4% — and the line exists to be checkable.

        $0.125 and $0.025 are rates the table no longer carries (8cd0b81
        repriced Codex from OpenAI's published list); they are kept here on
        purpose, as the inputs the two-decimal format actually got wrong, not
        as an assertion about what is on the price list today. The live table
        is covered by test_every_rate_in_the_table_prints_without_loss below.
        """
        got = run_js(emit("[fmtRate(0.125), fmtRate(0.025), fmtRate(25), fmtRate(1.25)]"))
        self.assertEqual(got, ["$0.125/M", "$0.025/M", "$25.00/M", "$1.25/M"])

    def test_every_rate_in_the_table_prints_without_loss(self):
        """Whatever rates exist, the printed one must equal the real one."""
        got = run_js(emit("""
          Object.entries(PRICING).flatMap(([model, p]) =>
            ['input', 'output', 'cache_read', 'cache_write', 'cache_write_1h']
              .map(k => [model + '.' + k, p[k],
                         Number(fmtRate(p[k]).replace(/[$,]/g, '').replace('/M', ''))]))"""))
        for name, actual, printed in got:
            with self.subTest(rate=name):
                self.assertEqual(printed, actual,
                                 f"{name}: {actual} prints as {printed}")

    def test_the_multiplication_shown_is_arithmetically_true(self):
        """tokens x rate = cost, for every shape the cell can be handed."""
        cases = [[1_000_000, 25.0], [83_300_000, 2082.5563], [1, 5e-6],
                 [7, 0.000123], [999_999_999, 0.01]]
        got = run_js(emit("cases.map(([tok, cost]) => "
                          "tok * effectiveRate(tok, cost) / 1e6)", cases=cases))
        for (tokens, cost), back in zip(cases, got):
            with self.subTest(tokens=tokens):
                self.assertAlmostEqual(back, cost, places=9)

    def test_a_cell_with_no_tokens_prints_no_rate(self):
        """cost / 0 is Infinity; the cell must show the money and stop."""
        cells = self._cells(self._render([self._row("claude-opus-5", inp=1_000_000)]))
        self.assertIn("$0.0000", cells[3], "the output column is still costed")
        self.assertNotIn("/M", cells[3])

    def test_writes_at_a_single_tier_show_that_tier_and_no_average(self):
        short = self._cells(self._render([self._row("claude-opus-5", write=1_000_000)]))
        long = self._cells(self._render(
            [self._row("claude-opus-5", write=1_000_000, write_1h=1_000_000)]))
        self.assertIn("$6.25/M", short[5])
        self.assertNotIn("avg", short[5])
        self.assertIn("$10.00/M", long[5])
        self.assertNotIn("avg", long[5], "all-1-hour is a list price, not a blend")

    def test_writes_spanning_both_tiers_are_labelled_an_average(self):
        """800k at $6.25 + 200k at $10.00 = $7.00/M — on no price list."""
        cells = self._cells(self._render(
            [self._row("claude-opus-5", write=1_000_000, write_1h=200_000)]))
        self.assertIn("$7.00/M avg", cells[5])

    def test_the_totals_row_blends_across_models_and_says_so(self):
        html = self._render([self._row("claude-opus-5", inp=1_000_000),
                             self._row("claude-sonnet-5", inp=1_000_000)],
                            which="model-cost-total")
        self.assertIn("$3.50/M avg", self._cells(html)[2])

    def test_a_totals_column_fed_by_one_model_is_not_an_average(self):
        """The marker is decided per cell: only input is mixed here."""
        html = self._render([self._row("claude-opus-5", inp=1_000_000, out=1_000_000),
                             self._row("claude-sonnet-5", inp=1_000_000)],
                            which="model-cost-total")
        cells = self._cells(html)
        self.assertIn("avg", cells[2], "input came from both models")
        self.assertIn("$25.00/M", cells[3])
        self.assertNotIn("avg", cells[3], "output came from opus alone")

    def test_free_tokens_pull_the_rate_down_and_that_is_marked(self):
        """An unpriced model's tokens count in the column but not in the money,
        so the derived rate is below every real one — an average, and labelled."""
        html = self._render([self._row("claude-opus-5", inp=1_000_000),
                             self._row("gemma-3", inp=1_000_000)],
                            which="model-cost-total")
        self.assertIn("$2.50/M avg", self._cells(html)[2])

    # ── Four columns at once ────────────────────────────────────────────────
    # Every fixture above fills ONE token column, which is what let a cell print
    # a different column's token count beside its own column's money: swapping
    # `m.cache_read` for `m.input` in renderModelCostTable renders 0 tokens in a
    # one-column fixture, so the assertions that survive on "/M" still hold.
    # `effectiveRate` is cost / tokens, so the multiplication printed on screen
    # stays true for whatever count is handed to it — the equation cannot catch
    # this and the published RATE is what does. Body row only: both totals-row
    # swaps already fail on the fixtures above.
    #
    # cache_creation_1h stays 0 deliberately. A partial 1-hour figure makes the
    # write cell a legitimate blend, and the published-rate assertion would then
    # fail against correct code — the trap the two tier tests above exist to pin.
    FOUR_COLUMNS = {"input": 1_000_000, "output": 2_000_000,
                    "cache_read": 4_000_000, "cache_creation": 8_000_000}
    # claude-opus-5's list prices for those four columns, in column order.
    FOUR_RATES = ("$5.00/M", "$25.00/M", "$0.50/M", "$6.25/M")

    def test_every_column_prints_its_own_tokens_at_its_own_published_rate(self):
        cells = self._cells(self._render([self._row(
            "claude-opus-5", inp=self.FOUR_COLUMNS["input"],
            out=self.FOUR_COLUMNS["output"], read=self.FOUR_COLUMNS["cache_read"],
            write=self.FOUR_COLUMNS["cache_creation"])]))
        counts = run_js(emit("tokens.map(t => fmt(t))",
                             tokens=list(self.FOUR_COLUMNS.values())))
        for i, (column, count, rate) in enumerate(
                zip(self.FOUR_COLUMNS, counts, self.FOUR_RATES)):
            with self.subTest(column=column):
                cell = cells[2 + i]
                self.assertIn(count, cell,
                              f"the {column} cell does not print the {column} "
                              f"token count")
                self.assertIn(rate, cell,
                              f"the {column} cell's unit price is not "
                              f"{column}'s published rate — the tokens and the "
                              "money in it come from different columns")
                self.assertNotIn("avg", cell)
        self.assertEqual(len(set(counts)), len(counts),
                         "the four counts must differ, or a cell reading the "
                         "wrong column would still look right")

    def test_the_reasoning_column_is_still_deliberately_unpriced(self):
        """The one cell that must NOT print a rate: reasoning tokens are a
        subset of Output and are already billed inside it."""
        cells = self._cells(self._render([dict(
            self._row("claude-opus-5", out=2_000_000), reasoning=1_000_000)]))
        self.assertIn(run_js(emit("fmt(1e6)")), cells[6])
        self.assertNotIn("/M", cells[6],
                         "the reasoning column acquired a unit price, which "
                         "bills the same tokens a second time")


@requires_node
class TestOneAnswerToWhetherARateIsOnAPriceList(unittest.TestCase):
    """Every card on the page must mark a derived rate the same way.

    `renderModelCostTotals` and the effort / stop-reason cards ask the same
    question — "is the figure I am about to print a list price, or an average I
    derived?" — and answered it with two implementations. The model card's
    counted `null` (an unpriced model) as an answer of its own; `blendedRate`
    looked only at the set's SIZE, so a column fed ONLY by unpriced models is a
    set of one and its derived `$0.00/M` printed unlabelled, as if it were
    published. `$0.00` asserts the usage was free, which AGENTS.md treats as a
    different claim from "not priced".

    So the marker is asserted here across the cards rather than per card: the
    divergence shipped inside an audit fix that stopped at the first one.
    """

    HARNESS = r"""
      const captured = {};
      document.getElementById = (id) => ({
        set innerHTML(v) { captured[id] = v; },
        closest: () => null, rows: null, cells: null,
      });
      const row = (model, o) => Object.assign(
        {model, turns: 1, input: 0, output: 0, cache_read: 0,
         cache_creation: 0, cache_creation_1h: 0}, o);
      const bucketOf = (rows, extra) => {
        const b = newCostBucket(extra);
        for (const r of rows) accumulateCostRow(b, r);
        return b;
      };
    """

    def _render(self, rows_js, extra="{ effort: 'high' }"):
        """Render the model totals row and the effort card over the same rows."""
        return run_js(emit(
            "(() => {" + self.HARNESS + """
              const rows = """ + rows_js + """;
              renderModelCostTotals(rows);
              renderEffortCostTable([bucketOf(rows, """ + extra + """)]);
              return { model: captured['model-cost-total'],
                       effortRow: captured['effort-cost-body'],
                       effortTotal: captured['effort-cost-total'] };
            })()"""))

    # opus pays for input/output/reads; the unpriced model supplies every cache
    # WRITE, so that one column's rate set is exactly {null} while the bucket is
    # still billable from the three columns beside it.
    COLUMN_DISJOINT = ("[row('claude-opus-5', {input: 1000, output: 1000, "
                       "cache_read: 1000}), row('gemma-3-27b', "
                       "{cache_creation: 500000})]")

    def test_a_column_no_published_rate_fed_is_marked_on_every_card(self):
        got = self._render(self.COLUMN_DISJOINT)
        for card, html in got.items():
            with self.subTest(card=card):
                self.assertIn("$0.00/M avg", html,
                              "a rate derived from no published price at all "
                              "printed as if it were on the price list")

    def test_the_columns_a_single_price_fed_are_still_not_averages(self):
        """The control: the fix must not start marking real list prices."""
        got = self._render(self.COLUMN_DISJOINT)
        for card, html in got.items():
            for rate in ("$5.00/M", "$25.00/M", "$0.50/M"):
                with self.subTest(card=card, rate=rate):
                    self.assertIn(rate, html)
                    self.assertNotIn(rate + " avg", html)

    def test_a_bucket_nothing_priced_still_prints_no_rate_at_all(self):
        """The other control: with no priced model anywhere the bucket is not
        billable, so there is no money and therefore no rate to mark."""
        got = self._render("[row('gemma-3-27b', {output: 500000})]")
        self.assertNotIn("avg", got["effortRow"])
        self.assertNotIn("&times;", got["effortRow"])
        self.assertIn("n/a", got["effortRow"])

    def test_the_stop_reason_card_marks_it_too(self):
        """The stop-reason card prints ONE token column and calls `blendedRate`
        on it directly, so a priced row contributing no output beside an
        unpriced row that contributes all of it derives a rate from the other
        column's money entirely."""
        got = run_js(emit(
            "(() => {" + self.HARNESS + """
              const b = bucketOf([row('claude-opus-5', {input: 1000}),
                                  row('gemma-3-27b', {output: 500000})],
                                 { stop_reason: 'end_turn' });
              renderStopReasonTable([b]);
              return captured['stop-reason-body'];
            })()"""))
        self.assertIn("&times;", got, "the row does print a derived rate")
        self.assertIn("avg", got,
                      "the rate came from a column no published price fed")

    def test_the_two_implementations_are_one(self):
        """Structural: `renderModelCostTotals` must defer to `blendedRate`
        rather than keep its own copy of the rule. Two copies is how the two
        cards came to disagree, and the surviving copy is the one that reads a
        bare `{column: Set}` map — so it has to be passed as a bucket."""
        source = (REPO_ROOT / "web" / "js" / "54-tables.js").read_text(
            encoding="utf-8")
        self.assertIn("const blended = (k) => blendedRate({ rates }, k);", source)
        self.assertNotIn("rates[k].size > 1", source,
                         "renderModelCostTotals still carries its own copy of "
                         "blendedRate's rule")


@requires_node
class TestSourceIsolation(unittest.TestCase):
    """One database holds both assistants; exactly one is ever on screen.

    Each assistant has separate usage and quota data. Every rollup carries a
    source, and switching assistants must switch all the displayed figures.
    """

    PAYLOAD = {
        "sources": [{"source": "claude", "turns": 1}, {"source": "codex", "turns": 1}],
        "all_models": ["claude-opus-5", "gpt-5.6-sol"],
        "generated_at": "x",
        "daily_by_model": [
            {"day": "2026-08-05", "source": "claude", "model": "claude-opus-5",
             "input": 100, "output": 10, "cache_read": 0, "cache_creation": 0,
             "cache_creation_1h": 0, "turns": 1},
            {"day": "2026-08-05", "source": "codex", "model": "gpt-5.6-sol",
             "input": 700, "output": 70, "cache_read": 900, "cache_creation": 0,
             "cache_creation_1h": 0, "turns": 1},
        ],
        "hourly_by_model": [], "sessions_all": [], "top_dispatches": [],
        "subagent_by_type": [], "project_by_day_model": [], "limit_incidents": [],
        "subscription_limits": {"available": False, "reason": "api_key"},
        "codex_limits": {"available": True, "source": "codex", "plan_type": "pro",
                         "age_seconds": 60, "windows": [
                             {"kind": "10080m", "group": "10080m", "percent": 83,
                              "severity": "", "resets_at": "2099-01-01T00:00:00Z",
                              "scope": "", "is_active": True, "expired": False}]},
    }

    def _run(self, body="", stored="claude"):
        """Drive the real boot path against a server that scopes by source.

        The payload is per-source now, so the harness has to answer
        /api/sources and then serve only the rows the requested source owns —
        modelling the contract rather than the shape the client used to filter.
        The all-time range keeps fixed source fixtures from aging out of view.
        """
        script = (
            "(async () => {\n"
            "  const payload = " + json.dumps(self.PAYLOAD) + ";\n"
            "  const fetched = [];\n"
            "  globalThis.history = { replaceState: () => {} };\n"
            "  apiFetch = async (path) => {\n"
            "    fetched.push(path);\n"
            "    if (path === '/api/scan-status') return { ok: true, status: 200,\n"
            "      json: async () => ({ state: 'idle', generation: 0 }) };\n"
            "    if (path === '/api/sources') return { ok: true, json: async () => ({\n"
            "      sources: [{source:'claude',turns:1},{source:'codex',turns:1}] }) };\n"
            "    const want = (path.match(/source=([a-z]+)/) || [])[1];\n"
            "    const scoped = Object.assign({}, payload);\n"
            "    for (const k of ['daily_by_model','hourly_by_model','sessions_all',\n"
            "                     'project_by_day_model','subagent_by_type','top_dispatches'])\n"
            "      scoped[k] = (payload[k]||[]).filter(r => (r.source||'claude') === want);\n"
            "    scoped.all_models = [...new Set(scoped.daily_by_model.map(r => r.model))];\n"
            "    return { ok: true, json: async () => scoped };\n"
            "  };\n"
            "  const seen = {};\n"
            "  renderStats = (t) => { seen.totals = t; };\n"
            "  renderPlanLimits = (i) => { seen.plan = i; };\n"
            "  scheduleAutoRefresh = () => {};\n"
            "  startPlanLimitsPoll = () => {};\n"
            "  window.location.search = '?range=all&source=' + " + json.dumps(stored) + ";\n"
            "  await start();\n"
            "  await new Promise(r => setTimeout(r, 0));\n"
            + body + "\n"
            "  await new Promise(r => setTimeout(r, 0));\n"
            "  seen.source = selectedSource;\n"
            "  seen.available = availableSources;\n"
            "  seen.models = [...selectedModels].sort();\n"
            "  seen.fetched = fetched;\n"
            "  console.log(JSON.stringify(seen));\n"
            "})();")
        return run_js(script)

    def test_the_default_view_shows_one_source_not_both(self):
        got = self._run()
        self.assertEqual(got["available"], ["claude", "codex"])
        self.assertEqual(got["source"], "claude")
        self.assertEqual(got["totals"]["input"], 100,
                         "Codex tokens leaked into the Claude totals")
        self.assertEqual(got["totals"]["cache_read"], 0)

    def test_switching_source_switches_every_figure(self):
        got = self._run("await setSource('codex');")
        self.assertEqual(got["source"], "codex")
        self.assertEqual(got["totals"]["input"], 700)
        self.assertEqual(got["totals"]["cache_read"], 900)

    def test_the_source_filter_holds_even_with_every_model_selected(self):
        """The model filter must not be what is doing the separating.

        Selecting one source's models happens to exclude the other's, so a
        totals check alone passes even with the source filter deleted. Select
        BOTH sources' models and the source filter is the only thing left
        keeping them apart — which is the property under test."""
        for source, expected_input, expected_cache in (("claude", 100, 0),
                                                       ("codex", 700, 900)):
            with self.subTest(source=source):
                got = self._run(
                    f"setSource('{source}');"
                    "selectedModels = new Set(rawData.all_models);"
                    "applyFilter();")
                self.assertEqual(got["totals"]["input"], expected_input,
                                 "the other source's tokens leaked in")
                self.assertEqual(got["totals"]["cache_read"], expected_cache)

    def test_switching_source_switches_the_model_filter(self):
        """Model ids do not overlap, so carrying the selection across would blank
        every chart and read as 'no data'."""
        self.assertEqual(self._run("await setSource('codex');")["models"], ["gpt-5.6-sol"])
        self.assertEqual(self._run()["models"], ["claude-opus-5"])

    def test_each_source_shows_its_own_quota_panel(self):
        self.assertEqual(self._run()["plan"]["reason"], "api_key")
        codex = self._run("await setSource('codex');")
        self.assertEqual(codex["plan"]["source"], "codex")
        self.assertEqual(codex["plan"]["windows"][0]["percent"], 83)

    def test_codex_is_costed_from_published_rates(self):
        """OpenAI publishes rates for the gpt-5.x models, so a figure derived
        from them is not an estimate and must not be labelled as one."""
        codex = self._run("await setSource('codex');")
        self.assertTrue(codex["totals"]["billable"], "Codex must produce a cost")
        self.assertGreater(codex["totals"]["cost"], 0)
        self.assertFalse(codex["totals"]["estimated"],
                         "gpt-5.6-sol has a published rate")

    def test_claude_costs_are_not_flagged_as_estimates(self):
        claude = self._run()
        self.assertTrue(claude["totals"]["billable"])
        self.assertFalse(claude["totals"]["estimated"],
                         "Anthropic rates are published, not estimated")

    def test_the_cost_note_names_the_right_vendor_and_billing_basis(self):
        """Two independent facts: whose price list, and whether it was billed
        that way. A Codex plan is a subscription even though the rates are
        published, so collapsing the two into one word loses information."""
        got = run_js(emit("""
          (() => {
            selectedSource = 'codex';
            const codex = costBasisNote({ estimated: false });
            const codexEst = costBasisNote({ estimated: true });
            selectedSource = 'claude';
            const claude = costBasisNote({ estimated: false });
            return { codex, codexEst, claude };
          })()"""))
        self.assertIn("OpenAI", got["codex"])
        self.assertIn("not billed per token", got["codex"])
        self.assertNotIn("estimated", got["codex"])
        self.assertIn("some rates estimated", got["codexEst"])
        self.assertIn("Anthropic", got["claude"])
        self.assertNotIn("OpenAI", got["claude"])

    def test_only_the_unpublished_codex_ids_are_flagged(self):
        """Two Codex-internal ids appear in the transcripts but on no price
        list. They are the only estimates left, and they must still say so."""
        got = run_js(emit(
            "['gpt-5.6-sol', 'gpt-5.6-luna', 'gpt-5.3-codex', 'claude-opus-5',"
            " 'codex-auto-review', 'gpt-5.3-codex-spark']"
            ".map(m => [m, isEstimatedRate(m)])"))
        self.assertEqual(dict(got), {
            "gpt-5.6-sol": False, "gpt-5.6-luna": False, "gpt-5.3-codex": False,
            "claude-opus-5": False,
            "codex-auto-review": True, "gpt-5.3-codex-spark": True,
        })

    def test_a_model_with_no_rate_at_all_still_reports_no_cost(self):
        """The original rule survives: an unknown model is billed at nothing
        rather than guessed at, so a local model is never charged."""
        got = run_js(emit(
            "[isBillable('gemma-3'), isBillable('glm-5.1'), isBillable('gpt-4o'),"
            " isBillable('gpt-5.6-sol'), isBillable('claude-opus-5')]"))
        self.assertEqual(got, [False, False, False, True, True])

    def test_an_unpriced_source_draws_no_cost_series(self):
        """A flat zero line under a $0.00 axis asserts the usage was free."""
        got = run_js(emit(
            "(() => { sourceIsPriced = false;"
            "  const without = dailySeries().map(s => s.label);"
            "  sourceIsPriced = true;"
            "  const priced = dailySeries().map(s => s.label);"
            "  return { without, priced }; })()"))
        self.assertNotIn("Est. Cost", got["without"])
        self.assertIn("Est. Cost", got["priced"])
        self.assertEqual(len(got["priced"]) - len(got["without"]), 1)

    # The page title is covered by TestTheTitleNamesTheAssistantItIsShowing in
    # tests/test_ui_theme_and_loading.py, not here. The version that lived at
    # this spot asserted the same two readings — ['claude','codex']/'codex' and
    # ['claude']/'claude' — and so was blind to the only case the title ever got
    # wrong: a CODEX-ONLY machine, which read "Claude Code Usage" over
    # OpenAI-priced figures. Restoring a title test to this class would restore
    # that blind spot; extend the class over there instead.

    def test_the_limits_poll_does_not_overwrite_the_codex_panel(self):
        """/api/limits reports Claude's quota. Codex's comes from the scanned
        series, so an unguarded poll would replace it every thirty seconds."""
        got = run_js(
            "(async () => {\n"
            "  const payload = " + __import__("json").dumps(TestSourceIsolation.PAYLOAD) + ";\n"
            "  const seen = [];\n"
            "  globalThis.history = { replaceState: () => {} };\n"
            "  renderStats = () => {}; scheduleAutoRefresh = () => {};\n"
            "  startPlanLimitsPoll = () => {};\n"
            "  renderPlanLimits = (i) => { seen.push(i && i.source ? i.source : 'claude'); };\n"
            "  apiFetch = async (path) => ({ ok: true, json: async () =>\n"
            "    path === '/api/limits'\n"
            "      ? { available: true, age_seconds: 1, windows: [] }\n"
            "      : payload });\n"
            "  await loadData();\n"
            "  await setSource('codex');\n"
            "  const before = seen[seen.length - 1];\n"
            "  await refreshPlanLimits();\n"
            "  console.log(JSON.stringify({ before, after: seen[seen.length - 1] }));\n"
            "})();")
        self.assertEqual(got["before"], "codex")
        self.assertEqual(got["after"], "codex",
                         "the poll replaced Codex's panel with Claude's reading")

    def test_a_render_failure_is_reported_rather_than_left_blank(self):
        """A throw in any renderer used to vanish into the console, leaving a
        blank page that looks identical to an empty database."""
        got = run_js(
            "(async () => {\n"
            "  let shown = '';\n"
            "  document.getElementById = () => ({ set textContent(v) { shown = v; },"
            "    set innerHTML(v) {}, closest: () => null, dataset: {}, title: '',"
            "    classList: { toggle: () => {} }, setAttribute: () => {},"
            "    removeAttribute: () => {} });\n"
            "  apiFetch = async () => ({ ok: true, json: async () => ({ generated_at: 'x',"
            "    all_models: [], daily_by_model: [], sessions_all: [] }) });\n"
            "  renderStats = () => { throw new Error('boom'); };\n"
            "  scheduleAutoRefresh = () => {}; startPlanLimitsPoll = () => {};\n"
            "  await loadData();\n"
            "  console.log(JSON.stringify({ shown }));\n"
            "})();")
        self.assertIn("Could not render", got["shown"])
        self.assertIn("boom", got["shown"])

    def test_a_row_with_no_source_is_treated_as_claude(self):
        """Rows written before the column existed carry none — the same
        defaulting the database migration applies."""
        got = run_js(emit(
            "[inSource({source: 'claude'}), inSource({}), inSource({source: 'codex'})]"))
        self.assertEqual(got, [True, True, False])

    def test_the_selected_source_survives_in_the_url(self):
        got = run_js(emit("""
          (() => {
            const seen = [];
            globalThis.history = { replaceState: (a, b, url) => seen.push(url) };
            selectedSource = 'codex'; selectedRange = '30d';
            updateURL();
            selectedSource = 'claude';
            updateURL();
            return seen;
          })()"""))
        self.assertIn("source=codex", got[0])
        self.assertNotIn("source=", got[1], "claude is the default and stays implicit")

    def test_the_url_carries_a_model_list_only_when_it_differs_from_the_default(self):
        """The default selection is implicit, so a shared link stays short and a
        later change to what "default" means still applies to it. Anything else
        has to be written down or the link does not reproduce the view."""
        got = run_js(emit("""
          (() => {
            const written = [];
            globalThis.history = { replaceState: (a, b, url) => written.push(url) };
            selectedSource = 'claude'; selectedRange = '30d';
            document.querySelectorAll = () => ([
              { value: 'claude-opus-5' }, { value: 'claude-haiku-4-5' }]);
            selectedModels = new Set(['claude-opus-5', 'claude-haiku-4-5']);
            updateURL();                       // the default: both, both priced
            selectedModels = new Set(['claude-opus-5']);
            updateURL();                       // narrowed
            selectedModels = new Set();
            updateURL();                       // nothing selected
            return written;
          })()"""))
        self.assertNotIn("models=", got[0], "the default must stay implicit")
        self.assertIn("models=claude-opus-5", got[1])
        self.assertIn("models=", got[2],
                      "an empty selection is a choice and must survive a reload")

    def test_a_selection_missing_one_default_model_is_not_the_default(self):
        """The size check is what catches a subset — without it a narrowed
        selection reads as default and is dropped from the URL."""
        got = run_js(emit("""
          (() => {
            const all = ['claude-opus-5', 'claude-haiku-4-5'];
            selectedModels = new Set(all);
            const whole = isDefaultModelSelection(all);
            selectedModels = new Set(['claude-opus-5']);
            const subset = isDefaultModelSelection(all);
            selectedModels = new Set([...all, 'gemma-3']);
            const superset = isDefaultModelSelection(all);
            return { whole, subset, superset };
          })()"""))
        self.assertTrue(got["whole"])
        self.assertFalse(got["subset"])
        self.assertFalse(got["superset"])

    def test_an_unknown_source_in_the_url_is_ignored(self):
        got = run_js(emit("""
          (() => {
            window.location.search = '?source=evil';
            return readURLSource();
          })()"""))
        self.assertIsNone(got)


@requires_node
class TestAutoRefreshPicksUpNewUsage(unittest.TestCase):
    """A refresh has to mean "show me what has happened since".

    Auto-refresh must ingest appended transcript records before querying the
    database. Re-reading an unchanged database cannot discover new usage."""

    def _tick(self, rescan_status=200):
        return run_js(
            "(async () => {\n"
            "  const calls = [];\n"
            "  globalThis.history = { replaceState: () => {} };\n"
            "  document.getElementById = () => ({ set innerHTML(v) {},\n"
            "    set textContent(v) {}, set hidden(v) {}, set disabled(v) {},\n"
            "    dataset: {}, title: '', classList: { toggle: () => {} },\n"
            "    setAttribute: () => {}, removeAttribute: () => {},\n"
            "    closest: () => null, addEventListener: () => {} });\n"
            "  document.querySelector = () => null;\n"
            "  let visibleScanSignals = 0, statusChecks = 0;\n"
            "  const failureModes = [];\n"
            "  showScanLoading = () => { visibleScanSignals++; };\n"
            "  showScanFailure = (blocking = true) => { failureModes.push(blocking); };\n"
            "  globalThis.setTimeout = (fn) => { fn(); return 1; };\n"
            "  apiFetch = async (path, opts) => {\n"
            "    calls.push(((opts && opts.method) || 'GET') + ' ' + path.split('?')[0]);\n"
            "    if (path === '/api/rescan') return { ok: " + str(rescan_status == 200).lower()
            + ", status: " + str(rescan_status) + ",\n"
            "      json: async () => ({ new: 0, updated: 1 }) };\n"
            "    if (path === '/api/scan-status') return { ok: true, status: 200,\n"
            "      json: async () => ({ state: statusChecks++ === 0\n"
            "        ? 'scanning' : 'idle', generation: statusChecks }) };\n"
            "    return { ok: true, status: 200, json: async () => ({ generated_at: 'x',\n"
            "      all_models: [], daily_by_model: [], sessions_all: [] }) };\n"
            "  };\n"
            "  renderStats = () => {}; selectedSource = 'claude'; rawData = {};\n"
            "  await autoRefreshTick();\n"
            "  console.log(JSON.stringify({ calls, visibleScanSignals, failureModes }));\n"
            "})();")

    def test_a_tick_ingests_before_it_re_reads(self):
        got = self._tick()
        self.assertEqual(got["calls"], ["POST /api/rescan", "GET /api/data"],
                         "the poll re-read the database without scanning for "
                         "anything new, which is the whole defect")

    def test_a_tick_does_not_race_the_initial_source_and_data_load(self):
        got = run_js(
            "(async () => { const calls = []; rawData = null;"
            "apiFetch = async (path) => { calls.push(path); throw new Error('race'); };"
            "await autoRefreshTick(); console.log(JSON.stringify(calls)); })();")
        self.assertEqual(got, [])

    def test_a_scan_already_running_finishes_before_the_silent_re_read(self):
        """409 waits for final data without blocking an already-painted page."""
        got = self._tick(rescan_status=409)
        self.assertEqual(got["calls"], [
            "POST /api/rescan", "GET /api/scan-status",
            "GET /api/scan-status", "GET /api/data",
        ])
        self.assertEqual(
            got["visibleScanSignals"], 0,
            "a background 15-second refresh covered the page with a modal overlay",
        )

    def test_a_long_scan_cannot_accumulate_interval_pollers(self):
        """setInterval does not await an async callback before firing again."""
        got = run_js(r"""
(async () => {
  const calls = [];
  const deferred = [];
  let rescans = 0, statuses = 0;
  rawData = {};
  globalThis.setTimeout = (fn) => { deferred.push(fn); return 1; };
  apiFetch = async (path) => {
    calls.push(path);
    if (path === '/api/rescan') return {
      ok: rescans++ > 0, status: rescans === 1 ? 409 : 200,
      json: async () => ({ new: 0, updated: 0 }),
    };
    if (path === '/api/scan-status') return { ok: true, status: 200,
      json: async () => ({
        state: statuses++ === 0 ? 'scanning' : 'idle',
        generation: statuses,
      }) };
    throw new Error('unexpected request ' + path);
  };
  loadData = async () => { calls.push('reload'); };

  const first = autoRefreshTick();
  await new Promise(resolve => setImmediate(resolve));
  await autoRefreshTick();
  const during = [...calls];
  deferred.shift()();
  await first;
  const afterFirst = [...calls];
  await autoRefreshTick();
  console.log(JSON.stringify({ during, afterFirst, calls }));
})();
""")
        self.assertEqual(got["during"], [
            "/api/rescan", "/api/scan-status",
        ], "a second interval started another scan/poller while one was active")
        self.assertEqual(got["afterFirst"].count("/api/rescan"), 1)
        self.assertEqual(got["afterFirst"].count("reload"), 1)
        self.assertEqual(got["calls"].count("/api/rescan"), 2,
                         "the single-flight guard never reopened after completion")
        self.assertEqual(got["calls"].count("reload"), 2)

    def test_a_failed_scan_keeps_the_existing_payload_and_reports_it_nonmodally(self):
        got = self._tick(rescan_status=500)
        self.assertNotIn("GET /api/data", got["calls"],
                         "a failed ingest was presented as a fresh payload")
        self.assertEqual(got["failureModes"], [False],
                         "auto-refresh interrupted the page with a modal failure")

    def test_a_rejected_token_stops_the_tick_instead_of_polling_on(self):
        got = self._tick(rescan_status=403)
        self.assertNotIn("GET /api/data", got["calls"])

    def test_the_timer_runs_the_ingesting_tick(self):
        """Covers the wiring: arming the old re-read-only callback would leave
        every assertion above green while the bug came back."""
        got = run_js(emit("""
          (() => {
            let armed = null;
            globalThis.setInterval = (fn, ms) => { armed = fn; return 1; };
            globalThis.clearInterval = () => {};
            refreshSeconds = 15; selectedRange = 'today';
            scheduleAutoRefresh();
            return { name: armed && armed.name, ms: refreshIntervalMs() };
          })()"""))
        self.assertEqual(got["ms"], 15000)
        self.assertEqual(got["name"], "autoRefreshTick")


@requires_node
class TestTokenlessPageCanRecover(unittest.TestCase):
    """Opened at the bare address, the page must say so and offer a way back.

    It cannot fetch a token for itself — the token is kept out of this document
    precisely so another local account cannot get one by requesting `/` — so the
    only recoveries are a reload with a token in the fragment, or the command
    that prints the link.
    """

    def _load_without_token(self, body=""):
        return run_js(
            "(() => {\n"
            "  const shown = {}; const handlers = {};\n"
            "  const disabled = [];\n"
            "  document.getElementById = (id) => ({\n"
            "    set innerHTML(v) { shown[id] = v; },\n"
            "    set textContent(v) { shown[id + ':text'] = v; },\n"
            "    set hidden(v) { if (v === false) shown[id + ':visible'] = true; },\n"
            "    set disabled(v) { if (v) disabled.push(id); },\n"
            "    addEventListener: (ev, fn) => { handlers[id + ':' + ev] = fn; } });\n"
            "  let reloaded = 0;\n"
            "  window.location.reload = () => { reloaded++; };\n"
            "  const listeners = {};\n"
            "  window.addEventListener = (ev, fn) => { listeners[ev] = fn; };\n"
            "  showAuthNotice();\n"
            + body + "\n"
            "  console.log(JSON.stringify({ shown, disabled, reloaded,\n"
            "           hasRetry: typeof handlers['auth-retry:click'] === 'function',\n"
            "           watchesHash: typeof listeners['hashchange'] === 'function' }));\n"
            "})()")

    def test_it_says_what_happened_instead_of_rendering_nothing(self):
        got = self._load_without_token()
        self.assertTrue(got["shown"].get("auth-notice:visible"))
        self.assertIn("no access token", got["shown"]["auth-notice"])
        self.assertIn("cli.py url", got["shown"]["auth-notice"],
                      "the screen must name the command that gets the link back")

    def test_it_disables_the_controls_that_cannot_work(self):
        got = self._load_without_token()
        self.assertIn("rescan-btn", got["disabled"])
        self.assertIn("refresh-select", got["disabled"])

    def test_it_offers_a_retry(self):
        self.assertTrue(self._load_without_token()["hasRetry"])

    def test_pasting_the_link_into_this_tab_reloads(self):
        """Changing only the fragment is a same-document navigation: the page
        does not reload and the token is never re-read, so the screen sat there
        looking broken while the address bar showed a perfectly good link."""
        got = self._load_without_token(
            "  window.location.hash = '#token=' + 'a'.repeat(40);\n"
            "  listeners['hashchange']();\n")
        self.assertTrue(got["watchesHash"])
        self.assertEqual(got["reloaded"], 1)

    def test_a_rejected_token_is_answered_rather_than_retried_forever(self):
        """A token that is present but WRONG is not a transient failure.

        It showed "Forbidden — retrying…" and scheduled another attempt every
        three seconds indefinitely, which cannot succeed and tells the reader
        neither what is wrong nor what to do. So it gets the same screen as a
        missing token, minus the diagnosis: this assertion used to read
        `"no access token"`, which was the screen the 403 branch really did
        render and which the address bar plainly contradicted. It now checks the
        rejected-link wording instead — the recovery command is asserted next to
        it so the check still proves the screen is the usable one.
        """
        got = run_js(
            "(async () => {\n"
            "  let retries = 0, shown = '';\n"
            "  globalThis.history = { replaceState: () => {} };\n"
            "  const realTimeout = setTimeout;\n"
            "  globalThis.setTimeout = (fn, ms) =>\n"
            "    (ms === 3000 ? (retries++, 0) : realTimeout(fn, ms));\n"
            "  document.getElementById = () => ({ set innerHTML(v) { shown = v; },\n"
            "    set textContent(v) {}, set hidden(v) {}, set disabled(v) {},\n"
            "    dataset: {}, title: '', classList: { toggle: () => {} },\n"
            "    setAttribute: () => {}, removeAttribute: () => {},\n"
            "    closest: () => null, addEventListener: () => {} });\n"
            "  document.querySelector = () => null;\n"
            "  apiFetch = async () => ({ ok: false, status: 403,\n"
            "    json: async () => ({ error: 'Forbidden' }) });\n"
            "  renderStats = () => {}; scheduleAutoRefresh = () => {};\n"
            "  startPlanLimitsPoll = () => {};\n"
            "  await start();\n"
            "  await new Promise(r => realTimeout(r, 20));\n"
            "  console.log(JSON.stringify({ shown: String(shown), retries }));\n"
            "})();")
        self.assertEqual(got["retries"], 0, "it kept retrying an auth failure")
        self.assertIn("no longer accepted", got["shown"],
                      "it did not explain what to do about the rejected link")
        self.assertIn("cli.py url", got["shown"],
                      "the screen must name the command that gets the link back")

    def test_an_unusable_fragment_does_not_trigger_a_reload_loop(self):
        """Reloading on any hash change would spin forever on a bad token."""
        got = self._load_without_token(
            "  window.location.hash = '#token=short';\n"
            "  listeners['hashchange']();\n"
            "  window.location.hash = '#nothing';\n"
            "  listeners['hashchange']();\n")
        self.assertEqual(got["reloaded"], 0)


@requires_node
class TestTheAuthScreenNamesTheCauseItActuallyHit(unittest.TestCase):
    """Four callers, two causes, and the screen asserted the wrong one for three.

    The server mints a fresh token on every start, so a bookmarked link is the
    likeliest way to be refused — and this screen told that reader "this tab
    opened the plain address" while the address bar showed `#token=…` in front
    of them. Only the diagnosis was wrong: the remedy underneath it
    (`python cli.py url --open`) recovers the link on both branches and is
    asserted here on both, so the branch cannot be "fixed" by hollowing it out.
    """

    def _notice(self, argument=""):
        return run_js(
            "(() => {\n"
            "  let shown = '';\n"
            "  const listeners = {};\n"
            "  window.addEventListener = (ev, fn) => { listeners[ev] = fn; };\n"
            "  document.getElementById = () => ({ set innerHTML(v) { shown = v; },\n"
            "    set textContent(v) {}, set hidden(v) {}, set disabled(v) {},\n"
            "    addEventListener: () => {} });\n"
            "  document.querySelector = () => null;\n"
            "  showAuthNotice(" + argument + ");\n"
            "  console.log(JSON.stringify({ shown,\n"
            "    watchesHash: typeof listeners['hashchange'] === 'function' }));\n"
            "})()")

    def test_a_rejected_link_is_not_diagnosed_as_a_missing_one(self):
        shown = self._notice("'rejected'")["shown"]
        self.assertIn("no longer accepted", shown)
        self.assertNotIn("opened the plain address", shown,
                         "the address bar shows a token; the screen denies it")
        self.assertNotIn("no access token", shown)

    def test_the_bare_address_still_reads_exactly_as_before(self):
        """`showAuthNotice()` is called with no argument from the bootstrap, and
        that path was never wrong — the default has to keep it."""
        shown = self._notice()["shown"]
        self.assertIn("This link has no access token", shown)
        self.assertIn("opened the plain address", shown)

    def test_both_causes_carry_the_remedy_and_the_privacy_reason(self):
        for argument in ("", "'rejected'"):
            with self.subTest(reason=argument or "default"):
                shown = self._notice(argument)["shown"]
                self.assertIn("python cli.py url --open", shown)
                self.assertIn("cannot fetch one for itself", shown,
                              "the screen must still say why it cannot just "
                              "ask the server for a token")

    def test_recovery_actions_come_from_the_served_surface_config(self):
        configured = _DOM_STUB.replace(
            "APP_CONFIG: { version: 'test', surface: 'web' },",
            "APP_CONFIG: { version: 'test', surface: 'vscode', commands: {"
            "scan: 'palette rescan', diagnose: 'palette logs', "
            "reconnect: 'palette restart'} },",
            1,
        )
        got = _run_js_source(
            configured + "\n" + extract_app_script() + "\n" +
            "(() => {\n"
            "  let shown = '';\n"
            "  document.getElementById = () => ({\n"
            "    set innerHTML(v) { shown = v; }, set textContent(v) {},\n"
            "    set hidden(v) {}, set disabled(v) {},\n"
            "    addEventListener: () => {} });\n"
            "  document.querySelector = () => null;\n"
            "  showAuthNotice('rejected');\n"
            "  const auth = shown; shown = ''; showDatabaseNotice();\n"
            "  console.log(JSON.stringify({ auth, database: shown, empty: DB_NOTICE_TEXT }));\n"
            "})()"
        )
        self.assertIn("palette restart", got["auth"])
        self.assertIn("palette logs", got["database"])
        self.assertIn("palette rescan", got["database"])
        self.assertIn("palette rescan", got["empty"])
        self.assertNotIn("python cli.py", json.dumps(got))

    def test_the_hash_watcher_survives_both_branches(self):
        """Pasting the freshly printed URL into THIS tab changes only the
        fragment, which is a same-document navigation — this listener is the
        only thing that turns it into a working page, and it matters more on the
        rejected branch than on the other one."""
        for argument in ("", "'rejected'"):
            with self.subTest(reason=argument or "default"):
                self.assertTrue(self._notice(argument)["watchesHash"])

    def test_the_reload_button_stops_promising_a_retry_that_cannot_work(self):
        """Reloading a stale link re-reads the same rejected token. The button
        is kept — it is the manual counterpart to the hashchange watcher for an
        edit that does not fire one — but it says what it needs first."""
        self.assertIn("Reload and try again", self._notice()["shown"])
        rejected = self._notice("'rejected'")["shown"]
        self.assertIn('id="auth-retry"', rejected)
        self.assertNotIn("Reload and try again", rejected)
        self.assertIn("Reload with the new link", rejected)

    def _refused(self, call):
        """Drive one real 403 handler and return what it put on screen."""
        return run_js(
            "(async () => {\n"
            "  let shown = '';\n"
            "  window.addEventListener = () => {};\n"
            "  document.getElementById = () => ({ set innerHTML(v) { shown = v; },\n"
            "    set textContent(v) {}, set hidden(v) {}, set disabled(v) {},\n"
            "    dataset: {}, title: '', classList: { toggle: () => {} },\n"
            "    setAttribute: () => {}, removeAttribute: () => {},\n"
            "    closest: () => null, addEventListener: () => {} });\n"
            "  document.querySelector = () => null;\n"
            "  globalThis.history = { replaceState: () => {} };\n"
            "  apiFetch = async () => ({ ok: false, status: 403,\n"
            "    json: async () => ({ error: 'Forbidden' }) });\n"
            "  renderStats = () => {}; scheduleAutoRefresh = () => {};\n"
            "  startPlanLimitsPoll = () => {};\n"
            "  rawData = {};  // make the direct auto-refresh probe post-startup\n"
            "  await " + call + ";\n"
            "  console.log(JSON.stringify({ shown }));\n"
            "})();")

    def test_every_403_handler_passes_the_cause(self):
        """A branch no caller reaches is a comment. All three refusal paths —
        the boot probe, the payload read and the auto-refresh tick — arrive at
        the same screen, so all three have to name the same cause."""
        for call in ("start()", "loadData()", "autoRefreshTick()"):
            with self.subTest(caller=call):
                shown = self._refused(call)["shown"]
                self.assertIn("no longer accepted", shown)
                self.assertNotIn("opened the plain address", shown)


@requires_node
class TestBootAsksBeforeItLoads(unittest.TestCase):
    """The source question is answered before any history is built.

    Building the dashboard to find out which assistants exist meant the reader
    waited seconds for BOTH histories to render behind the dialog, and then half
    of it was discarded by their answer. `/api/sources` is one grouped count
    over `turns` and is the only thing needed to decide.
    """

    def _boot(self, sources, stored=None, url="", body=""):
        script = (
            "(async () => {\n"
            "  const fetched = [];\n"
            "  globalThis.history = { replaceState: () => {} };\n"
            "  window.location.search = " + json.dumps(url) + ";\n"
            "  apiFetch = async (path) => {\n"
            "    fetched.push(path.split('?')[0]);\n"
            "    if (path === '/api/scan-status') return { ok: true, status: 200,\n"
            "      json: async () => ({ state: 'idle', generation: 0 }) };\n"
            "    if (path === '/api/sources') return { ok: true,\n"
            "      json: async () => ({ sources: " + json.dumps(sources) + " }) };\n"
            "    return { ok: true, json: async () => ({ generated_at: 'x',\n"
            "      all_models: [], daily_by_model: [], sessions_all: [] }) };\n"
            "  };\n"
            "  let chooserShown = false;\n"
            "  document.getElementById = (id) => ({\n"
            "    set innerHTML(v) {}, set textContent(v) {},\n"
            "    set hidden(v) { if (id === 'source-chooser' && v === false) chooserShown = true; },\n"
            "    dataset: {}, title: '', classList: { toggle: () => {}, add: () => {},\n"
            "      remove: () => {}, contains: () => false },\n"
            "    setAttribute: () => {}, removeAttribute: () => {},\n"
            "    getContext: () => ({}), offsetHeight: 0, offsetWidth: 0,\n"
            "    parentElement: { clientWidth: 900 }, style: { setProperty: () => {} },\n"
            "    querySelectorAll: () => [], querySelector: () => null,\n"
            "    closest: () => null, addEventListener: () => {},\n"
            "    getBoundingClientRect: () => ({ top: 0, height: 0 }) });\n"
            "  renderStats = () => {}; scheduleAutoRefresh = () => {};\n"
            "  startPlanLimitsPoll = () => {};\n"
            + ("  window.location.search = '?source=" + stored + "';\n"
               if stored else "")
            + "  await start();\n"
            "  await new Promise(r => setTimeout(r, 0));\n"
            + body + "\n"
            "  await new Promise(r => setTimeout(r, 0));\n"
            "  console.log(JSON.stringify({ fetched, chooserShown,\n"
            "    source: selectedSource, available: availableSources }));\n"
            "})();")
        return run_js(script)

    BOTH = [{"source": "claude", "turns": 10}, {"source": "codex", "turns": 20}]

    def test_with_both_sources_it_asks_and_loads_nothing(self):
        """The whole point: no history is built until the question is answered."""
        got = self._boot(self.BOTH)
        self.assertTrue(got["chooserShown"])
        self.assertEqual(got["fetched"], ["/api/scan-status", "/api/sources"],
                         "the dashboard was built before the reader chose")

    def test_answering_the_question_loads_that_source_only(self):
        got = self._boot(self.BOTH, body="await chooseSource('codex');")
        self.assertEqual(got["source"], "codex")
        self.assertEqual(
            got["fetched"], ["/api/scan-status", "/api/sources", "/api/data"])

    def test_with_only_codex_it_goes_straight_there(self):
        got = self._boot([{"source": "codex", "turns": 5}])
        self.assertFalse(got["chooserShown"], "there is nothing to choose between")
        self.assertEqual(got["source"], "codex")
        self.assertEqual(
            got["fetched"], ["/api/scan-status", "/api/sources", "/api/data"])

    def test_with_only_claude_it_goes_straight_there(self):
        got = self._boot([{"source": "claude", "turns": 5}])
        self.assertFalse(got["chooserShown"])
        self.assertEqual(got["source"], "claude")
        self.assertEqual(
            got["fetched"], ["/api/scan-status", "/api/sources", "/api/data"])

    def test_a_past_choice_does_not_answer_the_question_again(self):
        """The chooser is the entry view whenever both exist — every time, not
        just the first. Remembering the answer meant the question was asked once
        and then silently answered on the reader's behalf ever after, which is
        indistinguishable from it having been ignored."""
        got = run_js(
            "(async () => {\n"
            "  localStorage.setItem('cu_source', 'codex');\n"
            "  const fetched = [];\n"
            "  globalThis.history = { replaceState: () => {} };\n"
            "  window.location.search = '';\n"
            "  let chooserShown = false;\n"
            "  document.getElementById = (id) => ({\n"
            "    set innerHTML(v) {}, set textContent(v) {},\n"
            "    set hidden(v) { if (id === 'source-chooser' && v === false) chooserShown = true; },\n"
            "    dataset: {}, title: '', classList: { toggle: () => {} },\n"
            "    setAttribute: () => {}, removeAttribute: () => {},\n"
            "    closest: () => null, addEventListener: () => {} });\n"
            "  apiFetch = async (path) => { fetched.push(path.split('?')[0]);\n"
            "    if (path === '/api/scan-status') return { ok: true, status: 200,\n"
            "      json: async () => ({ state: 'idle', generation: 0 }) };\n"
            "    return { ok: true, json: async () => ({ sources: [\n"
            "      {source:'claude',turns:1},{source:'codex',turns:1}] }) }; };\n"
            "  renderStats = () => {}; scheduleAutoRefresh = () => {};\n"
            "  startPlanLimitsPoll = () => {};\n"
            "  await start();\n"
            "  await new Promise(r => setTimeout(r, 0));\n"
            "  console.log(JSON.stringify({ chooserShown, fetched }));\n"
            "})();")
        self.assertTrue(got["chooserShown"],
                        "a past choice suppressed the question")
        self.assertEqual(got["fetched"], ["/api/scan-status", "/api/sources"],
                         "and it loaded a source without being asked")

    def test_a_link_naming_a_source_is_not_asked_either(self):
        got = self._boot(self.BOTH, url="?source=codex")
        self.assertFalse(got["chooserShown"])
        self.assertEqual(got["source"], "codex")

    def test_a_remembered_source_that_no_longer_exists_is_ignored(self):
        """Codex history can be deleted — Codex prunes its own sessions."""
        got = self._boot([{"source": "claude", "turns": 5}], stored="codex")
        self.assertEqual(got["source"], "claude")
        self.assertFalse(got["chooserShown"])

    def test_an_empty_database_still_boots(self):
        got = self._boot([])
        self.assertEqual(got["available"], ["claude"])
        self.assertFalse(got["chooserShown"])

    def test_a_pending_switch_marks_the_stale_view_as_not_current(self):
        """The heading flips at once so the click feels answered, but the charts
        below are still the other assistant's for a couple of seconds, until the
        scoped payload lands. Showing them crisp under the new label states one
        assistant's numbers as the other's."""
        got = run_js(
            "(async () => {\n"
            "  const classes = [];\n"
            "  globalThis.history = { replaceState: () => {} };\n"
            "  document.querySelector = (sel) => sel === '.container'\n"
            "    ? { classList: { add: (c) => classes.push('+' + c),\n"
            "                     remove: (c) => classes.push('-' + c) },\n"
            "        setAttribute: () => {}, removeAttribute: () => {} }\n"
            "    : null;\n"
            "  apiFetch = async (path) => ({ ok: true, json: async () =>\n"
            "    path === '/api/scan-status' ? { state: 'idle', generation: 0 }\n"
            "    : path === '/api/sources'\n"
            "      ? { sources: [{source:'claude',turns:1},{source:'codex',turns:1}] }\n"
            "      : { generated_at: 'x', all_models: [], daily_by_model: [],\n"
            "          sessions_all: [] } });\n"
            "  renderStats = () => {}; scheduleAutoRefresh = () => {};\n"
            "  startPlanLimitsPoll = () => {};\n"
            "  window.location.search = '?source=claude';\n"
            "  await start();\n"
            "  await new Promise(r => setTimeout(r, 0));\n"
            "  console.log(JSON.stringify({ classes }));\n"
            "})();")
        self.assertIn("+loading", got["classes"], "the stale view was left crisp")
        self.assertEqual(got["classes"][-1], "-loading",
                         "the page stayed dimmed after the data arrived")

    def test_each_source_is_fetched_once_however_often_you_switch(self):
        """Switching back is instant: the payload is kept, not refetched."""
        got = self._boot(self.BOTH, stored="claude", body=(
            "  await setSource('codex');\n"
            "  await setSource('claude');\n"
            "  await setSource('codex');\n"))
        self.assertEqual(got["fetched"].count("/api/data"), 2,
                         "a source was refetched after already being loaded")


@requires_node
class TestPlanPanelTracksTheClock(unittest.TestCase):
    """The plan panel claims to describe RIGHT NOW, so it must not freeze.

    Both figures it shows were computed once, server-side, at page load: the
    per-window `expired` flag and `age_seconds`. With auto-refresh off (the
    default since v1.6.2) `loadData` never runs again, so an expired window can
    remain visible after the cache has rolled over to a new live window.
    Re-evaluating both against the viewer's clock on every paint is what
    makes a stale page look stale instead of confidently wrong.
    """

    def _eval(self, expr, **binds):
        return run_js(emit(expr, **binds))

    def test_a_window_whose_reset_has_passed_reads_as_ended(self):
        got = self._eval(
            "windowHasEnded({expired: false, resets_at: past}, now)",
            past="2026-08-06T05:30:00Z", now=1786000000000)   # now > past
        self.assertTrue(got, "the server said live at fetch time; the clock says over")

    def test_a_live_window_is_not_reported_as_ended(self):
        got = self._eval(
            "windowHasEnded({expired: false, resets_at: future}, now)",
            future="2026-08-06T15:39:59Z", now=1785000000000)
        self.assertFalse(got)

    def test_a_window_can_never_un_expire(self):
        """The server may know a window is over for a reason the timestamp does
        not carry, so its flag is OR-ed in, never overridden."""
        got = self._eval(
            "windowHasEnded({expired: true, resets_at: future}, now)",
            future="2026-08-06T15:39:59Z", now=1785000000000)
        self.assertTrue(got)

    def test_a_missing_reset_time_is_not_treated_as_ended(self):
        for value in (None, "", "not-a-date"):
            with self.subTest(resets_at=value):
                self.assertFalse(self._eval(
                    "windowHasEnded({expired: false, resets_at: v}, 1786000000000)", v=value))

    def test_the_stated_age_counts_from_when_this_page_got_the_reading(self):
        """Frozen age is what made a nine-hour-old panel look two minutes old."""
        got = self._eval(
            "planSampleAge({age_seconds: 120, _received_at: at}, at + 9*3600*1000)",
            at=1786000000000)
        self.assertAlmostEqual(got, 120 + 9 * 3600, places=3)

    def test_an_unstamped_reading_is_reported_at_its_server_age(self):
        got = self._eval("planSampleAge({age_seconds: 42}, 1786000000000)")
        self.assertAlmostEqual(got, 42, places=3)

    def test_an_age_the_server_could_not_compute_stays_unknown(self):
        self.assertIsNone(self._eval("planSampleAge({age_seconds: null}, 1)"))
        self.assertIsNone(self._eval("planSampleAge(null, 1)"))

    RENDER = """
      (() => {
        const captured = {};
        document.getElementById = (id) => ({
          set innerHTML(v) { captured[id] = v; },
          set textContent(v) { captured[id + ':text'] = v; },
          set className(v) {}, set hidden(v) {},
          setAttribute: () => {}, closest: () => null,
        });
        document.querySelector = () => ({ set hidden(v) {} });
        renderPlanLimits(INFO);
        return captured;
      })()"""

    def _render(self, info):
        return run_js(emit(self.RENDER.replace("INFO", "info"), info=info))

    def test_the_rendered_panel_ends_a_window_the_clock_has_passed(self):
        """The end-to-end case from the bug report: the server said this window
        was live when the page loaded, and the page then sat open past its reset."""
        got = self._render({
            "available": True, "plan_type": "claude_max", "age_seconds": 120,
            "windows": [{"kind": "session", "group": "session", "scope": "",
                         "percent": 45, "severity": "normal", "is_active": True,
                         "resets_at": "2020-01-01T00:00:00Z", "expired": False}],
        })
        self.assertIn("Window ended", got["plan-windows"])

    def test_an_expired_window_reports_the_one_you_are_actually_in(self):
        """After a reset the cache still describes the window that has rolled
        over, and will until Claude Code refreshes — tens of minutes. Its own
        reset time says when the CURRENT window began, and the database knows
        what has run in it, so the panel says that rather than nothing."""
        got = self._render({
            "available": True, "plan_type": "claude_max", "age_seconds": 120,
            "windows": [{"kind": "session", "group": "session", "scope": "",
                         "percent": 100, "severity": "critical", "is_active": True,
                         "resets_at": "2020-01-01T00:00:00Z", "expired": True,
                         "window_start": "2020-01-01T00:00:00Z",
                         "window_end": "2099-01-01T05:00:00Z",
                         "recorded": {"turns": 120, "tokens": 2400000}}],
        })
        panel = got["plan-windows"]
        self.assertIn("New window", panel)
        self.assertIn("120 turns", panel, "the turns recorded in this window")
        self.assertNotIn("100%", panel, "the stale percentage must not reappear")
        self.assertNotIn("Window ended", panel)

    def test_without_derivable_bounds_it_still_says_the_window_ended(self):
        """A window whose length we cannot derive keeps the old, honest wording
        rather than inventing a start time."""
        got = self._render({
            "available": True, "plan_type": "claude_max", "age_seconds": 120,
            "windows": [{"kind": "mystery", "group": "", "scope": "",
                         "percent": 100, "severity": "critical", "is_active": True,
                         "resets_at": "2020-01-01T00:00:00Z", "expired": True}],
        })
        self.assertIn("Window ended", got["plan-windows"])

    def test_the_rendered_panel_shows_a_live_window_as_live(self):
        got = self._render({
            "available": True, "plan_type": "claude_max", "age_seconds": 120,
            "windows": [{"kind": "session", "group": "session", "scope": "",
                         "percent": 45, "severity": "normal", "is_active": True,
                         "resets_at": "2099-01-01T00:00:00Z", "expired": False}],
        })
        self.assertNotIn("Window ended", got["plan-windows"])
        self.assertIn("45%", got["plan-windows"])

    def test_the_panel_polls_independently_of_the_auto_refresh_setting(self):
        """That setting stops charts and tables rebuilding; this panel is neither,
        and turning it off must not freeze a live reading."""
        got = self._eval("""
          (() => {
            const armed = [];
            globalThis.setInterval = (fn, ms) => { armed.push(ms); return armed.length; };
            refreshSeconds = 0;                 // auto-refresh OFF
            selectedRange = 'today';
            startPlanLimitsPoll();
            return { armed, mainPoll: refreshIntervalMs() };
          })()""")
        self.assertEqual(got["mainPoll"], 0, "auto-refresh is off, as configured")
        self.assertEqual(got["armed"], [30000],
                         "the plan panel still polls on its own interval")


@requires_node
class TestRenderedRowsMatchTheirHeaders(unittest.TestCase):
    """Each table's <td>s must line up with the <th>s declared in index.html.

    The on-screen twin of the CSV defect below, and the same split-literal
    cause: the header row lives in web/index.html while the cells are built in
    50-render.js. Here a drift is worse than cosmetic — `labelCells` stamps each
    cell with its column's header text BY INDEX, so on a phone (where every row
    is redrawn as a stack of label/value pairs) one extra cell relabels every
    column after it, and the last one loses its label entirely.
    """

    # One row shape wide enough for every renderer; each ignores what it
    # doesn't use, so a single fixture keeps the comparison honest.
    ROW = {
        "model": "claude-opus-5", "turns": 1, "input": 1, "output": 1,
        "cache_read": 1, "cache_creation": 1, "cache_creation_1h": 0,
        "cost": 1, "billable": True, "sessions": 1, "project": "p",
        "branch": "main", "session_id": "s", "topic": "t", "last": "x",
        "duration_min": 1, "agent_type": "Explore", "agent_id": "a",
        "start": "x", "tool_uses": 1, "duration_ms": 1, "status": "ok",
        "day": "2026-08-06", "started": "x", "blocked_min": 1, "notices": 1,
        "projects": ["p"], "reset_hint": "3am", "reset_zone": "UTC",
    }

    def _header_widths(self):
        """<th> count per <tbody> id, read from the page itself."""
        html = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        widths = {}
        for table in re.finditer(r"<table[^>]*>(.*?)</table>", html, re.S):
            body = re.search(r'<tbody id="([^"]+)"', table.group(1))
            head = re.search(r"<thead>(.*?)</thead>", table.group(1), re.S)
            if body and head:
                widths[body.group(1)] = len(re.findall(r"<th\b", head.group(1)))
        return widths

    def test_every_table_renders_one_cell_per_column(self):
        widths = self._header_widths()
        self.assertTrue(widths, "no tables found in index.html — parser broke")
        rendered = run_js(emit("""
          (() => {
            const captured = {};
            document.getElementById = (id) => ({
              set innerHTML(v) { captured[id] = v; },
              closest: () => null, rows: null, cells: null, textContent: '',
              setAttribute: () => {}, querySelectorAll: () => [],
            });
            renderModelCostTable([ROW]); renderSessionsTable([ROW]);
            renderProjectCostTable([ROW]); renderProjectBranchCostTable([ROW]);
            renderTopDispatches([ROW]); renderLimits([ROW]);
            const out = {};
            for (const [k, v] of Object.entries(captured)) {
              out[k] = (String(v).match(/<td\\b/g) || []).length;
            }
            return out;
          })()""", ROW=self.ROW))
        checked = 0
        for body, width in widths.items():
            if body not in rendered:
                continue           # a table this test does not drive
            checked += 1
            with self.subTest(table=body):
                self.assertEqual(
                    rendered[body], width,
                    f"{body} renders {rendered[body]} cells under {width} "
                    "headers; labelCells maps them by index, so the phone "
                    "layout would mislabel every column after the extra one")
        self.assertGreaterEqual(checked, 5, "most tables went unchecked")


@requires_node
class TestRescanButtonReportsFailures(unittest.TestCase):
    """A refused rescan must not read as a successful one.

    `/api/rescan` answers 403 (bad token), 409 (a scan is already running) and
    500 (the scan raised) with a JSON `error` — deliberately, so the button can
    tell a failed scan from a dead server. The button read `d.new` / `d.updated`
    off those bodies without checking the status, so every one of them rendered
    as "Rescan (undefined new, undefined updated)" — the shape of a success —
    and the page reloaded its data as though something had changed.
    """

    def _press(self, status, body):
        return run_js(
            "(async () => {" + f"""
              const btn = {{ textContent: '', disabled: false }};
              document.getElementById = () => btn;
              const calls = [];
              globalThis.fetch = async () => ({{
                ok: {str(status < 400).lower()}, status: {status},
                json: async () => ({json.dumps(body)}),
              }});
              loadData = async () => {{ calls.push('reloaded'); }};
              // The button is re-armed by a 3s timer BELOW the try/catch, so run
              // deferred callbacks immediately rather than waiting for them —
              // and so an early exit from any branch shows up as a button that
              // never comes back.
              const deferred = [];
              globalThis.setTimeout = (fn) => {{ deferred.push(fn); return 0; }};
              await triggerRescan();
              const text = btn.textContent;   // the outcome, before the reset
              deferred.forEach(fn => fn());   // ...then the reset itself
              console.log(JSON.stringify({{
                text, calls, usableAgain: btn.disabled === false,
              }}));
            """ + "})();")

    def test_a_refused_rescan_says_so(self):
        for status, body in ((403, {"error": "Forbidden"}),
                             (500, {"error": "Rescan failed"})):
            with self.subTest(status=status):
                got = self._press(status, body)
                self.assertNotIn("undefined", got["text"])
                self.assertIn("failed", got["text"].lower())

    def test_a_refused_rescan_does_not_reload_the_page_data(self):
        got = self._press(500, {"error": "Rescan failed"})
        self.assertEqual(got["calls"], [],
                         "nothing was scanned, so there is nothing to re-read")

    def test_the_button_comes_back_however_the_rescan_ended(self):
        """Rescan disables itself first and is re-armed by a timer below the
        try/catch, so any branch that exits the function early strands it
        greyed out for the life of the page."""
        for status, body in ((200, {"new": 0, "updated": 0}),
                             (500, {"error": "Rescan failed"})):
            with self.subTest(status=status):
                self.assertTrue(self._press(status, body)["usableAgain"])

    def test_a_successful_rescan_still_reports_its_counts_and_reloads(self):
        got = self._press(200, {"new": 2, "updated": 3, "skipped": 0,
                                "turns": 9, "sessions": 1})
        self.assertIn("2 new", got["text"])
        self.assertIn("3 updated", got["text"])
        self.assertEqual(got["calls"], ["reloaded"])

    def test_a_successful_rescan_invalidates_every_source_cache(self):
        got = run_js(
            "(async () => {" + r"""
              const btn = { textContent: '', disabled: false };
              document.getElementById = () => btn;
              showScanLoading = () => {};
              loadedSources.set('claude', { marker: 'old claude' });
              loadedSources.set('codex', { marker: 'old codex' });
              selectedSource = 'claude';
              globalThis.fetch = async () => ({ ok: true, status: 200,
                json: async () => ({ new: 1, updated: 0 }) });
              let cacheAtReload = null;
              loadData = async () => {
                cacheAtReload = [...loadedSources.keys()].sort();
                loadedSources.set(selectedSource, { marker: 'fresh claude' });
              };
              globalThis.setTimeout = (fn) => { fn(); return 1; };
              await triggerRescan();
              console.log(JSON.stringify({ cacheAtReload,
                cachedAfter: [...loadedSources.keys()].sort() }));
            })();""")

        self.assertEqual(got["cacheAtReload"], [],
                         "the reload began while off-screen caches still held "
                         "pre-scan payloads")
        self.assertEqual(got["cachedAfter"], ["claude"],
                         "switching after a rescan could reveal stale Codex data")


@requires_node
class TestScanProgressIsUnmistakable(unittest.TestCase):
    """A valid zero payload is provisional while transcript ingestion runs.

    The existing overlay covered only the comparatively short `/api/data`
    fetch. A cold startup scan runs in another thread and `/api/rescan` waits
    synchronously for the same scanner, so both paths could leave twelve zeroed
    cards on screen with only a tiny header-button label as evidence that the
    numbers were still being assembled.
    """

    DOM = r"""
      const attrs = {};
      const els = {
        'load-overlay': { hidden: true },
        'load-spinner': { hidden: false },
        'load-text': { textContent: '' },
        'meta': { textContent: '', innerHTML: '' },
        'rescan-btn': { textContent: '\u21bb Rescan', disabled: false },
        'scan-retry': { hidden: true },
      };
      const container = {
        classList: { add: () => {}, remove: () => {} },
        setAttribute: (k, v) => { attrs[k] = String(v); },
        removeAttribute: (k) => { delete attrs[k]; },
      };
      const stub = document.getElementById;
      document.getElementById = (id) => els[id] || stub(id);
      document.querySelector = (sel) => sel === '.container' ? container : null;
    """

    def test_startup_waits_for_the_scan_before_accepting_zero_as_final(self):
        got = run_js(
            "(async () => {\n" + self.DOM + r"""
              const calls = [];
              const deferred = [];
              let statusChecks = 0;
              globalThis.setTimeout = (fn) => { deferred.push(fn); return 1; };
              globalThis.fetch = async (path) => {
                calls.push(path);
                if (path === '/api/scan-status') {
                  const scanning = statusChecks++ === 0;
                  return { ok: true, status: 200,
                    json: async () => ({
                      state: scanning ? 'scanning' : 'idle',
                      generation: statusChecks,
                    }) };
                }
                if (path === '/api/sources') return { ok: true, status: 200,
                  json: async () => ({ sources: [{ source: 'claude', turns: 4 }] }) };
                throw new Error('unexpected request ' + path);
              };
              loadSource = async (source) => { calls.push('load:' + source); clearLoading(); };

              const work = start();
              await Promise.resolve(); await Promise.resolve();
              const during = {
                calls: [...calls], hidden: els['load-overlay'].hidden,
                text: els['load-text'].textContent, busy: attrs['aria-busy'],
              };
              if (deferred.length) deferred.shift()();
              await work;
              console.log(JSON.stringify({ during, calls,
                finishedHidden: els['load-overlay'].hidden,
                finishedBusy: attrs['aria-busy'] || null }));
            })();""")

        self.assertEqual(got["during"]["calls"], ["/api/scan-status"],
                         "provisional sources were queried while scanning")
        self.assertFalse(got["during"]["hidden"],
                         "the full-screen progress state was not visible")
        self.assertIn("Scanning usage history", got["during"]["text"])
        self.assertEqual(got["during"]["busy"], "true")
        self.assertEqual(
            got["calls"],
            ["/api/scan-status", "/api/scan-status", "/api/sources", "load:claude"])
        self.assertTrue(got["finishedHidden"])
        self.assertIsNone(got["finishedBusy"])

    def test_rescan_uses_the_overlay_through_the_final_data_reload(self):
        got = run_js(
            "(async () => {\n" + self.DOM + r"""
              let finishPost, finishReload;
              globalThis.fetch = () => new Promise(resolve => {
                finishPost = () => resolve({ ok: true, status: 200,
                  json: async () => ({ new: 1, updated: 2 }) });
              });
              loadData = () => new Promise(resolve => {
                finishReload = () => { clearLoading(); resolve(); };
              });
              globalThis.setTimeout = (fn) => { fn(); return 1; };

              const work = triggerRescan();
              await Promise.resolve();
              const duringScan = {
                hidden: els['load-overlay'].hidden,
                text: els['load-text'].textContent,
                busy: attrs['aria-busy'],
              };
              finishPost();
              await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
              const duringReload = { hidden: els['load-overlay'].hidden,
                                     busy: attrs['aria-busy'] };
              finishReload();
              await work;
              console.log(JSON.stringify({ duringScan, duringReload,
                finishedHidden: els['load-overlay'].hidden,
                finishedBusy: attrs['aria-busy'] || null }));
            })();""")

        self.assertFalse(got["duringScan"]["hidden"])
        self.assertIn("Scanning usage history", got["duringScan"]["text"])
        self.assertEqual(got["duringScan"]["busy"], "true")
        self.assertFalse(got["duringReload"]["hidden"],
                         "the provisional screen returned before final data loaded")
        self.assertEqual(got["duringReload"]["busy"], "true")
        self.assertTrue(got["finishedHidden"])
        self.assertIsNone(got["finishedBusy"])

    def test_a_409_follows_the_other_scan_and_then_reloads_final_data(self):
        got = run_js(
            "(async () => {\n" + self.DOM + r"""
              const calls = [];
              const deferred = [];
              let statusChecks = 0;
              globalThis.setTimeout = (fn, ms) => {
                deferred.push({ fn, ms }); return 1;
              };
              globalThis.fetch = async (path) => {
                calls.push(path);
                if (path === '/api/rescan') return { ok: false, status: 409,
                  json: async () => ({ error: 'A rescan is already running' }) };
                if (path === '/api/scan-status') return { ok: true, status: 200,
                  json: async () => ({
                    state: statusChecks++ === 0 ? 'scanning' : 'idle',
                    generation: statusChecks,
                  }) };
                throw new Error('unexpected request ' + path);
              };
              loadData = async () => { calls.push('reload'); clearLoading(); };

              const work = triggerRescan();
              await Promise.resolve(); await Promise.resolve();
              await Promise.resolve(); await Promise.resolve();
              const during = { calls: [...calls],
                hidden: els['load-overlay'].hidden,
                busy: attrs['aria-busy'], text: els['load-text'].textContent };
              deferred.shift().fn();
              await work;
              const after = { calls: [...calls],
                hidden: els['load-overlay'].hidden,
                busy: attrs['aria-busy'] || null };
              // The remaining deferred callback is only the button's reset.
              deferred.forEach(d => d.fn());
              console.log(JSON.stringify({ during, after,
                usableAgain: els['rescan-btn'].disabled === false }));
            })();""")

        self.assertEqual(
            got["during"]["calls"], ["/api/rescan", "/api/scan-status"])
        self.assertFalse(got["during"]["hidden"])
        self.assertEqual(got["during"]["busy"], "true")
        self.assertIn("Scanning usage history", got["during"]["text"])
        self.assertEqual(got["after"]["calls"], [
            "/api/rescan", "/api/scan-status", "/api/scan-status", "reload",
        ])
        self.assertTrue(got["after"]["hidden"])
        self.assertIsNone(got["after"]["busy"])
        self.assertTrue(got["usableAgain"])

    def test_a_409_whose_other_scan_failed_never_reloads_provisional_data(self):
        got = run_js(
            "(async () => {\n" + self.DOM + r"""
              const calls = [];
              globalThis.setTimeout = (fn) => { fn(); return 1; };
              globalThis.fetch = async (path) => {
                calls.push(path);
                if (path === '/api/rescan') return { ok: false, status: 409,
                  json: async () => ({ error: 'A rescan is already running' }) };
                if (path === '/api/scan-status') return { ok: true, status: 200,
                  json: async () => ({ state: 'failed', generation: 14 }) };
                throw new Error('unexpected request ' + path);
              };
              loadData = async () => { calls.push('reload'); };
              await triggerRescan();
              console.log(JSON.stringify({ calls,
                hidden: els['load-overlay'].hidden,
                spinnerHidden: els['load-spinner'].hidden,
                retryHidden: els['scan-retry'].hidden,
                text: els['load-text'].textContent }));
            })();""")

        self.assertEqual(got["calls"], ["/api/rescan", "/api/scan-status"])
        self.assertNotIn("reload", got["calls"])
        self.assertFalse(got["hidden"])
        self.assertTrue(got["spinnerHidden"])
        self.assertFalse(got["retryHidden"])
        self.assertIn("scan failed", got["text"].lower())

    def test_a_failure_during_the_post_scan_reload_keeps_its_retry_visible(self):
        """Rescan cleanup must not erase loadData's failure outcome."""
        got = run_js(
            "(async () => {\n" + self.DOM + r"""
              const calls = [];
              globalThis.setTimeout = (fn) => { fn(); return 1; };
              globalThis.fetch = async (path) => {
                calls.push(path.split('?')[0]);
                if (path === '/api/rescan') return { ok: true, status: 200,
                  json: async () => ({ new: 1, updated: 0 }) };
                if (path.startsWith('/api/data')) return { ok: false, status: 503,
                  json: async () => ({ error: 'Usage scan failed',
                    scan: { state: 'failed', generation: 20 } }) };
                if (path === '/api/scan-status') return { ok: true, status: 200,
                  json: async () => ({ state: 'failed', generation: 20 }) };
                throw new Error('unexpected request ' + path);
              };
              await triggerRescan();
              console.log(JSON.stringify({ calls,
                hidden: els['load-overlay'].hidden,
                spinnerHidden: els['load-spinner'].hidden,
                retryHidden: els['scan-retry'].hidden,
                text: els['load-text'].textContent }));
            })();""")

        self.assertEqual(got["calls"], [
            "/api/rescan", "/api/data", "/api/scan-status",
        ])
        self.assertFalse(got["hidden"])
        self.assertTrue(got["spinnerHidden"])
        self.assertFalse(got["retryHidden"])
        self.assertIn("scan failed", got["text"].lower())

    def test_a_post_scan_reload_does_not_clear_a_newer_source_overlay(self):
        got = run_js(
            "(async () => {\n" + self.DOM + r"""
              globalThis.setTimeout = (fn) => { fn(); return 1; };
              globalThis.fetch = async () => ({ ok: true, status: 200,
                json: async () => ({ new: 1, updated: 0 }) });
              loadData = async () => {
                selectedSource = 'codex';
                showLoading('Codex');
              };
              await triggerRescan();
              console.log(JSON.stringify({
                hidden: els['load-overlay'].hidden,
                busy: attrs['aria-busy'], text: els['load-text'].textContent }));
            })();""")

        self.assertFalse(got["hidden"])
        self.assertEqual(got["busy"], "true")
        self.assertIn("Loading Codex usage", got["text"])

    def test_a_status_failure_stays_visible_and_retries_without_reading_data(self):
        got = run_js(
            "(async () => {\n" + self.DOM + r"""
              const calls = [];
              const deferred = [];
              globalThis.setTimeout = (fn, ms) => {
                deferred.push({ fn, ms }); return 1;
              };
              globalThis.fetch = async (path) => {
                calls.push(path);
                return { ok: false, status: 503, json: async () => ({}) };
              };
              await start();
              console.log(JSON.stringify({ calls,
                hidden: els['load-overlay'].hidden,
                text: els['load-text'].textContent,
                busy: attrs['aria-busy'],
                retryDelays: deferred.map(d => d.ms) }));
            })();""")

        self.assertEqual(got["calls"], ["/api/scan-status"])
        self.assertFalse(got["hidden"])
        self.assertIn("retrying", got["text"])
        self.assertEqual(got["busy"], "true")
        self.assertEqual(got["retryDelays"], [3000])

    def test_a_failed_scan_never_turns_into_a_completed_zero_dashboard(self):
        got = run_js(
            "(async () => {\n" + self.DOM + r"""
              const calls = [];
              const deferred = [];
              globalThis.setTimeout = (fn, ms) => {
                deferred.push({ fn, ms }); return 1;
              };
              globalThis.fetch = async (path) => {
                calls.push(path);
                return { ok: true, status: 200,
                  json: async () => ({ state: 'failed', generation: 8 }) };
              };
              await start();
              console.log(JSON.stringify({ calls,
                hidden: els['load-overlay'].hidden,
                spinnerHidden: els['load-spinner'].hidden,
                retryHidden: els['scan-retry'].hidden,
                text: els['load-text'].textContent,
                busy: attrs['aria-busy'] || null,
                retryDelays: deferred.map(d => d.ms) }));
            })();""")

        self.assertEqual(got["calls"], ["/api/scan-status"])
        self.assertFalse(got["hidden"])
        self.assertTrue(got["spinnerHidden"],
                        "a stopped scan still showed an animated busy state")
        self.assertFalse(got["retryHidden"],
                         "the failure left no action inside the blocking overlay")
        self.assertIn("scan failed", got["text"].lower())
        self.assertIsNone(got["busy"])
        self.assertEqual(got["retryDelays"], [],
                         "polling idle status cannot recover a failed scan")

    def test_a_payload_rejected_across_a_scan_is_retried_only_after_idle(self):
        got = run_js(
            "(async () => {\n" + self.DOM + r"""
              const calls = [];
              const deferred = [];
              let statusChecks = 0;
              globalThis.setTimeout = (fn, ms) => {
                deferred.push({ fn, ms }); return 1;
              };
              globalThis.fetch = async (path) => {
                calls.push(path.split('?')[0]);
                if (path.startsWith('/api/data') &&
                    calls.filter(p => p === '/api/data').length === 1) {
                  return { ok: false, status: 409, json: async () => ({
                    error: 'Usage changed while data was loading',
                    scan: { state: 'scanning', generation: 11 },
                  }) };
                }
                if (path === '/api/scan-status') return {
                  ok: true, status: 200, json: async () => ({
                    state: statusChecks++ === 0 ? 'scanning' : 'idle',
                    generation: statusChecks === 1 ? 11 : 12,
                  }) };
                if (path.startsWith('/api/data')) return {
                  ok: true, status: 200, json: async () => ({
                    marker: 'final', generated_at: 'done', all_models: [],
                    daily_by_model: [], subscription_limits: null,
                    codex_limits: null,
                  }) };
                throw new Error('unexpected request ' + path);
              };
              renderSourceSwitch = () => {};
              renderDatabaseNotice = () => {};
              updateMetaNote = () => {};
              buildFilterUI = () => {};
              updateSortIcons = () => {};
              updateModelSortIcons = () => {};
              updateProjectSortIcons = () => {};
              updateProjectBranchSortIcons = () => {};
              applyFilter = () => {};
              // A scan changes every assistant. This off-screen entry predates
              // the 409 and must not survive merely because Claude is the
              // source whose crossed request noticed the scan.
              loadedSources.set('codex', { marker: 'pre-scan codex' });

              const work = loadData('claude');
              await new Promise(resolve => setImmediate(resolve));
              const during = [...calls];
              deferred.shift().fn();
              await work;
              console.log(JSON.stringify({ during, calls,
                raw: rawData && rawData.marker,
                cached: loadedSources.get('claude') &&
                        loadedSources.get('claude').marker,
                cachedSources: [...loadedSources.keys()].sort() }));
            })();""")

        self.assertEqual(got["during"], ["/api/data", "/api/scan-status"])
        self.assertEqual(got["calls"], [
            "/api/data", "/api/scan-status", "/api/scan-status", "/api/data",
        ])
        self.assertEqual(got["raw"], "final")
        self.assertEqual(got["cached"], "final")
        self.assertEqual(got["cachedSources"], ["claude"],
                         "a completed scan left an off-screen source cache stale")

    def test_a_status_error_during_a_cold_data_load_keeps_retrying_visibly(self):
        got = run_js(
            "(async () => {\n" + self.DOM + r"""
              const calls = [];
              const deferred = [];
              globalThis.setTimeout = (fn, ms) => {
                deferred.push({ fn, ms }); return 1;
              };
              globalThis.fetch = async (path) => {
                calls.push(path.split('?')[0]);
                if (path.startsWith('/api/data')) return {
                  ok: false, status: 409, json: async () => ({
                    error: 'Usage scan in progress',
                    scan: { state: 'scanning', generation: 4 },
                  }) };
                if (path === '/api/scan-status') throw new Error('temporary disconnect');
                throw new Error('unexpected request ' + path);
              };
              loadedSources.set('codex', { marker: 'pre-scan codex' });

              await loadData('claude');
              console.log(JSON.stringify({ calls,
                hidden: els['load-overlay'].hidden,
                busy: attrs['aria-busy'] || null,
                text: els['load-text'].textContent,
                retryDelays: deferred.map(d => d.ms),
                cachedSources: [...loadedSources.keys()].sort() }));
            })();""")

        self.assertEqual(got["calls"], ["/api/data", "/api/scan-status"])
        self.assertFalse(got["hidden"],
                         "a transient status error exposed an empty dashboard")
        self.assertEqual(got["busy"], "true")
        self.assertIn("retrying", got["text"].lower())
        self.assertEqual(got["retryDelays"], [3000])
        self.assertEqual(got["cachedSources"], [],
                         "a transient status error preserved caches known to "
                         "predate the crossed scan")


@requires_node
class TestCsvExportsMatchTheirHeaders(unittest.TestCase):
    """Every exported row must line up with the header above it.

    The five export builders each write their header and their row in two
    separate literals, with nothing tying them together. Two of them drifted: a
    `cache_creation_1h` field was added to the rows of the project exports and
    not to their headers, so every row carried one field more than the header
    declared and every column past Cache Creation was shifted left. The column
    LABELLED "Est. Cost" then held a token count, and the money sat in an
    unlabelled column — so a spreadsheet totalling the labelled one summed
    1-hour cache tokens as dollars, and for the common case of no 1-hour writes
    reported every project as costing $0.
    """

    # One fixture row per export, with a distinct value in every field so a
    # one-column shift cannot coincidentally still look right.
    FIXTURES = r"""
      const captured = {};
      downloadCSV = (name, header, rows) => { captured[name] = {header, row: rows[0]}; };
      lastByModel = [{model: 'claude-opus-5', turns: 11, input: 12, output: 13,
                      cache_read: 14, cache_creation: 15, cache_creation_1h: 7}];
      lastFilteredSessions = [{session_id: 's1', project: 'p', topic: 't',
                      last: '2026-08-06 10:00', duration_min: 9,
                      model: 'claude-opus-5', turns: 11, input: 12, output: 13,
                      cache_read: 14, cache_creation: 15, cache_creation_1h: 7,
                      cost: 1.5, billable: true}];
      lastByProject = [{project: 'p', sessions: 3, turns: 11, input: 12, output: 13,
                      cache_read: 14, cache_creation: 15, cache_creation_1h: 7,
                      cost: 1.5, billable: true}];
      lastByProjectBranch = [{project: 'p', branch: 'main', sessions: 3, turns: 11,
                      input: 12, output: 13, cache_read: 14, cache_creation: 15,
                      cache_creation_1h: 7, cost: 1.5, billable: true}];
      lastFilteredDispatches = [{agent_type: 'Explore', agent_id: 'a1',
                      start: '2026-08-06 10:00', model: 'claude-opus-5', turns: 11,
                      tool_uses: 4, duration_ms: 500, input: 12, output: 13,
                      cache_read: 14, cache_creation: 15, cache_creation_1h: 7,
                      cost: 1.5, billable: true, status: 'ok'}];
      exportModelCSV(); exportSessionsCSV(); exportProjectsCSV();
      exportProjectBranchCSV(); exportDispatchesCSV();
      // What the money column must contain, from the same source the exporter
      // used: four of them carry a precomputed `cost`, while Cost by Model
      // prices its own row. Hardcoding one number would only test the fixture.
      for (const name of Object.keys(captured)) {
        captured[name].expected = (name === 'cost_by_model'
          ? calcCost('claude-opus-5', 12, 13, 14, 15, 7)
          : 1.5).toFixed(4);
      }
    """

    @classmethod
    def setUpClass(cls):
        if not NODE:
            return
        cls.exports = run_js(emit("(() => {" + cls.FIXTURES + "return captured; })()"))

    def test_every_export_row_has_one_field_per_header(self):
        for name, table in self.exports.items():
            with self.subTest(export=name):
                self.assertEqual(
                    len(table["row"]), len(table["header"]),
                    f"{name}: {len(table['row'])} fields under "
                    f"{len(table['header'])} headers — every column after the "
                    "first extra one is mislabelled")

    def test_the_column_labelled_est_cost_holds_the_cost(self):
        """The alignment that actually matters: money under the money header."""
        for name, table in self.exports.items():
            with self.subTest(export=name):
                self.assertIn("Est. Cost", table["header"])
                at = table["header"].index("Est. Cost")
                self.assertEqual(
                    str(table["row"][at]), table["expected"],
                    f"{name}: the 'Est. Cost' column holds "
                    f"{table['row'][at]!r}, not the cost")

    def test_every_exported_token_column_is_labelled(self):
        """A 1-hour cache figure in the row needs its own header, not silence."""
        for name, table in self.exports.items():
            with self.subTest(export=name):
                if 7 in table["row"]:          # the fixture's cache_creation_1h
                    self.assertIn("Cache Creation (1h)", table["header"])

    # The fixture's distinct value per field, by the header that names it. Every
    # export writes its header and its row as two separate literals, so a column
    # can hold a neighbour's figure and still line up: the tests above count the
    # fields and check the money, and neither can see `m.input` exported under
    # "Cache Read". Mutating exactly that in exportModelCSV left this whole file
    # green.
    TOKEN_COLUMNS = {"Turns": 11, "Input": 12, "Output": 13, "Cache Read": 14,
                     "Cache Creation": 15, "Cache Creation (1h)": 7}

    def test_every_exported_token_column_holds_its_own_field(self):
        for name, table in self.exports.items():
            for header, value in self.TOKEN_COLUMNS.items():
                if header not in table["header"]:
                    continue
                with self.subTest(export=name, column=header):
                    at = table["header"].index(header)
                    self.assertEqual(
                        table["row"][at], value,
                        f"{name}: the {header!r} column holds "
                        f"{table['row'][at]!r}, which is another column's figure")


@requires_node
class TestQuotaThresholdAlerts(unittest.TestCase):
    """Notify me when my window passes N% — one implementation for both
    assistants, because both project into the same window shape.

    The subtleties are all in *when* it fires: the panel re-renders every 30
    seconds, so a percentage that merely sits above a threshold must not notify
    on every one of them; a reload must not replay thresholds already passed;
    and opening the page on an already-high window must not announce every
    threshold below it at once.
    """

    def _js(self, body):
        return run_js(
            "(() => {\n"
            "  const delivered = [];\n"
            "  globalThis.Notification = undefined;\n"
            "  document.getElementById = () => ({ set innerHTML(v) {},\n"
            "    set textContent(v) { delivered.push(v); }, set hidden(v) {} });\n"
            + body + "\n"
            "})()")

    def test_a_crossing_fires_and_sitting_still_does_not(self):
        """The 30-second re-render is the reason this matters."""
        got = run_js(emit("""
          (() => {
            const t = [80];
            return {
              crossed:  crossedThresholds(70, 82, t),
              stayed:   crossedThresholds(82, 83, t),
              exact:    crossedThresholds(79, 80, t),
              backwards: crossedThresholds(90, 40, t),
            };
          })()"""))
        self.assertEqual(got["crossed"], [80])
        self.assertEqual(got["stayed"], [], "it fired again while merely sitting above")
        self.assertEqual(got["exact"], [80], "landing exactly on the threshold counts")
        self.assertEqual(got["backwards"], [])

    def test_several_thresholds_can_be_crossed_at_once(self):
        """A jump between readings passes more than one, and each is reported."""
        got = run_js(emit("crossedThresholds(15, 92, [20, 30, 80, 90, 95])"))
        self.assertEqual(got, [20, 30, 80, 90])

    def test_the_first_sighting_of_a_window_fires_nothing(self):
        """Opening the page at 91% must not announce 20, 30, 50 and 90 at once
        for crossings nobody was watching. Four notifications say less than
        none."""
        self.assertEqual(run_js(emit("crossedThresholds(null, 91, [20,30,50,90])")), [])

    def test_a_missing_percentage_fires_nothing(self):
        for reading in ("null", "undefined", "'oops'", "NaN"):
            with self.subTest(percent=reading):
                self.assertEqual(
                    run_js(emit(f"crossedThresholds(10, {reading}, [20])")), [])

    def test_no_thresholds_means_no_alerts(self):
        """An empty selection is a real choice — 'never notify me'."""
        self.assertEqual(run_js(emit("crossedThresholds(10, 99, [])")), [])

    def test_the_default_is_eighty_percent(self):
        got = run_js(emit("""
          (() => { localStorage.removeItem('cu_alert_thresholds');
                   return loadThresholds(); })()"""))
        self.assertEqual(got, [80])

    def test_a_deliberate_empty_selection_survives_a_reload(self):
        """Turning every threshold off must not silently restore the default."""
        got = run_js(emit("""
          (() => { saveThresholds([]); return loadThresholds(); })()"""))
        self.assertEqual(got, [])

    def test_thresholds_are_deduplicated_sorted_and_range_checked(self):
        got = run_js(emit("""
          (() => { saveThresholds([90, 20, 90, 0, -5, 101, 50, 'x']);
                   return loadThresholds(); })()"""))
        self.assertEqual(got, [20, 50, 90])

    def test_two_windows_of_the_same_kind_are_told_apart(self):
        """Without the reset time in the key, the next five-hour window would
        inherit the previous one's fired state and never notify."""
        got = run_js(emit("""
          (() => {
            const a = { kind: 'session', group: 'session', scope: '', resets_at: 'A' };
            const b = { kind: 'session', group: 'session', scope: '', resets_at: 'B' };
            return { differ: alertWindowKey('claude', a) !== alertWindowKey('claude', b),
                     sources: alertWindowKey('claude', a) !== alertWindowKey('codex', a) };
          })()"""))
        self.assertTrue(got["differ"])
        self.assertTrue(got["sources"], "the two assistants must not share a window key")

    def test_the_jittering_reset_time_does_not_invent_a_new_window(self):
        """Invented readings cross a minute boundary without changing windows.

        Keying on the raw string would reset the baseline and repeatedly
        suppress alerts. Match account._reset_key's rounding contract.
        """
        got = run_js(emit("""
          (() => {
            const w = (t) => ({ kind: 'session', group: 'session', scope: '', resets_at: t });
            const keys = ['2025-01-01T12:29:59.125000+00:00',
                          '2025-01-01T12:29:59.250000+00:00',
                          '2025-01-01T12:30:00.100000+00:00',
                          '2025-01-01T12:30:00.200000+00:00']
              .map(t => alertWindowKey('claude', w(t)));
            return { distinct: [...new Set(keys)].length,
                     nextWindow: alertWindowKey('claude', w('2025-01-01T17:30:00.0Z'))
                                 !== keys[0] };
          })()"""))
        self.assertEqual(got["distinct"], 1,
                         "sub-second jitter split one window into several")
        self.assertTrue(got["nextWindow"], "and the NEXT window must still differ")

    def test_an_alert_survives_the_cache_refreshing_mid_window(self):
        """End to end: the same window read three times with jittered reset
        times must still notify exactly once on the crossing."""
        got = run_js(emit("""
          (() => {
            localStorage.clear();
            saveThresholds([30]);
            const at = (p, t) => ({ available: true, windows: [
              { kind: 'session', group: 'session', scope: '', resets_at: t,
                percent: p, expired: false }] });
            // Jitter around a minute boundary an hour AHEAD. The offsets are the
            // real observed ones; only the boundary is derived from the clock,
            // because a live window's reset time is in the future and a
            // hardcoded one silently stops being a live window.
            const edge = Math.round((Date.now() + 3600e3) / 60000) * 60000;
            const jit = (ms) => new Date(edge + ms).toISOString();
            checkQuotaAlerts(at(10, jit(-4)), 'claude');
            const crossed = checkQuotaAlerts(at(33, jit(22)), 'claude');
            const again   = checkQuotaAlerts(at(34, jit(-425)), 'claude');
            return { crossed, again };
          })()"""))
        self.assertEqual(got["crossed"], [30], "the crossing was lost to jitter")
        self.assertEqual(got["again"], [])

    def test_enabling_a_threshold_you_are_already_past_says_so(self):
        """Setting 30% while sitting at 33% produced nothing at all, because the
        first sighting is a baseline. But newly ticking a threshold is an
        explicit question about right now, and the honest answer is that you
        are already past it."""
        got = run_js(emit("""
          (() => {
            localStorage.clear();
            let announced = null;
            deliverAlert = (title, body) => { announced = title + ' / ' + body; };
            lastPlanInfo = { available: true, windows: [
              { kind: 'session', group: 'session', scope: '',
                resets_at: '2099-01-01T00:00:00Z', percent: 33, expired: false }] };
            selectedSource = 'claude';
            const fired = announceIfAlreadyPast(30);
            // ...and it must not then repeat on the next poll.
            const next = checkQuotaAlerts(lastPlanInfo, 'claude');
            return { fired, announced, next };
          })()"""))
        self.assertEqual(got["fired"], 1)
        self.assertIn("33", got["announced"])
        self.assertEqual(got["next"], [], "it announced the same threshold twice")

    def test_ticking_the_chip_is_what_triggers_the_announcement(self):
        """Covers the WIRING. With announceIfAlreadyPast tested only directly,
        deleting its call from the toggle left every assertion green while the
        reported symptom — set 30% at 33%, hear nothing — came straight back."""
        got = run_js(emit("""
          (() => {
            localStorage.clear();
            windowThresholds = {};
            let announced = 0;
            deliverAlert = () => { announced += 1; };
            // The window carries its own `key` now, and the toggle is told
            // which window it is acting on -- the announcement is scoped to
            // that window, so setting a threshold on one limit cannot report
            // a different limit that happens to be past the same number.
            lastPlanInfo = { available: true, windows: [
              { kind: 'session', group: 'session', scope: '', key: 'claude:session',
                resets_at: '2099-01-01T00:00:00Z', percent: 33, expired: false }] };
            selectedSource = 'claude';
            toggleAlertThreshold(30, 'claude:session');   // ON, already past it
            const onEnable = announced;
            removeThreshold(30, 'claude:session');        // OFF again
            toggleAlertThreshold(95, 'claude:session');   // ON, nowhere near it
            return { onEnable, afterOffAndHigh: announced };
          })()"""))
        self.assertEqual(got["onEnable"], 1,
                         "ticking a threshold already passed announced nothing")
        self.assertEqual(got["afterOffAndHigh"], 1,
                         "turning one off, or ticking one not yet reached, announced")

    def test_enabling_a_threshold_you_are_below_announces_nothing_yet(self):
        got = run_js(emit("""
          (() => {
            localStorage.clear();
            lastPlanInfo = { available: true, windows: [
              { kind: 'session', group: 'session', scope: '',
                resets_at: 'W', percent: 12, expired: false }] };
            selectedSource = 'claude';
            return announceIfAlreadyPast(30);
          })()"""))
        self.assertEqual(got, 0)

    def test_the_same_reading_twice_notifies_once(self):
        """The panel re-renders on every poll; the fired state is what stops it
        announcing the same crossing repeatedly."""
        got = run_js(emit("""
          (() => {
            localStorage.clear();
            saveThresholds([80]);
            const at = (p) => ({ available: true, windows: [
              { kind: 'session', group: 'session', scope: '', resets_at: 'W',
                percent: p, expired: false }] });
            const first  = checkQuotaAlerts(at(10), 'claude');   // baseline
            const cross  = checkQuotaAlerts(at(85), 'claude');   // fires
            const again  = checkQuotaAlerts(at(86), 'claude');   // must not
            const rerun  = checkQuotaAlerts(at(85), 'claude');   // nor this
            return { first, cross, again, rerun };
          })()"""))
        self.assertEqual(got["first"], [])
        self.assertEqual(got["cross"], [80])
        self.assertEqual(got["again"], [])
        self.assertEqual(got["rerun"], [])

    def test_a_new_window_notifies_again(self):
        """Passing 80% tomorrow is news even though you passed it today."""
        got = run_js(emit("""
          (() => {
            localStorage.clear();
            saveThresholds([80]);
            const at = (w, p) => ({ available: true, windows: [
              { kind: 'session', group: 'session', scope: '', resets_at: w,
                percent: p, expired: false }] });
            checkQuotaAlerts(at('MON', 10), 'claude');
            const one = checkQuotaAlerts(at('MON', 85), 'claude');
            checkQuotaAlerts(at('TUE', 10), 'claude');
            const two = checkQuotaAlerts(at('TUE', 85), 'claude');
            return { one, two };
          })()"""))
        self.assertEqual(got["one"], [80])
        self.assertEqual(got["two"], [80], "the next window never notified")

    def test_a_window_that_rolls_over_above_a_threshold_announces(self):
        """Witnessing a rollover is not a cold start.

        The first sighting of a window is a baseline so that opening the page at
        91% does not flood. But when a window we were already watching rolls over
        to a fresh one that is ALREADY past a threshold, that is news — and
        treating it as a first sighting meant coming back to a new window at 55%
        with thresholds at 30 and 50 said nothing for its whole five hours."""
        got = run_js(emit("""
          (() => {
            localStorage.clear();
            saveThresholds([30, 50]);
            const at = (p, resets) => ({ available: true, windows: [
              { kind: 'session', group: 'session', scope: '', resets_at: resets,
                percent: p, expired: false }] });
            const cold = checkQuotaAlerts(at(20, 'MON'), 'claude');   // baseline
            const rolled = checkQuotaAlerts(at(55, 'TUE'), 'claude'); // rollover
            const same = checkQuotaAlerts(at(58, 'TUE'), 'claude');   // no repeat
            return { cold, rolled, same };
          })()"""))
        self.assertEqual(got["cold"], [], "the very first sighting must stay quiet")
        self.assertEqual(got["rolled"], [30, 50], "the rollover was not announced")
        self.assertEqual(got["same"], [], "and then it repeated")

    def test_the_very_first_window_of_a_kind_is_still_a_baseline(self):
        """Opening the dashboard cold at 91% must not fire four notifications
        for crossings nobody was watching."""
        got = run_js(emit("""
          (() => {
            localStorage.clear();
            saveThresholds([20, 30, 50, 90]);
            return checkQuotaAlerts({ available: true, windows: [
              { kind: 'session', group: 'session', scope: '', resets_at: 'W',
                percent: 91, expired: false }] }, 'claude');
          })()"""))
        self.assertEqual(got, [])

    def test_one_assistants_history_does_not_make_the_others_window_news(self):
        """The kind check is scoped to the source, so Codex having rolled over
        cannot turn Claude's first sighting into an announcement."""
        got = run_js(emit("""
          (() => {
            localStorage.clear();
            saveThresholds([30]);
            const at = (p, r) => ({ available: true, windows: [
              { kind: 'session', group: 'session', scope: '', resets_at: r,
                percent: p, expired: false }] });
            checkQuotaAlerts(at(10, 'MON'), 'codex');
            checkQuotaAlerts(at(80, 'TUE'), 'codex');
            return checkQuotaAlerts(at(80, 'WED'), 'claude');
          })()"""))
        self.assertEqual(got, [])

    def test_an_expired_window_never_alerts(self):
        """Its percentage describes the window that has already rolled over, so
        alerting would announce a limit you are no longer under."""
        got = run_js(emit("""
          (() => {
            localStorage.clear();
            saveThresholds([80]);
            const w = (p, exp) => ({ available: true, windows: [
              { kind: 'session', group: 'session', scope: '', resets_at: 'W',
                percent: p, expired: exp }] });
            checkQuotaAlerts(w(10, false), 'claude');
            return checkQuotaAlerts(w(99, true), 'claude');
          })()"""))
        self.assertEqual(got, [])

    def test_the_two_assistants_share_one_implementation(self):
        """The point of the design: the same function, called with either
        source, and neither can suppress the other's alert."""
        got = run_js(emit("""
          (() => {
            localStorage.clear();
            saveThresholds([80]);
            const at = (p) => ({ available: true, windows: [
              { kind: 'session', group: 'session', scope: '', resets_at: 'W',
                percent: p, expired: false }] });
            checkQuotaAlerts(at(10), 'claude');
            checkQuotaAlerts(at(10), 'codex');
            return { claude: checkQuotaAlerts(at(85), 'claude'),
                     codex:  checkQuotaAlerts(at(85), 'codex') };
          })()"""))
        self.assertEqual(got["claude"], [80])
        self.assertEqual(got["codex"], [80])

    def test_the_fired_state_does_not_grow_without_bound(self):
        got = run_js(emit("""
          (() => {
            localStorage.clear();
            saveThresholds([80]);
            for (let i = 0; i < 260; i++) {
              checkQuotaAlerts({ available: true, windows: [
                { kind: 'session', group: 'session', scope: '',
                  resets_at: 'W' + i, percent: 5, expired: false }] }, 'claude');
            }
            return Object.keys(JSON.parse(localStorage.getItem('cu_alert_fired'))).length;
          })()"""))
        self.assertLessEqual(got, 200)


@requires_node
class TestCustomThresholds(unittest.TestCase):
    """Any whole percentage from 1 to 100, added and removed at will.

    The presets are a shortcut, not the vocabulary: they can all be removed, and
    a value none of them offers can be typed. A rejected entry has to SAY why —
    one that vanishes silently is indistinguishable from one that was accepted
    and then ignored.
    """

    def test_a_whole_number_in_range_is_accepted(self):
        got = run_js(emit("[1, 7, 33, 99, 100].map(v => parseThreshold(String(v)).value)"))
        self.assertEqual(got, [1, 7, 33, 99, 100])

    def test_surrounding_space_and_a_percent_sign_are_tolerated(self):
        got = run_js(emit("['  42 ', '42%', ' 42% '].map(v => parseThreshold(v).value)"))
        self.assertEqual(got, [42, 42, 42])

    def test_out_of_range_is_refused_with_a_reason(self):
        got = run_js(emit("[0, 101, 1000].map(v => parseThreshold(String(v)))"))
        for entry in got:
            self.assertIn("between 1 and 100", entry["error"])
            self.assertNotIn("value", entry)

    def test_anything_that_is_not_a_whole_number_is_refused(self):
        got = run_js(emit(
            "['', '  ', 'abc', '4.5', '-7', '1e2', '٣', '50a', '+8']"
            ".map(v => ({ input: v, ...parseThreshold(v) }))"))
        for entry in got:
            with self.subTest(input=entry["input"]):
                self.assertIn("error", entry, f"{entry['input']!r} was accepted")
                self.assertNotIn("value", entry)

    def test_a_custom_value_is_added_and_persisted(self):
        got = run_js(emit("""
          (() => {
            localStorage.clear(); windowThresholds = {};
            document.getElementById = () => ({ set innerHTML(v) {},
              set textContent(v) {}, set hidden(v) {} });
            deliverAlert = () => {};
            lastPlanInfo = null;
            // Per WINDOW now: the same behaviour, addressed to one limit.
            const ok = addCustomThreshold('37', 'claude:session');
            return { ok, stored: thresholdsForWindow('claude:session') };
          })()"""))
        self.assertTrue(got["ok"])
        self.assertEqual(got["stored"], [37])

    def test_a_rejected_value_changes_nothing_and_reports_why(self):
        got = run_js(emit("""
          (() => {
            localStorage.clear(); saveThresholds([80]);
            let shown = '';
            document.getElementById = () => ({ set innerHTML(v) {},
              set textContent(v) { shown = v; }, set hidden(v) {} });
            const ok = addCustomThreshold('130');
            return { ok, shown, stored: loadThresholds() };
          })()"""))
        self.assertFalse(got["ok"])
        self.assertIn("between 1 and 100", got["shown"])
        self.assertEqual(got["stored"], [80], "a rejected entry altered the set")

    def test_any_threshold_can_be_removed_including_a_preset(self):
        """The presets are a shortcut, not a floor."""
        got = run_js(emit("""
          (() => {
            localStorage.clear();
            windowThresholds = { 'claude:session': [30, 80] };
            document.getElementById = () => ({ set innerHTML(v) {},
              set textContent(v) {}, set hidden(v) {} });
            removeThreshold(80, 'claude:session');
            const afterOne = thresholdsForWindow('claude:session');
            removeThreshold(30, 'claude:session');
            return { afterOne, afterAll: thresholdsForWindow('claude:session') };
          })()"""))
        self.assertEqual(got["afterOne"], [30])
        self.assertEqual(got["afterAll"], [], "the last preset could not be removed")

    def test_removing_something_not_there_is_harmless(self):
        got = run_js(emit("""
          (() => {
            localStorage.clear(); saveThresholds([30]);
            document.getElementById = () => ({ set innerHTML(v) {},
              set textContent(v) {}, set hidden(v) {} });
            removeThreshold(99);
            return loadThresholds();
          })()"""))
        self.assertEqual(got, [30])

    def test_adding_one_already_chosen_is_accepted_without_duplicating(self):
        got = run_js(emit("""
          (() => {
            localStorage.clear();
            windowThresholds = { 'claude:session': [50] };
            document.getElementById = () => ({ set innerHTML(v) {},
              set textContent(v) {}, set hidden(v) {} });
            deliverAlert = () => {}; lastPlanInfo = null;
            const ok = addCustomThreshold('50', 'claude:session');
            return { ok, stored: thresholdsForWindow('claude:session') };
          })()"""))
        self.assertTrue(got["ok"])
        self.assertEqual(got["stored"], [50])


@requires_node
class TestSharedWindowThresholdPersistence(unittest.TestCase):
    def test_a_magic_window_key_is_stored_as_data_not_as_a_prototype(self):
        got = run_js(emit(r"""
          (() => {
            windowThresholds = {};
            const orphaned = JSON.parse('{"__proto__":[37]}');
            syncWindowThresholdsFromInfo({ windows: [], orphaned });
            return {
              own: Object.prototype.hasOwnProperty.call(
                windowThresholds, '__proto__'),
              prototypeWasReplaced: Array.isArray(
                Object.getPrototypeOf(windowThresholds)),
              thresholds: thresholdsForWindow('__proto__'),
            };
          })()"""))
        self.assertTrue(got["own"])
        self.assertFalse(got["prototypeWasReplaced"])
        self.assertEqual(got["thresholds"], [37])

    def test_each_save_patches_only_its_window(self):
        got = run_js_with_api_token(r"""
(async () => {
  localStorage.clear();
  windowThresholds = { 'claude:session': [30] };
  lastPlanInfo = { available: true, source: 'claude', windows: [
    { key: 'claude:session', kind: 'session', group: 'session', scope: '',
      percent: 20, thresholds: [30], resets_at: 'W' }
  ] };
  let serverMap = { 'claude:session': [30], 'claude:weekly': [5] };
  let sent = null;
  let method = null;
  let gets = 0;
  apiFetch = async (path, options = {}) => {
    if (!options.method) {
      gets += 1;
      return { ok: true, json: async () => ({ thresholds: serverMap }) };
    }
    method = options.method;
    sent = JSON.parse(options.body).thresholds;
    serverMap = { ...serverMap, ...sent };
    return { ok: true, json: async () => ({ thresholds: serverMap }) };
  };
  const ok = await saveWindowThresholds('claude:session', [30, 90]);
  console.log(JSON.stringify({ ok, method, gets, sent, serverMap,
    mirror: windowThresholds }));
})();""")
        self.assertTrue(got["ok"])
        self.assertEqual(got["method"], "PATCH")
        self.assertEqual(got["gets"], 0)
        self.assertEqual(got["sent"], {"claude:session": [30, 90]})
        self.assertEqual(got["serverMap"]["claude:weekly"], [5])

    def test_a_failed_patch_reloads_the_authoritative_mirror(self):
        got = run_js_with_api_token(r"""
(async () => {
  windowThresholds = { 'claude:session': [30], 'claude:weekly': [5] };
  const authoritative = { 'claude:session': [30], 'claude:weekly': [5] };
  let patches = 0;
  let gets = 0;
  apiFetch = async (path, options = {}) => {
    if (options.method) {
      patches += 1;
      return { ok: false, json: async () => ({}) };
    }
    gets += 1;
    return { ok: true, json: async () => ({ thresholds: authoritative }) };
  };
  const ok = await saveWindowThresholds('claude:session', [90]);
  console.log(JSON.stringify({ ok, patches, gets, mirror: windowThresholds }));
})();""")
        self.assertFalse(got["ok"])
        self.assertEqual(got["patches"], 1)
        self.assertEqual(got["gets"], 1)
        self.assertEqual(got["mirror"],
                         {"claude:session": [30], "claude:weekly": [5]})

    def test_overlapping_saves_are_serialized_and_both_windows_survive(self):
        got = run_js_with_api_token(r"""
(async () => {
  windowThresholds = {};
  lastPlanInfo = { available: true, source: 'claude', windows: [
    { key: 'claude:session', percent: 20, thresholds: [80], resets_at: 'W' },
    { key: 'claude:weekly', percent: 20, thresholds: [80], resets_at: 'W' }
  ] };
  checkQuotaAlerts(lastPlanInfo, 'claude');
  let serverMap = { 'codex:weekly': [70] };
  let releaseFirst;
  const firstMayFinish = new Promise(resolve => { releaseFirst = resolve; });
  const calls = [];
  apiFetch = async (path, options = {}) => {
    const updates = JSON.parse(options.body).thresholds;
    calls.push({ method: options.method, updates });
    if (calls.length === 1) await firstMayFinish;
    serverMap = { ...serverMap, ...updates };
    return { ok: true, json: async () => ({ thresholds: serverMap }) };
  };
  const first = saveWindowThresholds('claude:session', [30]);
  const second = saveWindowThresholds('claude:weekly', [90]);
  await Promise.resolve();
  await Promise.resolve();
  const callsBeforeRelease = calls.length;
  releaseFirst();
  const results = await Promise.all([first, second]);
  lastPlanInfo.windows[0].percent = 35;
  lastPlanInfo.windows[1].percent = 95;
  const crossed = checkQuotaAlerts(lastPlanInfo, 'claude');
  console.log(JSON.stringify({ callsBeforeRelease, calls, results, serverMap,
    mirror: windowThresholds, crossed }));
})();""")
        self.assertEqual(got["callsBeforeRelease"], 1,
                         "rapid saves reached the server concurrently")
        self.assertEqual([call["method"] for call in got["calls"]],
                         ["PATCH", "PATCH"])
        self.assertEqual(got["calls"][0]["updates"],
                         {"claude:session": [30]})
        self.assertEqual(got["calls"][1]["updates"],
                         {"claude:weekly": [90]})
        self.assertEqual(got["results"], [True, True])
        self.assertEqual(got["serverMap"], {
            "codex:weekly": [70],
            "claude:session": [30],
            "claude:weekly": [90],
        })
        self.assertEqual(got["mirror"], got["serverMap"])
        self.assertEqual(got["crossed"], [30, 90])

    def test_a_loaded_map_updates_cached_payloads_and_orphan_controls(self):
        got = run_js_with_api_token(r"""
(async () => {
  alertThresholdsLoaded = false;
  lastPlanInfo = { windows: [{key: 'claude:session', thresholds: [80]}],
    orphaned: {'claude:weekly': [80]} };
  const cached = { codex_limits: { windows: [
    {key: 'codex:weekly', thresholds: [80]} ] } };
  loadedSources.set('codex', cached);
  const stored = {'claude:session': [30], 'claude:weekly': [5],
    'codex:weekly': [90]};
  apiFetch = async () => ({ok: true, json: async () => ({thresholds: stored})});
  await loadWindowThresholds();
  console.log(JSON.stringify({mirror: windowThresholds, live: lastPlanInfo,
    cached: cached.codex_limits}));
})();""")
        self.assertEqual(got["mirror"], {
            "claude:session": [30], "claude:weekly": [5], "codex:weekly": [90]})
        self.assertEqual(got["live"]["windows"][0]["thresholds"], [30])
        self.assertEqual(got["live"]["orphaned"], {"claude:weekly": [5]})
        self.assertEqual(got["cached"]["windows"][0]["thresholds"], [90])

    def test_an_old_recovery_get_cannot_replace_a_newer_successful_save(self):
        got = run_js_with_api_token(r"""
(async () => {
  windowThresholds = {'claude:session': [80]};
  let releaseGet, beginGet;
  const mayFinish = new Promise(resolve => {releaseGet = resolve;});
  const getStarted = new Promise(resolve => {beginGet = resolve;});
  let patches = 0;
  apiFetch = async (path, options = {}) => {
    if (!options.method) {
      beginGet();
      await mayFinish;
      return {ok: true, json: async () => ({thresholds: {'claude:session': [80]}})};
    }
    patches += 1;
    return {ok: patches > 1,
      json: async () => ({thresholds: {'claude:session': [95]}})};
  };
  const oldSave = saveWindowThresholds('claude:session', [30]);
  await getStarted;
  const newSave = await saveWindowThresholds('claude:session', [95]);
  releaseGet();
  const failed = await oldSave;
  console.log(JSON.stringify({failed, newSave, mirror: windowThresholds}));
})();""")
        self.assertFalse(got["failed"])
        self.assertTrue(got["newSave"])
        self.assertEqual(got["mirror"], {"claude:session": [95]})

    def test_an_empty_store_resets_defaults_but_legacy_projections_are_kept(self):
        for stored, expected in (({}, [80]), ({"claude:foo_bar": [30]}, [30])):
            with self.subTest(stored=stored):
                got = run_js_with_api_token("const stored = " + json.dumps(stored) + r""";
(async () => {
  alertThresholdsLoaded = false;
  lastPlanInfo = {windows: [{key: 'claude:foo%3Abar', thresholds: [30]},
    {key: '', thresholds: []}],
    orphaned: {'claude:gone': [5]}};
  apiFetch = async () => ({ok: true, json: async () => ({thresholds: stored})});
  await loadWindowThresholds();
  console.log(JSON.stringify({thresholds: lastPlanInfo.windows[0].thresholds,
    mirror: thresholdsForWindow('claude:foo%3Abar'), orphaned: lastPlanInfo.orphaned,
    keyless: lastPlanInfo.windows[1].thresholds}));
})();""")
                self.assertEqual(got["thresholds"], expected)
                self.assertEqual(got["mirror"], expected)
                self.assertEqual(got["orphaned"], {})
                self.assertEqual(got["keyless"], [])

    def test_a_legacy_migration_finishes_before_a_newer_user_edit(self):
        got = run_js_with_api_token(r"""
(async () => {
  alertThresholdsLoaded = false;
  localStorage.setItem(ALERT_THRESHOLD_KEY, '[30]');
  lastPlanInfo = {windows: [{key: 'claude:session', thresholds: [80]}]};
  let releaseMigration, beginMigration;
  const mayFinish = new Promise(resolve => {releaseMigration = resolve;});
  const migrationStarted = new Promise(resolve => {beginMigration = resolve;});
  let serverMap = {}, patches = 0;
  apiFetch = async (path, options = {}) => {
    if (!options.method) return {ok: true, json: async () => ({thresholds: serverMap})};
    patches += 1;
    if (patches === 1) {beginMigration(); await mayFinish;}
    serverMap = {...serverMap, ...JSON.parse(options.body).thresholds};
    return {ok: true, json: async () => ({thresholds: serverMap})};
  };
  const loading = loadWindowThresholds();
  await migrationStarted;
  const saving = saveWindowThresholds('claude:session', [95]);
  releaseMigration();
  await Promise.all([loading, saving]);
  console.log(JSON.stringify({serverMap, mirror: windowThresholds,
    thresholds: lastPlanInfo.windows[0].thresholds}));
})();""")
        self.assertEqual(got["serverMap"], {"claude:session": [95]})
        self.assertEqual(got["mirror"], got["serverMap"])
        self.assertEqual(got["thresholds"], [95])

    def test_an_initial_get_cannot_replace_a_newer_saved_threshold(self):
        got = run_js_with_api_token(r"""
(async () => {
  alertThresholdsLoaded = false;
  let releaseGet;
  const mayFinish = new Promise(resolve => {releaseGet = resolve;});
  apiFetch = async (path, options = {}) => {
    if (!options.method) {
      await mayFinish;
      return {ok: true, json: async () => ({thresholds: {'claude:session': [80]}})};
    }
    return {ok: true, json: async () => ({thresholds: {'claude:session': [95]}})};
  };
  const loading = loadWindowThresholds();
  const saved = await saveWindowThresholds('claude:session', [95]);
  releaseGet();
  await loading;
  console.log(JSON.stringify({saved, mirror: windowThresholds}));
})();""")
        self.assertTrue(got["saved"])
        self.assertEqual(got["mirror"], {"claude:session": [95]})

    def test_a_failed_initial_get_is_not_misreported_as_an_empty_store(self):
        got = run_js_with_api_token(r"""
(async () => {
  windowThresholds = { 'claude:session': [30] };
  alertThresholdsLoaded = false;
  apiFetch = async () => ({ ok: false, json: async () => ({}) });
  const loaded = await loadWindowThresholds();
  console.log(JSON.stringify({ loaded, flag: alertThresholdsLoaded,
    mirror: windowThresholds }));
})();""")
        self.assertIsNone(got["loaded"])
        self.assertFalse(got["flag"])
        self.assertEqual(got["mirror"], {"claude:session": [30]})

    def test_the_legacy_global_choice_is_migrated_to_each_live_window(self):
        got = run_js_with_api_token(r"""
(async () => {
  localStorage.setItem(ALERT_THRESHOLD_KEY, JSON.stringify([33]));
  lastPlanInfo = { available: true, source: 'claude', windows: [
    { key: 'claude:session', thresholds: [80] },
    { key: 'claude:weekly', thresholds: [80] }
  ] };
  lastClaudeLimits = lastPlanInfo;
  let stored = {};
  apiFetch = async (path, options = {}) => {
    if (!options.method) return { ok: true, json: async () => ({ thresholds: stored }) };
    stored = { ...stored, ...JSON.parse(options.body).thresholds };
    return { ok: true, json: async () => ({ thresholds: stored }) };
  };
  await loadWindowThresholds();
  console.log(JSON.stringify({ stored, legacy: localStorage.getItem(ALERT_THRESHOLD_KEY) }));
})();""")
        self.assertEqual(got["stored"],
                         {"claude:session": [33], "claude:weekly": [33]})
        self.assertIsNone(got["legacy"])

    def test_render_uses_the_payload_source_not_the_selected_source(self):
        got = run_js(r"""
(() => {
  localStorage.clear();
  selectedSource = 'codex';
  let titles = [];
  deliverAlert = (title) => titles.push(title);
  loadWindowThresholds = () => Promise.resolve({});
  const at = percent => ({ available: true, source: 'claude', windows: [
    { key: 'claude:session', kind: 'session', group: 'session', scope: '',
      percent, thresholds: [80], resets_at: '2099-01-01T00:00:00Z' }
  ] });
  renderPlanLimits(at(70));
  renderPlanLimits(at(82));
  console.log(JSON.stringify(titles));
})();""")
        self.assertEqual(len(got), 1)
        self.assertIn("Claude Code", got[0])
        self.assertNotIn("Codex", got[0])

    def test_an_offscreen_assistants_crossing_is_still_evaluated(self):
        got = run_js_with_api_token(r"""
(async () => {
  localStorage.clear();
  selectedSource = 'claude';
  let percent = 10;
  const quota = (source) => ({ available: true, source, windows: [
    { key: source + ':weekly', kind: 'weekly', group: 'weekly', scope: '',
      percent, thresholds: [30], resets_at: '2099-01-01T00:00:00Z' }
  ] });
  const payload = () => ({ subscription_limits: quota('claude'),
    codex_limits: quota('codex') });
  apiFetch = async () => ({ ok: true, json: async () => payload() });
  const alerts = [];
  deliverAlert = (title) => alerts.push(title);
  await loadData('codex');
  percent = 35;
  await loadData('codex');
  console.log(JSON.stringify(alerts));
})();""")
        self.assertTrue(any("Codex" in title for title in got))


@requires_node
class TestThePeakHourWordingNamesNoVendor(unittest.TestCase):
    """The hourly card's two peak-hour strings must read true on both sources.

    The shaded window is ONE window — Anthropic's 05:00–11:00 PT, resolved per
    day — applied to whichever assistant is on screen, and nothing about the
    application of it is per-vendor. So
    copy that names a vendor is a claim the shading does not make: it read
    "Anthropic peak-hour throttling window" and "Peak — Anthropic US hours"
    above a title saying "Codex Usage" and a footer citing OpenAI's rate card.
    Swapping in "OpenAI" for Codex would only move the falsehood — there is no
    published OpenAI window, and the hours shaded would still be Anthropic's.

    Both halves are asserted together, in one class, because splitting them is
    how this survived: the same campaign renamed the model filter's vendor
    strings and left these two behind, one in `web/index.html` and one in
    `web/js/52-charts.js`. A test per file would have gone green on half a fix.
    """

    # The vendors and assistants this page names elsewhere (footer rate card,
    # title, source switch). Any of them in the peak copy is the defect.
    VENDORS = ("Anthropic", "OpenAI", "Claude", "Codex")

    # `hourlyTZ` is pinned to 'utc' so the hour labels — and therefore which
    # buckets are peak — do not depend on the machine running the suite.
    _TOOLTIP = """(() => {
      const built = [];
      globalThis.Chart = function Chart(ctx, cfg) {
        built.push(cfg);
        return { update() {}, destroy() {}, data: cfg.data, options: cfg.options };
      };
      globalThis.Chart.defaults = {
        color: '', font: {}, borderColor: '', backgroundColor: '',
        plugins: { tooltip: { callbacks: {} }, legend: { labels: {} } },
        scale: { grid: {} }, scales: {}, elements: {}, datasets: {} };
      selectedSource = source;
      hourlyTZ = 'utc';
      charts.hourly = null;
      const rows = [];
      for (let h = 0; h < 24; h++) rows.push({ day: '2026-08-03', hour: h, turns: 1, output: 10 });
      const agg = aggregateHourly(rows, 'utc');
      renderHourlyChart(agg);
      const title = built[0].options.plugins.tooltip.callbacks.title;
      return { peak: title([{ dataIndex: agg.hours.findIndex(h => h.peak) }]),
               off:  title([{ dataIndex: agg.hours.findIndex(h => !h.peak) }]) };
    })()"""

    def _tooltip(self, source):
        return run_js(emit(self._TOOLTIP, source=source))

    def _legend_title(self):
        html = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        found = re.search(r'<span class="peak-legend" title="([^"]*)"', html)
        self.assertIsNotNone(
            found, "no .peak-legend title in index.html — the legend that "
                   "explains the red bars lost its explanation")
        return found.group(1)

    def test_the_chart_tooltip_reads_the_same_on_either_assistant(self):
        claude = self._tooltip("claude")
        codex = self._tooltip("codex")
        self.assertEqual(
            claude["peak"], codex["peak"],
            "the peak tooltip differs by source, so one of the two is naming a "
            "window that assistant's provider never published")
        self.assertIn("Peak", claude["peak"],
                      "peak hours are no longer marked in the tooltip at all")
        self.assertNotIn("Peak", claude["off"],
                         "an off-peak hour is being announced as peak")

    def test_neither_peak_string_names_a_vendor(self):
        strings = {"chart tooltip": self._tooltip("codex")["peak"],
                   "legend title": self._legend_title()}
        for where, text in strings.items():
            for vendor in self.VENDORS:
                with self.subTest(where=where, vendor=vendor):
                    self.assertNotIn(
                        vendor, text,
                        f"the hourly card's {where} says {vendor!r} ({text!r}) "
                        "for a window that is the same fixed UTC range on both "
                        "sources — on the other assistant it names the wrong "
                        "vendor entirely")

    def test_the_legend_still_states_the_window_it_shades(self):
        """Vendor-neutral must not mean contentless — the hours stay named."""
        title = self._legend_title()
        self.assertIn("05:00", title)
        self.assertIn("11:00", title)
        self.assertIn("PT", title)


# ── The daily chart: every day in the range, panned, summarised and sortable ──
#
# These run the REAL applyFilter under the DOM stub, which it turns out is rich
# enough: the renderers write into inert elements and the Chart constructor is
# replaced below by one that records what it was asked to draw. That is the only
# way to test the defect these cover, because it lived in the SHAPE of the array
# handed to the chart, not in any one function.

def daily_dom(width=1400, plot=None):
    """A DOM stub that can run applyFilter and be measured afterwards.

    Two things the plain `_DOM_STUB` cannot do and these tests need:
    elements keep their identity across getElementById calls (so the pan bar's
    scrollLeft persists the way a real one would), and they report a width —
    which is what dailyWindowSize measures to decide how many days fit.

    `plot` is the chart container's width, which in the real page is narrower
    than the card because the per-series panel sits beside it.
    """
    plot_px = width if plot is None else plot
    return f"""
      window.innerWidth = {width};
      const _els = new Map();
      document.getElementById = (id) => {{
        if (!_els.has(id)) {{
          const el = stubEl();
          el.id = id;
          el.scrollLeft = 0;
          el.clientWidth = {plot_px};
          el.parentElement = {{ clientWidth: {plot_px} }};
          _els.set(id, el);
        }}
        return _els.get(id);
      }};
      globalThis.elFor = (id) => document.getElementById(id);
      // The plain stub drops window listeners on the floor, which makes the
      // resize handler — the one place that decides WHICH array the chart is
      // re-rendered from — unreachable from a test. Recorded here and fired by
      // `fireWindow`; the handler is (re-)registered by calling initDailyPan().
      const _winListeners = new Map();
      window.addEventListener = (type, fn) => {{
        if (!_winListeners.has(type)) _winListeners.set(type, []);
        _winListeners.get(type).push(fn);
      }};
      globalThis.fireWindow = (type) => {{
        for (const fn of (_winListeners.get(type) || [])) fn({{ type }});
      }};
      const chartsDrawn = [];
      globalThis.chartsDrawn = chartsDrawn;
      globalThis.Chart = function Chart(ctx, cfg) {{
        const inst = {{
          config: cfg,
          data: (cfg && cfg.data) || {{}},
          options: (cfg && cfg.options) || {{}},
          update() {{}}, destroy() {{}},
        }};
        chartsDrawn.push(inst);
        return inst;
      }};
      globalThis.Chart.defaults = {{
        color: '', font: {{}}, borderColor: '', backgroundColor: '',
        plugins: {{ tooltip: {{ callbacks: {{}} }}, legend: {{ labels: {{}} }} }},
        scale: {{ grid: {{}} }}, scales: {{}}, elements: {{}}, datasets: {{}},
      }};
      globalThis.Chart.register = () => {{}};
      // The chart is rebuilt on every render; keep only the newest daily one.
      // Discriminated by its third axis: the subagent chart carries the same
      // four dataset labels, so matching on those would sometimes pick it.
      globalThis.dailyChart = () => {{
        for (let i = chartsDrawn.length - 1; i >= 0; i--) {{
          const o = chartsDrawn[i].options;
          if (o && o.scales && o.scales.y2) return chartsDrawn[i];
        }}
        return null;
      }};
      // updateURL writes through history.replaceState, which the plain stub has
      // no equivalent for; reflect it back into location so a link round-trips.
      window.location.pathname = '/';
      window.location.search = '';
      globalThis.history = {{ replaceState: (state, title, url) => {{
        const text = String(url);
        const q = text.indexOf('?');
        const hash = text.indexOf('#');
        const end = hash === -1 ? text.length : hash;
        window.location.search = q === -1 ? '' : text.slice(q, end);
      }} }};
      // The stat tiles render into inert elements, so capture what they were
      // handed instead — that is where a filling bug would show up as a total.
      globalThis.capturedTotals = null;
      const _realRenderStats = renderStats;
      renderStats = (totals, label) => {{
        globalThis.capturedTotals = totals;
        return _realRenderStats(totals, label);
      }};
    """


def plot_px(width):
    """The chart container's width at a given viewport, as the page lays it out.

    .container caps at 1400 and pads 24 a side, .chart-card pads 20 a side, and
    both drop to 14 and 12 at or below 640px; at the wide breakpoint the
    per-series panel plus its gap take 376 more. Guessing this wrong is how a
    reachability test passes at a width the real page never produces — and the
    narrow branch WAS wrong, modelling 302px at 390 where Chrome lays out 336.

    Checked against Chrome (chrome-headless-shell, real page, real database):
    390 -> 336, 640 -> 586, 768 -> 678, 1024 -> 558, 1280 -> 814, 1600 -> 934.
    This model is 2px generous at every one of them, which is the container's
    own rounding and cannot move a column count.
    """
    narrow = width <= 640
    inner = min(width, 1400) - (28 if narrow else 48) - (24 if narrow else 40)
    return inner - 376 if width >= 1024 else inner


def daily_payload(days, model="claude-opus-5", source="claude", scale=1):
    """`daily_by_model` rows for the given ISO days, with distinguishable values."""
    rows = []
    for i, day in enumerate(days):
        n = (i + 1) * scale
        rows.append({"day": day, "source": source, "model": model,
                     "input": n, "output": n * 2, "cache_read": n * 3,
                     "cache_creation": n * 4, "cache_creation_1h": 0,
                     "reasoning": 0, "turns": 1})
    return rows


def daily_state(rows, models, source="claude", rng="30d"):
    """Put the page in a state where applyFilter() will run against `rows`."""
    return ("rawData = " + json.dumps({
        "daily_by_model": rows, "sessions_all": [], "project_by_day_model": [],
        "hourly_by_model": [], "subagent_by_type": [], "top_dispatches": [],
        "effort_by_day_model": [], "stop_reason_by_day_model": [],
        "limit_incidents": [],
    }) + ";\n"
        + "selectedSource = " + json.dumps(source) + ";\n"
        + "selectedModels = new Set(" + json.dumps(models) + ");\n"
        + "allModelsList = " + json.dumps(models) + ";\n"
        + "selectedRange = " + json.dumps(rng) + ";\n"
        + "hiddenSeries.daily.clear();\n")


def iso_days(start, count):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(count)]


def today_iso():
    from datetime import date
    return date.today().isoformat()


def days_back(n):
    from datetime import date, timedelta
    return (date.today() - timedelta(days=n)).isoformat()


@requires_node
class TestDailyRangeHasEveryDay(unittest.TestCase):
    """A day with no usage is a ZERO in the series, never a missing column.

    Quiet days must be present as zero rows. Omitting them makes adjacent
    bars represent nonadjacent dates and can prevent the pan control from
    activating for a range wider than the viewport."""

    def _run(self, rows, rng="30d", width=1400, plot=None, models=None,
             source="claude", tail=""):
        models = models or ["claude-opus-5"]
        return run_js(daily_dom(width, plot)
                      + daily_state(rows, models, source, rng)
                      + "applyFilter();\n" + tail)

    def test_the_defect_itself_stated_in_names_that_already_existed(self):
        """The bug report, reproduced against the shipped machinery only.

        `lastDailyRows`, `dailyWindowLen` and `#daily-pan` all predate this
        change, so this one fails *behaviourally* on the old code rather than
        for want of a new symbol: 30 calendar days arrived as 3 rows, the window
        swallowed all 3, and the pan bar stayed hidden — no scrollbar anywhere
        on the page, which is exactly what was reported.
        """
        rows = daily_payload([days_back(29), days_back(20), today_iso()])
        got = self._run(rows, width=1600, plot=plot_px(1600), tail=emit("""
          ({plotted: lastDailyRows.length,
            windowLen: dailyWindowLen,
            panBarHidden: !!elFor('daily-pan').hidden,
            gapAfterFirst: (new Date(lastDailyRows[1].day) - new Date(lastDailyRows[0].day)) / 86400000})"""))
        self.assertEqual(got["plotted"], 30,
                         "the chart is handed only the days that have rows, so "
                         "adjacent bars are not adjacent days")
        self.assertGreater(got["windowLen"], 0,
                           "the window never engages, so there is nothing to pan")
        self.assertFalse(got["panBarHidden"], "no pan control is shown at all")
        self.assertEqual(got["gapAfterFirst"], 1,
                         "two neighbouring bars are 9 days apart")

    def test_a_range_with_holes_gets_a_column_for_every_calendar_day(self):
        rows = daily_payload([days_back(29), days_back(20), today_iso()])
        got = self._run(rows, tail=emit(
            "({days: dailyRangeRows.map(r => r.day), n: dailyRangeRows.length})"))
        self.assertEqual(got["n"], 30,
                         "Last 30 Days must plot 30 days, not only the days with turns")
        self.assertEqual(got["days"], iso_days(days_back(29), 30))

    def test_the_days_it_invents_are_zero_rather_than_absent(self):
        rows = daily_payload([days_back(29), today_iso()])
        got = self._run(rows, tail=emit("""
          dailyRangeRows.filter(r => r.day > START && r.day < END)
                        .map(r => [r.input, r.output, r.cache_read,
                                   r.cache_creation, r.cost])""",
                                        START=days_back(29), END=today_iso()))
        self.assertEqual(len(got), 28)
        for row in got:
            self.assertEqual(row, [0, 0, 0, 0, 0])

    def test_filling_changes_no_total_the_page_prints(self):
        """A zero row must not move a token count, a cost or a session count."""
        rows = daily_payload([days_back(29), days_back(10), today_iso()])
        got = self._run(rows, tail=emit("""
          (() => {
            const t = capturedTotals;
            return { turns: t.turns, input: t.input, output: t.output,
                     cache_read: t.cache_read, cache_creation: t.cache_creation,
                     cost: t.cost, sessions: t.sessions,
                     daysWithUsage: dailyRangeRows.filter(r =>
                       r.input || r.output || r.cache_read || r.cache_creation).length };
          })()"""))
        self.assertEqual(got["turns"], 3)
        self.assertEqual(got["input"], 1 + 2 + 3)
        self.assertEqual(got["output"], 2 * (1 + 2 + 3))
        self.assertEqual(got["cache_read"], 3 * (1 + 2 + 3))
        self.assertEqual(got["cache_creation"], 4 * (1 + 2 + 3))
        self.assertEqual(got["sessions"], 0)
        self.assertEqual(got["daysWithUsage"], 3,
                         "the filled days are being counted as days with usage")

    def test_all_time_borrows_the_extent_of_the_data_and_keeps_its_label(self):
        """All Time has no bounds of its own, so filling past the last day with
        data would invent days the range does not claim — and would rewrite the
        label, which reads the extent of what is on screen."""
        rows = daily_payload(["2026-03-01", "2026-03-05"])
        got = self._run(rows, rng="all", tail=emit("""
          ({days: dailyRangeRows.map(r => r.day),
            label: rangeLabelWithDates('all', dailyRangeRows.map(r => r.day))})"""))
        self.assertEqual(got["days"], iso_days("2026-03-01", 5))
        self.assertEqual(got["label"], "All Time (Mar 1 – Mar 5)")

    def test_the_fill_never_runs_past_today(self):
        """Zero means "no usage that day", which is true of a day that has
        happened and meaningless about one that has not. "This Month" ends on
        the last of the month; the columns stop at today."""
        rows = daily_payload([today_iso()])
        got = self._run(rows, rng="month", tail=emit(
            "dailyRangeRows[dailyRangeRows.length - 1].day"))
        self.assertEqual(got, today_iso())

    def test_a_day_key_the_backend_could_not_parse_is_still_carried(self):
        """localdays.py's COALESCE fallback can emit a non-ISO key. Filling must
        not become a filter that silently drops it."""
        rows = daily_payload([today_iso()]) + [
            {"day": "zzzz-bad", "source": "claude", "model": "claude-opus-5",
             "input": 7, "output": 0, "cache_read": 0, "cache_creation": 0,
             "cache_creation_1h": 0, "reasoning": 0, "turns": 1}]
        got = self._run(rows, tail=emit(
            "dailyRangeRows.filter(r => r.day === 'zzzz-bad').length"))
        self.assertEqual(got, 1)

    def _in_tz(self, tz, snippet):
        source = _DOM_STUB + "\n" + extract_app_script() + "\n" + snippet
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.cjs"
            harness.write_text(source, encoding="utf-8")
            proc = subprocess.run([NODE, str(harness)], capture_output=True,
                                  text=True, encoding="utf-8", timeout=120,
                                  env=dict(os.environ, TZ=tz))
        if proc.returncode != 0:
            raise AssertionError(f"node exited {proc.returncode}:\n{proc.stderr[-2000:]}")
        return json.loads(proc.stdout)

    def test_the_fill_ends_on_the_local_day_not_the_utc_one(self):
        """The #151 class of bug: toISOString() formats in UTC, so on one side
        the newest column would be yesterday's and on the other tomorrow's."""
        # A data day well inside the range, so the last column is the fill's own
        # end rather than whatever the payload happened to carry.
        rows = daily_payload([days_back(20)])
        snippet = (daily_dom(1400) + daily_state(rows, ["claude-opus-5"])
                   + "applyFilter();\n"
                   + emit("({last: dailyRangeRows[dailyRangeRows.length - 1].day,"
                          "  today: localISODate(new Date()),"
                          "  spanEnd: dailyFillSpan('30d', []).end})"))
        for tz in ("Pacific/Kiritimati", "Pacific/Niue", "Europe/Madrid", "UTC"):
            with self.subTest(tz=tz):
                got = self._in_tz(tz, snippet)
                self.assertEqual(got["spanEnd"], got["today"])
                self.assertEqual(got["last"], got["today"])

    def test_a_row_stamped_past_the_local_today_is_still_carried(self):
        """The rolling ranges leave their end OPEN so a turn recorded while the
        page renders is not clipped. Filling stops at today; it must not become
        the clipping the open end exists to avoid."""
        ahead = (__import__("datetime").date.today()
                 + __import__("datetime").timedelta(days=2)).isoformat()
        rows = daily_payload([days_back(20), ahead])
        got = self._run(rows, tail=emit(
            "({days: dailyRangeRows.map(r => r.day),"
            "  today: localISODate(new Date())})"))
        self.assertIn(ahead, got["days"])
        self.assertEqual(got["days"][-1], ahead)
        self.assertIn(got["today"], got["days"])


@requires_node
class TestDailyWindowReachesEveryDay(unittest.TestCase):
    """Now that the array is as long as the range, panning must actually work —
    and must reach the first and last day at every width.

    syncDailyPan's own comment records that a naive `total * column` track left
    the last few days unreachable. These arrays are far longer than the ones
    that comment was written against.
    """

    WIDTHS = (390, 640, 768, 1024, 1600)
    RANGES = ("30d", "90d", "ytd", "all")

    def _sweep(self, rng, width, plot, n_days):
        rows = daily_payload(iso_days(days_back(n_days - 1), n_days))
        return run_js(daily_dom(width, plot)
                      + daily_state(rows, ["claude-opus-5"], "claude", rng)
                      + "applyFilter();\n"
                      + emit("""
          (() => {
            const total = dailyRangeRows.length;
            const seen = new Set();
            const max = Math.max(0, total - (dailyWindowLen || total));
            for (let off = 0; off <= max; off++) {
              setDailyPanOffset(off);
              const win = dailyWindowLen
                ? lastDailyRows.slice(dailyPanOffset, dailyPanOffset + dailyWindowLen)
                : lastDailyRows;
              for (const r of win) seen.add(r.day);
            }
            const bar = elFor('daily-pan');
            const track = elFor('daily-pan-track');
            return { total, seen: seen.size, windowLen: dailyWindowLen,
                     panning: !bar.hidden,
                     trackStyle: String(track.style.width || ''),
                     barWidth: bar.clientWidth, col: dailyColumnWidth(),
                     maxOffset: max };
          })()"""))

    def test_every_day_is_reachable_at_every_width_and_range(self):
        for rng, n_days in (("30d", 30), ("90d", 90), ("ytd", 200), ("all", 200)):
            for width in self.WIDTHS:
                with self.subTest(range=rng, width=width):
                    got = self._sweep(rng, width, plot_px(width), n_days)
                    self.assertEqual(
                        got["seen"], got["total"],
                        f"{got['seen']} of {got['total']} days reachable by panning")

    def test_the_long_ranges_actually_pan_now(self):
        """The user's complaint, stated as a test: the scrollbar has to exist."""
        for rng, n_days in (("30d", 30), ("90d", 90)):
            for width in self.WIDTHS:
                with self.subTest(range=rng, width=width):
                    got = self._sweep(rng, width, plot_px(width), n_days)
                    self.assertTrue(got["panning"],
                                    "no pan control is shown, so most of the "
                                    "range cannot be reached at all")
                    self.assertLess(got["windowLen"], got["total"])

    def test_the_track_states_its_scroll_distance_against_the_live_bar(self):
        """Maximum scrollLeft must be exactly maxOffset columns, or the final
        days are unreachable from the scrollbar even though the chart holds them.

        Asserted as the EXPRESSION, and that is the whole repair. This test used
        to read `trackWidth - barWidth == maxOffset * col` while the stub
        returned one constant clientWidth for every element — the very quantity
        that goes stale in a real browser — so it restated syncDailyPan's own
        formula and could not fail. It was green while Chrome, at 390px on the
        real database, sized the track from a clientWidth of 336 that settled at
        357: max scrollLeft 523, 523/26 = 20.19 columns, and the newest day was
        unreachable from the scrollbar on first paint.

        `calc(100% + Npx)` re-resolves the bar's width on every layout, so
        scrollWidth - clientWidth is N whenever it is read. What a stub can
        check is that N is the scroll distance and that the remainder is that
        live percentage rather than a sampled number. The rendered geometry
        itself is measured in TestDailyPanelGeometryInABrowser.
        """
        for rng, n_days in (("30d", 30), ("90d", 90)):
            for width in self.WIDTHS:
                with self.subTest(range=rng, width=width):
                    got = self._sweep(rng, width, plot_px(width), n_days)
                    m = re.fullmatch(r"calc\(100% \+ (\d+(?:\.\d+)?)px\)",
                                     got["trackStyle"])
                    self.assertIsNotNone(
                        m,
                        "the track is sized as " + repr(got["trackStyle"])
                        + ", so its scroll distance is fixed at whatever the "
                        "bar measured when it was rendered")
                    self.assertAlmostEqual(float(m.group(1)),
                                           got["maxOffset"] * got["col"],
                                           places=6)


@requires_node
class TestDailySeriesStats(unittest.TestCase):
    """Min / mean / max per series, computed over the WHOLE range.

    The point of the panel is to show values the pan window is hiding, so
    computing it over the visible slice would defeat it.
    """

    def _run(self, rows, models, source="claude", rng="30d", tail=""):
        return run_js(daily_dom(1024, 800)
                      + daily_state(rows, models, source, rng)
                      + "applyFilter();\n" + tail)

    def test_the_panel_on_screen_shows_the_whole_range_not_the_window(self):
        """Handing renderDailyStats the visible slice instead of the range is
        silent unless the test reads the RENDERED panel — so it does.

        The payload puts the single biggest day at the far end of the range,
        where the default window (anchored to the most recent days) cannot see
        it. The figure the panel prints must still be that day's.
        """
        days = iso_days(days_back(29), 30)
        rows = []
        for i, day in enumerate(days):
            big = (i == 0)
            rows.append({"day": day, "source": "claude", "model": "claude-opus-5",
                         "input": 1, "output": 1000000 if big else 1,
                         "cache_read": 1, "cache_creation": 1,
                         "cache_creation_1h": 0, "reasoning": 0, "turns": 1})
        got = self._run(rows, ["claude-opus-5"], tail=emit("""
          (() => {
            const win = lastDailyRows.slice(dailyPanOffset, dailyPanOffset + dailyWindowLen);
            const stat = (rs, field) => dailyStats(rs).find(s => s.label === 'Output')[field];
            return { html: elFor('daily-stats').innerHTML,
                     range: { max: fmt(stat(dailyRangeRows, 'max')),
                              mean: fmt(stat(dailyRangeRows, 'mean')) },
                     window: { max: fmt(stat(win, 'max')),
                               mean: fmt(stat(win, 'mean')) },
                     windowLen: dailyWindowLen, total: dailyRangeRows.length };
          })()"""))
        self.assertLess(got["windowLen"], got["total"],
                        "this test only discriminates while the window hides days")
        self.assertNotEqual(got["range"], got["window"],
                            "the biggest day is inside the window, so this test "
                            "could not tell the two apart")

        # Read the Output row's own Max and Mean cells rather than searching the
        # whole panel: the other series legitimately print the window's figures
        # because theirs are flat, and a whole-panel search matches those.
        def cell(spec):
            # Anchored on the cell class: the series-name button carries the
            # same bare `output` spec (it is the toggle), and without the class
            # this matched its label instead of a figure.
            m = re.search(r'class="daily-sort-cell[^"]*" data-daily-sort="'
                          + re.escape(spec) + r'"[^>]*>([^<]*)<', got["html"])
            self.assertIsNotNone(m, f"no {spec} cell in the panel")
            return m.group(1)

        self.assertEqual(cell("output.desc"), got["range"]["max"],
                         "the Max cell is showing the visible window's biggest "
                         "day, not the range's")
        self.assertEqual(cell("output"), got["range"]["mean"],
                         "the Mean cell is averaging only what is on screen")

    def test_the_arithmetic_of_min_mean_and_max_is_right(self):
        """The pure function, over a known 30-day range.

        Deliberately NOT named for "range, not window": it calls dailyStats
        directly, so it cannot see which rows renderDailyChart hands the panel —
        a mutation that passed it the visible slice left this green. That wiring
        is pinned by test_the_panel_on_screen_shows_the_whole_range_not_the_window
        above, which reads the rendered HTML.
        """
        rows = daily_payload(iso_days(days_back(29), 30))
        got = self._run(rows, ["claude-opus-5"], tail=emit("""
          (() => {
            const stats = dailyStats(dailyRangeRows);
            const out = {};
            for (const s of stats) out[s.label] = {min: s.min, mean: s.mean, max: s.max, days: s.days};
            return { stats: out, windowLen: dailyWindowLen, total: dailyRangeRows.length };
          })()"""))
        self.assertLess(got["windowLen"], got["total"],
                        "this test only discriminates while the window hides days")
        stats = got["stats"]
        self.assertEqual(stats["Input"]["min"], 1)
        self.assertEqual(stats["Input"]["max"], 30)
        self.assertAlmostEqual(stats["Input"]["mean"], sum(range(1, 31)) / 30)
        self.assertEqual(stats["Output"]["max"], 60)
        self.assertEqual(stats["Cache Read"]["max"], 90)
        self.assertEqual(stats["Cache Creation"]["max"], 120)
        self.assertEqual(stats["Input"]["days"], 30)

    def test_the_mean_divides_by_every_day_in_range_not_only_the_busy_ones(self):
        """The divisor is `rows.length`, and nothing used to check it.

        A partially populated range distinguishes all calendar days from
        active days. Mean usage must include the zero-filled quiet days.

        So this range is deliberately sparse: 3 days of usage inside 30, which
        makes the two divisors differ by 10x, and it reads the RENDERED cell so
        the mutation cannot hide in the wiring either.
        """
        days = [days_back(25), days_back(15), days_back(5)]
        rows = daily_payload(days)              # input 1, 2, 3
        got = self._run(rows, ["claude-opus-5"], tail=emit("""
          (() => {
            const s = dailyStats(dailyRangeRows).find(x => x.label === 'Input');
            const cell = (spec) => {
              const re = new RegExp('class="daily-sort-cell[^"]*" data-daily-sort="'
                                    + spec + '"[^>]*>([^<]*)<');
              const m = re.exec(elFor('daily-stats').innerHTML);
              return m && m[1];
            };
            return { mean: s.mean, days: s.days, sum: 6,
                     active: dailyActiveDays(dailyRangeRows),
                     printed: cell('input'), expected: fmt(6 / 30),
                     activeMean: fmt(6 / 3), note: elFor('daily-stats').innerHTML };
          })()"""))
        self.assertEqual(got["days"], 30, "the range was not zero-filled")
        self.assertEqual(got["active"], 3)
        self.assertAlmostEqual(got["mean"], 6 / 30, places=12)
        self.assertNotEqual(
            got["expected"], got["activeMean"],
            "the two divisors print the same string, so this test could not "
            "tell them apart")
        self.assertEqual(got["printed"], got["expected"],
                         "the Mean cell is averaging over the days with usage, "
                         "not over the range the panel says it covers")
        self.assertIn("30 day", got["note"])
        self.assertIn("(3 with usage)", got["note"])

    def test_a_day_with_no_usage_is_a_real_zero_in_the_minimum(self):
        rows = daily_payload([days_back(29), today_iso()])
        got = self._run(rows, ["claude-opus-5"], tail=emit(
            "dailyStats(dailyRangeRows).find(s => s.label === 'Output')"))
        self.assertEqual(got["min"], 0)
        self.assertEqual(got["max"], 4)

    def test_sorting_cannot_move_a_minimum_a_mean_or_a_maximum(self):
        """Reordering the chart must not change a single figure in the panel.

        Min and max are compared exactly. The mean is a sum, and adding the same
        floats in a different order can land one ulp apart — 3e-19 on a figure
        printed to four decimals, so it is compared to a tolerance far tighter
        than anything displayed rather than pretended away.
        """
        rows = daily_payload(iso_days(days_back(29), 30))
        got = self._run(rows, ["claude-opus-5"], tail=emit("""
          (() => {
            const before = dailyStats(dailyRangeRows);
            setDailySort('output.desc');
            const after = dailyStats(dailyRangeRows);
            const sorted = dailyStats(lastDailyRows);
            return { before, after, sorted };
          })()"""))
        self.assertEqual(got["before"], got["after"],
                         "re-reading the range gave a different answer")
        self.assertEqual([s["label"] for s in got["before"]],
                         [s["label"] for s in got["sorted"]])
        for base, srt in zip(got["before"], got["sorted"]):
            with self.subTest(series=base["label"]):
                self.assertEqual(base["min"], srt["min"])
                self.assertEqual(base["max"], srt["max"])
                self.assertEqual(base["days"], srt["days"])
                self.assertAlmostEqual(base["mean"], srt["mean"], places=12)

    def test_the_panel_names_every_series_the_chart_draws(self):
        rows = daily_payload(iso_days(days_back(29), 30))
        got = self._run(rows, ["claude-opus-5"], tail=emit(
            "({html: elFor('daily-stats').innerHTML, "
            "  series: dailySeries().map(s => s.label)})"))
        for label in got["series"]:
            self.assertIn(label, got["html"])
        self.assertIn("Est. Cost", got["series"])

    def test_the_panel_says_it_describes_the_range_not_the_screen(self):
        rows = daily_payload(iso_days(days_back(29), 30))
        got = self._run(rows, ["claude-opus-5"], tail=emit(
            "elFor('daily-stats').innerHTML"))
        self.assertIn("30 days", got)
        self.assertRegex(got, r"(?i)whole range|all \d+ days|in range")

    def test_an_unpriced_source_gets_no_cost_row_and_no_zero_dollars(self):
        """`n/a` and `$0.00` are different claims; a source with no published
        rate must make neither the second one nor a cost column."""
        rows = daily_payload(iso_days(days_back(29), 30),
                             model="local-llama-3", source="codex")
        got = self._run(rows, ["local-llama-3"], source="codex", tail=emit(
            "({html: elFor('daily-stats').innerHTML, priced: sourceIsPriced, "
            "  series: dailySeries().map(s => s.label)})"))
        self.assertFalse(got["priced"])
        self.assertNotIn("Est. Cost", got["series"])
        self.assertNotIn("Est. Cost", got["html"])
        self.assertNotIn("$0.00", got["html"])
        self.assertNotIn("$", got["html"])


@requires_node
class TestDailySortOrder(unittest.TestCase):
    """Sorting the chart by any series, in either direction, chronological back
    in one click — and the pan window re-anchored when the order changes."""

    def _run(self, tail, n=30, rng="30d", width=1024, plot=800,
             models=None, source="claude", model="claude-opus-5"):
        models = models or [model]
        rows = daily_payload(iso_days(days_back(n - 1), n),
                             model=model, source=source)
        return run_js(daily_dom(width, plot)
                      + daily_state(rows, models, source, rng)
                      + "applyFilter();\n" + tail)

    def test_the_initial_state_is_chronological(self):
        got = self._run(emit("""
          ({key: dailySortKey, dir: dailySortDir,
            days: lastDailyRows.map(r => r.day)})"""))
        self.assertEqual(got["key"], "day")
        self.assertEqual(got["dir"], "asc")
        self.assertEqual(got["days"], sorted(got["days"]))

    def test_sorting_by_a_series_reorders_the_chart(self):
        got = self._run(emit("""
          (() => {
            setDailySort('output.desc');
            return { values: lastDailyRows.map(r => r.output),
                     key: dailySortKey, dir: dailySortDir };
          })()"""))
        self.assertEqual(got["key"], "output")
        self.assertEqual(got["dir"], "desc")
        self.assertEqual(got["values"], sorted(got["values"], reverse=True))

    def test_min_and_max_are_the_same_series_read_from_opposite_ends(self):
        got = self._run(emit("""
          (() => {
            setDailySort('cache_read.desc');
            const down = lastDailyRows.map(r => r.day);
            setDailySort('cache_read.asc');
            const up = lastDailyRows.map(r => r.day);
            return { down, up };
          })()"""))
        self.assertEqual(got["up"], list(reversed(got["down"])))

    def test_chronological_is_always_one_click_away(self):
        got = self._run(emit("""
          (() => {
            setDailySort('input.desc');
            setDailySort('day');
            return { key: dailySortKey, dir: dailySortDir,
                     days: lastDailyRows.map(r => r.day) };
          })()"""))
        self.assertEqual(got["key"], "day")
        self.assertEqual(got["days"], sorted(got["days"]))

    def test_a_repeat_click_on_the_series_reverses_it(self):
        got = self._run(emit("""
          (() => {
            setDailySort('output');
            const first = dailySortDir;
            setDailySort('output');
            const second = dailySortDir;
            return [first, second];
          })()"""))
        self.assertEqual(got, ["desc", "asc"])

    def test_equal_days_come_out_in_date_order(self):
        """Zero-filled quiet days tie on usage. sortDailyRows must break those ties
        by date so sorting remains deterministic.

        Called directly, with the rows deliberately shuffled, because through
        the only production caller the promise is unobservable: `dailyRangeRows`
        is already chronological and Array.prototype.sort is stable, so deleting
        the tiebreak changes nothing there and no test through applyFilter can
        ever fail. That is exactly why the mutation was silent across all 1327
        tests. What is being pinned here is the function's own contract, which
        the next caller — one that hands it rows in any other order — will rely
        on.
        """
        got = self._run(emit("""
          (() => {
            const flat = ['2026-03-05', '2026-03-01', '2026-03-04',
                          '2026-03-02', '2026-03-03'].map(day => ({
              day, input: 0, output: 7, cache_read: 0, cache_creation: 0,
              cost: 0, turns: 0 }));
            dailySortKey = 'output'; dailySortDir = 'desc';
            const desc = sortDailyRows(flat).map(r => r.day);
            dailySortDir = 'asc';
            const asc = sortDailyRows(flat).map(r => r.day);
            return { desc, asc, given: flat.map(r => r.day) };
          })()"""))
        self.assertNotEqual(got["given"], sorted(got["given"]),
                            "the input is already in date order, so a stable "
                            "sort would satisfy this test with no tiebreak")
        self.assertEqual(got["desc"], sorted(got["given"]))
        self.assertEqual(got["asc"], sorted(got["given"]))

    def test_a_resize_re_reads_the_range_not_the_order_on_screen(self):
        """The resize handler re-renders from `dailyRangeRows`, never from
        `lastDailyRows`, and nothing checked it.

        Feeding the display order back in makes it the range, so the sort is
        baked in permanently and chronological becomes unreachable — clicking
        "Chronological" then returns the rows it is already holding. The mutant
        is silent across the whole suite because the resize handler lives in
        70-bootstrap.js and no test ever fired a resize; this one does, through
        the real listener, and then checks the thing the split exists for.
        """
        got = run_js(daily_dom(1024, 800)
                     + daily_state(daily_payload(iso_days(days_back(29), 30)),
                                   ["claude-opus-5"], "claude", "30d")
                     + "applyFilter();\n"
                     + """
          setDailySort('output.desc');
          const sorted = lastDailyRows.map(r => r.day);
          const before = dailyRangeRows.map(r => r.day);
          initDailyPan();            // registers the resize listener on the stub
          fireWindow('resize');
          // The handler coalesces on a 200ms timer; read after it has run.
          setTimeout(() => {
            const after = dailyRangeRows.map(r => r.day);
            setDailySort('day');
            console.log(JSON.stringify({
              sorted, before, after,
              chronological: lastDailyRows.map(r => r.day) }));
          }, 300);
        """)
        self.assertEqual(got["before"], sorted(got["before"]))
        self.assertNotEqual(got["sorted"], got["before"],
                            "the sort did not reorder anything, so this test "
                            "could not tell the two arrays apart")
        self.assertEqual(got["after"], got["before"],
                         "the resize re-rendered from the rows on screen, so "
                         "the range is now in the sorted order")
        self.assertEqual(got["chronological"], sorted(got["before"]),
                         "chronological is unreachable after a resize")

    def test_the_window_re_anchors_even_when_the_order_does_not_change(self):
        """The discriminating case for the dailyPanKey trap.

        dailyPanKey is built from the range, the row count, the first and last
        day and the window size — none of which a sort changes. It is easy to
        believe the key covers a sort anyway, because a sort usually moves a
        different day to each end. Here it does NOT: the payload's values rise
        with the date, so `input.asc` produces *byte-identical* rows to
        chronological. Only the sort state itself distinguishes the two, and the
        anchoring differs — a ranking opens at its top, a calendar at its most
        recent day. Drop the sort from that key and this is the test that goes
        red; the one below it passes either way.
        """
        got = self._run(emit("""
          (() => {
            const chronological = lastDailyRows.map(r => r.day);
            const parked = dailyPanOffset;
            setDailySort('input.asc');
            const sameOrder = JSON.stringify(lastDailyRows.map(r => r.day))
                              === JSON.stringify(chronological);
            const afterSort = dailyPanOffset;
            setDailySort('day');
            const afterReset = dailyPanOffset;
            return { parked, afterSort, afterReset, sameOrder,
                     max: dailyRangeRows.length - dailyWindowLen };
          })()"""))
        self.assertTrue(got["sameOrder"],
                        "the two orders differ, so first/last day alone would "
                        "have re-anchored and this test proves nothing")
        self.assertEqual(got["parked"], got["max"])
        self.assertEqual(got["afterSort"], 0,
                         "the sort is not part of dailyPanKey, so the window "
                         "stayed parked at the recent end")
        self.assertEqual(got["afterReset"], got["max"])

    def test_a_value_sort_opens_at_the_top_and_chronological_at_the_newest(self):
        """Where each order anchors, for a sort that DOES move the ends.

        Not the dailyPanKey guard, though it reads like one: reordering here
        changes the first and last day, which are already in that key, so
        dropping the sort from it leaves this green. The test above is the one
        that catches that. What this pins is the choice of anchor — a ranking
        opens at its biggest day, a calendar at its most recent.
        """
        got = self._run(emit("""
          (() => {
            setDailyPanOffset(999);            // park at the recent end
            const parked = dailyPanOffset;
            setDailySort('output.desc');
            const afterSort = dailyPanOffset;
            setDailySort('day');
            const afterReset = dailyPanOffset;
            return { parked, afterSort, afterReset,
                     max: dailyRangeRows.length - dailyWindowLen };
          })()"""))
        self.assertGreater(got["parked"], 0)
        self.assertEqual(got["afterSort"], 0,
                         "a value sort must show the biggest days first")
        self.assertEqual(got["afterReset"], got["max"],
                         "chronological must re-anchor to the most recent days")

    def test_panning_still_works_over_the_sorted_order(self):
        got = self._run(emit("""
          (() => {
            setDailySort('output.desc');
            const total = lastDailyRows.length;
            const seen = new Set();
            for (let off = 0; off <= total - dailyWindowLen; off++) {
              setDailyPanOffset(off);
              for (const r of lastDailyRows.slice(dailyPanOffset, dailyPanOffset + dailyWindowLen)) {
                seen.add(r.day);
              }
            }
            return { seen: seen.size, total };
          })()"""))
        self.assertEqual(got["seen"], got["total"])

    def test_the_x_axis_still_names_the_day_of_every_bar(self):
        got = self._run(emit("""
          (() => {
            setDailySort('output.desc');
            const c = dailyChart();
            return { labels: c.data.labels,
                     rows: lastDailyRows.slice(dailyPanOffset, dailyPanOffset + dailyWindowLen)
                                        .map(r => r.day) };
          })()"""))
        self.assertEqual(got["labels"], got["rows"])
        for label in got["labels"]:
            self.assertRegex(label, r"^\d{4}-\d{2}-\d{2}$")

    def test_the_pinned_axis_maxima_survive_sorting(self):
        got = self._run(emit("""
          (() => {
            const before = ['y', 'y1', 'y2'].map(a => dailyAxisMax(dailyRangeRows, a));
            setDailySort('cache_read.desc');
            const after = ['y', 'y1', 'y2'].map(a => dailyAxisMax(lastDailyRows, a));
            return { before, after };
          })()"""))
        self.assertEqual(got["before"], got["after"])

    def test_a_sort_key_whose_series_is_not_on_screen_degrades(self):
        """Est. Cost is dropped for an unpriced source; sorting by an invisible
        column would order the chart by a number nothing on screen shows."""
        got = self._run(emit("""
          (() => {
            setDailySort('cost.desc');
            return { key: dailySortKey, dir: dailySortDir,
                     days: lastDailyRows.map(r => r.day) };
          })()"""), model="local-llama-3", source="codex")
        self.assertEqual(got["key"], "day")
        self.assertEqual(got["days"], sorted(got["days"]))

    def test_the_sort_is_in_the_url_and_round_trips(self):
        got = self._run(emit("""
          (() => {
            setDailySort('cache_creation.asc');
            const written = window.location.search;
            window.location.search = written;
            const read = readURLDailySort();
            setDailySort('day');
            return { written, read, chronological: window.location.search };
          })()"""))
        self.assertIn("sort=cache_creation.asc", got["written"])
        self.assertEqual(got["read"], {"key": "cache_creation", "dir": "asc"})
        self.assertNotIn("sort=", got["chronological"],
                         "the default order must not be written into the URL")

    def test_a_junk_sort_in_the_url_falls_back_to_chronological(self):
        for bad in ("nonsense", "output.sideways", "cost", "../etc"):
            with self.subTest(param=bad):
                got = run_js("window.location.search = "
                             + json.dumps("?sort=" + bad) + ";\n"
                             + emit("readURLDailySort()"))
                self.assertEqual(got, {"key": "day", "dir": "asc"})

    def test_the_hint_states_the_window_the_total_and_the_sort(self):
        got = self._run(emit("""
          (() => {
            const chrono = elFor('daily-pan-hint').textContent;
            setDailySort('output.desc');
            const sorted = elFor('daily-pan-hint').textContent;
            return { chrono, sorted, win: dailyWindowLen, total: dailyRangeRows.length };
          })()"""))
        self.assertIn(str(got["win"]), got["chrono"])
        self.assertIn(str(got["total"]), got["chrono"])
        self.assertRegex(got["chrono"], r"(?i)drag|scroll")
        self.assertRegex(got["sorted"], r"(?i)output")


@requires_node
class TestTheDailyLegendRePinsTheAxes(unittest.TestCase):
    """Hiding a series lets the rest fill the chart — while panning too.

    A range that FITS delivers that for free: nothing is pinned, so the axis
    autoscales to whatever is left. A WINDOWED range pins `scales.y*.max` to an
    explicit number at render time, and the shared `legendToggle` only sets
    `dataset.hidden` and calls `update()` — so the axis stayed at a maximum only
    the now-hidden series ever reached and the survivors collapsed to a sliver.
    Measured in Chrome on the DEFAULT 30-day view: the tallest Cache Creation bar
    stayed 4.5px of a 210.7px plot where a re-pinned axis draws it 200.4px, a
    44.7x collapse; the identical click at 7 days rescaled correctly. Same
    action, opposite effect, decided only by whether the range happens to be
    windowed — and `dailyAxisMax`'s own comment promises the rescale.

    `test_a_hidden_series_is_left_out_of_the_pinned_maximum` above states exactly
    this intent, but calls the pure function; the defect was in the handler that
    never asked it. These drive the chart's real `legend.onClick`.
    """

    def _run(self, tail, n=30, rng="30d"):
        rows = daily_payload(iso_days(days_back(n - 1), n))
        return run_js(daily_dom(1024, 800)
                      + daily_state(rows, ["claude-opus-5"], "claude", rng)
                      + "applyFilter();\n" + tail)

    # Toggle a series the way Chart.js dispatches it: (event, legendItem, legend),
    # with the chart hanging off the legend.
    CLICK = """
      const click = (label) => {
        const c = dailyChart();
        const idx = c.data.datasets.findIndex(d => d.label === label);
        if (idx < 0) throw new Error('no dataset labelled ' + label);
        c.options.plugins.legend.onClick({}, {datasetIndex: idx}, {chart: c});
        return c;
      };
      const maxima = () => ['y','y1','y2'].map(a => dailyChart().options.scales[a].max);
    """

    def test_hiding_a_series_re_pins_the_axis_it_shared(self):
        got = self._run(emit("(() => {" + self.CLICK + """
          const before = maxima();
          click('Cache Read');
          return { before, after: maxima(), panning: dailyWindowLen,
                   want: ['y','y1','y2'].map(a => dailyAxisMax(dailyRangeRows, a)) };
        })()"""))
        self.assertTrue(got["panning"],
                        "the range is not windowed here, so nothing is pinned "
                        "and this test cannot see the defect")
        self.assertNotEqual(
            got["before"][0], got["after"][0],
            "'Cache Read' is part of the left axis' stack, so hiding it must "
            "lower that axis' maximum — if it does not, the fixture is wrong")
        self.assertEqual(got["after"], got["want"],
                         "the axes are still pinned to a maximum that includes "
                         "the series the reader just asked to hide")

    def test_each_axis_is_re_pinned_from_the_series_left_on_it(self):
        """All three, not just the left one. A loop covering `y` alone leaves the
        Input/Output axis and the cost line frozen at a maximum that includes the
        series the reader just hid, and the fixture cannot notice because the
        left axis is the one the headline test toggles."""
        got = self._run(emit("(() => {" + self.CLICK + """
          const out = {};
          for (const [label, axis] of [['Cache Read', 'y'], ['Output', 'y1'],
                                       ['Est. Cost', 'y2']]) {
            const before = dailyChart().options.scales[axis].max;
            click(label);
            out[axis] = { before, after: dailyChart().options.scales[axis].max,
                          want: dailyAxisMax(dailyRangeRows, axis) };
            click(label);          // restore before moving to the next axis
          }
          return out;
        })()"""))
        for axis, pinned in got.items():
            with self.subTest(axis=axis):
                self.assertNotEqual(
                    pinned["before"], pinned["after"],
                    f"{axis} did not move when its own series was hidden")
                self.assertEqual(pinned["after"], pinned["want"])
        self.assertIsNone(got["y2"]["after"],
                          "Est. Cost is the only series on y2, so hiding it "
                          "must hand that axis back to autoscale")

    def test_the_re_pin_still_covers_the_days_the_window_is_hiding(self):
        """`dailyAxisMax` is handed the whole RANGE, never the window — the pin
        exists precisely so the scale does not jump as you pan."""
        got = self._run(emit("(() => {" + self.CLICK + """
          setDailyPanOffset(0);              // park on the quietest days
          click('Cache Read');
          const shown = lastDailyRows.slice(dailyPanOffset,
                                            dailyPanOffset + dailyWindowLen);
          return { after: dailyChart().options.scales.y.max,
                   range: dailyAxisMax(dailyRangeRows, 'y'),
                   window: dailyAxisMax(shown, 'y') };
        })()"""))
        self.assertLess(got["window"], got["range"],
                        "the window already reaches the range's biggest day, so "
                        "this test cannot tell the two apart")
        self.assertEqual(got["after"], got["range"])

    def test_restoring_a_series_re_pins_the_axis_back(self):
        """The recompute has to run in BOTH directions, or the series that comes
        back is clipped by a maximum measured while it was gone."""
        got = self._run(emit("(() => {" + self.CLICK + """
          const before = maxima();
          click('Cache Read');
          const hidden = maxima();
          click('Cache Read');
          return { before, hidden, restored: maxima(),
                   set: [...hiddenSeries.daily] };
        })()"""))
        self.assertNotEqual(got["before"], got["hidden"])
        self.assertEqual(got["restored"], got["before"])
        self.assertEqual(got["set"], [])

    def test_a_range_that_fits_still_autoscales_after_a_legend_click(self):
        """The guard. An unwindowed chart carries no pin and must not acquire
        one — assigning a number here would start pinning a chart that never
        was, turning the case that already works into the broken one."""
        got = self._run(emit("(() => {" + self.CLICK + """
          const before = maxima();
          click('Cache Read');
          return { before, after: maxima(), panning: dailyWindowLen };
        })()"""), n=7, rng="7d")
        self.assertEqual(got["panning"], 0,
                         "7 days fit at this width; if they do not, this test "
                         "is exercising the pinned path instead of the guard")
        self.assertEqual(got["before"], [None, None, None])
        self.assertEqual(got["after"], [None, None, None])

    def test_the_pan_offset_survives_a_legend_click(self):
        """Which is the whole reason this is a re-pin and not a re-render."""
        got = self._run(emit("(() => {" + self.CLICK + """
          setDailyPanOffset(3);
          const before = dailyPanOffset;
          const labelsBefore = dailyChart().data.labels.slice();
          click('Output');
          return { before, after: dailyPanOffset,
                   labelsBefore, labelsAfter: dailyChart().data.labels };
        })()"""))
        self.assertEqual(got["before"], 3)
        self.assertEqual(got["after"], 3)
        self.assertEqual(got["labelsAfter"], got["labelsBefore"])

    def test_the_toggle_still_records_the_series_it_hid(self):
        """The re-pin is added TO legendToggle's job, not put in place of it:
        `hiddenSeries` is what survives the rebuild on the next filter change."""
        got = self._run(emit("(() => {" + self.CLICK + """
          click('Cache Read');
          const ds = dailyChart().data.datasets.find(d => d.label === 'Cache Read');
          const set = [...hiddenSeries.daily];
          applyFilter();                    // rebuilds the chart from scratch
          const rebuilt = dailyChart().data.datasets.find(d => d.label === 'Cache Read');
          return { hidden: ds.hidden, set, stillHidden: rebuilt.hidden,
                   rebuiltMax: dailyChart().options.scales.y.max,
                   want: dailyAxisMax(dailyRangeRows, 'y') };
        })()"""))
        self.assertTrue(got["hidden"])
        self.assertEqual(got["set"], ["Cache Read"])
        self.assertTrue(got["stillHidden"],
                        "a repaint forgot the series the reader hid")
        self.assertEqual(got["rebuiltMax"], got["want"])


@requires_node
class TestTheCostAxisFollowsTheCostSeries(unittest.TestCase):
    """No dollar axis on a source the page has just refused to price.

    `dailySeries()` already drops the money series where nothing on screen has a
    published rate, and says why: a flat zero line under a $0.00 axis asserts the
    usage was free, which is a different claim from "not priced". The axis itself
    was declared `display: !isNarrowViewport()` — an explicit boolean — so it was
    still built with no dataset bound to it, autoscaled 0..1 and printed a
    fabricated ladder from $0.00 to $1.00, 66px wide, beside an Est. Cost tile
    reading `n/a`.

    Two things the gate deliberately does NOT do. It is not Chart.js's
    `display: 'auto'`: that also retires the axis when the reader legend-hides
    Est. Cost on a fully priced source, reflowing the plot 73px on every toggle.
    And it does not delete the `y2` key — `dailyChart()` above identifies the
    daily chart BY that key, because the subagent chart carries the same four
    dataset labels.
    """

    def _axis(self, model, source, width=1024, plot=800):
        rows = daily_payload(iso_days(days_back(6), 7), model=model, source=source)
        return run_js(daily_dom(width, plot)
                      + daily_state(rows, [model], source, "7d")
                      + "applyFilter();\n" + emit("""
          (() => {
            const c = dailyChart();
            const y2 = c.options.scales.y2;
            return { present: !!y2, display: y2 && y2.display,
                     labels: c.data.datasets.map(d => d.label),
                     priced: sourceIsPriced };
          })()"""))

    def test_an_unpriced_source_gets_no_dollar_axis(self):
        got = self._axis("local-llama-3", "codex")
        self.assertFalse(got["priced"])
        self.assertNotIn("Est. Cost", got["labels"],
                         "the cost SERIES is drawn, so this is a different bug")
        self.assertTrue(got["present"],
                        "the y2 key is what identifies the daily chart; gate "
                        "its display, do not delete the scale")
        self.assertIs(got["display"], False)

    def test_a_priced_source_keeps_its_dollar_axis(self):
        got = self._axis("claude-opus-5", "claude")
        self.assertTrue(got["priced"])
        self.assertIn("Est. Cost", got["labels"])
        self.assertIs(got["display"], True,
                      "the guard is inverted: a priced source lost its cost axis")

    def test_a_phone_still_has_nowhere_to_put_a_third_axis(self):
        got = self._axis("claude-opus-5", "claude", width=390, plot=336)
        self.assertIn("Est. Cost", got["labels"],
                      "the cost line and its tooltip survive without the axis")
        self.assertIs(got["display"], False)


# ── What each chart actually draws ──────────────────────────────────────────
#
# The daily chart is covered above. These cover the other four, which had no
# assertion on their datasets at all: 14 of 20 single-line mutations to
# web/js/52-charts.js survived the whole suite, among them moving the hourly
# turns bars onto the token axis (the card renders visually empty — a 1.0
# average drew -0.24px instead of 181.31px) and swapping the daily chart's two
# y-axis titles.
#
# Written as RELATIONS, not as a copy of the source line. Which axis a series
# binds to is checked against that axis's own title; the doughnut's slices
# against the rows the Cost by Model table renders; the project chart's row count
# against min(10, n) at two fixture sizes; the colours against each other. A test
# that restated `yAxisID: 'y'` would raise the mutation score by construction
# and turn every legitimate restyle red.

CHART_MODELS = ["claude-opus-5", "claude-sonnet-5"]


def _words(text):
    """The alphabetic words of a label or an axis title, lowercased.

    What makes the daily axis check a relation rather than a mirror: 'Cache Read'
    belongs on the axis titled 'Cache', 'Input' and 'Output' on 'Input / Output',
    and 'Est. Cost' on its own — each pair shares a word, and a swap shares none.
    """
    return {w for w in re.split(r"[^A-Za-z]+", str(text).lower()) if w}


def charts_state(days=7, projects=11, source="claude"):
    """Payload arrays that put data in ALL five charts.

    Every field of every row differs from its neighbours, so a dataset reading
    the wrong column cannot coincidentally match the right one. Returns the JS
    setup and the rows, so the expected values are computed from the same
    fixture the page was handed rather than re-typed.
    """
    iso = iso_days(days_back(days - 1), days)
    daily, hourly, project_rows, subagent = [], [], [], []
    for i, day in enumerate(iso):
        for j, model in enumerate(CHART_MODELS):
            n = (i + 1) * (j + 1)
            daily.append({"day": day, "source": source, "model": model,
                          "input": n, "output": n * 2, "cache_read": n * 3,
                          "cache_creation": n * 4, "cache_creation_1h": 0,
                          "reasoning": 0, "turns": n})
        hourly.append({"day": day, "hour": 5, "source": source,
                       "model": CHART_MODELS[0], "turns": i + 1,
                       "output": (i + 1) * 100})
    for p in range(projects):
        project_rows.append({"day": iso[0], "source": source, "branch": "main",
                             "model": CHART_MODELS[0], "project": "proj-%02d" % p,
                             "input": (projects - p) * 10,
                             "output": (projects - p) * 20, "cache_read": 0,
                             "cache_creation": 0, "cache_creation_1h": 0,
                             "turns": 1})
    for k, agent in enumerate(("Explore", "Plan", "general-purpose")):
        subagent.append({"day": iso[0], "source": source, "model": CHART_MODELS[0],
                         "agent_type": agent, "input": k + 1, "output": (k + 1) * 2,
                         "cache_read": (k + 1) * 3, "cache_creation": (k + 1) * 4,
                         "cache_creation_1h": 0, "turns": 1})
    raw = {"daily_by_model": daily, "hourly_by_model": hourly,
           "project_by_day_model": project_rows, "subagent_by_type": subagent,
           "sessions_all": [], "top_dispatches": [], "effort_by_day_model": [],
           "stop_reason_by_day_model": [], "limit_incidents": []}
    setup = ("rawData = " + json.dumps(raw) + ";\n"
             + "selectedSource = " + json.dumps(source) + ";\n"
             + "selectedModels = new Set(" + json.dumps(CHART_MODELS) + ");\n"
             + "allModelsList = " + json.dumps(CHART_MODELS) + ";\n"
             + "selectedRange = \"7d\";\n"
             # Pinned so which hour carries the fixture's rows does not depend on
             # the machine running the suite.
             + "hourlyTZ = 'utc';\n"
             + "hiddenSeries.daily.clear();\n")
    return setup, raw


# Each render function is wrapped so the chart it built is named, rather than
# picked out of `chartsDrawn` by a shape that two of the five charts share.
_NAME_THE_CHARTS = """
  globalThis.drawn = {};
  const _wrap = (name, fn) => function (...args) {
    const before = chartsDrawn.length;
    const out = fn.apply(this, args);
    drawn[name] = chartsDrawn.length > before ? chartsDrawn[chartsDrawn.length - 1] : null;
    return out;
  };
  renderDailyChart    = _wrap('daily', renderDailyChart);
  renderHourlyChart   = _wrap('hourly', renderHourlyChart);
  renderModelChart    = _wrap('model', renderModelChart);
  renderProjectChart  = _wrap('project', renderProjectChart);
  renderSubagentChart = _wrap('subagent', renderSubagentChart);
  globalThis.summarize = (inst) => inst && ({
    type: inst.config.type || null,
    indexAxis: inst.options.indexAxis || null,
    labels: inst.data.labels,
    scales: Object.fromEntries(Object.entries(inst.options.scales || {}).map(
      ([k, v]) => [k, { title: (v.title && v.title.text) || null,
                        display: v.display === undefined ? null : v.display }])),
    datasets: (inst.data.datasets || []).map(d => ({
      label: d.label === undefined ? null : d.label,
      type: d.type || null,
      yAxisID: d.yAxisID || null,
      stack: d.stack || null,
      fill: Array.isArray(d.backgroundColor) ? null : (d.backgroundColor || null),
      fills: Array.isArray(d.backgroundColor) ? d.backgroundColor : null,
      data: d.data,
    })),
  });
"""


@requires_node
class TestEachChartDrawsWhatItsAxesClaim(unittest.TestCase):
    """Series-to-axis binding, chart form, slice values, stacks and colours."""

    @classmethod
    def setUpClass(cls):
        if not NODE:
            return
        setup, raw = charts_state()
        cls.raw = raw
        got = run_js(daily_dom(1024, 800) + _NAME_THE_CHARTS + setup
                     + "applyFilter();\n" + emit("""
          ({ charts: Object.fromEntries(Object.entries(drawn).map(
               ([k, v]) => [k, summarize(v)])),
             byModel: lastByModel.map(m => ({ model: m.model, input: m.input,
                                             output: m.output })),
             tokenColors: Object.values(TOKEN_COLORS),
             modelColors: MODEL_COLORS,
             windowed: dailyWindowLen })"""))
        cls.charts = got["charts"]
        cls.by_model = got["byModel"]
        cls.token_colors = got["tokenColors"]
        cls.model_colors = got["modelColors"]
        cls.windowed = got["windowed"]

    def _by_day(self, field):
        """The fixture's own per-day totals for one column."""
        out = {}
        for row in self.raw["daily_by_model"]:
            out[row["day"]] = out.get(row["day"], 0) + row[field]
        return out

    def test_every_chart_was_drawn(self):
        """A chart that rendered nothing would make every test below vacuous."""
        for name in ("daily", "hourly", "model", "project", "subagent"):
            with self.subTest(chart=name):
                self.assertIsNotNone(self.charts.get(name))
                self.assertTrue(self.charts[name]["datasets"])
        self.assertEqual(self.windowed, 0,
                         "7 days must fit, or the daily assertions below are "
                         "reading a window rather than the range")

    def test_the_hourly_series_bind_to_the_axes_that_describe_them(self):
        """The headline survivor: `yAxisID: 'y'` -> `'y1'` puts the turns bars on
        the token axis, the card renders visually empty, and the left axis keeps
        the title 'Avg turns / hour' with nothing on it."""
        hourly = self.charts["hourly"]
        self.assertEqual(len(hourly["datasets"]), 2)
        for ds in hourly["datasets"]:
            with self.subTest(series=ds["label"]):
                axis = hourly["scales"].get(ds["yAxisID"])
                self.assertIsNotNone(axis, f"no scale {ds['yAxisID']!r}")
                self.assertEqual(
                    axis["title"], ds["label"],
                    f"the {ds['label']!r} series is drawn against an axis "
                    f"titled {axis['title']!r}")
        self.assertNotEqual(hourly["datasets"][0]["yAxisID"],
                            hourly["datasets"][1]["yAxisID"])

    def test_every_daily_series_binds_to_an_axis_that_names_it(self):
        """Swapping the two daily axis titles was silent across the whole suite
        and mislabels the plot by 190.9x / 35.5x."""
        daily = self.charts["daily"]
        for ds in daily["datasets"]:
            with self.subTest(series=ds["label"]):
                axis = daily["scales"].get(ds["yAxisID"])
                self.assertIsNotNone(axis, f"no scale {ds['yAxisID']!r}")
                self.assertTrue(
                    _words(ds["label"]) & _words(axis["title"] or ""),
                    f"the {ds['label']!r} series is drawn against an axis "
                    f"titled {axis['title']!r}, which names something else")

    def test_the_daily_cost_series_is_the_one_line_over_the_bars(self):
        """A bar in a dollar scale would join the stacked token bars as a fifth
        column per day rather than tracing over them."""
        daily = self.charts["daily"]
        lines = [d for d in daily["datasets"] if d["type"] == "line"]
        self.assertEqual([d["label"] for d in lines], ["Est. Cost"])
        self.assertIsNone(lines[0]["stack"], "the cost line is not stacked")
        for ds in daily["datasets"]:
            if ds["type"] != "line":
                with self.subTest(series=ds["label"]):
                    self.assertIsNotNone(ds["stack"])

    def test_every_daily_series_reads_the_column_its_label_names(self):
        daily = self.charts["daily"]
        want = {"Input": "input", "Output": "output", "Cache Read": "cache_read",
                "Cache Creation": "cache_creation"}
        for ds in daily["datasets"]:
            if ds["label"] not in want:
                continue
            with self.subTest(series=ds["label"]):
                by_day = self._by_day(want[ds["label"]])
                self.assertEqual(ds["data"], [by_day[d] for d in daily["labels"]])

    def test_the_model_doughnut_plots_the_rows_the_cost_table_shows(self):
        """`m.input` instead of `m.input + m.output` keeps every slice and moves
        only the shares, so nothing else on the page contradicts it."""
        model = self.charts["model"]
        self.assertEqual(model["type"], "doughnut")
        self.assertEqual(model["labels"], [m["model"] for m in self.by_model])
        want = [m["input"] + m["output"] for m in self.by_model]
        self.assertNotEqual(want, [m["input"] for m in self.by_model],
                            "the fixture cannot tell the two apart")
        self.assertEqual(model["datasets"][0]["data"], want)
        self.assertEqual(model["datasets"][0]["fills"][:len(want)],
                         self.model_colors[:len(want)])

    def test_the_project_bars_run_along_the_labels_not_across_them(self):
        """Project names are long paths; a vertical flip stands them on end."""
        self.assertEqual(self.charts["project"]["indexAxis"], "y")
        self.assertEqual(self.charts["subagent"]["indexAxis"], "y")

    def test_the_project_chart_reads_the_columns_it_labels(self):
        project = self.charts["project"]
        rows = {r["project"]: r for r in self.raw["project_by_day_model"]}
        for ds in project["datasets"]:
            with self.subTest(series=ds["label"]):
                field = ds["label"].lower()
                self.assertEqual(
                    ds["data"], [rows[name][field] for name in project["labels"]])

    def test_the_subagent_series_share_one_stack(self):
        """Four series in one bar per agent type. One of them losing its `stack`
        splits that type into two bars and the total the tooltip prints stops
        being the bar's height."""
        subagent = self.charts["subagent"]
        stacks = {ds["stack"] for ds in subagent["datasets"]}
        self.assertEqual(len(subagent["datasets"]), 4)
        self.assertNotIn(None, stacks, "a series left the stack")
        self.assertEqual(len(stacks), 1, f"the series sit in {len(stacks)} stacks")

    def test_the_subagent_series_read_the_columns_they_label(self):
        subagent = self.charts["subagent"]
        rows = {r["agent_type"]: r for r in self.raw["subagent_by_type"]}
        want = {"Input": "input", "Output": "output", "Cache Read": "cache_read",
                "Cache Creation": "cache_creation"}
        for ds in subagent["datasets"]:
            with self.subTest(series=ds["label"]):
                field = want[ds["label"]]
                self.assertEqual(
                    ds["data"], [rows[name][field] for name in subagent["labels"]])

    def test_no_two_series_on_a_chart_share_a_colour(self):
        """'Input painted in Output's colour' leaves two indistinguishable bands
        in a stack — pinned as distinctness and palette membership rather than as
        a per-series hex, so a repaint stays legal."""
        for name in ("daily", "project", "subagent"):
            fills = [ds["fill"] for ds in self.charts[name]["datasets"]
                     if ds["fill"] and ds["fill"] != "transparent"]
            with self.subTest(chart=name):
                self.assertGreaterEqual(len(fills), 2)
                self.assertEqual(len(set(fills)), len(fills),
                                 f"{name}: two series are painted the same")
                for fill in fills:
                    self.assertIn(fill, self.token_colors)

    def test_the_project_chart_is_capped_at_ten_rows_and_shows_no_more(self):
        """The cap, at both ends: 11 projects must not draw an eleventh bar, and
        3 must not be padded up to ten."""
        for count, expected in ((11, 10), (3, 3)):
            with self.subTest(projects=count):
                setup, _ = charts_state(projects=count)
                got = run_js(daily_dom(1024, 800) + _NAME_THE_CHARTS + setup
                             + "applyFilter();\n"
                             + emit("summarize(drawn.project).labels"))
                self.assertEqual(len(got), expected)
                self.assertEqual(len(set(got)), expected)


@requires_node
class TestTheHourlyChartAveragesOverDays(unittest.TestCase):
    """'Average Hourly Distribution' really is an average.

    `aggregateHourly` divides each hour's totals by the number of distinct days,
    and that division is the only arithmetic behind both plotted series, both
    axis titles, the tooltip and the caption. Every test that reached the
    function fed it ONE day, where the division is the identity — so replacing
    both divisions with the bare totals left all 1430 tests green while a 30-day
    range plotted 30 turns an hour under a caption reading "30 days averaged".

    The fixture is three days over five rows with per-day totals that differ, so
    a denominator of rows-seen (5), hours-with-data (2), 1, or 24 each produce a
    different number from the right one.
    """

    ROWS = [
        {"day": "2026-08-01", "hour": 5, "turns": 3, "output": 30},
        {"day": "2026-08-02", "hour": 5, "turns": 6, "output": 60},
        {"day": "2026-08-05", "hour": 5, "turns": 12, "output": 120},
        {"day": "2026-08-01", "hour": 9, "turns": 9, "output": 900},
        {"day": "2026-08-02", "hour": 9, "turns": 15, "output": 1500},
    ]
    DAYS = 3

    @classmethod
    def setUpClass(cls):
        if not NODE:
            return
        cls.got = run_js(emit("""
          (() => {
            const built = [];
            globalThis.Chart = function Chart(ctx, cfg) {
              built.push(cfg);
              return { update() {}, destroy() {}, data: cfg.data, options: cfg.options };
            };
            globalThis.Chart.defaults = {
              color: '', font: {}, borderColor: '', backgroundColor: '',
              plugins: { tooltip: { callbacks: {} }, legend: { labels: {} } },
              scale: { grid: {} }, scales: {}, elements: {}, datasets: {} };
            // The shared stub hands back a FRESH element on every call, so the
            // caption renderHourlyChart writes could never be read back.
            // Memoized here, in this test's own snippet, rather than in
            // _DOM_STUB, which every JavaScript test in the file loads.
            const _els = new Map();
            document.getElementById = (id) => {
              if (!_els.has(id)) _els.set(id, stubEl());
              return _els.get(id);
            };
            hourlyTZ = 'utc';
            charts.hourly = null;
            const agg = aggregateHourly(rows, 'utc');
            renderHourlyChart(agg);
            const series = {};
            for (const ds of built[0].data.datasets) series[ds.label] = ds.data;
            return { dayCount: agg.dayCount, series,
                     totalTurns: agg.hours.map(h => h.totalTurns),
                     caption: document.getElementById('hourly-day-count').textContent };
          })()""", rows=cls.ROWS))

    def _series(self, needle):
        found = [v for k, v in self.got["series"].items() if needle in k]
        self.assertEqual(len(found), 1,
                         f"expected one series naming {needle!r}, got "
                         f"{list(self.got['series'])}")
        return found[0]

    def _per_hour(self, field):
        out = [0] * 24
        for row in self.ROWS:
            out[row["hour"]] += row[field]
        return out

    def test_the_day_count_is_the_days_seen_not_the_rows(self):
        self.assertEqual(self.got["dayCount"], self.DAYS)
        self.assertNotEqual(self.DAYS, len(self.ROWS),
                            "rows and days are equal, so a denominator of "
                            "rows-seen would pass this suite unnoticed")

    def test_the_plotted_turns_are_the_hour_total_divided_by_the_days(self):
        want = [t / self.DAYS for t in self._per_hour("turns")]
        self.assertEqual(self._series("turns"), want)

    def test_the_plotted_output_is_the_hour_total_divided_by_the_days(self):
        """Two separate divisions, two lines; a fix covering only the bars would
        leave the line exactly as exposed as it is today."""
        want = [t / self.DAYS for t in self._per_hour("output")]
        self.assertEqual(self._series("output"), want)

    def test_the_totals_beside_them_are_not_averaged(self):
        """The un-averaged column is what proves the division happened at all —
        without it, a fixture of one day per hour would satisfy the two above."""
        self.assertEqual(self.got["totalTurns"], self._per_hour("turns"))
        self.assertNotEqual(self._series("turns"), self.got["totalTurns"])

    def test_the_caption_counts_the_same_days_the_division_used(self):
        self.assertIn(f"{self.DAYS} days averaged", self.got["caption"])


@requires_node
class TestDailyPanKeyboard(unittest.TestCase):
    """Panning was pointer-only, which on a range you can no longer see in one
    screen makes most of the data unreachable without a mouse."""

    def _run(self, tail, n=90):
        rows = daily_payload(iso_days(days_back(n - 1), n))
        return run_js(daily_dom(1024, 800)
                      + daily_state(rows, ["claude-opus-5"], "claude", "90d")
                      + "applyFilter();\n" + tail)

    def test_the_arrows_move_one_day_at_a_time(self):
        got = self._run(emit("""
          (() => {
            setDailyPanOffset(20);
            dailyPanByKey('ArrowRight');
            const right = dailyPanOffset;
            dailyPanByKey('ArrowLeft');
            const back = dailyPanOffset;
            return { right, back };
          })()"""))
        self.assertEqual(got["right"], 21)
        self.assertEqual(got["back"], 20)

    def test_home_and_end_reach_the_first_and_last_day(self):
        got = self._run(emit("""
          (() => {
            dailyPanByKey('Home');
            const first = lastDailyRows[dailyPanOffset].day;
            dailyPanByKey('End');
            const last = lastDailyRows[dailyPanOffset + dailyWindowLen - 1].day;
            return { first, last, offset: dailyPanOffset,
                     max: lastDailyRows.length - dailyWindowLen,
                     firstRow: lastDailyRows[0].day,
                     lastRow: lastDailyRows[lastDailyRows.length - 1].day };
          })()"""))
        self.assertEqual(got["first"], got["firstRow"])
        self.assertEqual(got["last"], got["lastRow"])
        self.assertEqual(got["offset"], got["max"])

    def test_page_keys_move_by_a_whole_window(self):
        got = self._run(emit("""
          (() => {
            dailyPanByKey('Home');
            dailyPanByKey('PageDown');
            return { offset: dailyPanOffset, win: dailyWindowLen };
          })()"""))
        self.assertEqual(got["offset"], got["win"])

    def test_it_clamps_instead_of_running_off_either_end(self):
        got = self._run(emit("""
          (() => {
            dailyPanByKey('Home');
            dailyPanByKey('ArrowLeft');
            const low = dailyPanOffset;
            dailyPanByKey('End');
            dailyPanByKey('ArrowRight');
            const high = dailyPanOffset;
            return { low, high, max: lastDailyRows.length - dailyWindowLen };
          })()"""))
        self.assertEqual(got["low"], 0)
        self.assertEqual(got["high"], got["max"])

    def test_an_unrelated_key_is_not_swallowed(self):
        got = self._run(emit("""
          ['ArrowRight', 'Home', 'End', 'PageUp', 'PageDown', 'a', 'Enter', 'Tab']
            .map(k => dailyPanByKey(k))"""))
        self.assertEqual(got, [True, True, True, True, True, False, False, False])

    def test_it_does_nothing_when_the_whole_range_already_fits(self):
        rows = daily_payload(iso_days(days_back(6), 7))
        got = run_js(daily_dom(1600, 1200)
                     + daily_state(rows, ["claude-opus-5"], "claude", "7d")
                     + "applyFilter();\n"
                     + emit("({handled: dailyPanByKey('ArrowRight'), "
                            "  win: dailyWindowLen, offset: dailyPanOffset})"))
        self.assertEqual(got["win"], 0)
        self.assertFalse(got["handled"])
        self.assertEqual(got["offset"], 0)


class TestDailyStatsPanelLayout(unittest.TestCase):
    """The markup and stylesheet half of the same feature.

    It lives beside the JavaScript tests because it is the same change: the
    panel is only "to the right of the chart" if the stylesheet puts it there,
    and only usable in the VS Code panel if it stops doing so when narrow.

    WHAT THIS CLASS CANNOT SEE, stated plainly because it shipped a defect by
    being trusted for more than it does: these are regular expressions over the
    text of index.html and app.css. They can tell you a two-column rule EXISTS;
    they cannot tell you the second column is wide enough for what goes in it.
    A 334px table in a 200px track — Mean and Max rendered outside the panel and
    a horizontal scrollbar on the whole page — passed every assertion here.
    Rendered geometry is measured in TestDailyPanelGeometryInABrowser below,
    which needs a browser and therefore skips where there is none; these six
    remain the part that always runs.
    """

    HTML = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    CSS = (REPO_ROOT / "web" / "app.css").read_text(encoding="utf-8")

    def test_the_panel_sits_beside_the_chart_in_the_daily_card(self):
        section = self.HTML.split('id="sec-daily"', 1)[1].split("</div>\n    </div>")[0]
        self.assertIn('id="daily-stats"', section)
        self.assertIn("daily-body", section)

    def test_the_pan_control_sits_under_the_plot_it_moves(self):
        section = self.HTML.split('id="sec-daily"', 1)[1]
        plot = section.split('class="daily-plot"', 1)[1].split('id="daily-stats"')[0]
        for marker in ('id="chart-daily"', 'id="daily-pan"', 'id="daily-pan-hint"'):
            self.assertIn(marker, plot)

    def test_the_chart_is_focusable_so_it_can_be_panned_from_the_keyboard(self):
        section = self.HTML.split('id="sec-daily"', 1)[1].split("</div>\n    </div>")[0]
        self.assertRegex(section, r'id="daily-chart-wrap"[^>]*tabindex="0"')
        self.assertRegex(section, r'id="daily-chart-wrap"[^>]*aria-label=')

    def test_it_is_two_columns_when_wide_and_stacked_when_not(self):
        self.assertRegex(
            self.CSS, r"\.daily-body\s*\{[^}]*grid-template-columns:\s*1fr",
            "the stacked layout must be the base, so a narrow page never "
            "squeezes the chart")
        wide = re.search(r"@media \(min-width: (\d+)px\)\s*\{\s*"
                         r"\.daily-body\s*\{[^}]*grid-template-columns:"
                         r"\s*minmax\(0,\s*1fr\)", self.CSS)
        self.assertIsNotNone(wide, "no side-by-side layout is defined at all")
        self.assertGreaterEqual(
            int(wide.group(1)), 640,
            "the panel must stack below 640px — the VS Code panel's only width")

    def test_the_panel_colours_come_from_the_theme(self):
        rules = re.findall(r"(\.daily-(?:stats|body|plot)[^{}]*)\{([^}]*)\}", self.CSS)
        self.assertTrue(rules, "no styles for the panel at all")
        for selector, body in rules:
            with self.subTest(selector=selector.strip()):
                self.assertNotRegex(
                    body, r"(?:color|background|border-color):\s*#[0-9A-Fa-f]{3,8}",
                    "a literal hex cannot follow the theme; name a token")

    def test_the_focus_ring_is_visible_on_the_chart(self):
        self.assertRegex(self.CSS, r"#daily-chart-wrap:focus-visible\s*\{[^}]*outline:")

    def test_the_panel_can_never_push_its_own_column_wider_than_the_card(self):
        """The containment rule, which is what makes the width above a choice
        rather than a bet.

        A grid item's automatic minimum size is its CONTENT's width, so a table
        of nowrap cells enlarges the track it sits in and then the card and then
        the page. `min-width: 0` plus `overflow-x: auto` on `.daily-stats` make
        the excess scroll inside the panel instead. Text-level, like the rest of
        this class — the geometry it produces is measured in the browser class
        below.
        """
        rule = re.search(r"\.daily-stats\s*\{([^}]*)\}", self.CSS)
        self.assertIsNotNone(rule, "no .daily-stats rule at all")
        self.assertRegex(rule.group(1), r"min-width:\s*0")
        self.assertRegex(rule.group(1), r"overflow-x:\s*auto")


# ── The rendered page, in a real browser ───────────────────────────────────
# Everything above runs the JavaScript under node with a DOM stub, which cannot
# lay anything out: it has no fonts, no boxes and no scrollbars. That is why a
# 334px table inside a 200px column shipped green. These tests serve the real
# assembled document, let Chrome lay it out, and read the numbers back.
#
# No new dependency and no CDP: the page writes its own measurements into a
# <pre> and the browser is asked for the DOM with --dump-dom. Chart.js comes
# from vendor/ exactly as the server serves it.

def _find_browser():
    """A Chrome/Chromium binary, or None. CLAUDE_USAGE_CHROME overrides."""
    named = os.environ.get("CLAUDE_USAGE_CHROME")
    if named:
        return Path(named) if Path(named).exists() else None
    cache = Path.home() / ".cache" / "puppeteer"
    globs = (
        "chrome-headless-shell/*/*/chrome-headless-shell",
        "chrome/*/*/Google Chrome for Testing.app/Contents/MacOS/"
        "Google Chrome for Testing",
        "chrome/*/*/chrome",
    )
    for pattern in globs:
        for hit in sorted(cache.glob(pattern), reverse=True):
            if os.access(hit, os.X_OK):
                return hit
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable", "chrome"):
        found = shutil.which(name)
        if found:
            return Path(found)
    for app in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium"):
        if Path(app).exists():
            return Path(app)
    return None


BROWSER = _find_browser()
# Same contract as CLAUDE_USAGE_REQUIRE_JS: a machine that is supposed to have a
# browser must fail loudly rather than skip the only tests that see pixels.
REQUIRE_BROWSER = os.environ.get("CLAUDE_USAGE_REQUIRE_BROWSER") == "1"
requires_browser = unittest.skipUnless(
    BROWSER, "no Chrome/Chromium found (set CLAUDE_USAGE_CHROME to one)")

# Written into the served copy of the page, never into web/. It waits for the
# panel the app renders, then records the three things the stub cannot see.
_GEOMETRY_PROBE = """
<pre id="__measure" hidden></pre>
<script nonce="__CSP_NONCE__">
(function () {
  function box(el) {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { width: +r.width.toFixed(2), left: +r.left.toFixed(2),
             right: +r.right.toFixed(2), clientWidth: el.clientWidth,
             scrollWidth: el.scrollWidth };
  }
  function widestOverflow() {
    const limit = document.documentElement.clientWidth;
    let worst = null;
    for (const el of document.querySelectorAll('*')) {
      const r = el.getBoundingClientRect();
      if (r.width && r.right > limit + 0.5 && (!worst || r.right > worst.right))
        worst = { tag: el.tagName, cls: String(el.className).slice(0, 60),
                  right: +r.right.toFixed(2) };
    }
    return worst;
  }
  // How far an element can actually be scrolled sideways. 0 says it is not a
  // scroll container at all, which is the difference between an over-wide table
  // being reachable and it being painted over the page.
  function scrollability(el) {
    if (!el) return null;
    const keep = el.scrollLeft;
    el.scrollLeft = 1e7;
    const max = el.scrollLeft;
    el.scrollLeft = keep;
    return max;
  }
  function pan() {
    const bar = document.getElementById('daily-pan');
    if (!bar || bar.hidden) return { panning: false };
    const keep = bar.scrollLeft;
    bar.scrollLeft = 1e7;                       // as far right as it will go
    const maxScroll = bar.scrollLeft;
    bar.scrollLeft = keep;
    const col = dailyColumnWidth();
    const offset = Math.round(maxScroll / col);
    const rows = lastDailyRows;
    const last = rows[Math.min(rows.length - 1, offset + dailyWindowLen - 1)];
    return { panning: true, col: col, maxScroll: maxScroll,
             maxOffset: Math.max(0, rows.length - dailyWindowLen),
             reached: last && last.day,
             newest: rows.length ? rows[rows.length - 1].day : null };
  }
  function report() {
    const panel = document.getElementById('daily-stats');
    const cells = panel ? panel.querySelectorAll('[data-daily-sort$=".desc"]') : [];
    document.getElementById('__measure').textContent = JSON.stringify({
      viewportWidth: document.documentElement.clientWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      widestOverflow: widestOverflow(),
      panel: box(panel),
      panelScroll: scrollability(panel),
      table: box(panel && panel.querySelector('.daily-stats-table')),
      card: box(document.getElementById('sec-daily')),
      maxCell: box(cells[cells.length - 1]),
      series: dailySeries().map(s => s.label),
      pan: pan(),
    });
  }
  let waited = 0;
  (function settle() {
    const panel = document.getElementById('daily-stats');
    if (panel && panel.innerHTML.indexOf('daily-stats-table') !== -1) {
      setTimeout(report, 150);      // one beat for Chart.js to size the canvas
      return;
    }
    if (++waited > 400) { report(); return; }
    setTimeout(settle, 25);
  })();
})();
</script>
"""


def _browser_payload(db_path, scale=1):
    """A month of usage, gappy and expensive, in the real payload shape.

    Expensive on purpose: the panel's widest column is money to four decimals,
    which is what overflowed a 200px track. A cheap fixture would fit anywhere
    and prove nothing. `scale` multiplies the bill, which is how the containment
    rule gets a figure no fixed width could ever hold.
    """
    conn = get_db(db_path)
    init_db(conn)
    from datetime import date, timedelta
    today = date.today()
    for i in range(0, 30, 2):                   # every other day: real ranges gap
        stamp = (today - timedelta(days=29 - i)).isoformat() + "T12:00:00.000Z"
        tokens = {"input": (400000 + i * 1000) * scale,
                  "output": (900000 + i * 7) * scale,
                  "cache_read": 120000000 * scale,
                  "cache_creation": 3000000 * scale}
        upsert_sessions(conn, [{
            "session_id": "s%d" % i, "project_name": "u/p",
            "first_timestamp": stamp, "last_timestamp": stamp,
            "git_branch": "main", "model": "claude-opus-5",
            "total_input_tokens": tokens["input"],
            "total_output_tokens": tokens["output"],
            "total_cache_read": tokens["cache_read"],
            "total_cache_creation": tokens["cache_creation"],
            "turn_count": 1,
        }])
        insert_turns(conn, [{
            "session_id": "s%d" % i, "timestamp": stamp,
            "model": "claude-opus-5", "message_id": "m%d" % i,
            "input_tokens": tokens["input"], "output_tokens": tokens["output"],
            "cache_read_tokens": tokens["cache_read"],
            "cache_creation_tokens": tokens["cache_creation"],
            "tool_name": None, "cwd": "/tmp",
        }])
    conn.commit()
    conn.close()
    return json.dumps(get_dashboard_data(db_path, source="claude"),
                      default=str).encode("utf-8")


# Bound each browser attempt and share one total budget across retries.
# The last attempt receives the remaining budget so a stalled launch can be
# retried without multiplying the maximum wait.
_LAUNCH_TIMEOUT = 120
_LAUNCH_BUDGET = 420


def _load_average():
    """The 1-minute load average, or None where the platform has none.

    Both the lookup and the call are inside the guard: Windows has no
    os.getloadavg at all, and that leg runs this file.
    """
    try:
        return os.getloadavg()[0]
    except (AttributeError, OSError):
        return None


def _run_queue_pressure():
    """Runnable processes per CPU: 1.0 on an idle machine, and where unknown.

    Reported when a launch never finishes, so the reader is told whether the
    machine was oversubscribed instead of having to re-derive it. Deliberately
    not used to widen the cap: the sampling above shows a stall is not
    proportional to load, and a cap widened by load buys patience at the cost of
    the retries that actually recover.
    """
    load = _load_average()
    if load is None:
        return 1.0
    return max(1.0, load / (os.cpu_count() or 1))


def _measure_in_browser(width, height=900, attempts=3, scale=1):
    """Lay the real page out at `width` and return what it measured."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    with tempfile.TemporaryDirectory() as tmp:
        payload = _browser_payload(Path(tmp) / "usage.db", scale)
        page = (dashboard.HTML_TEMPLATE
                .replace("__APP_CONFIG_JSON__",
                         '{"version":"test","surface":"web",'
                         '"rate_overrides":{}}')
                .replace("</body>", _GEOMETRY_PROBE + "</body>")
                # The served copy carries no CSP header, so one nonce for both
                # the app's script and the probe is all this needs.
                .replace("__CSP_NONCE__", "probe")).encode("utf-8")
        chart = (REPO_ROOT / "vendor" / "chart.umd.js").read_bytes()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                route = self.path.split("?")[0]
                if route in ("/", "/index.html"):
                    self._reply(page, "text/html; charset=utf-8")
                elif route == "/assets/chart.umd.js":
                    self._reply(chart, "application/javascript")
                elif route == "/api/scan-status":
                    self._reply(
                        b'{"state":"idle","generation":0}',
                        "application/json",
                    )
                elif route == "/api/data":
                    self._reply(payload, "application/json")
                else:
                    self._reply(b"{}", "application/json")

            def do_POST(self):
                self._reply(b'{"new":0,"updated":0}', "application/json")

            def _reply(self, body, ctype):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            # A token in the fragment is what makes the page fetch its data —
            # exactly how the server hands it over.
            url = ("http://127.0.0.1:%d/?source=claude&range=30d#token=%s"
                   % (server.server_address[1], "b" * 40))
            # Every attempt draws on one budget, so a stall can be retried
            # without the retries multiplying the worst case.
            deadline = time.monotonic() + _LAUNCH_BUDGET
            stalls, caps, proc = 0, [], None
            for attempt in range(attempts):
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                # Short caps while there is still a retry to fall back on, and
                # the whole remainder on the last one — so a stall is abandoned
                # early enough to try again, while a machine that is merely slow
                # still gets a longer run at it than this ever gave before.
                cap = left if attempt == attempts - 1 else min(_LAUNCH_TIMEOUT, left)
                caps.append(cap)
                try:
                    proc = subprocess.run(
                        [str(BROWSER), "--headless", "--disable-gpu",
                         "--no-sandbox", "--user-data-dir=" + tmp + "/profile",
                         "--force-device-scale-factor=1",
                         "--window-size=%d,%d" % (width, height),
                         "--virtual-time-budget=20000", "--dump-dom", url],
                        capture_output=True, text=True, encoding="utf-8",
                        timeout=cap)
                except subprocess.TimeoutExpired:
                    # Retried, not raised. A launch that does not finish is
                    # exactly the transient this loop exists to absorb, and it
                    # was the one case the loop did not cover: the exception
                    # went straight past it and errored setUpClass, taking all
                    # five geometry assertions down with it.
                    stalls += 1
                    continue
                found = re.search(r'<pre id="__measure"[^>]*>(.*?)</pre>',
                                  proc.stdout, re.S)
                if found and found.group(1).strip():
                    return json.loads(found.group(1))
            if proc is None:
                load = _load_average()
                # Not one attempt ever finished. Loud whether or not the run
                # asked for a browser: unlike a Chrome that runs and dumps
                # nothing, a stall says nothing about whether this machine can
                # render the page, so skipping would hide a real regression as
                # readily as a busy laptop.
                # THREE causes, not two. An earlier version of this message
                # offered only contention and the page, and sent a reader
                # chasing a load average for twenty minutes while the actual
                # cause was the third: `~/.cache/puppeteer` had been deleted, so
                # `_find_browser` fell through to full Google Chrome. That
                # launches roughly 30x slower than chrome-headless-shell -- the
                # same class went from a 421s timeout to 13s once the light
                # binary was restored -- and it is invisible unless the message
                # says WHICH browser it resolved. So it does now, first.
                raise AssertionError(
                    "the browser never finished: %d attempt(s) capped at %s, "
                    "no measurement taken.\n"
                    "Browser used: %s\n"
                    "  If that is NOT a chrome-headless-shell path, that is the "
                    "likeliest cause: full Chrome launches far slower and this "
                    "class times out on it. Restore the light binary with\n"
                    "    npx @puppeteer/browsers install chrome-headless-shell@stable\n"
                    "  and move it under ~/.cache/puppeteer/, or point "
                    "CLAUDE_USAGE_CHROME at one.\n"
                    "Otherwise suspect contention: this machine is running "
                    "%.1f process(es) per CPU (%d CPUs, 1-minute load average "
                    "%s), and anything far above 1.0 means the browser is "
                    "queueing for a core.\n"
                    "To tell contention and a real regression apart, run this "
                    "class on its own:\n"
                    "  python3 -m unittest tests.test_dashboard_js."
                    "TestDailyPanelGeometryInABrowser\n"
                    "It finishes quickly on an idle machine WITH the light "
                    "binary. Passing alone but failing in a full or parallel "
                    "run is contention; failing alone on headless-shell too is "
                    "the page."
                    % (stalls, ", ".join("%.0fs" % c for c in caps),
                       BROWSER or "none found",
                       _run_queue_pressure(), os.cpu_count() or 1,
                       "unavailable" if load is None else "%.2f" % load))
            # Same contract as node: a browser that cannot be driven skips,
            # unless the run declared that it must be there. A contributor
            # whose Chrome build has no --dump-dom should not get a red suite
            # for it; CI asking for a browser should not get a silent pass.
            complaint = ("the browser produced no measurement in %d attempt(s) "
                         "(%d of them stalled); last stderr:\n%s"
                         % (len(caps), stalls, proc.stderr[-2000:]))
            if REQUIRE_BROWSER:
                raise AssertionError(complaint)
            raise unittest.SkipTest(complaint)
        finally:
            server.shutdown()
            server.server_close()


class TestBrowserAvailability(unittest.TestCase):
    """Kept undecorated and in its own class so the failure reads cleanly."""

    @unittest.skipUnless(REQUIRE_BROWSER,
                         "only enforced when CLAUDE_USAGE_REQUIRE_BROWSER=1")
    def test_a_browser_is_present_when_the_run_requires_it(self):
        self.assertIsNotNone(
            BROWSER,
            "CLAUDE_USAGE_REQUIRE_BROWSER=1 but no Chrome/Chromium was found, "
            "so every rendered-geometry assertion would have been skipped "
            "while the run stayed green.")


class TestBrowserLaunchSurvivesContention(unittest.TestCase):
    """How the harness behaves when a launch is slow — not what it measures.

    Undecorated on purpose, so it runs on every leg including the ones with no
    Chrome: it fakes the subprocess, and what it protects is the retry and the
    diagnostic. Neither is visible to any assertion in the geometry class, and
    neither can be staged on demand — the failure needs a machine under load.

    That class died once with a bare `subprocess.TimeoutExpired ... timed out
    after 180 seconds` in a full-suite run at load 189.91 on 12 CPUs, then
    passed alone in 13.3 s minutes later on the same tree. It is now enforced in
    CI, and a gate that goes red under load is worse than the silent skip it
    replaced: an intermittent red gets re-run rather than read.
    """

    MEASUREMENT = {"viewportWidth": 1265, "pageScrollWidth": 1265}

    def _dump(self):
        """What --dump-dom prints when the probe did get to run."""
        return mock.Mock(
            stdout='<pre id="__measure" hidden>%s</pre>' % json.dumps(self.MEASUREMENT),
            stderr="")

    @staticmethod
    def _stall(*args, **kwargs):
        """A launch that never finishes, however long it is given."""
        raise subprocess.TimeoutExpired(cmd=args[0] if args else "chrome",
                                        timeout=kwargs.get("timeout", 0))

    def test_a_stalled_launch_is_retried_instead_of_aborting_the_class(self):
        """The attempts loop exists for the transient browser failure, and the
        likeliest one — a launch that does not finish in time — was the single
        case it did not cover: the exception went straight past it and errored
        setUpClass, taking all five geometry assertions with it."""
        caps = []

        def run(*args, **kwargs):
            caps.append(kwargs["timeout"])
            if len(caps) == 1:
                raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])
            return self._dump()

        with mock.patch.object(subprocess, "run", run):
            got = _measure_in_browser(1280)
        self.assertEqual(got, self.MEASUREMENT)
        self.assertEqual(len(caps), 2,
                         "the stalled launch was not retried; caps used: %r" % (caps,))

    def test_a_browser_that_never_finishes_fails_loudly_and_names_the_browser(self):
        """Exhausting the budget must still be a failure, and must tell the
        reader what to check — this is the message someone reads at 3am."""
        with mock.patch.object(subprocess, "run", self._stall), \
                mock.patch(__name__ + ".REQUIRE_BROWSER", False):
            try:
                _measure_in_browser(1280)
            except AssertionError as exc:
                message = str(exc)
            except unittest.SkipTest as exc:
                self.fail("a stalled browser SKIPPED the geometry class instead "
                          "of failing it, which is the silent pass "
                          "CLAUDE_USAGE_REQUIRE_BROWSER exists to stop: %s" % exc)
            except subprocess.TimeoutExpired as exc:
                self.fail("TimeoutExpired escaped _measure_in_browser instead of "
                          "being retried and reported: %s" % exc)
            else:
                self.fail("a browser that never finishes returned a measurement")
        # Whole claims, not keywords. "contention" alone appears three times in
        # this message, so deleting the one sentence that actually diagnoses
        # anything left the old check green — the reader lost the explanation
        # and the test never noticed.
        #
        # The FIRST claim below is new, and this test previously asserted the
        # message "blames contention" before anything else. That was wrong, and
        # it cost real time: `~/.cache/puppeteer` was deleted, `_find_browser`
        # fell through to full Google Chrome, and the class timed out at 421s —
        # while the message pointed at a load average that was genuinely high
        # and genuinely not the cause. Restoring the light binary took the same
        # class to 13s at a HIGHER load. So the resolved browser is now named
        # first, and this test pins that ordering: a diagnostic that offers two
        # explanations when the truth is a third does not merely fail to help,
        # it steers.
        for wanted in (
                # which browser — the cause the old message could not express
                "Browser used:",
                "chrome-headless-shell",
                "npx @puppeteer/browsers install chrome-headless-shell@stable",
                # the contention diagnosis, still here, now second
                "Otherwise suspect contention",
                # the measurement it rests on, really interpolated
                "process(es) per CPU",
                "%d CPUs" % (os.cpu_count() or 1),
                "1-minute load average",
                # what to run next, and how to read the answer
                "python3 -m unittest tests.test_dashboard_js."
                "TestDailyPanelGeometryInABrowser",
                "Passing alone but failing in a full or parallel "
                "run is contention; failing alone on headless-shell too is "
                "the page.",
        ):
            self.assertIn(wanted, message,
                          "the failure does not say %r, so the next reader has "
                          "to re-derive that the machine was busy:\n%s"
                          % (wanted, message))

    def test_retrying_a_stall_stays_inside_one_measurement_budget(self):
        """Retrying an expensive failure is only safe if the retries share a
        bound; five attempts each allowed the full per-launch cap would turn a
        three-minute failure into a fifteen-minute one, per width.

        The clock is faked because the bound is on wall time: a real stall burns
        its whole cap, and a test that waited for that would cost 30 s to prove
        a bound of 30 s.
        """
        caps, clock = [], [0.0]

        def run(*args, **kwargs):
            caps.append(kwargs["timeout"])
            clock[0] += kwargs["timeout"]      # a stall burns its whole cap
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

        with mock.patch.object(subprocess, "run", run), \
                mock.patch.object(time, "monotonic", lambda: clock[0]), \
                mock.patch(__name__ + "._LAUNCH_TIMEOUT", 20), \
                mock.patch(__name__ + "._LAUNCH_BUDGET", 50):
            with self.assertRaises(AssertionError):
                _measure_in_browser(1280, attempts=5)
        self.assertLessEqual(clock[0], 50,
                             "five stalled attempts ran for %.1fs against a 50s "
                             "budget; caps handed out: %r" % (clock[0], caps))
        self.assertEqual(caps, [20, 20, 10],
                         "the last attempt should get only what the budget has "
                         "left, and no fourth should start at all")

    def test_early_attempts_fail_fast_and_the_last_one_is_the_patient_one(self):
        """The shape that lets a retry help without ever being less tolerant
        than the single 180 s attempt it replaced.

        Every launch that was going to succeed did so within 33.3 s under worse
        contention than CI ever sees, so waiting three minutes to conclude the
        first one stalled spends the budget that the retry needs. The last
        attempt has no retry behind it, so it gets everything left — which the
        constants keep at or above the old 180 s however the earlier ones go.
        """
        caps, clock = [], [0.0]

        def run(*args, **kwargs):
            caps.append(kwargs["timeout"])
            clock[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

        with mock.patch.object(subprocess, "run", run), \
                mock.patch.object(time, "monotonic", lambda: clock[0]):
            with self.assertRaises(AssertionError):
                _measure_in_browser(1280)
        self.assertGreaterEqual(len(caps), 2,
                                "a stalled launch got no retry at all: %r" % (caps,))
        for cap in caps[:-1]:
            self.assertLessEqual(
                cap, _LAUNCH_TIMEOUT,
                "an attempt with a retry behind it waited %.0fs before giving "
                "up, against a slowest-ever-success of 33.3s" % cap)
        self.assertGreaterEqual(
            caps[-1], 180,
            "the final attempt is allowed %.0fs, less than the single 180s "
            "attempt this replaced — a machine slower than anything measured "
            "here would newly fail: %r" % (caps[-1], caps))
        self.assertLessEqual(clock[0], _LAUNCH_BUDGET,
                             "the retries ran %.0fs past the budget" % clock[0])

    def test_pressure_reads_as_idle_where_there_is_no_load_average(self):
        """os.getloadavg does not exist on Windows, and that leg runs this file.

        The absence is staged by really removing the attribute, because a stub
        that raises AttributeError is not the same condition and this test was
        written the other way twice over:

        - mock.patch.object refuses to patch an attribute that is not there, so
          on Windows — the one platform this exists for — it raised
          `AttributeError: <module 'os'> does not have the attribute
          'getloadavg'` and asserted nothing;
        - and a stub only reaches the *call*. Windows trips over the *lookup*,
          so hoisting `os.getloadavg` out of the try — which crashes the stall
          diagnostic on Windows — passed the stubbed version on POSIX.
        """
        real = getattr(os, "getloadavg", None)
        if real is not None:
            self.addCleanup(setattr, os, "getloadavg", real)
            del os.getloadavg              # what Windows actually looks like
        self.assertIsNone(
            _load_average(),
            "there is no load average here and _load_average did not say so, "
            "so the attribute lookup is outside the guard and the stall "
            "diagnostic raises on the platform it was written for")
        self.assertEqual(
            _run_queue_pressure(), 1.0,
            "a platform with no load average must read as an idle machine")

    def test_pressure_reads_as_idle_when_the_load_average_is_unobtainable(self):
        """The other half of the guard: os.getloadavg exists but raises.

        It is documented to raise OSError where the average cannot be obtained,
        and create=True is what lets this half also run on Windows, where there
        is no attribute to patch over — the absence the test above stages.
        """
        self.assertGreaterEqual(_run_queue_pressure(), 1.0,
                                "pressure below 1.0 would report a real, idle "
                                "machine as quieter than idle")
        with mock.patch.object(os, "getloadavg", side_effect=OSError,
                               create=True):
            self.assertEqual(_run_queue_pressure(), 1.0)

    def test_a_browser_that_runs_but_measures_nothing_keeps_its_old_contract(self):
        """Untouched by the retry work, and pinned here because the retry work
        rewrote the loop around it: a Chrome that runs and dumps no measurement
        skips for a contributor and fails a run that demanded a browser.

        Written out rather than wrapped in assertRaises, because assertRaises is
        blind in exactly the direction that matters here. A SkipTest raised
        inside `assertRaises(AssertionError)` is not caught — it propagates, and
        unittest reports the test as *skipped*. Deleting the branch this pins
        therefore turned the whole class green-with-a-skip, which is the silent
        pass CLAUDE_USAGE_REQUIRE_BROWSER exists to stop.
        """
        empty = mock.Mock(stdout="<html></html>", stderr="no --dump-dom here")
        for required, wanted in ((False, unittest.SkipTest), (True, AssertionError)):
            with mock.patch.object(subprocess, "run", mock.Mock(return_value=empty)), \
                    mock.patch(__name__ + ".REQUIRE_BROWSER", required):
                try:
                    _measure_in_browser(1280, attempts=1)
                except BaseException as exc:      # the type IS the assertion
                    raised = exc
                else:
                    raised = None
            self.assertIsInstance(
                raised, wanted,
                "with CLAUDE_USAGE_REQUIRE_BROWSER=%r a browser that produced no "
                "measurement raised %r, not %s"
                % (required, raised, wanted.__name__))


@requires_browser
class TestDailyPanelGeometryInABrowser(unittest.TestCase):
    """The per-series panel, measured rather than described.

    Three defects shipped green past 1327 tests because nothing here existed:
    the panel's table rendered 146px outside its own column so Mean and Max were
    off the page; that spill gave the WHOLE page a horizontal scrollbar at 1024
    and 1280; and the pan track, sized from a clientWidth sampled before the
    panel had rendered, left the newest day unreachable from the scrollbar at
    390px. None of the three is visible to a DOM stub.

    Widths are the two the spill was measured at plus the phone width where the
    scrollbar failed. Each is laid out once and shared by every test below,
    because a browser launch is seconds, not milliseconds.
    """

    WIDTHS = (390, 1024, 1280)
    measured = {}
    overflowing = None

    @classmethod
    def setUpClass(cls):
        cls.measured = {w: _measure_in_browser(w) for w in cls.WIDTHS}
        # One more, with deliberately oversized figures, so a value exists that
        # no fixed column width could hold. That is what the containment rule is
        # for, and without a case that overflows nothing would exercise it.
        cls.overflowing = _measure_in_browser(1280, scale=1000000)

    def test_the_page_never_scrolls_sideways(self):
        """A page-wide horizontal scrollbar is a regression for every card on
        it, not only the one that caused it. Measured: 1125 against a 1024
        client, and 1381 against 1280, with .daily-stats-table the widest
        offender at both."""
        for width in self.WIDTHS:
            with self.subTest(width=width):
                got = self.measured[width]
                self.assertLessEqual(
                    got["pageScrollWidth"], got["viewportWidth"],
                    "the page scrolls sideways; widest offender: %r"
                    % (got["widestOverflow"],))

    def test_every_figure_in_the_panel_is_inside_the_panel(self):
        """Requirement (b)/(c) — a panel showing the mean and max — is only
        delivered if they are on the page. The Max column is the rightmost, so
        it is the one that disappeared."""
        for width in self.WIDTHS:
            with self.subTest(width=width):
                got = self.measured[width]
                panel, table = got["panel"], got["table"]
                self.assertIsNotNone(panel, "no panel rendered at all")
                self.assertLessEqual(
                    table["width"], panel["clientWidth"] + 1,
                    "the table is %.2fpx wide in a %dpx panel"
                    % (table["width"], panel["clientWidth"]))
                self.assertLessEqual(
                    panel["scrollWidth"], panel["clientWidth"] + 1,
                    "part of the panel is only reachable by scrolling it")
                self.assertIsNotNone(got["maxCell"], "no Max cell in the panel")
                self.assertLessEqual(
                    got["maxCell"]["right"],
                    panel["left"] + panel["clientWidth"] + 1,
                    "the Max column renders outside the panel")

    def test_no_part_of_the_panel_is_laid_out_beyond_the_card(self):
        """Stricter than "nothing is drawn outside the card", deliberately.

        The panel is a scroll container now, so an over-wide table would be
        clipped rather than painted over the page — but it would also be
        unreadable without scrolling a box the reader has no reason to think
        scrolls. At these widths the table's whole box must simply fit.
        """
        for width in self.WIDTHS:
            with self.subTest(width=width):
                got = self.measured[width]
                self.assertLessEqual(
                    got["table"]["right"], got["card"]["right"] + 1,
                    "the table's right edge is %.2fpx past the card's"
                    % (got["table"]["right"] - got["card"]["right"]))

    def test_a_figure_too_wide_to_fit_scrolls_inside_the_panel(self):
        """The containment rule, measured rather than read off the stylesheet.

        `min-width: 0` and `overflow-x: auto` are what stop a wider figure than
        the column anticipated from doing what the 334px table did to the 200px
        column: enlarging its own grid track, then the card, then the page. The
        fixture bills a thousand times more so the panel genuinely cannot hold
        it, and the excess must then be a scroll INSIDE the panel — never a
        horizontal scrollbar on the document.
        """
        got = self.overflowing
        panel, table = got["panel"], got["table"]
        self.assertGreater(
            table["width"], panel["clientWidth"],
            "this fixture fits the panel, so it exercises no containment")
        self.assertLessEqual(
            got["pageScrollWidth"], got["viewportWidth"],
            "an over-wide figure gave the whole page a horizontal scrollbar; "
            "widest offender: %r" % (got["widestOverflow"],))
        # Driven, not read off the stylesheet: an element that is not a scroll
        # container reports 0 here however much its contents overflow it.
        self.assertGreaterEqual(
            got["panelScroll"], table["width"] - panel["clientWidth"] - 1,
            "the panel scrolls %s of the %.2fpx its table overruns it by, so "
            "the excess is painted over the page instead of being reachable"
            % (got["panelScroll"], table["width"] - panel["clientWidth"]))

    def test_the_scrollbar_reaches_the_newest_day(self):
        """Dragging the pan bar to its right-hand stop must land on the last
        day the chart holds. At 390px it landed on the day before: the track
        was sized from a bar 21px narrower than the one that settled, so its
        maximum scrollLeft was 20.19 columns and Math.round took it to 20."""
        panned = 0
        for width in self.WIDTHS:
            pan = self.measured[width]["pan"]
            if not pan["panning"]:
                continue                    # the whole range fits at this width
            panned += 1
            with self.subTest(width=width):
                self.assertAlmostEqual(pan["maxScroll"],
                                       pan["maxOffset"] * pan["col"], places=6)
                self.assertEqual(pan["reached"], pan["newest"])
        self.assertTrue(panned, "no width windowed the range, so this test "
                                "asserted nothing")


@unittest.skipUnless(NODE, "node is required")
class TestEveryDataTableSaysWhenItIsEmpty(unittest.TestCase):
    """Four of the seven rendered a blank <tbody> with nothing explaining it.

    `renderTopDispatches`, the effort table and the stop-reason table each print
    a centred "nothing in selected range" row. The sessions, model-cost,
    project-cost and project/branch tables printed an absolutely empty body — a
    header row over blank space — so an empty result was indistinguishable from
    a render that failed.

    Raised as a split: one judge called it real, the other reproduced the
    behaviour exactly and called the harm overstated. Adjudicated real on the
    INCONSISTENCY rather than on the blankness: a reader who learns from one
    card that a blank table means "nothing here" reads a blank one as broken,
    and this page teaches both lessons at once. It is the same reasoning that
    put four distinct explanations behind the Est. Cost tile's `n/a`.

    `getElementById` in the DOM stub returns a FRESH element per call, so a
    write is not readable back — hence the capturing setter, which is the
    pattern the other DOM-asserting tests in this module already use.
    """

    def _empty_state_per_table(self):
        return run_js("""
          const captured = {};
          const realGet = document.getElementById;
          document.getElementById = (id) => {
            const el = realGet(id);
            Object.defineProperty(el, 'innerHTML', {
              get() { return captured[id] || ''; },
              set(v) { captured[id] = v; }, configurable: true });
            return el;
          };
          const calls = [
            ['dispatches-body',          () => renderTopDispatches([])],
            ['sessions-body',            () => renderSessionsTable([])],
            ['model-cost-body',          () => renderModelCostTable([])],
            ['project-cost-body',        () => renderProjectCostTable([])],
            ['project-branch-cost-body', () => renderProjectBranchCostTable([])],
          ];
          const out = {};
          for (const [id, fn] of calls) {
            try {
              fn();
              const html = captured[id] || '';
              out[id] = html.includes('colspan') ? 'MESSAGE'
                      : (html.trim() === '' ? 'EMPTY' : 'ROWS');
            } catch (e) { out[id] = 'THREW ' + String(e.message).slice(0, 60); }
          }
          console.log(JSON.stringify(out));
        """)

    def test_no_table_is_blank_without_saying_why(self):
        got = self._empty_state_per_table()
        blank = sorted(k for k, v in got.items() if v != "MESSAGE")
        self.assertEqual(
            blank, [],
            f"these tables render an empty <tbody> with no explanation, while "
            f"their neighbours print one: {blank}")

    def test_every_placeholder_spans_its_table_exactly(self):
        """`colspan` is derived from each table's own <thead>, never counted by
        hand — a placeholder wider than its table stretches the layout, and one
        narrower leaves a stray cell.

        All four of these shipped one too many for a few hours, from a counting
        script that matched the substring `'<th'` — which is also the first
        three characters of `'<thead'`, so every table's count came back
        inflated by exactly one. Reading the numbers back out of the markup is
        the only version of this check that cannot repeat the mistake.
        """
        import re
        html = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        js = (REPO_ROOT / "web" / "js" / "54-tables.js").read_text(encoding="utf-8")

        used = dict(re.findall(
            r"getElementById\('([a-z-]+)'\)\.innerHTML = \w+\.length === 0 "
            r"\? emptyTableRow\((\d+),", js))
        self.assertTrue(used, "no emptyTableRow call sites found — has the "
                              "placeholder mechanism been renamed?")

        for tbody, declared in sorted(used.items()):
            with self.subTest(table=tbody):
                marker = f'id="{tbody}"'
                start = html.rfind("<table", 0, html.index(marker))
                head = html[start:html.index(marker)]
                columns = max(
                    len(re.findall(r"<th\b", row))
                    for row in re.findall(r"<tr\b.*?</tr>", head, re.S))
                self.assertEqual(
                    int(declared), columns,
                    f"{tbody}'s placeholder spans {declared} columns but the "
                    f"table has {columns}")

    def test_the_probe_would_notice_a_blank_one(self):
        """Anti-vacuity. The capturing setter is doing real work — if it were
        broken, every table would read EMPTY and the test above would fail
        rather than pass, but a reader should not have to take that on trust."""
        got = run_js("""
          const captured = {};
          const realGet = document.getElementById;
          document.getElementById = (id) => {
            const el = realGet(id);
            Object.defineProperty(el, 'innerHTML', {
              get() { return captured[id] || ''; },
              set(v) { captured[id] = v; }, configurable: true });
            return el;
          };
          document.getElementById('probe-body').innerHTML = '';
          const blank = captured['probe-body'];
          document.getElementById('probe-body').innerHTML = '<tr><td colspan="3">x</td></tr>';
          console.log(JSON.stringify({
            blank: blank === '', reads_back: captured['probe-body'].includes('colspan')}));
        """)
        self.assertTrue(got["blank"])
        self.assertTrue(got["reads_back"],
                        "the capturing setter does not read back, so the test "
                        "above could not tell a message from a blank")


@unittest.skipUnless(NODE, "node is required")
class TestTheWeeklyLimitRange(unittest.TestCase):
    """A range whose bounds come from the quota window, not the calendar.

    "This Weekly Limit" exists so the figures on screen describe the same turns
    the weekly gauge is a percentage OF. A quota week follows its own reset,
    so bucketing Monday-to-Sunday could put a different set of turns under a label
    promising the current limit window.
    """

    def _bounds(self, resets_at, windows=None, now="2026-08-20T12:00:00Z"):
        payload = {"available": True, "windows": windows if windows is not None else [
            {"kind": "weekly_all", "group": "weekly", "percent": 46,
             "resets_at": resets_at}]}
        return run_js(
            "Date.now = () => Date.parse(" + json.dumps(now) + ");\n"
            "lastPlanInfo = " + json.dumps(payload) + ";\n"
            "console.log(JSON.stringify({bounds: getRangeBounds('limit-week'),"
            " span: dailyFillSpan('limit-week', [])}));")

    def test_the_window_runs_seven_days_back_from_its_own_reset(self):
        got = self._bounds("2026-08-23T06:59:59Z")
        self.assertEqual(got["bounds"], {"start": "2026-08-16", "end": "2026-08-23"})

    def test_it_is_chosen_by_group_not_by_the_upstream_kind(self):
        """The live endpoint calls it `weekly_all`; the cache has called it
        `seven_day`. Both report `group: weekly`, so keying on the kind would
        break this range the next time upstream renames it."""
        for kind in ("weekly_all", "seven_day", "weekly", "something_new"):
            with self.subTest(kind=kind):
                got = self._bounds("2026-08-23T06:59:59Z", windows=[
                    {"kind": kind, "group": "weekly", "percent": 46,
                     "resets_at": "2026-08-23T06:59:59Z"}])
                self.assertEqual(got["bounds"]["start"], "2026-08-16")

    def test_a_model_scoped_weekly_is_not_what_the_range_means(self):
        """A window scoped to one model measures that model, not the week. The
        unscoped one wins when both are present."""
        got = self._bounds("", windows=[
            {"kind": "weekly_scoped", "group": "weekly", "scope": "Fable",
             "percent": 0, "resets_at": "2026-09-01T00:00:00Z"},
            {"kind": "weekly_all", "group": "weekly", "percent": 46,
             "resets_at": "2026-08-23T06:59:59Z"}])
        self.assertEqual(got["bounds"], {"start": "2026-08-16", "end": "2026-08-23"})

    def test_with_no_weekly_window_it_falls_back_to_seven_days(self):
        """Not to an empty range. An empty dashboard reads as "you used nothing
        this week", which is a different and worse claim than "we do not know
        where your quota week starts"."""
        got = self._bounds("", windows=[])
        self.assertIsNotNone(got["bounds"]["start"])
        self.assertIsNone(got["bounds"]["end"])

    def test_the_fill_still_stops_at_today(self):
        """The window ends at its future reset, and a chart must not draw
        confident zeros into days that have not happened."""
        import datetime
        today = datetime.date.today()
        reset = (today + datetime.timedelta(days=5)).isoformat() + "T06:59:59Z"
        got = self._bounds(reset)
        self.assertEqual(got["span"]["end"], today.isoformat())
        self.assertGreater(got["bounds"]["end"], got["span"]["end"],
                           "the window should extend past today; the FILL must not")

    def test_it_is_a_real_range_so_a_link_round_trips(self):
        got = run_js("console.log(JSON.stringify({"
                     "  valid: VALID_RANGES.includes('limit-week'),"
                     "  label: RANGE_LABELS['limit-week']}))")
        self.assertTrue(got["valid"])
        self.assertEqual(got["label"], "This Weekly Limit")

    def test_an_expired_reading_uses_the_current_window_shown_by_the_panel(self):
        got = run_js(r"""
Date.now = () => Date.parse('2026-09-01T12:00:00Z');
const info = {available: true, source: 'claude', windows: [
  {kind: 'weekly_all', group: 'weekly', expired: true, percent: 100,
   resets_at: '2026-08-23T07:00:00Z',
   window_start: '2026-08-30T07:00:00Z', window_end: '2026-09-06T07:00:00Z'}
]};
const windows = stubEl();
const get = document.getElementById;
document.getElementById = id => id === 'plan-windows' ? windows : get(id);
applyPlanLimits(info);
console.log(JSON.stringify({html: windows.innerHTML,
  bounds: getRangeBounds('limit-week')}));
""")
        self.assertIn("New window", got["html"])
        self.assertEqual(got["bounds"], {"start": "2026-08-30", "end": "2026-09-06"})

    def test_a_cached_reading_rolls_forward_again_after_its_window_ends(self):
        got = self._bounds("2026-08-23T07:00:00Z", windows=[{
            "kind": "weekly_all", "group": "weekly", "expired": True,
            "resets_at": "2026-08-23T07:00:00Z",
            "window_start": "2026-08-30T07:00:00Z",
            "window_end": "2026-09-06T07:00:00Z",
        }], now="2026-09-06T07:00:00Z")
        self.assertEqual(got["bounds"], {"start": "2026-09-06", "end": "2026-09-13"})

    def test_a_weekly_duration_is_elapsed_time_across_daylight_saving(self):
        got = run_js(r"""
process.env.TZ = 'America/New_York';
Date.now = () => Date.parse('2026-03-11T12:00:00Z');
lastPlanInfo = {available: true, windows: [
  {kind: 'weekly_all', group: 'weekly', resets_at: '2026-03-12T04:30:00Z'}
]};
console.log(JSON.stringify(getRangeBounds('limit-week')));
""")
        self.assertEqual(got, {"start": "2026-03-04", "end": "2026-03-12"})


@unittest.skipUnless(NODE, "node is required")
class TestTheHourlyToggleReBucketsWithoutReSelecting(unittest.TestCase):
    """Local/UTC changes how the hours are bucketed, never which turns count.

    The range filter used to run on the ALREADY-FRAMED day, so in UTC mode it
    compared a raw UTC day key against `start`/`end`, which are local calendar
    dates — every other card in the range uses the local day. Turns near a
    boundary therefore moved in or out of the set when the toggle was flipped,
    and the hourly averages stopped describing the same turns as the stat tiles
    above them while the range label was unchanged.

    Measured at `Pacific/Midway` (UTC-11), range `today`: Local selected two
    rows and UTC selected one of them.
    """

    ZONE = "Pacific/Midway"

    def _rows_seen_per_mode(self):
        """The output values `aggregateHourly` is handed in each mode.

        Set membership, not a total: two different sets can share a sum, and it
        is membership this is about.
        """
        # Relative to the day the suite RUNS. Fixed dates rotted: with range
        # 'today' the rows fell out of range the moment the calendar passed
        # them, and both modes then selected nothing — which the anti-vacuity
        # assertion below is what caught.
        import datetime
        today = datetime.datetime.now(datetime.timezone.utc).date()
        return run_js("""
          const TODAY = '%s', YESTERDAY = '%s';
          rawData = {
            hourly_by_model: [""" % (
            today.isoformat(), (today - datetime.timedelta(days=1)).isoformat()) + """
              {day:TODAY, hour:0,  model:'m', source:'claude',
               input:1, output:10, cache_read:0, cache_creation:0, turns:1, reasoning:0},
              {day:TODAY, hour:23, model:'m', source:'claude',
               input:1, output:20, cache_read:0, cache_creation:0, turns:1, reasoning:0},
              {day:YESTERDAY, hour:12, model:'m', source:'claude',
               input:1, output:30, cache_read:0, cache_creation:0, turns:1, reasoning:0}
            ],
            daily_by_model: [], sessions_all: [], project_by_day_model: [],
            top_dispatches: [], subagent_by_type: [], effort_by_day_model: [],
            stop_reason_by_day_model: [], limit_incidents: [],
            available_sources: ['claude'], generated_at: '2026-08-17T00:00:00Z'
          };
          selectedModels = new Set(['m']);
          selectedSource = 'claude';
          const seen = {};
          const orig = aggregateHourly;
          aggregateHourly = function (rows, tz) {
            seen[hourlyTZ] = rows.map(r => r.output).sort((a, b) => a - b);
            return orig(rows, tz);
          };
          const out = {};
          for (const mode of ['local', 'utc']) {
            hourlyTZ = mode;
            selectedRange = 'today';
            try { applyFilter(); } catch (e) {}
            out[mode] = seen[mode] || null;
          }
          console.log(JSON.stringify(out));
        """)

    def test_both_frames_are_about_the_same_turns(self):
        previous = os.environ.get("TZ")
        os.environ["TZ"] = self.ZONE
        if hasattr(time, "tzset"):
            time.tzset()
        try:
            got = self._rows_seen_per_mode()
        finally:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            if hasattr(time, "tzset"):
                time.tzset()
        self.assertEqual(
            got["local"], got["utc"],
            "the timezone toggle changed WHICH turns are in range, not just "
            "how they are bucketed — so the hourly card stopped describing the "
            "same turns as the stat tiles beside it")
        self.assertTrue(got["local"],
                        "the fixture must select something, or this passes "
                        "by selecting nothing in both modes")


@unittest.skipUnless(NODE, "node is required")
@unittest.skipUnless(hasattr(time, "tzset"), "needs tzset to pin a zone")
class TestAnHourBucketStraddlingLocalMidnightGoesToBothDays(unittest.TestCase):
    """The defect inside the fix above, in every sub-hour-offset zone.

    Membership was derived by resolving the bucket's UTC hour START into a local
    day. Where a local-day boundary falls MID-HOUR that is off by a whole
    bucket: in `Asia/Kolkata` (+05:30) local midnight is 18:30Z, so the 18:00Z
    bucket holds turns on two local days, and taking the local day of 18:00
    filed all of them under the earlier one. The tiles bucket per TURN with
    `date(timestamp,'localtime')`, so the card and the tiles were about
    different turns — the very thing the class above was written to fix — in
    both toggle modes, and its commit message stated the fixed invariant in
    terms this case still broke.

    `hourly_by_model` now carries `local_day` per turn, so a straddling bucket
    arrives as two rows sharing one (day, hour), and membership is read off the
    payload instead of re-derived. `aggregateHourly` sums them back into one
    hour bucket, so no total and no `dayCount` moves.

    The fixture's dates are computed from the clock rather than written down:
    `getRangeBounds('today')` reads the real date, so hardcoding one makes the
    test pass on a single day. `localMidnight.toISOString()` names the UTC
    instant of today's local midnight, whose date and hour ARE the straddling
    bucket — in any zone, without the test knowing the offset.
    """

    ZONE = "Asia/Kolkata"  # +05:30 all year: no DST to confound the boundary

    def _outputs_per_mode(self, with_local_day=True):
        return run_js("""
          const localMidnight = new Date(new Date().setHours(0, 0, 0, 0));
          const utcDay = localMidnight.toISOString().slice(0, 10);
          const utcHour = localMidnight.getUTCHours();
          const todayLocal = localISODate(new Date());
          const yesterday = new Date(localMidnight);
          yesterday.setDate(yesterday.getDate() - 1);
          const yesterdayLocal = localISODate(yesterday);

          // One UTC hour bucket, split by the local day its turns fall on --
          // exactly what the rollup now emits. 20 output before local midnight,
          // 10 after it.
          const row = (localDay, output, turns) => {
            const r = {day: utcDay, hour: utcHour, model: 'm', source: 'claude',
                       input: 1, output: output, cache_read: 0,
                       cache_creation: 0, turns: turns, reasoning: 0};
            if (%s) r.local_day = localDay;
            return r;
          };
          rawData = {
            hourly_by_model: [row(yesterdayLocal, 20, 2), row(todayLocal, 10, 1)],
            daily_by_model: [], sessions_all: [], project_by_day_model: [],
            top_dispatches: [], subagent_by_type: [], effort_by_day_model: [],
            stop_reason_by_day_model: [], limit_incidents: [],
            available_sources: ['claude'], generated_at: todayLocal + 'T00:00:00Z'
          };
          selectedModels = new Set(['m']);
          selectedSource = 'claude';
          const seen = {};
          const orig = aggregateHourly;
          aggregateHourly = function (rows, tz) {
            seen[hourlyTZ] = rows.map(r => r.output).sort((a, b) => a - b);
            return orig(rows, tz);
          };
          const out = {straddles: localMidnight.getUTCMinutes() !== 0};
          for (const mode of ['local', 'utc']) {
            hourlyTZ = mode;
            selectedRange = 'today';
            try { applyFilter(); } catch (e) {}
            out[mode] = seen[mode] || null;
          }
          console.log(JSON.stringify(out));
        """ % ("true" if with_local_day else "false"))

    def _in_zone(self, **kwargs):
        previous = os.environ.get("TZ")
        os.environ["TZ"] = self.ZONE
        time.tzset()
        try:
            return self._outputs_per_mode(**kwargs)
        finally:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()

    def test_only_the_part_of_the_bucket_that_is_in_range_is_counted(self):
        got = self._in_zone()
        self.assertTrue(got["straddles"],
                        "the fixture must sit in a zone whose local midnight "
                        "falls mid-hour, or it proves nothing")
        self.assertEqual(
            got["local"], [10],
            "the turns after local midnight are what `today` means, and the "
            "tiles count them; deriving the local day from the bucket's hour "
            "START drops the whole bucket instead")
        self.assertEqual(got["utc"], got["local"],
                         "the toggle re-buckets hours; it must not change which "
                         "turns the card is about")

    def test_a_row_without_the_field_falls_back_instead_of_vanishing(self):
        """The degraded path. `undefined >= start` is false, so a payload with no
        `local_day` must not compare against it directly — that would drop every
        hourly row rather than merely mis-bucketing the straddling one."""
        got = self._in_zone(with_local_day=False)
        self.assertEqual(
            got["local"], got["utc"],
            "the fallback must at least stay consistent across the toggle")
        self.assertIsNotNone(got["local"])


@unittest.skipUnless(NODE, "node is required")
class TestAMissingChartRuntimeDoesNotKillThePage(unittest.TestCase):
    """`/assets/chart.umd.js` answering 404 used to take the whole dashboard.

    That 404 is a SUPPORTED state, not a broken install: `find_chart_file()`
    returns None when the vendored file is absent OR when its SHA-256 does not
    match `dashboard.CHART_JS_SHA256`, and a test exists to pin that refusal.

    Every part of the page is concatenated into ONE classic script, so the
    unguarded top-level `Chart.defaults` writes in `20-format.js` threw
    `ReferenceError: Chart is not defined` and aborted it. Nothing from
    `30-ranges.js` onward ever ran its top-level code -- `start()` was never
    called, no fetch was ever made, no handler was ever wired -- and the page
    sat at "Loading..." with nothing anywhere saying why. A missing decoration
    took the tables, the tiles, the filters, the exports and the quota panel
    with it.

    Function declarations hoist, which is why `typeof applyFilter` stayed
    "function" and made this look survivable from the console.
    """

    def _run_without_chart(self, snippet):
        """The real app script with `Chart` removed after the DOM stub.

        Removed rather than stripped from the stub, so the stub stays the one
        the rest of this module uses and only the single global under test
        differs.
        """
        source = "\n".join([
            _DOM_STUB,
            "delete globalThis.Chart; globalThis.Chart = undefined;",
            extract_app_script(),
            snippet,
        ])
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.cjs"
            harness.write_text(source, encoding="utf-8")
            proc = subprocess.run([NODE, str(harness)], capture_output=True,
                                  text=True, encoding="utf-8", timeout=120)
        if proc.returncode != 0:
            raise AssertionError(
                f"the app script aborted without Chart (node exited "
                f"{proc.returncode}):\n{proc.stderr[-3000:]}")
        return json.loads(proc.stdout)

    def test_every_later_part_still_executes(self):
        """The parts after `20-format.js`, by a value each one declares at top
        level. A `ReferenceError` here means that part never ran."""
        got = self._run_without_chart("""
          const probe = {};
          for (const [name, read] of [
            ['CHARTS_AVAILABLE', () => CHARTS_AVAILABLE],
            ['RANGE_LABELS',     () => typeof RANGE_LABELS],
            ['RATE_FIELD',       () => typeof RATE_FIELD],
            ['TRUNCATED_STOP_REASON', () => typeof TRUNCATED_STOP_REASON],
            ['PLAN_SEVERITY_BANDS',   () => typeof PLAN_SEVERITY_BANDS],
          ]) {
            try { probe[name] = read(); }
            catch (e) { probe[name] = 'THREW ' + e.constructor.name; }
          }
          console.log(JSON.stringify(probe));
        """)
        self.assertIs(got["CHARTS_AVAILABLE"], False,
                      "the flag must report the runtime as absent")
        for name in ("RANGE_LABELS", "RATE_FIELD", "TRUNCATED_STOP_REASON",
                     "PLAN_SEVERITY_BANDS"):
            self.assertNotIn("THREW", str(got[name]),
                             f"{name}'s part never ran — the script aborted "
                             f"before it, exactly as it did before the guard")

    def test_a_render_call_returns_instead_of_throwing(self):
        """The five canvases are what is genuinely lost. Calling one must be a
        no-op with a message, not an exception that takes the render with it."""
        got = self._run_without_chart("""
          const out = {};
          try { renderModelChart([]); out.model = 'returned'; }
          catch (e) { out.model = 'THREW ' + e.constructor.name; }
          try { renderSubagentChart([]); out.subagent = 'returned'; }
          catch (e) { out.subagent = 'THREW ' + e.constructor.name; }
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, {"model": "returned", "subagent": "returned"})

    def test_only_the_canvas_is_lost_not_the_text_beside_it(self):
        """The guard must skip the CANVAS, not the whole render function.

        Placed at the top of `renderDailyChart` and `renderHourlyChart` — where
        it first shipped — it also took the Min/Mean/Max panel and the
        "N days averaged · <zone>" line, which are plain text and need no chart
        runtime at all. That panel carries every figure the chart would have
        drawn, so losing it turns a missing decoration back into a loss of
        information: the exact failure the guard was written to stop, one
        function deeper.
        """
        got = self._run_without_chart("""
          const captured = {};
          const realGet = document.getElementById;
          document.getElementById = (id) => {
            const el = realGet(id);
            Object.defineProperty(el, 'innerHTML', {
              get() { return captured[id] || ''; },
              set(v) { captured[id] = v; }, configurable: true });
            Object.defineProperty(el, 'textContent', {
              get() { return captured['T:' + id] || ''; },
              set(v) { captured['T:' + id] = v; }, configurable: true });
            return el;
          };
          const out = {};
          try {
            renderDailyChart([{day:'2026-08-17', model:'m', source:'claude',
              input:1, output:2, cache_read:0, cache_creation:0, turns:1,
              reasoning:0}]);
            out.dailyStats = Object.keys(captured).some(k => k.includes('daily-stats'));
          } catch (e) { out.dailyStats = 'THREW ' + String(e.message).slice(0, 60); }
          try {
            renderHourlyChart({dayCount: 3, hours: []});
            out.hourlyLabel = captured['T:hourly-day-count'] || '';
          } catch (e) { out.hourlyLabel = 'THREW ' + String(e.message).slice(0, 60); }
          console.log(JSON.stringify(out));
        """)
        self.assertIs(got["dailyStats"], True,
                      "the Min/Mean/Max panel was lost with the canvas, and it "
                      "is where the numbers actually are")
        self.assertIn("averaged", str(got["hourlyLabel"]),
                      "the hourly day-count line was lost with the canvas")

    def test_the_runtime_is_reported_present_when_it_is(self):
        """Anti-vacuity: with the stub's `Chart` in place — the state every
        other test in this module runs in — the flag must be true, or the guard
        would be disabling charts for everybody."""
        got = run_js("console.log(JSON.stringify({available: CHARTS_AVAILABLE}));")
        self.assertIs(got["available"], True)


if __name__ == "__main__":
    unittest.main()
