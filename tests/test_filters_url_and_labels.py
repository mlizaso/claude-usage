"""Three rules `web/js/40-filters.js` states in comments and nothing asserted.

Each class here exists because a mutation that breaks the rule left the whole
suite green — the rule was written down, believed, and unguarded:

* Source choice must survive shareable URLs. Session counts and totals must be
derived from rows matching the active date range.

All three drive the page's real code through `tests.test_dashboard_js`'s node
harness, the same way `tests/test_frontend_data_path.py` and
`tests/test_effort_frontend.py` do, so they measure what the browser would do
rather than a Python restatement of it.
"""

import json
import re
import unittest

from pricing import PRICING, calc_cost

from tests.test_dashboard_js import emit, fmt_money, requires_node, run_js
from tests.timestamps import local_day

OPUS = "claude-opus-4-8"      # $5 / $25 per M in / out
OPUS_TWIN = "claude-opus-5"   # a DIFFERENT id at the IDENTICAL published rates
HAIKU = "claude-haiku-4-5"    # $1 / $5 — exactly 5x cheaper than opus


# ───────────────────────────────────────────────────────────────────────────
# The source chooser survives a reload the same way for both assistants.
# ───────────────────────────────────────────────────────────────────────────

BOTH_SOURCES = [{"source": "claude", "turns": 10},
                {"source": "codex", "turns": 20}]

# Boots the real `start()` against a real `history` object — one that stores the
# state `replaceState` is given and hands it back through `history.state`, which
# is the whole mechanism under test. A "reload" is a second boot handed the
# search string AND the history state the first one left behind; a link opened
# in a fresh tab is a second boot handed the search string and no state.
_BOOT = """
(async () => {
  const written = [];
  let state = INCOMING_STATE;
  globalThis.history = {
    get state() { return state; },
    replaceState: (s, t, url) => { state = s; written.push(url); },
  };
  window.location.search = INCOMING_SEARCH;
  window.location.pathname = '/';
  apiFetch = async (path) => {
    if (path === '/api/scan-status') return { ok: true, status: 200,
      json: async () => ({ state: 'idle', generation: 0 }) };
    if (path === '/api/sources') return { ok: true,
      json: async () => ({ sources: SOURCES_JSON }) };
    return { ok: true, json: async () => ({ generated_at: 'x',
      all_models: [], daily_by_model: [], sessions_all: [] }) };
  };
  let chooserShown = false;
  document.getElementById = (id) => ({
    set innerHTML(v) {}, set textContent(v) {},
    set hidden(v) { if (id === 'source-chooser' && v === false) chooserShown = true; },
    dataset: {}, title: '', classList: { toggle: () => {}, add: () => {},
      remove: () => {}, contains: () => false },
    setAttribute: () => {}, removeAttribute: () => {},
    getContext: () => ({}), offsetHeight: 0, offsetWidth: 0,
    parentElement: { clientWidth: 900 }, style: { setProperty: () => {} },
    querySelectorAll: () => [], querySelector: () => null,
    closest: () => null, addEventListener: () => {},
    getBoundingClientRect: () => ({ top: 0, height: 0 }) });
  document.querySelectorAll = () => [];
  renderStats = () => {}; scheduleAutoRefresh = () => {};
  startPlanLimitsPoll = () => {};
  await start();
  await new Promise(r => setTimeout(r, 0));
  BODY
  await new Promise(r => setTimeout(r, 0));
  console.log(JSON.stringify({ chooserShown, source: selectedSource,
    written, state, url: written.length ? written[written.length - 1] : null }));
})();
"""


@requires_node
class TestChoosingASourceIsSymmetricAcrossAReload(unittest.TestCase):
    """Choose an assistant, reload: both answers must behave the same way.

    `start()`'s comment states the rule — "the chooser is the entry view — every
    time, not just the first … the only thing that skips it now is a link that
    names a source explicitly, which is an instruction from whoever made the
    link rather than a preference inferred from a past visit". A URL the page
    wrote for itself through `history.replaceState` is not such a link, and it
    used to be read back as one: choosing Codex wrote `?source=codex` and every
    later reload went straight there, while choosing Claude wrote nothing and
    every later reload put the dialog back. Two identical user actions, opposite
    results, decided by an omit-at-default rule that has nothing to do with the
    chooser.

    Nothing covered the composition: one test pins that `?source=codex` is
    written, another that a URL naming a source skips the dialog, and no test
    fed the first one's output into the second.
    """

    def boot(self, search="", state=None, body=""):
        script = (_BOOT
                  .replace("SOURCES_JSON", json.dumps(BOTH_SOURCES))
                  .replace("INCOMING_SEARCH", json.dumps(search))
                  .replace("INCOMING_STATE", json.dumps(state))
                  .replace("BODY", body))
        return run_js(script)

    def choose_then_reload(self, source):
        """Answer the dialog with `source`, then reload what the page wrote."""
        chosen = self.boot(body="await chooseSource(%s);" % json.dumps(source))
        self.assertEqual(chosen["source"], source)
        url = chosen["url"] or "/"
        search = "?" + url.split("?", 1)[1] if "?" in url else ""
        return chosen, self.boot(search=search, state=chosen["state"])

    def test_the_dialog_is_the_entry_view_before_anything_is_chosen(self):
        """The guard on everything below: without this the reload assertions
        would pass against a page that never asks at all."""
        first = self.boot()
        self.assertTrue(first["chooserShown"])
        self.assertEqual(first["written"], [],
                         "nothing may be written before the reader answers")

    def test_a_reload_asks_again_whichever_source_was_chosen(self):
        asked = {}
        for source in ("claude", "codex"):
            with self.subTest(source=source):
                _, reloaded = self.choose_then_reload(source)
                asked[source] = reloaded["chooserShown"]
                self.assertTrue(
                    reloaded["chooserShown"],
                    "choosing %s and reloading answered the question on the "
                    "reader's behalf" % source)
        self.assertEqual(asked["claude"], asked["codex"],
                         "the two answers behave differently on reload")

    def test_a_link_naming_a_source_still_instructs_for_both(self):
        """The one thing that legitimately skips the dialog. A link arrives in a
        fresh history entry, so it carries no state of this page's making."""
        for source in ("claude", "codex"):
            with self.subTest(source=source):
                got = self.boot(search="?source=" + source)
                self.assertFalse(got["chooserShown"],
                                 "an explicit link was ignored")
                self.assertEqual(got["source"], source)

    def test_an_authored_link_survives_the_first_filter_click_for_both(self):
        """`?source=claude` used to be stripped from the address bar by the
        first `updateURL()` while `?source=codex` survived, so a link copied out
        of the bar afterwards reproduced the sender's view for one assistant and
        showed the dialog for the other."""
        for source in ("claude", "codex"):
            with self.subTest(source=source):
                got = self.boot(search="?source=" + source,
                                body="setRange('7d');")
                self.assertIsNotNone(got["url"], "nothing was written")
                self.assertIn("source=" + source, got["url"])
                self.assertIn("range=7d", got["url"])

    def test_with_one_assistant_the_source_stays_out_of_the_url(self):
        """Nothing to choose between, so `source` is a genuine default and stays
        implicit like `range` and `models` — a link off a Claude-only machine
        keeps the short form it has always had."""
        script = (_BOOT
                  .replace("SOURCES_JSON",
                           json.dumps([{"source": "claude", "turns": 10}]))
                  .replace("INCOMING_SEARCH", '""')
                  .replace("INCOMING_STATE", "null")
                  .replace("BODY", "setRange('7d');"))
        got = run_js(script)
        self.assertFalse(got["chooserShown"])
        self.assertIn("range=7d", got["url"])
        self.assertNotIn("source=", got["url"])


# ───────────────────────────────────────────────────────────────────────────
# The Sessions table's Model column names the session's top model.
# ───────────────────────────────────────────────────────────────────────────

# One session, four (day, model) rows, in the order `sessions_all` ships them
# (ORDER BY session_id, day, model). Opus does the most work in total and the
# LEAST in any single row, which is the only shape that separates "sum per
# model, then pick" from "whichever row came last wins" — the fixture in
# tests/test_sessions_range_attribution.py gives every day its own model, so
# there the two rules are arithmetically identical.
_SESSION_DAYS = [
    (local_day(3), OPUS,  (300_000, 40_000, 50_000, 10_000)),
    (local_day(2), OPUS,  (300_000, 40_000, 50_000, 10_000)),
    (local_day(1), OPUS,  (300_000, 40_000, 50_000, 10_000)),
    (local_day(0), HAIKU, (700_000, 100_000, 80_000, 20_000)),
]


def _day_row(day, model, tokens):
    inp, out, cache_read, cache_creation = tokens
    return {"day": day, "model": model, "input": inp, "output": out,
            "cache_read": cache_read, "cache_creation": cache_creation,
            "cache_creation_1h": 0, "turns": 1}


def _session_payload():
    rows = [_day_row(*d) for d in _SESSION_DAYS]
    by_model = {}
    for row in rows:
        acc = by_model.setdefault(row["model"], {"model": row["model"],
                                                 "input": 0, "output": 0,
                                                 "cache_read": 0,
                                                 "cache_creation": 0,
                                                 "cache_creation_1h": 0,
                                                 "turns": 0})
        for k in ("input", "output", "cache_read", "cache_creation",
                  "cache_creation_1h", "turns"):
            acc[k] += row[k]
    session = {
        "session_id": "s-one-model-many-days", "source": "claude",
        "project": "myproj", "branch": "main", "topic": "long haul",
        "last": local_day(0) + " 11:00", "last_date": local_day(0),
        "duration_min": 4320.0, "model": OPUS,
        "turns": sum(r["turns"] for r in rows),
        "input": sum(r["input"] for r in rows),
        "output": sum(r["output"] for r in rows),
        "cache_read": sum(r["cache_read"] for r in rows),
        "cache_creation": sum(r["cache_creation"] for r in rows),
        "cache_creation_1h": 0,
        "by_day_model": rows,
        "by_model": list(by_model.values()),
    }
    daily = [dict(row, source="claude", reasoning=0) for row in rows]
    return {"generated_at": "x", "all_models": [OPUS, HAIKU],
            "daily_by_model": daily, "sessions_all": [session]}


_DRIVE_SESSIONS = """(() => {
  rawData = payload;
  selectedSource = 'claude';
  selectedModels = new Set(models);
  selectedRange = 'all';
  const html = {};
  const els = new Map();
  document.getElementById = (id) => {
    if (!els.has(id)) {
      const el = stubEl();
      Object.defineProperty(el, 'innerHTML', {
        get() { return html[id] === undefined ? '' : html[id]; },
        set(v) { html[id] = v; },
      });
      els.set(id, el);
    }
    return els.get(id);
  };
  renderStats = () => {};
  applyFilter();
  let csv = null;
  downloadCSV = (name, header, rows) => { csv = { header: header, rows: rows }; };
  exportSessionsCSV();
  return {
    sessions: lastFilteredSessions.map(s => ({
      session_id: s.session_id, model: s.model, cost: s.cost, turns: s.turns,
      input: s.input, output: s.output })),
    csv: csv,
    html: html,
  };
})()"""


@requires_node
class TestTheSessionModelIsTheTopModelNotTheLastRow(unittest.TestCase):
    """`sessionFromParts` sums tokens per model before naming one.

    Accumulate every row per model before choosing a session label.
    Assignment would let the last row win; a multi-row synthetic fixture
    distinguishes it from the model with the largest total.

    The label is display-only — the cost is summed per row, at each row's own
    model, before any of this — so the assertions below deliberately pin the
    money as unchanged too, rather than letting a later reader mistake this for
    a costing test.
    """

    @classmethod
    def setUpClass(cls):
        cls.payload = _session_payload()

    def drive(self, models):
        return run_js(emit(_DRIVE_SESSIONS, payload=self.payload,
                           models=models))

    def test_the_fixture_would_not_notice_the_defect_if_it_were_narrowed(self):
        """The guard on the two tests below. Narrowing to one model, or to one
        day, collapses the session to a single row per model and the two rules
        become the same rule — a fixture that did either would look like
        protection while providing none."""
        rows = self.payload["sessions_all"][0]["by_day_model"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[-1]["model"], HAIKU,
                         "the last row must be the model that must NOT win")
        opus_rows = [r for r in rows if r["model"] == OPUS]
        self.assertEqual(len(opus_rows), 3, "opus must span several rows")

        def tokens(r):
            return r["input"] + r["output"] + r["cache_read"] + r["cache_creation"]

        self.assertGreater(sum(tokens(r) for r in opus_rows),
                           tokens(rows[-1]), "opus must win on the total")
        self.assertLess(max(tokens(r) for r in opus_rows), tokens(rows[-1]),
                        "and must lose on every single row")

    def test_the_model_column_names_the_model_that_did_the_most_work(self):
        got = self.drive([OPUS, HAIKU])
        self.assertEqual(len(got["sessions"]), 1)
        self.assertEqual(got["sessions"][0]["model"], OPUS)
        self.assertIn('<span class="model-tag">%s</span>' % OPUS,
                      got["html"]["sessions-body"])
        self.assertNotIn(HAIKU, got["html"]["sessions-body"])

    def test_the_exported_model_column_agrees_with_the_table(self):
        """The CSV is the label's other consumer and is written from the same
        array, so a fix applied to one alone would show up here."""
        got = self.drive([OPUS, HAIKU])
        column = got["csv"]["header"].index("Model")
        self.assertEqual([row[column] for row in got["csv"]["rows"]], [OPUS])

    def test_the_money_does_not_move_with_the_label(self):
        """Cost is summed per row at that row's own model before the label is
        chosen. Stated here so a later reader does not take the two tests above
        for a costing assertion — the defect they guard is display-only."""
        got = self.drive([OPUS, HAIKU])
        honest = sum(calc_cost(model, *tokens, 0)
                     for _day, model, tokens in _SESSION_DAYS)
        self.assertAlmostEqual(got["sessions"][0]["cost"], honest, places=6)
        column = got["csv"]["header"].index("Est. Cost")
        self.assertAlmostEqual(float(got["csv"]["rows"][0][column]), honest,
                               places=4)


# ───────────────────────────────────────────────────────────────────────────
# A totals row that blends published rates says `avg`.
# ───────────────────────────────────────────────────────────────────────────

_EFFORT_TOKENS = (1_000_000, 200_000, 500_000, 100_000)

# Each (effort, model) pair is ONE row, so every level is a single model at its
# published rate and no body row is a blend. The only place two rates meet is
# the totals row `mergeCostBuckets` builds — which is exactly why no per-row
# test can see the union being lost.
#
# The trailing weight is what makes the blend 2:3 rather than 1:1, so the
# derived rates ($2.60/M, $13.00/M, $0.26/M) are on nobody's price list —
# `test_the_marked_rate_really_is_on_no_price_list` asserts that against
# PRICING. An even split lands on $3.00/M, which is sonnet's published input
# rate, and a test whose "unfindable" figure is findable proves the wrong thing.
_EFFORT_ROWS = [
    ("high", OPUS, 2), ("low", HAIKU, 3),
    ("high", OPUS_TWIN, 2), ("low", OPUS_TWIN, 3),
]

# Every unit price the page could send a reader looking for.
_PUBLISHED_RATES = {round(float(rate), 6)
                    for model in PRICING.values() for rate in model.values()}

_EFFORT_COLUMNS = ("effort", "turns", "input", "output", "cache_read",
                   "cache_creation", "reasoning", "cost")

# The three columns whose `avg` marker can come only from the rate-set union.
# `cache_creation` is deliberately excluded: `effortCostCell` ORs in
# `mixedTiers(bucket)` for that one column, so it can stay marked with the union
# gone — which is precisely why the existing totals-row test in
# tests/test_effort_frontend.py cannot fail.
_UNION_ONLY_COLUMNS = ("input", "output", "cache_read")


def _effort_payload():
    day = local_day(0)
    rows = []
    for effort, model, weight in _EFFORT_ROWS:
        inp, out, cache_read, cache_creation = (t * weight
                                                for t in _EFFORT_TOKENS)
        rows.append({"day": day, "source": "claude", "model": model,
                     "effort": effort, "input": inp, "output": out,
                     "cache_read": cache_read, "cache_creation": cache_creation,
                     "cache_creation_1h": 0, "reasoning": 0, "turns": 1})
    # The same turns grouped by model instead of by effort, so the two cards on
    # screen would agree — the effort table is only honest beside one that does.
    daily = [{k: row[k] for k in row if k != "effort"} for row in rows]
    return {"generated_at": "x", "all_models": [OPUS, HAIKU, OPUS_TWIN],
            "daily_by_model": daily, "sessions_all": [],
            "effort_by_day_model": rows}


_DRIVE_EFFORT = """(() => {
  rawData = payload;
  selectedSource = 'claude';
  selectedModels = new Set(models);
  selectedRange = 'all';
  const html = {};
  const els = new Map();
  document.getElementById = (id) => {
    if (!els.has(id)) {
      const el = stubEl();
      Object.defineProperty(el, 'innerHTML', {
        get() { return html[id] === undefined ? '' : html[id]; },
        set(v) { html[id] = v; },
      });
      els.set(id, el);
    }
    return els.get(id);
  };
  renderStats = () => {};
  applyFilter();
  return {
    levels: lastByEffort.map(e => ({ effort: e.effort, cost: e.cost,
      parts: Object.assign({}, e.parts) })),
    html: html,
  };
})()"""


def _cells(markup):
    return re.findall(r"<td\b[^>]*>(.*?)</td>", markup, re.S)


@requires_node
class TestTheEffortTotalsRowMarksARateNoPriceListCarries(unittest.TestCase):
    """The `avg` suffix on the "Cost by reasoning effort" totals row.

    `mergeCostBuckets` unions each level's contributing rate sets, and its own
    comment says that union "is what decides the 'avg' marker". Deleting the
    union line left every totals-row rate set empty, so `blendedRate` answered
    false and the row printed a derived per-million figure — one that is on no
    price list — as if it were a published one. The full suite stayed green:
    the only existing totals-row assertion is on `cache_creation`, the single
    column `effortCostCell` can mark from `mixedTiers` instead.

    `renderEffortCostTotals` is `mergeCostBuckets`'s only production caller, so
    this row is the whole surface. Cost by Model's totals row looks the same and
    is not this code — it counts its own contributors.
    """

    @classmethod
    def setUpClass(cls):
        cls.payload = _effort_payload()

    def drive(self, models):
        return run_js(emit(_DRIVE_EFFORT, payload=self.payload, models=models))

    def totals_cells(self, models):
        got = self.drive(models)
        cells = _cells(got["html"]["effort-cost-total"])
        self.assertEqual(len(cells), len(_EFFORT_COLUMNS),
                         "the totals row changed shape: %r" % (cells,))
        return got, cells

    def cell(self, cells, column):
        return cells[_EFFORT_COLUMNS.index(column)]

    def test_no_single_level_is_itself_a_blend(self):
        """The guard that makes this a totals-row test. If a body row were
        already mixed-model the marker could arrive without the union ever
        being read, and the assertions below would pass against the defect."""
        got = self.drive([OPUS, HAIKU])
        body = got["html"]["effort-cost-body"]
        self.assertNotIn("avg", body, "a level is a blend; the fixture is blunt")
        self.assertEqual(len(got["levels"]), 2)

    def test_a_totals_row_fed_by_two_price_lists_says_avg(self):
        _got, cells = self.totals_cells([OPUS, HAIKU])
        for column in _UNION_ONLY_COLUMNS:
            with self.subTest(column=column):
                self.assertIn("avg", self.cell(cells, column),
                              "a derived rate is passing itself off as a "
                              "published one")

    def test_the_marked_rate_really_is_on_no_price_list(self):
        """The marker matters because the number cannot be looked up: opus and
        haiku bill input at $5.00/M and $1.00/M, the row is fed 2 parts of one
        and 3 of the other, and the $2.60/M it prints belongs to no model."""
        _got, cells = self.totals_cells([OPUS, HAIKU])
        for column in _UNION_ONLY_COLUMNS:
            shown = re.search(r"&times; \$([0-9.]+)/M", self.cell(cells, column))
            with self.subTest(column=column):
                self.assertIsNotNone(shown, self.cell(cells, column))
                self.assertNotIn(round(float(shown.group(1)), 6),
                                 _PUBLISHED_RATES,
                                 "the fixture's blend is a published rate, so "
                                 "an unmarked cell would be findable after all")
        self.assertIn("&times; $2.60/M avg", self.cell(cells, "input"))

    def test_two_ids_at_the_same_published_rate_are_not_an_average(self):
        """The control that keeps the marker meaningful, and the one this repo
        has already been bitten by: PRICING lists the opus models as separate
        literals holding identical numbers, so a totals row fed by two of them
        prints the published $5.00/M and must not call it an average."""
        _got, cells = self.totals_cells([OPUS, OPUS_TWIN])
        for column in _UNION_ONLY_COLUMNS:
            with self.subTest(column=column):
                self.assertNotIn("avg", self.cell(cells, column))
        self.assertIn("&times; $5.00/M</span>", self.cell(cells, "input"))

    def test_the_totals_row_prints_the_sum_of_the_levels_it_totals(self):
        """The same loop carries the money. Dropping `total.parts[k]` prints
        $0.0000 in all four column cells beside a correct grand total, and that
        survives the suite too — so the row's money is asserted here, not only
        its labelling."""
        got, cells = self.totals_cells([OPUS, HAIKU])
        for column in _UNION_ONLY_COLUMNS + ("cache_creation",):
            expected = sum(level["parts"][column] for level in got["levels"])
            with self.subTest(column=column):
                self.assertIn("= " + fmt_money(expected),
                              self.cell(cells, column))
        grand = sum(level["cost"] for level in got["levels"])
        self.assertIn(fmt_money(grand), self.cell(cells, "cost"))
