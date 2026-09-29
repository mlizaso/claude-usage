"""Tests that the project cost tables agree with the stat tiles.

"Cost by Project" and "Cost by Project & Branch" used to be aggregated from
`sessions_all`, whose rows carry a session's LIFETIME totals beside a single
"primary" model and a single last-active date. Two consequences, both reproduced
below against the real scanner and the real client aggregation:

* **Wrong model.** A session spanning opus and haiku was priced entirely at
  whichever model won `_model_priority`/`most_common` — up to 5x out. AGENTS.md
  is explicit that this is wrong: "Aggregating tokens first and applying a
  single price is wrong for sessions that span multiple models."
* **Wrong range.** A session was included whole whenever its last-active day
  fell in range, so one crossing local midnight contributed every token it had
  ever used to "Today" — 101x on the numbers used here.

Both are fixed by aggregating from the server's `project_by_day_model` rollup,
which is per local day and per model. These tests drive the page's real
`applyFilter` under node so they measure what the table would actually show.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import scanner
from cli import calc_cost
from dashboard import get_dashboard_data

from tests.test_dashboard_js import emit, requires_node, run_js

MILLION = 1_000_000

# Drives the page's real applyFilter over a real /api/data payload, capturing
# both the stat-tile totals and the project tables so they can be compared.
_APPLY_FILTER = """(() => {
  rawData = payload;
  selectedModels = new Set(payload.all_models);
  // `range` is either a named range the page knows ('all', 'today', ...) or an
  // explicit {start, end} pair, which we inject by shadowing getRangeBounds —
  // the page has no vocabulary for "just this one historical day".
  if (range && typeof range === 'object') {
    selectedRange = 'all';
    getRangeBounds = () => range;
  } else {
    selectedRange = range;
  }
  let capturedTotals = null;
  renderStats = (t) => { capturedTotals = t; };
  applyFilter();
  return {
    totals: capturedTotals,
    byProject: lastByProject.map(p => ({
      project: p.project, cost: p.cost, input: p.input, output: p.output,
      cache_read: p.cache_read, cache_creation: p.cache_creation,
      cache_creation_1h: p.cache_creation_1h,
      turns: p.turns, sessions: p.sessions, billable: !!p.billable })),
    byBranch: lastByProjectBranch.map(p => ({
      project: p.project, branch: p.branch, cost: p.cost, turns: p.turns,
      sessions: p.sessions })),
    // The Recent Sessions rows, for the one thing that has to NOT move with the
    // branch table: a session still prints its single label.
    sessions: lastFilteredSessions.map(s => ({
      session_id: s.session_id, branch: s.branch, branches: s.branches })),
  };
})()"""


def _assistant(session_id, model, ts, inp, out, message_id, cwd="/home/u/myproj",
               branch="main", cache_read=0, cache_creation=0, cache_creation_1h=0,
               agent_id=None):
    """One assistant record, optionally carrying cache tokens.

    Use nonzero cache-read and both cache-write tiers so project-versus-tile
    comparisons can detect missing cache costs.

    `usage.cache_creation` is the transcript's own split of the flat
    `cache_creation_input_tokens` into its two TTL tiers, with the 5-minute part
    as the remainder — exactly what invariant 6 stores.

    `agent_id` writes the record's `agentId`, which is what makes the scanner
    file the turn as a subagent dispatch — the shape `top_dispatches` reads.
    """
    usage = {"input_tokens": inp, "output_tokens": out,
             "cache_read_input_tokens": cache_read,
             "cache_creation_input_tokens": cache_creation}
    if cache_creation:
        usage["cache_creation"] = {
            "ephemeral_1h_input_tokens": cache_creation_1h,
            "ephemeral_5m_input_tokens": cache_creation - cache_creation_1h,
        }
    record = {
        "type": "assistant", "sessionId": session_id, "timestamp": ts,
        "cwd": cwd, "gitBranch": branch,
        "message": {"id": message_id, "model": model, "content": [],
                    "usage": usage},
    }
    if agent_id:
        record["agentId"] = agent_id
    return json.dumps(record)


class _ScannedFixture(unittest.TestCase):
    """Writes a real transcript, runs the real scanner, reads the real API."""

    def scan_records(self, records):
        tmp = Path(tempfile.mkdtemp())
        projects = tmp / "projects" / "u" / "proj"
        projects.mkdir(parents=True)
        (projects / "sess.jsonl").write_text("\n".join(records) + "\n",
                                             encoding="utf-8")
        db = tmp / "usage.db"
        scanner.scan(projects_dir=tmp / "projects", db_path=db, verbose=False)
        return get_dashboard_data(db)

    def apply_filter(self, payload, date_range="all"):
        """Run the page's own applyFilter and read the numbers back out.

        `renderStats` is shadowed to capture the totals object the stat tiles
        receive — comparing the project table against *that* is the whole point,
        since the two are independent aggregations of one dataset.
        """
        return run_js(emit(_APPLY_FILTER, payload=payload, range=date_range))


@requires_node
class TestMixedModelSessionIsPricedPerModel(_ScannedFixture):
    """One opus turn + ten haiku turns in a single session."""

    def setUp(self):
        records = [_assistant("s1", "claude-opus-4-8", "2026-04-08T10:00:00Z",
                              100, 100, "m-opus")]
        for i in range(10):
            records.append(_assistant(
                "s1", "claude-haiku-4-5", f"2026-04-08T10:{i + 1:02d}:00Z",
                200_000, 40_000, f"m-haiku-{i}"))
        self.payload = self.scan_records(records)
        # Ground truth, computed per turn exactly as cli.py does.
        self.truth = (calc_cost("claude-opus-4-8", 100, 100, 0, 0)
                      + calc_cost("claude-haiku-4-5", 2_000_000, 400_000, 0, 0))

    def test_the_session_really_is_mixed_model(self):
        """Guard the fixture: the defect needs one model recorded per session."""
        session = self.payload["sessions_all"][0]
        models = {r["model"] for r in self.payload["project_by_day_model"]}
        self.assertEqual(len(models), 2, "fixture is not mixed-model")
        self.assertIn(session["model"], models)

    def test_cost_by_project_matches_the_per_turn_truth(self):
        result = self.apply_filter(self.payload)
        self.assertEqual(len(result["byProject"]), 1)
        self.assertAlmostEqual(result["byProject"][0]["cost"], self.truth, places=6)

    def test_cost_by_branch_matches_the_per_turn_truth(self):
        """Every record here carries `main`, so the branch table must collapse to
        exactly ONE row. Asserting the row count as well as the cost is what
        keeps this honest now that the branch is a per-turn column: a rollup that
        split a single-branch session would still pass a bare `[0]["cost"]`
        check on a row holding a fraction of the money."""
        result = self.apply_filter(self.payload)
        self.assertEqual([p["branch"] for p in result["byBranch"]], ["main"])
        self.assertAlmostEqual(result["byBranch"][0]["cost"], self.truth, places=6)

    def test_project_cost_is_not_the_single_model_figure(self):
        """The old behaviour, stated as a number so it cannot creep back."""
        wrong = calc_cost("claude-opus-4-8", 2_000_100, 400_100, 0, 0)
        result = self.apply_filter(self.payload)
        self.assertNotAlmostEqual(result["byProject"][0]["cost"], wrong, places=4)
        self.assertLess(result["byProject"][0]["cost"], wrong / 2)


@requires_node
class TestSessionCrossingMidnightIsRangeScoped(_ScannedFixture):
    """A session whose turns fall on two different local days."""

    def setUp(self):
        from datetime import datetime, time, timedelta, timezone
        from datetime import date as ddate

        def utc_for(day_offset, hour):
            local = datetime.combine(ddate(2026, 4, 8) + timedelta(days=day_offset),
                                     time(hour, 0))
            return (local.astimezone().astimezone(timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%S.000Z"))

        self.day_one = "2026-04-08"
        self.day_two = "2026-04-09"
        # Same model throughout, so the mixed-model defect cannot contribute.
        self.payload = self.scan_records([
            _assistant("s1", "claude-sonnet-4-6", utc_for(0, 12), MILLION, 200_000, "m-1"),
            _assistant("s1", "claude-sonnet-4-6", utc_for(1, 1), 10_000, 2_000, "m-2"),
        ])
        self.big = calc_cost("claude-sonnet-4-6", MILLION, 200_000, 0, 0)
        self.small = calc_cost("claude-sonnet-4-6", 10_000, 2_000, 0, 0)

    def test_fixture_spans_two_local_days(self):
        days = sorted({r["day"] for r in self.payload["project_by_day_model"]})
        self.assertEqual(days, [self.day_one, self.day_two])

    def test_each_day_gets_only_its_own_cost(self):
        first = self.apply_filter(self.payload, {"start": self.day_one, "end": self.day_one})
        second = self.apply_filter(self.payload, {"start": self.day_two, "end": self.day_two})
        self.assertAlmostEqual(first["byProject"][0]["cost"], self.big, places=6)
        self.assertAlmostEqual(second["byProject"][0]["cost"], self.small, places=6)

    def test_the_later_day_is_not_charged_the_whole_session(self):
        """The 101x defect: day two used to bill the session's entire life."""
        second = self.apply_filter(self.payload, {"start": self.day_two, "end": self.day_two})
        self.assertLess(second["byProject"][0]["cost"], self.big / 10)

    def test_all_time_still_sums_to_the_whole_session(self):
        every = self.apply_filter(self.payload, "all")
        self.assertAlmostEqual(every["byProject"][0]["cost"],
                               self.big + self.small, places=6)


@requires_node
class TestProjectTableAgreesWithTheStatTiles(_ScannedFixture):
    """The tiles and the project table are separate code paths over one dataset."""

    # Every money-bearing column non-zero. The tiles are costed from
    # `daily_by_model` and the table from `project_by_day_model`, so the
    # equality below is only a check on the columns the fixture actually
    # exercises — and it exercised two of five.
    MONEY_COLUMNS = ("input", "output", "cache_read", "cache_creation",
                     "cache_creation_1h")

    def setUp(self):
        records = []
        for i in range(6):
            records.append(_assistant(
                "s1", "claude-opus-4-8" if i % 2 else "claude-haiku-4-5",
                f"2026-04-0{i + 1}T12:00:00Z", 100_000 * (i + 1), 10_000, f"m-{i}",
                cache_read=50_000 * (i + 1),
                cache_creation=20_000 * (i + 1),
                cache_creation_1h=8_000 * (i + 1)))
        self.payload = self.scan_records(records)

    def test_the_fixture_carries_every_money_bearing_column(self):
        """Guard the guard. A fixture whose cache columns are all zero makes
        every comparison below agree 0 == 0 on three of the five, which is how
        `project_by_day_model` came to be able to drop them unnoticed."""
        rows = self.payload["project_by_day_model"]
        for column in self.MONEY_COLUMNS:
            with self.subTest(column=column):
                self.assertGreater(sum(r[column] for r in rows), 0)

    def test_project_costs_sum_to_the_total_cost(self):
        for date_range in ("all", "90d"):
            with self.subTest(range=date_range):
                result = self.apply_filter(self.payload, date_range)
                summed = sum(p["cost"] for p in result["byProject"])
                self.assertAlmostEqual(summed, result["totals"]["cost"], places=6)

    def test_project_tokens_sum_to_the_total_tokens(self):
        result = self.apply_filter(self.payload)
        for column in self.MONEY_COLUMNS + ("turns",):
            with self.subTest(column=column):
                self.assertEqual(sum(p[column] for p in result["byProject"]),
                                 result["totals"][column])


class TestSessionRowsCarryEveryTokenColumn(_ScannedFixture):
    """`sessions_all` is a cost-bearing rollup too, and nothing priced it.

    Its row totals come from the `sessions` table and its per-model split from a
    second SELECT over `turns`, so either half can lose a column on its own —
    both did, silently, under a sandbox mutation that blanked the 1-hour write
    tier on each. Compared against `daily_by_model`, which is the rollup the
    stat tiles are built from, so a disagreement here is money on screen
    disagreeing with money on screen.
    """

    TOKEN_COLUMNS = ("input", "output", "cache_read", "cache_creation",
                     "cache_creation_1h")

    def setUp(self):
        self.payload = self.scan_records([
            _assistant("s1", "claude-opus-4-8", "2026-04-08T10:00:00Z",
                       120_000, 9_000, "m-1", cache_read=70_000,
                       cache_creation=30_000, cache_creation_1h=11_000),
            _assistant("s1", "claude-haiku-4-5", "2026-04-08T10:05:00Z",
                       400_000, 40_000, "m-2", cache_read=250_000,
                       cache_creation=60_000, cache_creation_1h=25_000),
            _assistant("s2", "claude-sonnet-4-6", "2026-04-09T11:00:00Z",
                       90_000, 7_000, "m-3", cache_read=15_000,
                       cache_creation=9_000, cache_creation_1h=4_000),
        ])

    def daily(self, column):
        return sum(r[column] for r in self.payload["daily_by_model"])

    def test_the_fixture_carries_every_token_column(self):
        for column in self.TOKEN_COLUMNS:
            with self.subTest(column=column):
                self.assertGreater(self.daily(column), 0)

    def test_session_row_totals_match_the_daily_rollup(self):
        for column in self.TOKEN_COLUMNS:
            with self.subTest(column=column):
                self.assertEqual(
                    sum(s[column] for s in self.payload["sessions_all"]),
                    self.daily(column))

    def test_the_per_model_split_matches_the_daily_rollup(self):
        """The split is what the model filter sums, so it prices the session."""
        for column in self.TOKEN_COLUMNS:
            with self.subTest(column=column):
                self.assertEqual(
                    sum(m[column] for s in self.payload["sessions_all"]
                        for m in s["by_model"]),
                    self.daily(column))

    def test_the_split_costs_the_same_as_the_daily_rollup(self):
        """Per-turn costing means per-model rows on both sides; a column lost
        from either one is money that quietly leaves the page."""
        def cost(rows):
            return sum(calc_cost(r["model"], r["input"], r["output"],
                                 r["cache_read"], r["cache_creation"],
                                 r["cache_creation_1h"]) for r in rows)

        by_model = [m for s in self.payload["sessions_all"] for m in s["by_model"]]
        self.assertGreater(cost(by_model), 0)
        self.assertAlmostEqual(cost(by_model),
                               cost(self.payload["daily_by_model"]), places=9)


class TestDispatchRowsCarryEveryTokenColumn(_ScannedFixture):
    """`top_dispatches` is priced per row, and none of its columns were checked.

    The client collapses these rows back per `agent_id` and costs each one at
    its own model — the reason the rollup is grouped by `(agent_id, model)` and
    not by `agent_id` alone. So a column that stops being summed here is money
    missing from the Top Dispatches table and from its CSV export, and blanking
    the 1-hour write tier in this query left the whole suite green.

    Every turn in the fixture is a subagent turn, so the dispatch rows have to
    account for exactly what `daily_by_model` accounts for. Two dispatches, two
    models each, so the per-(dispatch, model) split is exercised rather than
    assumed.
    """

    TOKEN_COLUMNS = ("input", "output", "cache_read", "cache_creation",
                     "cache_creation_1h")

    def setUp(self):
        records = []
        for i, (agent, model) in enumerate((
                ("agent-a", "claude-opus-4-8"), ("agent-a", "claude-haiku-4-5"),
                ("agent-b", "claude-sonnet-4-6"), ("agent-b", "claude-opus-4-8"))):
            records.append(_assistant(
                "s1", model, f"2026-04-08T1{i}:00:00Z",
                100_000 * (i + 1), 12_000 * (i + 1), f"m-{i}",
                cache_read=40_000 * (i + 1), cache_creation=25_000 * (i + 1),
                cache_creation_1h=9_000 * (i + 1), agent_id=agent))
        self.payload = self.scan_records(records)

    def daily(self, column):
        return sum(r[column] for r in self.payload["daily_by_model"])

    def test_the_fixture_really_is_two_dispatches_over_two_models_each(self):
        rows = self.payload["top_dispatches"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({r["agent_id"] for r in rows}), 2)
        for column in self.TOKEN_COLUMNS:
            with self.subTest(column=column):
                self.assertGreater(self.daily(column), 0)

    def test_dispatch_tokens_match_the_daily_rollup(self):
        for column in self.TOKEN_COLUMNS:
            with self.subTest(column=column):
                self.assertEqual(
                    sum(r[column] for r in self.payload["top_dispatches"]),
                    self.daily(column))

    def test_dispatch_cost_matches_the_daily_rollup(self):
        def cost(rows):
            return sum(calc_cost(r["model"], r["input"], r["output"],
                                 r["cache_read"], r["cache_creation"],
                                 r["cache_creation_1h"]) for r in rows)

        self.assertGreater(cost(self.payload["top_dispatches"]), 0)
        self.assertAlmostEqual(cost(self.payload["top_dispatches"]),
                               cost(self.payload["daily_by_model"]), places=9)


@requires_node
class TestUnpricedProjectsReadAsNotApplicable(_ScannedFixture):
    def setUp(self):
        self.payload = self.scan_records([
            _assistant("s1", "gemma-3-27b-local", "2026-04-08T12:00:00Z",
                       5_000_000, 900_000, "m-1"),
        ])

    def test_a_local_model_project_is_not_marked_billable(self):
        """Otherwise the table asserts a 5.9M-token project was free."""
        result = self.apply_filter(self.payload)
        self.assertEqual(len(result["byProject"]), 1)
        self.assertFalse(result["byProject"][0]["billable"])
        self.assertEqual(result["byProject"][0]["cost"], 0)


class _BranchSwitchFixture(_ScannedFixture):
    """One session, one project, one model, one day — two branches.

    Everything except the branch is held constant so nothing else can split the
    rows: change this fixture's model or its day instead and the table already
    produced two rows, which is what showed the grain was wrong only for branch.
    The cheap turn is first so it is also the session's label, the shape that
    filed 99.7% of the money under a branch it was not spent on.
    """

    CHEAP = (1_000, 1_000)
    DEAR = (MILLION, 200_000)
    MODEL = "claude-opus-5"

    def setUp(self):
        self.payload = self.scan_records([
            _assistant("s1", self.MODEL, "2026-04-08T10:00:00Z",
                       *self.CHEAP, "m1", branch="main"),
            _assistant("s1", self.MODEL, "2026-04-08T11:00:00Z",
                       *self.DEAR, "m2", branch="feature"),
        ])
        self.cheap = calc_cost(self.MODEL, *self.CHEAP, 0, 0)
        self.dear = calc_cost(self.MODEL, *self.DEAR, 0, 0)


class TestABranchSwitchSplitsTheMoneyItSpent(_BranchSwitchFixture):
    """The finding: `Cost by Project & Branch` split money by a branch stored
    once per SESSION while `gitBranch` is on every record.

    A replay with a different checkout must not move earlier usage to the
    later branch. Compare per-turn attribution before and after the replay.

    These assertions read the payload rather than the rendered table, so they
    run where node does not — the client half is the class below.
    """

    def test_the_fixture_switches_branch_and_nothing_else(self):
        """Guard the fixture: one project, one model, one day, two branches."""
        rows = self.payload["project_by_day_model"]
        self.assertEqual({r["project"] for r in rows}, {"u/myproj"})
        self.assertEqual({r["model"] for r in rows}, {self.MODEL})
        self.assertEqual({r["day"] for r in rows}, {"2026-04-08"})
        self.assertEqual(self.payload["sessions_all"][0]["branch"], "main")

    def test_each_branch_gets_only_the_money_spent_on_it(self):
        by_branch = {r["branch"]: calc_cost(
            r["model"], r["input"], r["output"], r["cache_read"],
            r["cache_creation"], r["cache_creation_1h"])
            for r in self.payload["project_by_day_model"]}
        self.assertEqual(sorted(by_branch), ["feature", "main"])
        self.assertAlmostEqual(by_branch["main"], self.cheap, places=6)
        self.assertAlmostEqual(by_branch["feature"], self.dear, places=6)

    def test_the_expensive_branch_is_not_filed_under_the_session_label(self):
        """The old behaviour as a number so it cannot creep back: one row
        holding both turns' tokens under `main`."""
        rows = [r for r in self.payload["project_by_day_model"]
                if r["branch"] == "main"]
        self.assertEqual(len(rows), 1)
        self.assertLess(rows[0]["input"], self.DEAR[0])


@requires_node
class TestTheBranchTableShowsTheSplit(_BranchSwitchFixture):
    """The same fixture through the page's own `applyFilter`."""

    def test_the_table_renders_a_row_per_branch(self):
        result = self.apply_filter(self.payload)
        rendered = {p["branch"]: p["cost"] for p in result["byBranch"]}
        self.assertEqual(sorted(rendered), ["feature", "main"])
        self.assertAlmostEqual(rendered["main"], self.cheap, places=6)
        self.assertAlmostEqual(rendered["feature"], self.dear, places=6)

    def test_the_branch_rows_still_sum_to_the_project_and_the_tiles(self):
        """The branch split must redistribute the money, never change it."""
        result = self.apply_filter(self.payload)
        summed = sum(p["cost"] for p in result["byBranch"])
        self.assertAlmostEqual(summed, self.cheap + self.dear, places=6)
        self.assertAlmostEqual(summed, result["byProject"][0]["cost"], places=6)
        self.assertAlmostEqual(summed, result["totals"]["cost"], places=6)
        self.assertEqual(sum(p["turns"] for p in result["byBranch"]),
                         result["totals"]["turns"])

    def test_the_sessions_column_counts_a_session_on_each_branch_it_ran_on(self):
        """FLIPPED LANDMINE — this asserted `{"main": 1, "feature": 0}`.

        Project and branch turn counts must be grouped at the same grain as
        their costs, including responses completed after an incremental
        boundary.

        The values below — 1 and 1 — are the ones the old docstring named as
        correct. `sessions_all[].by_day_model` now carries the branch, the
        payload key set in `tests/test_payload_surface.py` declares it, and
        `sessionFromParts` derives the branch set from the same filtered rows
        this table's money comes from. `tests/test_sessions_range_attribution.py
        ::test_the_branch_table_counts_them_the_same_way` could not see any of
        it while its fixture had one branch; it has two now.
        """
        result = self.apply_filter(self.payload)
        rendered = {p["branch"]: p["sessions"] for p in result["byBranch"]}
        self.assertEqual(rendered, {"main": 1, "feature": 1})

    def test_the_session_row_itself_still_carries_its_one_label(self):
        """The count moved to turn grain; `sessions.git_branch` did not.

        The sessions table prints one branch per session and the first-non-empty
        rule that picks it is invariant 8. Counting the branch table off the
        per-day rows is what let that stay true — the alternative, restamping
        the session label, would have reintroduced the scan-path dependence that
        rule exists to prevent."""
        result = self.apply_filter(self.payload)
        self.assertEqual([s["branch"] for s in result["sessions"]], ["main"])
        self.assertEqual(self.payload["sessions_all"][0]["branch"], "main")
        # And the set the count is taken from is beside it, not instead of it.
        self.assertEqual([sorted(s["branches"]) for s in result["sessions"]],
                         [["feature", "main"]])


class TestARowWithNoTurnBranchKeepsTheSessionLabel(_ScannedFixture):
    """The COALESCE onto `sessions.git_branch` is load-bearing, not defensive.

    Missing per-turn branches fall back to the session branch. This handles
    absent transcript metadata and Codex sessions without per-turn branches."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        projects = self.tmp / "projects" / "u" / "proj"
        projects.mkdir(parents=True)
        (projects / "sess.jsonl").write_text("\n".join([
            _assistant("s1", "claude-opus-5", "2026-04-08T10:00:00Z",
                       1_000, 1_000, "m1", branch="main"),
        ]) + "\n", encoding="utf-8")
        self.db = self.tmp / "usage.db"
        scanner.scan(projects_dir=self.tmp / "projects", db_path=self.db,
                     verbose=False)

    def _blank_the_turn_branches(self):
        """Exactly what a database migrated but not yet re-read looks like."""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE turns SET git_branch = ''")
            conn.commit()
        finally:
            conn.close()

    def test_a_pre_migration_row_still_reports_the_session_branch(self):
        self._blank_the_turn_branches()
        rows = get_dashboard_data(self.db)["project_by_day_model"]
        self.assertEqual([r["branch"] for r in rows], ["main"])

    def test_the_fallback_is_what_supplies_it(self):
        """Discriminates: blank BOTH and the row reports no branch, so the test
        above cannot be passing on some other row's value."""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE turns SET git_branch = ''")
            conn.execute("UPDATE sessions SET git_branch = ''")
            conn.commit()
        finally:
            conn.close()
        rows = get_dashboard_data(self.db)["project_by_day_model"]
        self.assertEqual([r["branch"] for r in rows], [""])

    @requires_node
    def test_the_sessions_column_falls_back_the_same_way_the_money_does(self):
        """The client half of the same COALESCE, and the whole of what keeps
        Codex working.

        `sessions_all[].by_day_model[].branch` is the raw turn value, so on a
        row the backfill cannot reach — and on EVERY Codex row, which stores no
        per-turn branch by design — it is blank while the money row beside it
        has already fallen back to the session label. Count the blank and the
        two keys stop agreeing again, which is the same `Sessions 0` the branch
        split shipped: `sessionFromParts` therefore resolves a blank part
        against `s.branch` exactly as the SQL resolves it against
        `s.git_branch`.
        """
        self._blank_the_turn_branches()
        result = self.apply_filter(get_dashboard_data(self.db))
        self.assertEqual([(p["branch"], p["sessions"])
                          for p in result["byBranch"]], [("main", 1)])


if __name__ == "__main__":
    unittest.main()
