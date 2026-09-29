"""The front-end data path: the filter chain, the cost tables, the formatters.

Everything here drives the page's own `applyFilter` and renderers under node,
through `tests/test_dashboard_js`'s harness, against a payload the real scanner
built from real transcripts — the same mechanism `tests/test_effort_frontend.py`
and `tests/test_project_cost_attribution.py` use. Nothing is re-implemented in
Python except the deliberately dumb mirrors that say what the answer should be;
a mirror sharing code with the thing it checks could not detect it being wrong.

The properties covered here are the ones nothing else in the suite could see.
(No count: the list below is the count — a prose tally beside a literal list is
the drift this campaign already had to fix once, in `20-format.js`.)

* **A cost sort must order by the number the column prints.** `sortSessions`
  re-derived its key with `calcCost(row.model, ...)` — one model for a row that
  may hold several — which is the "anything holding one model per row cannot be
  priced" error AGENTS.md forbids, reintroduced inside a comparator. No test
  referenced `sortSessions` or `sessionSortCol` at all.

* **`n/a` and `$0.00` are different claims.** `$0.00` asserts the usage was
  free; a model the tool deliberately leaves unpriced (gemma, glm, an ollama
  proxy) was never priced at all. Three cost cells printed `$0.0000` for one
  while every other cell on the same screen said `n/a` — and the CSV export of
  the same row left the field blank, so a table disagreed with its own export.

* **`inSource` gates every filter.** AGENTS.md calls one source on screen a
  correctness rule, not a UI preference. Six of the eight gates could be deleted
  one at a time with the whole JS suite green. Every assertion below is made
  with EVERY model selected, because selecting one source's models happens to
  exclude the other's — which is how a deleted source filter passes a totals
  assertion. AGENTS.md names that trap explicitly.

* The response-ending card must handle an empty max_tokens bucket and a
populated one, with correct shares.

* **The "avg" marker on a derived rate.** Forcing `blendedRate` to `return
  false` left the suite green. An unlabelled per-million figure that is on no
  price list invites the reader to go looking for it, where it is not.

* **...and the marker's other direction.** `blendedRate` counted the rate
  OBJECTS `getPricing` returns, and `PRICING` lists five Opus ids as separate
  literals holding identical numbers — so a bucket fed by two of them printed
  the published `$5.00/M avg`. The test above could not see it: opus beside
  haiku renders `avg` either way.

* **An empty view is not an unpriced one.** `totals.billable` is vacuously
  false when the range holds no turns or the model filter is empty, and the
  Est. Cost tile read that one flag — so it asserted "No published per-token
  rate for these models" about models it had priced one selection earlier.

* **The model filter's read side.** The page writes a `models=` parameter that
  nothing proved it ever reads back, and the documented start-up rule
  ("everything priced, or everything when nothing is priced") was unguarded
  because no fixture had an unpriced model in `all_models` at boot.

* **Every number goes through the pinned formatter.** The two project tables
  interpolated their session count raw, so a busy project read `1234` in the
  table beside `1,234` in the tile above it.

* Dispatch filtering belongs before ranking. A global cap can discard all
entries relevant to a narrow source, model or date selection.

* **...and it is range-scoped per DAY, not by its start day.** It used to be the
  latter, which dropped a dispatch that outlived its start day out of every
  later range and charged that one day for its whole life — 4.0x on the fixture
  below, and the same defect AGENTS.md records for `sessions_all`. The last
  class in this file was a landmine asserting that wrong answer; the assertions
  are flipped, and the class name is kept so the history stays findable.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path

import db
import rollups
import scanner
from dashboard import get_dashboard_data
from pricing import calc_cost

from tests.test_dashboard_js import (REPO_ROOT, emit, fmt_money, requires_node,
                                     run_js)
from tests.timestamps import local_day_of, utc_ts_on_local_day

OPUS = "claude-opus-4-8"      # $5 / $25 per M in / out
HAIKU = "claude-haiku-4-5"    # $1 / $5   — exactly 5x cheaper than opus
SONNET = "claude-sonnet-4-6"  # $3 / $15
LOCAL = "gemma-3-27b-local"   # on no price list, deliberately — see AGENTS.md

SOL = "gpt-5.6-sol"    # $4.00 / $20.00 promotional rate
LUNA = "gpt-5.6-luna"  # $0.20 /  $1.20

CODEX_THREAD = "019fcf22-c0de-4dea-9e5f-0d0e0d0e0d01"
CODEX_SUB_THREAD = "019fcf22-c0de-4dea-9e5f-0d0e0d0e0d02"


def _assistant(session_id, model, ts, message_id, inp=0, out=0, cache_read=0,
               cache_creation=0, effort=None, stop_reason=None,
               cwd="/home/u/projA", branch="main", agent_id=None):
    """One Claude Code assistant record.

    `effort` sits at the TOP LEVEL and `stop_reason` inside `message` — the
    asymmetry the real transcripts carry. `agent_id` makes the record a
    dispatched subagent's turn: `isSidechain` plus an `agentId` are the two
    things the scanner reads for that, and both are what a real subagent
    transcript carries.
    """
    message = {
        "id": message_id, "model": model, "content": [],
        "usage": {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        },
    }
    if stop_reason is not None:
        message["stop_reason"] = stop_reason
    record = {
        "type": "assistant", "sessionId": session_id, "timestamp": ts,
        "cwd": cwd, "gitBranch": branch, "message": message,
    }
    if effort is not None:
        record["effort"] = effort
    if agent_id is not None:
        record["isSidechain"] = True
        record["agentId"] = agent_id
    return json.dumps(record)


def _dispatch_result(session_id, ts, agent_id, agent_type):
    """The parent's tool_result record, which names the subagent's type."""
    return json.dumps({
        "type": "user", "sessionId": session_id, "timestamp": ts,
        "cwd": "/home/u/projA",
        "toolUseResult": {"agentId": agent_id, "agentType": agent_type,
                          "status": "completed", "totalDurationMs": 1000,
                          "totalToolUseCount": 2},
    })


# ── The Claude corpus ──────────────────────────────────────────────────────
# Shaped for four separate jobs, each needing something the others do not, so
# the shape is stated here rather than left to be reverse-engineered:
#
#  * `s-mixed` uses TWO models and the CHEAP one carries four times the tokens,
#    so its true cost ($13.50) and the cost of pricing it at its top-token model
#    ($7.50) fall on opposite sides of `s-sonnet` ($9.00). That ordering is the
#    only thing that can catch a cost sort keyed on the wrong number.
#  * `projC` uses ONLY an unpriced model, so it is a project that has to read
#    "n/a" rather than "$0.0000".
#  * `max_tokens` appears on exactly one turn and only on HAIKU, so a filter
#    that excludes HAIKU is a range with no truncation in it — the state the
#    card's "None in this range." copy has to be right about.
#  * one record carries `stop_sequence` on a `<synthetic>` all-zero-usage
#    message, which is the only shape the real transcripts ever write it in, so
#    the card can be checked for a bucket that must never appear.
#  * one turn is a dispatched subagent, so `subagent_by_type` and
#    `top_dispatches` are non-empty for this source.
_SESS_MAIN = [
    _assistant("s-mixed", OPUS, utc_ts_on_local_day(0, 9), "m-opus",
               inp=1_000_000, out=100_000, effort="high", stop_reason="end_turn"),
    _assistant("s-mixed", HAIKU, utc_ts_on_local_day(0, 10), "m-haiku",
               inp=4_000_000, out=400_000, effort="high", stop_reason="end_turn"),
    # A stop reason that must never become a turn. Timed inside `s-mixed`'s
    # existing span so it moves no session bound, and left all-zero because that
    # is what the real records are.
    _assistant("s-mixed", "<synthetic>", utc_ts_on_local_day(0, 9, 30),
               "m-synthetic", stop_reason="stop_sequence"),
    _dispatch_result("s-mixed", utc_ts_on_local_day(0, 10, 30), "agent-1",
                     "Explore"),
    _assistant("s-sonnet", SONNET, utc_ts_on_local_day(0, 11), "m-sonnet",
               inp=2_000_000, out=200_000, effort="low", stop_reason="end_turn",
               cwd="/home/u/projB", branch="feature"),
    _assistant("s-local", LOCAL, utc_ts_on_local_day(0, 12), "m-local",
               inp=500_000, out=50_000, stop_reason="tool_use",
               cwd="/home/u/projC"),
]

# The dispatched subagent's own transcript, as Claude Code writes it.
_SESS_SUB = [
    _assistant("s-sub", HAIKU, utc_ts_on_local_day(0, 10, 15), "m-sub",
               inp=100_000, out=10_000, effort="high", stop_reason="max_tokens",
               agent_id="agent-1"),
]

# What the two sessions the cost sort turns on really cost, per model.
COST_MIXED = (calc_cost(OPUS, 1_000_000, 100_000, 0, 0)
              + calc_cost(HAIKU, 4_000_000, 400_000, 0, 0))          # $13.50
COST_SONNET = calc_cost(SONNET, 2_000_000, 200_000, 0, 0)            # $9.00
# And what pricing `s-mixed` at its single top-token model would have given.
COST_MIXED_AT_TOP_MODEL = calc_cost(HAIKU, 5_000_000, 500_000, 0, 0)  # $7.50


def _codex(rtype, payload, ts):
    return json.dumps({"timestamp": ts, "type": rtype, "payload": payload})


def _codex_meta(thread, ts, thread_source="user"):
    return _codex("session_meta", {
        "id": thread, "session_id": thread, "cwd": "/home/u/codexproj",
        "originator": "codex_vscode", "cli_version": "0.146.0",
        "source": "vscode", "thread_source": thread_source,
        "model_provider": "openai",
        "git": {"commit_hash": "abc123", "branch": "main",
                "repository_url": "git@example:me/codexproj.git"},
    }, ts)


def _codex_context(model, effort, ts):
    """Codex establishes (model, effort) out of band and carries them forward."""
    return _codex("turn_context", {"turn_id": "t", "cwd": "/home/u/codexproj",
                                   "model": model, "effort": effort}, ts)


def _codex_turn(cum, inp, cached, out, reasoning, ts):
    """One Codex API response.

    Two subset relations the parser normalises, and the fixture has to respect
    them or it is not describing anything real: `cached` is part of `inp`, and
    `reasoning` is part of `out`.
    """
    return _codex("event_msg", {
        "type": "token_count",
        "info": {
            "last_token_usage": {
                "input_tokens": inp, "cached_input_tokens": cached,
                "cache_write_input_tokens": 0, "output_tokens": out,
                "reasoning_output_tokens": reasoning, "total_tokens": inp + out,
            },
            # Per-thread monotonic counter; `codex:<thread>:<cumulative>` is the
            # message-id analogue the dedupe index keys on.
            "total_token_usage": {
                "input_tokens": 0, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "output_tokens": 0,
                "reasoning_output_tokens": 0, "total_tokens": cum,
            },
            "model_context_window": 258400,
        },
    }, ts)


_CODEX_MAIN = [
    _codex_meta(CODEX_THREAD, utc_ts_on_local_day(0, 9)),
    _codex_context(SOL, "high", utc_ts_on_local_day(0, 9)),
    _codex_turn(1_000_000, inp=1_000_000, cached=800_000, out=200_000,
                reasoning=150_000, ts=utc_ts_on_local_day(0, 9, 5)),
    _codex_context(LUNA, "medium", utc_ts_on_local_day(0, 10)),
    _codex_turn(5_000_000, inp=4_000_000, cached=3_000_000, out=800_000,
                reasoning=600_000, ts=utc_ts_on_local_day(0, 10, 5)),
]

# A Codex subagent thread, so `subagent_by_type` and `top_dispatches` carry rows
# for BOTH sources. Without it those two source gates could only be checked in
# one direction.
_CODEX_SUB = [
    _codex_meta(CODEX_SUB_THREAD, utc_ts_on_local_day(0, 11),
                thread_source="subagent"),
    _codex_context(LUNA, "low", utc_ts_on_local_day(0, 11)),
    _codex_turn(300_000, inp=300_000, cached=200_000, out=50_000,
                reasoning=30_000, ts=utc_ts_on_local_day(0, 11, 5)),
]


# Drives the real filter chain and hands back both the derived state and the
# markup the browser would receive. `renderStats`, `renderHourlyChart` and
# `renderSubagentChart` are shadowed because their arguments are the only place
# three of the eight source gates are observable — nothing stores them.
_DRIVE = """(() => {
  rawData = payload;
  selectedSource = source;
  selectedModels = new Set(models === null ? payload.all_models : models);
  // `range` is either a named range the page knows or an explicit {start, end}
  // pair, injected by shadowing getRangeBounds.
  if (range && typeof range === 'object') {
    selectedRange = 'all';
    getRangeBounds = () => range;
  } else {
    selectedRange = range;
  }
  sessionSortCol = sortCol;
  sessionSortDir = sortDir;

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

  let totals = null, hourly = null, agentTypes = null;
  // Captured AND rendered: the stat tiles' own markup is where the Est. Cost
  // note lives, and that note makes a claim the totals object alone cannot show
  // to be true or false.
  const realRenderStats = renderStats;
  renderStats = (t, label) => { totals = t; realRenderStats(t, label); };
  renderHourlyChart = (agg) => { hourly = agg; };
  renderSubagentChart = (rows) => { agentTypes = rows; };
  applyFilter();

  return {
    totals: totals,
    modelNames: lastByModel.map(m => m.model),
    modelHasSessionsField: lastByModel.map(
      m => Object.prototype.hasOwnProperty.call(m, 'sessions')),
    byProject: lastByProject.map(p => ({
      project: p.project, cost: p.cost, turns: p.turns,
      sessions: p.sessions, billable: !!p.billable })),
    byProjectBranch: lastByProjectBranch.map(p => ({
      project: p.project, branch: p.branch, cost: p.cost, turns: p.turns,
      billable: !!p.billable })),
    sessions: lastFilteredSessions.map(s => ({
      session_id: s.session_id, model: s.model, cost: s.cost,
      turns: s.turns, input: s.input, output: s.output,
      duration_min: s.duration_min, billable: !!s.billable })),
    dispatches: lastFilteredDispatches.map(d => ({
      agent_id: d.agent_id, turns: d.turns, cost: d.cost })),
    agentTypes: agentTypes.map(a => ({ agent_type: a.agent_type, turns: a.turns })),
    hourlyTurns: hourly.hours.reduce((s, h) => s + h.totalTurns, 0),
    byEffort: lastByEffort.map(e => ({ effort: e.effort, turns: e.turns })),
    byStopReason: lastByStopReason.map(r => ({
      stop_reason: r.stop_reason, turns: r.turns })),
    html: html,
  };
})()"""


def _header_widths():
    """`<th>` count per `<tbody>` id, read from the page itself.

    The headers live in `web/index.html` while the cells are built in
    `54-tables.js`, so the count has to come from the page rather than from a
    second copy here — a second copy is exactly what would drift.
    """
    html = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    widths = {}
    for table in re.finditer(r"<table[^>]*>(.*?)</table>", html, re.S):
        body = re.search(r'<tbody id="([^"]+)"', table.group(1))
        head = re.search(r"<thead>(.*?)</thead>", table.group(1), re.S)
        if body and head:
            widths[body.group(1)] = len(re.findall(r"<th\b", head.group(1)))
    return widths


def _rows(payload, key, source):
    """Payload rows of one source. Deliberately a dumb second implementation."""
    return [r for r in payload[key] if (r.get("source") or "claude") == source]


def _cells(row_html):
    """The `<td>`s of one rendered `<tr>`, in column order."""
    return re.findall(r"<td\b[^>]*>(.*?)</td>", row_html, re.S)


def _row_with(body, needle):
    """The single `<tr>` of `body` containing `needle`."""
    rows = [r for r in body.split("<tr>") if needle in r]
    if len(rows) != 1:
        raise AssertionError(
            f"expected exactly one row containing {needle!r}, found {len(rows)}")
    return rows[0]


class _Fixture(unittest.TestCase):
    """Writes real transcripts, runs the real scanner, reads the real API."""

    @classmethod
    def setUpClass(cls):
        tmp = Path(tempfile.mkdtemp())
        projects = tmp / "projects"
        claude = projects / "u" / "proj"
        claude.mkdir(parents=True)
        (claude / "sess.jsonl").write_text("\n".join(_SESS_MAIN) + "\n",
                                           encoding="utf-8")
        (claude / "sub.jsonl").write_text("\n".join(_SESS_SUB) + "\n",
                                          encoding="utf-8")
        # Its own directory: the parser is chosen by sniffing the first record,
        # so the two formats cannot share a file.
        codex = projects / "codex"
        codex.mkdir(parents=True)
        (codex / f"rollout-2026-01-01T09-00-00-{CODEX_THREAD}.jsonl").write_text(
            "\n".join(_CODEX_MAIN) + "\n", encoding="utf-8")
        (codex / f"rollout-2026-01-01T11-00-00-{CODEX_SUB_THREAD}.jsonl").write_text(
            "\n".join(_CODEX_SUB) + "\n", encoding="utf-8")
        db = tmp / "usage.db"
        scanner.scan(projects_dir=projects, db_path=db, verbose=False)
        cls.payload = get_dashboard_data(db)

    def drive(self, source="claude", models=None, date_range="all",
              sort_col="cost", sort_dir="desc", payload=None):
        # `payload` overrides the class fixture for the one case the fixture
        # cannot express: what the API serves before anything has been scanned.
        return run_js(emit(_DRIVE,
                           payload=self.payload if payload is None else payload,
                           source=source, models=models, range=date_range,
                           sortCol=sort_col, sortDir=sort_dir))


@requires_node
class TestFixtureIsDiscriminating(_Fixture):
    """Guards every test below. Each of these is a premise something asserts."""

    def test_the_mixed_session_really_holds_two_models(self):
        row = next(s for s in self.payload["sessions_all"]
                   if s["session_id"] == "s-mixed")
        self.assertEqual({b["model"] for b in row["by_model"]}, {OPUS, HAIKU})

    def test_the_cheap_model_carries_most_of_the_mixed_session(self):
        """So pricing the session at its top-token model moves it by a factor."""
        parts = {b["model"]: b for b in
                 next(s for s in self.payload["sessions_all"]
                      if s["session_id"] == "s-mixed")["by_model"]}
        self.assertGreater(parts[HAIKU]["input"], 3 * parts[OPUS]["input"])

    def test_the_local_model_is_on_no_price_list(self):
        """The whole "n/a is not $0.00" argument rests on this staying true."""
        self.assertIn(LOCAL, self.payload["all_models"])
        self.assertEqual(calc_cost(LOCAL, 1_000_000, 1_000_000, 0, 0), 0.0)

    def test_both_sources_reach_every_gated_array(self):
        """A source gate can only be checked where the other source has rows."""
        for key in ("daily_by_model", "sessions_all", "project_by_day_model",
                    "hourly_by_model", "subagent_by_type", "top_dispatches",
                    "effort_by_day_model", "stop_reason_by_day_model"):
            for source in ("claude", "codex"):
                with self.subTest(array=key, source=source):
                    self.assertTrue(_rows(self.payload, key, source),
                                    "nothing to leak, so the gate is untestable")

    def test_only_one_turn_was_truncated_and_only_on_haiku(self):
        rows = [r for r in _rows(self.payload, "stop_reason_by_day_model", "claude")
                if r["stop_reason"] == "max_tokens"]
        self.assertEqual([r["model"] for r in rows], [HAIKU])
        self.assertEqual(sum(r["turns"] for r in rows), 1)


@requires_node
class TestSessionCostSortOrdersByTheColumnItPrints(_Fixture):
    """Sorting by "Est. Cost" must order by the cost the cell shows.

    `sessionForSelection` sums a session's money per model, over the selected
    models only, and the cell prints exactly that. The comparator recomputed it
    as `calcCost(row.model, <the row's whole token total>)` — the row's model is
    only its TOP-TOKEN one, so a session that used opus and haiku was ranked at
    haiku's rate while being printed at the true per-model figure. Every printed
    number stays right; only the order is wrong, which is a defect no totals
    assertion can ever see.
    """

    def _printed_costs(self, source="claude", direction="desc"):
        got = self.drive(source=source, sort_dir=direction)
        body = got["html"]["sessions-body"]
        cells = re.findall(r'<td class="cost">([^<]*)</td>', body)
        return got, [float(c.replace("$", "").replace(",", "")) for c in cells]

    def test_the_printed_cost_column_is_monotonic(self):
        got, printed = self._printed_costs()
        self.assertGreater(len(printed), 2, "too few priced sessions to order")
        self.assertEqual(printed, sorted(printed, reverse=True),
                         f"the Est. Cost column is out of order: {printed}")
        # Ascending is the same comparator with the sign flipped; a fix that
        # only happened to work one way would show up here.
        _, up = self._printed_costs(direction="asc")
        self.assertEqual(up, sorted(up))

    def test_the_mixed_model_session_outranks_the_cheaper_one(self):
        """The concrete failure, named as an ordering.

        s-mixed really costs $13.50 and s-sonnet $9.00, but s-mixed's top-token
        model is haiku, so pricing its whole token total at haiku gives $7.50
        and puts the more expensive session BELOW the cheaper one.
        """
        got = self.drive()
        order = [s["session_id"] for s in got["sessions"]]
        costs = {s["session_id"]: s["cost"] for s in got["sessions"]}
        self.assertAlmostEqual(costs["s-mixed"], COST_MIXED, places=6)
        self.assertAlmostEqual(costs["s-sonnet"], COST_SONNET, places=6)
        # The two candidate keys fall on opposite sides of the cheaper session,
        # which is what makes the wrong one a visible inversion, not a tie.
        self.assertGreater(COST_MIXED, COST_SONNET)
        self.assertLess(COST_MIXED_AT_TOP_MODEL, COST_SONNET)
        self.assertLess(order.index("s-mixed"), order.index("s-sonnet"))

    def test_the_order_the_table_shows_is_the_order_the_csv_exports(self):
        """`exportSessionsCSV` maps `lastFilteredSessions`, so the two cannot be
        allowed to disagree — a fix applied to the renderer alone would."""
        got = self.drive()
        from_state = [s["cost"] for s in got["sessions"]]
        self.assertEqual(from_state, sorted(from_state, reverse=True))

    def test_unbillable_sessions_still_sink_to_the_bottom(self):
        """`sessionForSelection` gives them cost 0, so nothing about this moves
        when the comparator stops recomputing the figure."""
        got = self.drive()
        order = [s["session_id"] for s in got["sessions"]]
        self.assertEqual(order[-1], "s-local")
        self.assertFalse(next(s for s in got["sessions"]
                              if s["session_id"] == "s-local")["billable"])

    def test_the_columns_the_generic_branch_already_served_still_sort(self):
        """`cost` now goes through the same tail as these; none of them moved.

        `duration_min` keeps its own branch because the payload can carry it as
        a string, which would otherwise compare lexicographically.
        """
        for col in ("turns", "input", "output", "duration_min"):
            with self.subTest(column=col):
                got = self.drive(sort_col=col)
                values = [float(s[col]) for s in got["sessions"]]
                self.assertEqual(len(values), 4)
                self.assertEqual(values, sorted(values, reverse=True))


@requires_node
class TestUnpricedCostCellsSayNotApplicable(_Fixture):
    """`$0.00` asserts the usage was free. "Not priced" is a different claim.

    AGENTS.md keeps local and third-party models unpriced on purpose, and the
    page already says `n/a` in the dispatches, sessions, model-row and
    effort-row cost cells. Three cells did not: the Cost by Project cell, the
    Cost by Project & Branch cell, and the Cost by Model totals row — which
    printed `$0.0000` directly underneath a body of `n/a` rows, so one card
    contradicted itself. `exportProjectsCSV` already exported the field blank,
    so the table also disagreed with its own export.
    """

    def setUp(self):
        # Only the unpriced model selected: every figure in view is unpriced, so
        # every cost cell on screen has to make the same claim.
        self.got = self.drive(models=[LOCAL])

    def test_the_fixture_really_shows_an_unpriced_project(self):
        expected = {r["project"] for r in
                    _rows(self.payload, "project_by_day_model", "claude")
                    if r["model"] == LOCAL}
        self.assertEqual({p["project"] for p in self.got["byProject"]}, expected)
        self.assertEqual(len(expected), 1)
        self.assertFalse(self.got["byProject"][0]["billable"])
        self.assertGreater(self.got["byProject"][0]["turns"], 0)

    def test_the_project_table_says_not_applicable(self):
        body = self.got["html"]["project-cost-body"]
        self.assertIn('<td class="cost-na">n/a</td>', body)
        self.assertNotIn('<td class="cost">$0.0000</td>', body)

    def test_the_project_branch_table_says_not_applicable(self):
        body = self.got["html"]["project-branch-cost-body"]
        self.assertIn('<td class="cost-na">n/a</td>', body)
        self.assertNotIn('<td class="cost">$0.0000</td>', body)

    def test_the_model_totals_row_says_not_applicable(self):
        """The sharpest self-contradiction: both cells are inside one table."""
        self.assertIn('<td class="cost-na">n/a</td>',
                      self.got["html"]["model-cost-body"])
        self.assertIn('<td class="cost-na">n/a</td>',
                      self.got["html"]["model-cost-total"])
        self.assertNotIn("$0.0000", self.got["html"]["model-cost-total"])

    def test_every_cost_cell_on_the_page_agrees(self):
        """The point is agreement, not any one cell: a reader comparing two
        cards must not be told the same tokens were free and unpriced."""
        for body in ("sessions-body", "model-cost-body", "model-cost-total",
                     "project-cost-body", "project-branch-cost-body",
                     "effort-cost-body"):
            with self.subTest(table=body):
                markup = self.got["html"][body]
                self.assertNotIn('<td class="cost">', markup)
                self.assertIn("n/a", markup)

    def test_the_rows_keep_their_cell_count(self):
        """`labelCells` maps a cell to its header BY INDEX, so the `n/a` cell
        has to be one `<td>` in exactly the place the money one held."""
        widths = _header_widths()
        for body, table in (("project-cost-body", "project-cost-body"),
                            ("project-branch-cost-body", "project-branch-cost-body"),
                            ("model-cost-total", "model-cost-body")):
            with self.subTest(table=body):
                cells = len(re.findall(r"<td\b", self.got["html"][body]))
                self.assertEqual(cells, widths[table])

    def test_a_priced_view_still_prints_money(self):
        """The control: the gate must not swallow a real figure."""
        got = self.drive()
        self.assertIn('<td class="cost">', got["html"]["project-cost-body"])
        self.assertIn(fmt_money(COST_MIXED), got["html"]["sessions-body"])
        self.assertIn(fmt_money(COST_SONNET), got["html"]["sessions-body"])

    def test_the_token_totals_are_unaffected(self):
        """Only the money cell changes; the counts still include every model."""
        truth = sum(r["turns"] for r in _rows(self.payload, "daily_by_model",
                                              "claude")
                    if r["model"] == LOCAL)
        self.assertGreater(truth, 0)
        self.assertEqual(self.got["totals"]["turns"], truth)


@requires_node
class TestProjectSessionCountsAreGroupedLikeEveryOtherNumber(unittest.TestCase):
    """The two project tables printed their session count with no grouping.

    Everything else on the page goes through a formatter pinned to en-US — the
    Sessions tile through `NUM.format`, every token column through `fmt()` —
    for the reason `20-format.js` states at the top: a figure formatted one way
    beside the same figure formatted another reads as two different numbers.
    These two cells interpolated the raw value, so a 1,234-session project read
    `1234` in the table and `1,234` in the tile directly above it, on an en-US
    browser, with nothing wrong with either number.

    `fmt()` is the wrong formatter here, and deliberately not the fix: it
    abbreviates at a thousand, so the count would read `1.2K` — the tile does
    not do that either, because a session count is small enough to be worth
    reading exactly.
    """

    #: Four digits, so grouping is visible; not a round number, so a formatter
    #: that truncated instead of grouping could not accidentally agree.
    COUNT = 1234

    @classmethod
    def setUpClass(cls):
        cls.rendered = run_js(emit("""
          (() => {
            const html = {};
            document.getElementById = (id) => ({
              set innerHTML(v) { html[id] = v; },
              closest: () => null, rows: null, cells: null, textContent: '',
              setAttribute: () => {}, querySelectorAll: () => [],
            });
            const row = { project: 'projA', branch: 'main', sessions: n,
                          turns: 12, input: 1000, output: 500, cost: 1.5,
                          billable: true };
            renderProjectCostTable([row]);
            renderProjectBranchCostTable([row]);
            return { project: html['project-cost-body'],
                     branch: html['project-branch-cost-body'],
                     tile: NUM.format(n), abbreviated: fmt(n) };
          })()""", n=cls.COUNT))

    def test_the_project_table_groups_its_digits(self):
        self.assertIn('<td class="num">1,234</td>', self.rendered["project"])

    def test_the_project_branch_table_groups_its_digits(self):
        self.assertIn('<td class="num">1,234</td>', self.rendered["branch"])

    def test_the_tables_and_the_sessions_tile_print_the_same_thing(self):
        """The tile is the reference: it is the other place this count appears,
        and it is what a reader compares the row against."""
        self.assertEqual(self.rendered["tile"], "1,234")
        for table in ("project", "branch"):
            with self.subTest(table=table):
                self.assertIn(f'>{self.rendered["tile"]}<', self.rendered[table])

    def test_the_count_is_not_abbreviated(self):
        """`fmt()` would have made it `1.2K`, which the tile never says."""
        self.assertEqual(self.rendered["abbreviated"], "1.2K")
        for table in ("project", "branch"):
            with self.subTest(table=table):
                self.assertNotIn("1.2K", self.rendered[table])


@requires_node
class TestEverySourceGateIsExercised(_Fixture):
    """Exactly one assistant is on screen, in all eight places it is filtered.

    Checked with EVERY model selected. That is the whole point: selecting one
    source's models happens to exclude the other's, so a totals assertion made
    with the default selection passes even with the source filter deleted.
    AGENTS.md names that trap; six of these eight gates could be removed one at
    a time with the whole JS suite green.

    The payload here is unscoped (`get_dashboard_data(db)` with no source),
    which is what makes the gates observable at all. Production scopes in SQL
    too, so these are a second layer — but they are the layer AGENTS.md
    documents as the mechanism, and a filter chain nothing pins is a filter
    chain a refactor can quietly drop.
    """

    #: One driven view per source, built once — every test below reads the same
    #: two, and each one is a node process.
    _views = {}

    def _both(self):
        """Both views, EVERY model selected in each."""
        if not TestEverySourceGateIsExercised._views:
            TestEverySourceGateIsExercised._views = {
                source: self.drive(source=source)
                for source in ("claude", "codex")}
        return sorted(TestEverySourceGateIsExercised._views.items())

    def test_the_daily_rollup_is_gated(self):
        for source, got in self._both():
            with self.subTest(source=source):
                truth = _rows(self.payload, "daily_by_model", source)
                self.assertEqual(set(got["modelNames"]),
                                 {r["model"] for r in truth})
                self.assertEqual(got["totals"]["turns"],
                                 sum(r["turns"] for r in truth))

    def test_the_session_list_is_gated(self):
        for source, got in self._both():
            with self.subTest(source=source):
                truth = {r["session_id"]
                         for r in _rows(self.payload, "sessions_all", source)}
                self.assertEqual({s["session_id"] for s in got["sessions"]}, truth)
                self.assertEqual(got["totals"]["sessions"], len(truth))

    def test_the_project_rollup_is_gated(self):
        for source, got in self._both():
            with self.subTest(source=source):
                truth = {}
                branches = {}
                for r in _rows(self.payload, "project_by_day_model", source):
                    truth[r["project"]] = truth.get(r["project"], 0) + r["turns"]
                    key = r["project"] + "\x00" + (r["branch"] or "")
                    branches[key] = branches.get(key, 0) + r["turns"]
                self.assertEqual(
                    {p["project"]: p["turns"] for p in got["byProject"]}, truth)
                self.assertEqual(
                    {p["project"] + "\x00" + (p["branch"] or ""): p["turns"]
                     for p in got["byProjectBranch"]}, branches)

    def test_the_subagent_tile_is_gated(self):
        for source, got in self._both():
            with self.subTest(source=source):
                truth = sum(r["input"] + r["output"] + r["cache_read"]
                            + r["cache_creation"]
                            for r in _rows(self.payload, "subagent_by_type",
                                           source))
                self.assertGreater(truth, 0, "nothing to leak into the other view")
                self.assertEqual(got["totals"]["subagent_tokens"], truth)

    def test_the_subagent_breakdown_is_gated(self):
        for source, got in self._both():
            with self.subTest(source=source):
                truth = {}
                for r in _rows(self.payload, "subagent_by_type", source):
                    truth[r["agent_type"]] = (truth.get(r["agent_type"], 0)
                                              + r["turns"])
                self.assertEqual(
                    {a["agent_type"]: a["turns"] for a in got["agentTypes"]},
                    truth)

    def test_the_hourly_panel_is_gated(self):
        for source, got in self._both():
            with self.subTest(source=source):
                truth = sum(r["turns"] for r in
                            _rows(self.payload, "hourly_by_model", source))
                self.assertEqual(got["hourlyTurns"], truth)

    def test_the_dispatch_table_is_gated(self):
        for source, got in self._both():
            with self.subTest(source=source):
                truth = {r["agent_id"] for r in
                         _rows(self.payload, "top_dispatches", source)}
                self.assertTrue(truth, "nothing to leak into the other view")
                self.assertEqual({d["agent_id"] for d in got["dispatches"]}, truth)

    def test_the_effort_and_stop_reason_cards_are_gated(self):
        for source, got in self._both():
            with self.subTest(source=source):
                effort = sum(r["turns"] for r in
                             _rows(self.payload, "effort_by_day_model", source))
                stop = sum(r["turns"] for r in
                           _rows(self.payload, "stop_reason_by_day_model", source))
                self.assertEqual(sum(e["turns"] for e in got["byEffort"]), effort)
                self.assertEqual(sum(r["turns"] for r in got["byStopReason"]), stop)

    def test_neither_view_shows_the_others_models(self):
        """The cheap check that must keep holding while the eight above do."""
        claude, codex = self.drive("claude"), self.drive("codex")
        self.assertFalse(set(claude["modelNames"]) & set(codex["modelNames"]))
        self.assertTrue(set(claude["modelNames"]))
        self.assertTrue(set(codex["modelNames"]))


@requires_node
class TestWhyResponsesEndedIsInformative(_Fixture):
    """The card's two informative parts: the share column and the headline.

    Keep max_tokens visible when a response was cut off. An empty bucket must
    produce clear copy without implying the field is unsupported."""

    def _share_cells(self, got):
        body = got["html"]["stop-reason-body"]
        out = {}
        for row in body.split("<tr>")[1:]:
            cells = _cells(row)
            self.assertEqual(len(cells), 4, f"stop-reason row: {row[:200]!r}")
            label = re.sub(r"<[^>]+>", "", cells[0]).strip()
            out[label] = cells[2].strip()
        return out

    def test_the_share_column_prints_the_real_share(self):
        got = self.drive()
        truth = {}
        total = 0
        for r in _rows(self.payload, "stop_reason_by_day_model", "claude"):
            label = r["stop_reason"] or "not recorded"
            truth[label] = truth.get(label, 0) + r["turns"]
            total += r["turns"]
        self.assertGreater(total, 0)
        expected = {k: f"{v / total * 100:.1f}%" for k, v in truth.items()}
        # At least two distinct shares, or an inverted fmtPct would still agree.
        self.assertGreater(len(set(expected.values())), 1)
        self.assertEqual(self._share_cells(got), expected)

    def test_the_shares_add_up_to_the_whole(self):
        shares = self._share_cells(self.drive())
        total = sum(float(s.rstrip("%")) for s in shares.values())
        self.assertAlmostEqual(total, 100.0, places=1)

    def test_the_truncated_row_is_flagged(self):
        body = self.drive()["html"]["stop-reason-body"]
        row = _row_with(body, ">max_tokens<")
        self.assertIn("stop-tag truncated", row,
                      "the class is what visually separates it from the rest")
        self.assertEqual(body.count("truncated"), 1,
                         "only the truncated row may carry the flag")

    def test_the_headline_reports_the_count_when_something_was_cut_off(self):
        note = self.drive()["html"]["stop-reason-note"]
        self.assertIn("<code>max_tokens</code>", note)
        self.assertIn("<strong>1 in this range.</strong>", note)

    def test_the_headline_reads_sensibly_with_the_bucket_empty(self):
        """Synthetic fixture with no stored row for the omitted field."""
        got = self.drive(models=[OPUS, SONNET, LOCAL])
        body = got["html"]["stop-reason-body"]
        self.assertNotIn(">max_tokens<", body)
        note = got["html"]["stop-reason-note"]
        self.assertIn("None in this range.", note)
        self.assertNotIn("<strong>", note)

    def test_a_reason_only_synthetic_records_carry_never_reaches_the_card(self):
        """`stop_sequence` is in the transcripts and must stay off the card.

        A stop_sequence notice on an all-zero synthetic response is not a
        stored usage turn. Exercise that distinction explicitly."""
        self.assertIn('"stop_reason": "stop_sequence"', "\n".join(_SESS_MAIN))
        self.assertEqual(
            [r for r in _rows(self.payload, "stop_reason_by_day_model", "claude")
             if r["stop_reason"] == "stop_sequence"], [])
        got = self.drive()
        self.assertNotIn("stop_sequence",
                         [r["stop_reason"] for r in got["byStopReason"]])
        self.assertNotIn("stop_sequence", got["html"]["stop-reason-body"])

    def test_the_other_source_says_it_records_no_reason_at_all(self):
        """Never "nothing was cut short" — Codex writes no stop reason, which is
        a gap in what is recorded, not evidence about what happened."""
        note = self.drive("codex")["html"]["stop-reason-note"]
        self.assertIn("no stop reason at all", note)
        self.assertNotIn("None in this range.", note)

    def test_fmt_pct_is_a_share_of_the_whole_not_the_other_way_round(self):
        """`renderStopReasonTable` is the only call site of `fmtPct` in the
        repo, so this and the share assertions above are the only things that
        can ever protect it."""
        got = run_js(emit("[fmtPct(1, 4), fmtPct(5, 8), fmtPct(3, 3), "
                          "fmtPct(1, 0), fmtPct(0, 4)]"))
        self.assertEqual(got, ["25.0%", "62.5%", "100.0%", "—", "0.0%"])


@requires_node
class TestEveryEffortAndStopRowFillsItsHeaders(unittest.TestCase):
    """The two cards `TestRenderedRowsMatchTheirHeaders` does not drive.

    `labelCells` stamps each cell with its column's header BY INDEX, so a cell
    added or dropped here relabels every column after it on a phone and leaves
    the last one unlabelled. Deleting the share `<td>` from the stop-reason row
    left three cells under four headers with the whole suite green.
    """

    def test_both_cards_render_one_cell_per_column(self):
        widths = _header_widths()
        for body in ("stop-reason-body", "effort-cost-body"):
            self.assertIn(body, widths, "index.html no longer declares this table")
        rendered = run_js(emit("""
          (() => {
            const captured = {};
            document.getElementById = (id) => ({
              set innerHTML(v) { captured[id] = v; },
              closest: () => null, rows: null, cells: null, textContent: '',
              setAttribute: () => {}, querySelectorAll: () => [],
            });
            const bucket = newCostBucket({ effort: 'high' });
            accumulateCostRow(bucket, {
              model: 'claude-opus-5', turns: 1, input: 1000, output: 1000,
              cache_read: 1000, cache_creation: 1000, cache_creation_1h: 0,
              reasoning: 0,
            });
            renderEffortCostTable([bucket]);
            renderStopReasonTable([{ stop_reason: 'end_turn', turns: 1,
                                     output: 1000, cost: 0.025, billable: true,
                                     rates: { output: new Set([1]) } }]);
            const out = {};
            for (const [k, v] of Object.entries(captured)) {
              out[k] = (String(v).match(/<td\\b/g) || []).length;
            }
            return out;
          })()"""))
        for body in ("stop-reason-body", "effort-cost-body"):
            with self.subTest(table=body):
                self.assertEqual(
                    rendered[body], widths[body],
                    f"{body} renders {rendered[body]} cells under "
                    f"{widths[body]} headers; labelCells maps them by index")


@requires_node
class TestABlendedRateSaysSo(_Fixture):
    """A derived per-million figure that is on no price list has to say `avg`.

    Forcing `blendedRate` to `return false` left the whole JS suite green: the
    marker driven by *several models in one bucket* was asserted nowhere, and
    only the cache-tier path (`mixedTiers`) was covered. Unlabelled, the number
    invites the reader to look it up on the price list, where it is not.
    """

    EFFORT_COLUMNS = ("effort", "turns", "input", "output", "cache_read",
                      "cache_creation", "reasoning", "cost")

    def _effort_cell(self, body, level, column):
        row = _row_with(body, ">" + level + "<")
        cells = _cells(row)
        self.assertEqual(len(cells), len(self.EFFORT_COLUMNS), row[:300])
        return cells[self.EFFORT_COLUMNS.index(column)]

    def test_a_level_fed_by_two_rates_is_marked(self):
        """`high` holds opus ($5.00/M in) and haiku ($1.00/M in); the derived
        rate is neither."""
        body = self.drive()["html"]["effort-cost-body"]
        cell = self._effort_cell(body, "high", "input")
        self.assertIn("avg", cell)
        self.assertNotIn("$5.00/M avg", cell, "that would be opus's list price")
        self.assertNotIn("$1.00/M avg", cell, "that would be haiku's")

    def test_a_level_fed_by_one_rate_is_not(self):
        """The control that keeps the marker meaningful: `low` is sonnet alone,
        so its derived rate IS sonnet's published $3.00/M."""
        body = self.drive()["html"]["effort-cost-body"]
        cell = self._effort_cell(body, "low", "input")
        self.assertNotIn("avg", cell)
        self.assertIn("$3.00/M", cell)

    def test_the_stop_reason_output_column_is_marked_too(self):
        """The card's own money cell goes through the same predicate; it was
        equally unasserted."""
        body = self.drive()["html"]["stop-reason-body"]
        end_turn = _cells(_row_with(body, ">end_turn<"))[3]
        truncated = _cells(_row_with(body, ">max_tokens<"))[3]
        self.assertIn("avg", end_turn, "end_turn was answered by three models")
        self.assertNotIn("avg", truncated, "max_tokens was haiku alone")


@requires_node
class TestModelFilterReadSide(unittest.TestCase):
    """The `?models=` link and the start-up selection rule.

    `updateURL` (the write side) and `isDefaultModelSelection` are tested;
    `readURLModels` and `defaultModelSelection` were not. So the page wrote a
    parameter nothing proved it read back, and the documented rule — everything
    priced, or everything when nothing is priced — was unguarded because no
    fixture had an unpriced model in `all_models` at boot.
    """

    ALL = ["claude-opus-5", "claude-sonnet-5"]
    MIXED = ["claude-opus-5", "gemma-3-27b-local"]

    def test_a_models_parameter_selects_exactly_what_it_names(self):
        got = run_js(emit("""
          (() => {
            window.location.search = '?models=claude-opus-5';
            return [...readURLModels(all)].sort();
          })()""", all=self.ALL))
        self.assertEqual(got, ["claude-opus-5"])

    def test_a_list_matching_nothing_falls_back_instead_of_blanking_the_page(self):
        """A link written for the other source intersects to nothing, and an
        empty dashboard reads as "switching is broken"."""
        got = run_js(emit("""
          (() => {
            window.location.search = '?models=gpt-5.6-sol,gpt-5.6-luna';
            return [...readURLModels(all)].sort();
          })()""", all=self.ALL))
        self.assertEqual(got, sorted(self.ALL))

    def test_no_parameter_means_the_default_selection(self):
        got = run_js(emit("""
          (() => {
            window.location.search = '';
            return [...readURLModels(all)].sort();
          })()""", all=self.MIXED))
        self.assertEqual(got, ["claude-opus-5"])

    def test_the_link_the_page_writes_is_the_link_it_reads_back(self):
        """`updateURL` pushes through `history.replaceState`, which does not
        move `window.location` in the harness — so the round trip has to be
        threaded by hand, exactly as a reload would do it. It also derives the
        model list from `#model-checkboxes input`, hence the querySelectorAll
        stub (the same one test_dashboard_js's write-side test uses)."""
        got = run_js(emit("""
          (() => {
            let written = null;
            globalThis.history = { replaceState: (a, b, url) => { written = url; } };
            document.querySelectorAll = () => all.map(v => ({ value: v }));
            selectedSource = 'claude';
            selectedRange = '30d';
            selectedModels = new Set(['claude-opus-5']);
            updateURL();
            window.location.search =
              new URL(written, 'http://127.0.0.1:8080/').search;
            return { written: written, read: [...readURLModels(all)].sort() };
          })()""", all=self.ALL))
        self.assertIn("models=claude-opus-5", got["written"])
        self.assertEqual(got["read"], ["claude-opus-5"])

    def test_the_default_selection_is_the_priced_models(self):
        got = run_js(emit("[...defaultModelSelection(mixed)].sort()",
                          mixed=self.MIXED))
        self.assertEqual(got, ["claude-opus-5"])

    def test_a_source_with_no_prices_at_all_starts_fully_selected(self):
        """Otherwise a machine running only local models opens on an empty page."""
        got = run_js(emit("[...defaultModelSelection(none)].sort()",
                          none=["gemma-3-27b-local", "glm-4-9b"]))
        self.assertEqual(got, ["gemma-3-27b-local", "glm-4-9b"])


@requires_node
class TestTheModelRollupCarriesNoDeadSessionCount(_Fixture):
    """`byModel[].sessions` was accumulated on every filter pass and read by
    nothing — the Cost by Model card has no Sessions column, the model chart
    plots input+output, and the model CSV has no such field.

    It was not merely dead but wrong: it credited a whole session to its
    top-token model and silently dropped any session whose top model had no
    daily row in range. A future author adding a Sessions column would have
    trusted it. If that column is ever wanted, derive it the way the project
    tables do — from the range-filtered session list.
    """

    def test_the_field_is_gone(self):
        got = self.drive()
        self.assertTrue(got["modelNames"])
        self.assertEqual(got["modelHasSessionsField"],
                         [False] * len(got["modelNames"]))

    def test_the_project_tables_still_count_their_sessions(self):
        """The live counter, which does come from the session list."""
        got = self.drive()
        self.assertEqual(sum(p["sessions"] for p in got["byProject"]),
                         got["totals"]["sessions"])


#: Two Anthropic ids priced identically, listed as two separate object literals
#: in `PRICING` (web/js/10-pricing.js). A bucket fed by both derives exactly the
#: published $5.00/M — nothing is blended — which is what makes them the pair the
#: `avg` marker has to get right. `OPUS` above is the second of the two.
OPUS_5 = "claude-opus-5"
#: Sol's dated policy before the conservative 2026-08-22 boundary shares
#: Opus's 5-minute cache-write rate ($6.25/M) while differing on the 1-hour one
#: ($6.25 vs $10.00). No view mixes the assistants, so this is a structural
#: control for WHICH rate the cache-write column keys on, not a live mixed-source
#: row. The explicit pricing day makes the effective rate part of the fixture.
SOL_PRIOR_SAME_5M_WRITE = "gpt-5.6-sol"
# Keep this cache-tier fixture below the long-context threshold. Its purpose is
# to isolate the 5-minute/1-hour write-rate mix, not the separate surcharge.
CACHE_TIER_FIXTURE_TOKENS = 100_000

#: One effort bucket built from arbitrary rows through the page's real
#: accumulator, then rendered by the page's real renderer.
_EFFORT_BUCKET = """(() => {
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
  const bucket = newCostBucket({ effort: 'high' });
  for (const row of rows) {
    accumulateCostRow(bucket, Object.assign(
      { turns: 1, input: 0, output: 0, cache_read: 0, cache_creation: 0,
        cache_creation_1h: 0, reasoning: 0 }, row));
  }
  renderEffortCostTable([bucket]);
  return { body: html['effort-cost-body'],
           identicalNumbers: JSON.stringify(PRICING[a]) === JSON.stringify(PRICING[b]),
           sameObject: PRICING[a] === PRICING[b] };
})()"""

#: The stop-reason card has no bucket constructor of its own — its accumulator
#: is inline in `applyFilter` — so its column is only reachable through the real
#: filter chain.
_STOP_REASON_ROWS = """(() => {
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
  rawData = {
    all_models: models,
    daily_by_model: [], sessions_all: [], project_by_day_model: [],
    hourly_by_model: [], subagent_by_type: [], top_dispatches: [],
    effort_by_day_model: [], limit_incidents: [],
    stop_reason_by_day_model: rows,
  };
  selectedSource = 'claude';
  selectedModels = new Set(models);
  selectedRange = 'all';
  renderHourlyChart = () => {};
  renderSubagentChart = () => {};
  applyFilter();
  return { body: html['stop-reason-body'] };
})()"""


@requires_node
class TestTheAvgMarkerMeansTheRateIsNotOnThePriceList(unittest.TestCase):
    """`avg` says the printed per-million figure is on no price list.

    Blended rates should be weighted by the relevant token categories, not by
    the count of pricing objects returned for model IDs.

    The existing coverage could not see it. `TestABlendedRateSaysSo` feeds a
    bucket opus and haiku, whose rates differ, so it renders `avg` identically
    whether the set is keyed on objects or on numbers.
    """

    EFFORT_COLUMNS = ("effort", "turns", "input", "output", "cache_read",
                      "cache_creation", "reasoning", "cost")

    def _bucket(self, rows, a=OPUS_5, b=OPUS):
        return run_js(emit(_EFFORT_BUCKET, rows=rows, a=a, b=b))

    def _cell(self, got, column):
        row = _row_with(got["body"], ">high<")
        cells = _cells(row)
        self.assertEqual(len(cells), len(self.EFFORT_COLUMNS), row[:300])
        return cells[self.EFFORT_COLUMNS.index(column)]

    def test_the_two_ids_really_are_one_price_written_twice(self):
        """The premise: identical numbers, two objects. Without both halves the
        assertions below would be testing nothing."""
        got = self._bucket([{"model": OPUS_5, "input": 1_000_000}])
        self.assertTrue(got["identicalNumbers"])
        self.assertFalse(got["sameObject"])

    def test_two_ids_at_the_same_published_rate_are_not_called_an_average(self):
        """$5.00/M is Opus's list price. Nothing about it is blended."""
        got = self._bucket([{"model": OPUS_5, "input": 1_000_000},
                            {"model": OPUS, "input": 1_000_000}])
        cell = self._cell(got, "input")
        self.assertIn("$5.00/M", cell)
        self.assertNotIn("avg", cell)

    def test_the_output_column_too(self):
        """Every column keys on its own rate, so each one can be wrong alone."""
        got = self._bucket([{"model": OPUS_5, "output": 1_000_000},
                            {"model": OPUS, "output": 1_000_000}])
        cell = self._cell(got, "output")
        self.assertIn("$25.00/M", cell)
        self.assertNotIn("avg", cell)

    def test_two_models_at_different_rates_still_are(self):
        """The control that keeps the marker worth printing: opus ($5.00/M) and
        haiku ($1.00/M) derive $3.00/M, which is on neither price list."""
        got = self._bucket([{"model": OPUS_5, "input": 1_000_000},
                            {"model": HAIKU, "input": 1_000_000}])
        cell = self._cell(got, "input")
        self.assertIn("avg", cell)
        self.assertIn("$3.00/M avg", cell)

    def test_a_priced_model_beside_an_unpriced_one_is_still_an_average(self):
        """An unpriced model's tokens land in the count and not in the money, so
        they pull the derived rate below every published one. `null` in the set
        is a genuinely different rate and has to keep marking the cell."""
        got = self._bucket([{"model": OPUS_5, "input": 1_000_000},
                            {"model": LOCAL, "input": 1_000_000}])
        cell = self._cell(got, "input")
        self.assertIn("avg", cell)
        self.assertIn("$2.50/M avg", cell)

    def test_a_single_model_still_prints_its_list_price_unmarked(self):
        got = self._bucket([{"model": OPUS_5, "input": 1_000_000}])
        cell = self._cell(got, "input")
        self.assertIn("$5.00/M", cell)
        self.assertNotIn("avg", cell)

    def test_the_cache_write_column_keys_on_the_tier_mix_the_rows_carry(self):
        """Cache writes bill at two rates and the row carries the split, so the
        column has no single published rate to key on.

        Both shortcuts are wrong, in opposite directions, and this pair of
        models shows each of them:

        * keyed on the 5-minute rate alone, two all-1-hour rows at $10.00/M and
          $6.25/M derive $8.125/M — on no price list — and would go unmarked;
        * keyed on the (5-minute, 1-hour) pair, two all-5-minute rows both
          charged the published $6.25/M would be called an average.

        Keying on the rate each row's own mix produced answers both. A row that
        spans the tiers by itself is a third case, and `mixedTiers` already
        marks it from the bucket's totals.
        """
        long_lived = self._bucket(
            [{"model": OPUS_5, "cache_creation": CACHE_TIER_FIXTURE_TOKENS,
              "cache_creation_1h": CACHE_TIER_FIXTURE_TOKENS},
             {"model": SOL_PRIOR_SAME_5M_WRITE,
              "pricing_day": "2026-08-21",
              "cache_creation": CACHE_TIER_FIXTURE_TOKENS,
              "cache_creation_1h": CACHE_TIER_FIXTURE_TOKENS}])
        cell = self._cell(long_lived, "cache_creation")
        self.assertIn("$8.125/M avg", cell)

        short_lived = self._bucket(
            [{"model": OPUS_5, "cache_creation": CACHE_TIER_FIXTURE_TOKENS},
             {"model": SOL_PRIOR_SAME_5M_WRITE,
              "pricing_day": "2026-08-21",
              "cache_creation": CACHE_TIER_FIXTURE_TOKENS}])
        cell = self._cell(short_lived, "cache_creation")
        self.assertIn("$6.25/M", cell)
        self.assertNotIn("avg", cell)

    def test_a_bucket_that_spans_both_tiers_is_still_marked(self):
        """`mixedTiers` answers a different question about the same cell and
        must keep being OR-ed in: one model, one rate in the set, but the
        derived write rate sits between its two published tiers."""
        got = self._bucket([{"model": OPUS_5, "cache_creation": 1_000_000},
                            {"model": OPUS_5, "cache_creation": 1_000_000,
                             "cache_creation_1h": 1_000_000}])
        cell = self._cell(got, "cache_creation")
        self.assertIn("$8.125/M avg", cell)

    def _stop_reason_cell(self, models):
        rows = [{"day": "2026-01-15", "source": "claude", "model": m,
                 "stop_reason": "end_turn", "turns": 1, "output": 1_000_000}
                for m in models]
        got = run_js(emit(_STOP_REASON_ROWS, rows=rows, models=models))
        return _cells(_row_with(got["body"], ">end_turn<"))[3]

    def test_the_stop_reason_output_column_follows_the_same_rule(self):
        """It has its own accumulator, inline in `applyFilter`, so it could be
        fixed in one place and left wrong in the other."""
        same = self._stop_reason_cell([OPUS_5, OPUS])
        self.assertIn("$25.00/M", same)
        self.assertNotIn("avg", same)
        different = self._stop_reason_cell([OPUS_5, HAIKU])
        self.assertIn("$15.00/M avg", different)


@requires_node
class TestAnEmptyViewDoesNotBlameThePriceList(_Fixture):
    """"No usage here" and "these models are not priced" are different claims.

    `totals.billable` is `byModel.some(m => isBillable(m.model))`, which is
    vacuously false when nothing is in view — an empty range, or every model
    unchecked. `renderStats` read that one flag and printed "No published
    per-token rate for these models", a statement about the price list, over a
    range whose models the same page had priced one selection earlier.

    AGENTS.md makes `n/a` versus `$0.00` a correctness rule because they are
    different claims about the same tokens. "Not priced" and "not there" are a
    third and a fourth, and the tile has to make the one that is true. The value
    stays `n/a` in every case: whether zero tokens cost zero dollars is exactly
    the question the source determines, and an empty `byModel` cannot answer it.
    """

    def _est_cost_tile(self, **drive_args):
        got = self.drive(**drive_args)
        card = next(c for c in got["html"]["stats-row"].split(
            '<div class="stat-card">') if "Est. Cost" in c)
        value = re.search(r'class="value"[^>]*>([^<]*)<', card).group(1)
        note = re.search(r'class="note">([^<]*)<', card).group(1)
        return got, value, note

    def test_the_range_that_holds_nothing_is_empty_and_priced(self):
        """The premise: `prev-month` has no turns, and the models it would have
        shown are on the price list — so `n/a` cannot be about pricing."""
        got = self.drive(date_range="prev-month")
        self.assertEqual(got["modelNames"], [])
        self.assertEqual(got["totals"]["turns"], 0)
        self.assertTrue(all(calc_cost(m, 1_000_000, 0, 0, 0) > 0
                            for m in (OPUS, HAIKU, SONNET)))

    def test_an_empty_range_says_so_instead(self):
        _, value, note = self._est_cost_tile(date_range="prev-month")
        self.assertEqual(value, "n/a")
        self.assertNotIn("No published per-token rate", note)
        self.assertEqual(note, "No usage in this range for the selected models")

    def test_deselecting_every_model_says_that_instead(self):
        """The other way into an empty view, and "no usage in this range" would
        itself be false here — the range is full, the filter is empty."""
        _, value, note = self._est_cost_tile(models=[])
        self.assertEqual(value, "n/a")
        self.assertEqual(note, "No models selected")

    def test_a_database_with_nothing_in_it_says_there_is_nothing_yet(self):
        """The first screen a new install sees, and the one case where every
        other reason is vacuously true. There are no models to select, so
        "No models selected" is a statement about a filter the reader never
        touched — beside a dropdown that itself reads "No models"."""
        empty = self._payload_of_an_unscanned_database()
        self.assertEqual(empty["all_models"], [],
                         "the premise: an unscanned database offers no models")
        _, value, note = self._est_cost_tile(payload=empty)
        self.assertEqual(value, "n/a")
        self.assertEqual(note, "No usage recorded yet — run a scan")

    @staticmethod
    def _payload_of_an_unscanned_database():
        """A real empty database through the real `get_dashboard_data`, rather
        than a hand-written dict: the point of the case is what an untouched
        install actually sends."""
        db_path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = scanner.get_db(db_path)
        scanner.init_db(conn)
        conn.close()
        return get_dashboard_data(db_path)

    def test_a_genuinely_unpriced_view_still_blames_the_price_list(self):
        """The claim the note was written for, which must keep being made."""
        got, value, note = self._est_cost_tile(models=[LOCAL])
        self.assertGreater(got["totals"]["turns"], 0)
        self.assertEqual(value, "n/a")
        self.assertEqual(note, "No published per-token rate for these models")

    def test_a_view_with_priced_data_still_names_the_price_list(self):
        """The control: none of this may touch the tile that shows money."""
        _, value, note = self._est_cost_tile()
        self.assertTrue(value.startswith("$"))
        self.assertEqual(note, "Anthropic list API pricing")

    def test_the_other_source_keeps_its_own_basis_note(self):
        _, value, note = self._est_cost_tile(source="codex")
        self.assertTrue(value.startswith("$"))
        self.assertIn("OpenAI list rates", note)


# ── The Top Dispatches card ────────────────────────────────────────────────
# Both classes below need a corpus of dispatched subagents rather than the one
# the fixture above carries — one needs more dispatches than the table can show,
# the other needs a single dispatch that outlives a day — so each builds its own
# through the same real path: transcripts on disk, the real scanner, the real
# `/api/data` payload.


def _dispatch_corpus(sub_records, parent_records):
    """Scan a subagent transcript and its parent's, return (db path, payload).

    Two files because that is how Claude Code writes them: the dispatched
    subagent's turns land in its own transcript, and the `toolUseResult` naming
    the agent's type, status, duration and tool count is written by the parent.
    """
    tmp = Path(tempfile.mkdtemp())
    proj = tmp / "projects" / "u" / "proj"
    proj.mkdir(parents=True)
    (proj / "main.jsonl").write_text("\n".join(parent_records) + "\n",
                                     encoding="utf-8")
    (proj / "sub.jsonl").write_text("\n".join(sub_records) + "\n",
                                    encoding="utf-8")
    db_path = tmp / "usage.db"
    scanner.scan(projects_dir=proj.parent.parent, db_path=db_path, verbose=False)
    return db_path, get_dashboard_data(db_path)


# Drives the real filter chain, then walks the real "Show more" control and runs
# the real CSV exporter over the very array the table was rendered from. The two
# findings below are about the table, its footer and its export disagreeing with
# each other or with the card beside them, so all four come back from one run.
_DRIVE_DISPATCHES = """(() => {
  rawData = payload;
  selectedSource = 'claude';
  selectedModels = new Set(payload.all_models);
  // Either a named range the page knows, or an explicit {start, end} pair
  // injected by shadowing getRangeBounds — the page has no vocabulary for
  // "just this one historical day".
  if (range && typeof range === 'object') {
    selectedRange = 'all';
    getRangeBounds = () => range;
  } else {
    selectedRange = range;
  }
  const html = {};
  document.getElementById = (id) => {
    const el = stubEl();
    Object.defineProperty(el, 'innerHTML', {
      get() { return html[id] === undefined ? '' : html[id]; },
      set(v) { html[id] = v; },
    });
    return el;
  };
  let totals = null, agentTypes = null;
  renderStats = (t) => { totals = t; };
  renderHourlyChart = () => {};
  renderSubagentChart = (rows) => { agentTypes = rows; };
  applyFilter();
  // Each click is one "Show more"; three is enough to reach the cap from the
  // first step whatever TABLE_STEPS holds.
  for (let i = 0; i < clicks; i++) moreDispatchRows();
  let csv = null;
  downloadCSV = (name, header, rows) => { csv = {header: header, rows: rows}; };
  exportDispatchesCSV();
  return {
    totals: totals,
    tableMax: TABLE_MAX,
    dispatches: lastFilteredDispatches.map(d => ({
      agent_id: d.agent_id, model: d.model, start: d.start, turns: d.turns,
      input: d.input, output: d.output, cost: d.cost })),
    agentTypes: agentTypes.map(a => ({
      agent_type: a.agent_type, turns: a.turns, input: a.input })),
    renderedRows: (html['dispatches-body'] || '').split('<tr>').length - 1,
    foot: html['dispatches-foot'] || '',
    csv: csv,
  };
})()"""


def _csv_column(csv, name):
    """One CSV column by header name, so an added column moves nothing here."""
    return [row[csv["header"].index(name)] for row in csv["rows"]]


class _RecordingConnection:
    """Passes every call through and remembers the SQL it was asked to run.

    A rollup's shape can be asserted from its output; whether it was *truncated*
    cannot, unless the fixture is bigger than the bound — and no fixture is
    bigger than an arbitrary one. So this reads the statement that actually ran,
    which a comment cannot fake.
    """

    def __init__(self, conn):
        self._conn = conn
        self.statements = []

    def execute(self, sql, *args, **kwargs):
        self.statements.append(sql)
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@requires_node
class TestTheWholeDispatchListReachesTheClient(unittest.TestCase):
    """`top_dispatches` is sent unbounded, and three consumers need it that way.

    A complete dispatch list can be large. Preserve the rows required for
    client-side filters instead of applying an unrelated global limit.

    The premise is false, and this is the shape that says so. Past the cap the
    footer stops offering another step and offers `Download CSV to see all (N)`
    instead, `N` being the true number of dispatches in range, and
    `exportDispatchesCSV` writes every one of them — a promise commit 10e1b05
    made deliberately when it *removed* a server-side `LIMIT 50` ("The full
    ranked set is sent to the client so paging/CSV cover everything").

    A globally low-ranked dispatch can rank first after a date or model
    filter. The API must retain it for that filtered view.

    The fixture is 60 dispatches: more than the table can render, which is the
    number the contract is about, and the 40 cheapest are the ones a rank cut
    reaches first. It cannot outrun an arbitrary `n` — no fixture can — so
    `test_the_dispatch_query_is_not_bounded_server_side` carries that half.

    If the array is ever moved behind its own lazily-fetched endpoint, or the
    range filter and the export move server-side, these assertions describe the
    old contract and should be rewritten to describe the new one — deliberately,
    not by deletion.
    """

    CHEAP = 40
    COSTLY = 20

    @classmethod
    def setUpClass(cls):
        sub, parent = [], []
        stamps = {}
        for i in range(cls.CHEAP + cls.COSTLY):
            cheap = i < cls.CHEAP
            days_ago, scale = (1, 1) if cheap else (0, 1000)
            agent = "agent-%03d" % i
            ts = utc_ts_on_local_day(days_ago, 10, i % 60)
            stamps[cheap] = ts
            sub.append(_assistant(
                "s-sub", HAIKU, ts, "m-d%d" % i,
                inp=1_000 * scale, out=100 * scale, agent_id=agent))
            parent.append(_dispatch_result(
                "s-main", utc_ts_on_local_day(days_ago, 11), agent, "Explore"))
        cls.CHEAP_DAY = local_day_of(stamps[True])
        cls.COSTLY_DAY = local_day_of(stamps[False])
        cls.db_path, cls.payload = _dispatch_corpus(sub, parent)

    def drive(self, date_range="all", clicks=3):
        return run_js(emit(_DRIVE_DISPATCHES, payload=self.payload,
                           range=date_range, clicks=clicks))

    def test_the_fixture_is_larger_than_the_table_can_render(self):
        """Guard the guard: below the cap every assertion here is vacuous."""
        got = self.drive()
        self.assertGreater(self.CHEAP + self.COSTLY, got["tableMax"])
        self.assertEqual(got["renderedRows"], got["tableMax"])

    def test_the_payload_carries_every_dispatch(self):
        self.assertEqual(len(self.payload["top_dispatches"]),
                         self.CHEAP + self.COSTLY)

    def test_the_footer_advertises_the_true_total_and_the_csv_delivers_it(self):
        """The one place the reader is told a number past the cap exists."""
        got = self.drive()
        self.assertIn("Download CSV to see all (%d)" % (self.CHEAP + self.COSTLY),
                      got["foot"])
        self.assertEqual(len(got["csv"]["rows"]), self.CHEAP + self.COSTLY)
        self.assertEqual(len(set(_csv_column(got["csv"], "Agent ID"))),
                         self.CHEAP + self.COSTLY)

    def test_a_range_holding_only_the_cheapest_dispatches_keeps_all_of_them(self):
        """The failure a bound produces, in the direction it produces it.

        These 40 rank last by lifetime tokens, so they are exactly what a
        server-side top-N discards — while being the entire content of the day
        the reader selected.
        """
        got = self.drive(date_range={"start": self.CHEAP_DAY,
                                     "end": self.CHEAP_DAY})
        self.assertEqual(len(got["dispatches"]), self.CHEAP)
        self.assertEqual(len(got["csv"]["rows"]), self.CHEAP)
        self.assertEqual({d["start"][:10] for d in got["dispatches"]},
                         {self.CHEAP_DAY})

    def test_the_dispatch_query_is_not_bounded_server_side(self):
        """The scale-free half: read the statement that actually ran.

        A bound larger than any fixture passes everything above and still
        truncates a real user's history, silently — the whole round-4 suite ran
        green with `LIMIT 200` in place. If a bound is ever right here, the
        range filter and the export have to move to the server with it, and this
        test is what must be rewritten to say so.
        """
        conn = db.get_db(self.db_path)
        try:
            recorder = _RecordingConnection(conn)
            rows = rollups.top_dispatches(recorder)
        finally:
            conn.close()
        self.assertTrue(recorder.statements,
                        "the recorder saw no query, so it proves nothing")
        self.assertIsNone(
            re.search(r"\bLIMIT\b", "\n".join(recorder.statements), re.I),
            "the dispatch query is bounded server-side. The client filters "
            "these rows by range, model and source AFTER they arrive, and the "
            "footer and CSV promise every one of them, so a bound here removes "
            "rows no particular view would have dropped and reports a smaller "
            "total than exists. Move the filter and the export to the server "
            "with the bound, or leave the query whole.")
        self.assertEqual(len(rows), self.CHEAP + self.COSTLY)


@requires_node
class TestADispatchIsScopedByItsStartDayAlone(unittest.TestCase):
    """A dispatch that outlives a day is scoped per DAY, not by its start day.

    These four assertions were a landmine: they pinned the wrong answer with the
    right one written beside them, because the remedy needed two files that were
    not in one assignment. They have been flipped, and the class name is kept so
    the history of what it caught stays findable.

    Every other cost-bearing card is built from a rollup keyed by local day, so
    it can be range-scoped. `top_dispatches` is one row per (dispatch, source,
    model) carrying the dispatch's LIFETIME totals beside a single `start_date`,
    and `applyFilter` used to select on that one day. So a dispatch that outlived
    a day was

    * **dropped whole** from any range that began after it started, taking
      every in-range turn it had with it, and
    * **counted whole** in the range its start day fell in, spending tokens and
      dollars on days outside it.

    A dispatch spanning multiple days needs day-specific totals. Filtering
    only by its start date attributes its entire lifetime to one day.

    The split is shipped only for those rows — a row that lived one local day
    gets `[]`, because `start_date` already IS that day. What that empty array
    means on the client is pinned below; that the server ships it only where it
    is needed is pinned by `tests/test_payload_surface.py`, which owns the only
    fixture in the suite carrying a multi-day dispatch beside a single-day one.
    """

    OLD = dict(inp=1_000_000, out=100_000)   # $1.00 + $0.50 = $1.50
    NEW = dict(inp=3_000_000, out=300_000)   # $3.00 + $1.50 = $4.50

    @classmethod
    def setUpClass(cls):
        old_ts = utc_ts_on_local_day(2, 10)
        new_ts = utc_ts_on_local_day(0, 10)
        cls.OLD_DAY = local_day_of(old_ts)
        cls.NEW_DAY = local_day_of(new_ts)
        sub = [
            _assistant("s-sub", HAIKU, old_ts, "m-old",
                       agent_id="agent-span", **cls.OLD),
            _assistant("s-sub", HAIKU, new_ts, "m-new",
                       agent_id="agent-span", **cls.NEW),
        ]
        parent = [_dispatch_result("s-main", new_ts, "agent-span", "Explore")]
        _, cls.payload = _dispatch_corpus(sub, parent)
        cls.cost_old = calc_cost(HAIKU, cls.OLD["inp"], cls.OLD["out"], 0, 0)
        cls.cost_new = calc_cost(HAIKU, cls.NEW["inp"], cls.NEW["out"], 0, 0)

    def drive(self, day):
        return run_js(emit(_DRIVE_DISPATCHES, payload=self.payload,
                           range={"start": day, "end": day}, clicks=0))

    def test_the_fixture_really_is_one_dispatch_over_two_local_days(self):
        rows = self.payload["top_dispatches"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["start_date"], self.OLD_DAY)
        self.assertEqual(rows[0]["turns"], 2)
        self.assertEqual({r["day"] for r in self.payload["subagent_by_type"]},
                         {self.OLD_DAY, self.NEW_DAY})

    def test_the_row_ships_a_split_that_accounts_for_every_one_of_its_turns(self):
        """The server half. A split that did not add back up to the row would
        move money out of the table without moving it anywhere else."""
        row = self.payload["top_dispatches"][0]
        self.assertEqual([b["day"] for b in row["by_day"]],
                         [self.OLD_DAY, self.NEW_DAY])
        for column in ("input", "output", "turns"):
            with self.subTest(column=column):
                self.assertEqual(sum(b[column] for b in row["by_day"]),
                                 row[column])

    def test_a_dispatch_that_started_before_the_range_is_still_shown(self):
        """One row, 1 turn, 3,000,000 input, $4.50 — the half of the dispatch
        that happened on this day. It used to be dropped whole."""
        got = self.drive(self.NEW_DAY)
        self.assertEqual(len(got["dispatches"]), 1)
        row = got["dispatches"][0]
        self.assertEqual(row["turns"], 1)
        self.assertEqual(row["input"], self.NEW["inp"])
        self.assertAlmostEqual(row["cost"], self.cost_new)
        self.assertEqual(self.cost_new, 4.5)             # what the row owes
        self.assertEqual(len(got["csv"]["rows"]), 1)

    def test_a_dispatch_selected_by_its_start_day_reports_only_that_day(self):
        """1 turn, 1,000,000 input, $1.50 — not the 2 turns, 4,000,000 input and
        $6.00 of its whole life, which was 4.0x on every one of them."""
        got = self.drive(self.OLD_DAY)
        self.assertEqual(len(got["dispatches"]), 1)
        row = got["dispatches"][0]
        self.assertEqual(row["turns"], 1)
        self.assertEqual(row["input"], self.OLD["inp"])
        self.assertAlmostEqual(row["cost"], self.cost_old)
        self.assertAlmostEqual(self.cost_old + self.cost_new,
                               4.0 * self.cost_old)      # what it used to print

    def _drive_without_a_split(self, day, drop_the_key):
        """The same run against a payload whose rows carry no usable split.

        Round-tripped through `json` rather than deep-copied because that is
        exactly what the browser gets, and it is the shape the fallback is
        written for.
        """
        payload = json.loads(json.dumps(self.payload))
        for row in payload["top_dispatches"]:
            if drop_the_key:
                del row["by_day"]
            else:
                row["by_day"] = []
        return run_js(emit(_DRIVE_DISPATCHES, payload=payload,
                           range={"start": day, "end": day}, clicks=0))

    def test_a_row_with_no_usable_split_falls_back_to_its_start_day(self):
        """What an empty — or absent — `by_day` means, pinned in both shapes.

        `[]` is what the server ships for a row that lived on ONE local day: the
        day is `start_date` and the row's own totals are already that day's, so
        selecting on `start_date` is exact rather than a degradation. A row with
        no `by_day` key at all is an OLDER payload, from before the split
        existed, and takes the same path deliberately — a degraded payload
        should render a degraded table rather than an empty one, the same choice
        `sessionForSelection` makes.

        Here the fixture's row is multi-day, so that path is *wrong* for it —
        which is the point: both shapes must reach it, and reach it identically.
        """
        for drop_the_key in (False, True):
            with self.subTest(absent=drop_the_key):
                got = self._drive_without_a_split(self.OLD_DAY, drop_the_key)
                self.assertEqual(len(got["dispatches"]), 1)
                self.assertEqual(got["dispatches"][0]["turns"], 2)
                self.assertEqual(
                    self._drive_without_a_split(self.NEW_DAY,
                                                drop_the_key)["dispatches"], [])

    def test_the_card_beside_it_gets_the_same_range_right(self):
        """The control, and what makes this a disagreement a reader can see:
        `subagent_by_type` carries the day, so the By-Agent-Type card and the
        stat tiles report the range correctly while the table below them does
        not."""
        for day, part in ((self.OLD_DAY, self.OLD), (self.NEW_DAY, self.NEW)):
            with self.subTest(day=day):
                got = self.drive(day)
                self.assertEqual(got["agentTypes"],
                                 [{"agent_type": "Explore", "turns": 1,
                                   "input": part["inp"]}])
                self.assertEqual(got["totals"]["input"], part["inp"])

if __name__ == "__main__":
    unittest.main()
