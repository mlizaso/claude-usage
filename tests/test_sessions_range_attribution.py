"""The Recent Sessions table, its CSV, and the session counts beside them.

Every other cost-bearing card on the page is built from a rollup keyed by local
day, so it can be range-filtered and priced per model. The sessions view was
not: `applyFilter` picked `sessions_all` rows on `last_date` alone and then
rendered each row's LIFETIME totals. A session was therefore

* Range selection must include only turns within the requested local days,
including sessions that started earlier or finish later.

That is the exact failure AGENTS.md records for the project tables ("credited
its whole history to whatever range that date falls in (101x on a session
crossing local midnight)") and which was fixed there by moving them onto
`project_by_day_model`. The sessions view kept it, and its CSV — whose `Est.
Cost` column is the one thing a reader will sum — kept it too.

`tests/test_local_day_bucketing.py` could not see any of this: its session
fixture is a single turn, so lifetime == in-range by construction.

The fix is the same shape: `rollups.sessions_all` now hands each session a
`by_day_model` split, and `sessionForSelection` sums only the (day, model) rows
inside the range. What a per-day rollup CANNOT supply — the session id, project,
title, primary model, Last Active and Duration — still comes from the session
row, so two of those columns are deliberately whole-session values sitting
beside range-scoped money; `test_session_metadata_stays_whole_session` pins that
choice so it is a decision rather than an oversight.

Two smaller `web/js/40-filters.js` defects are pinned at the foot of this file.
They belong with the filter, not with the sessions table, and would sit more
naturally in `tests/test_dashboard_js.py` — they are here because this file and
that one were assigned to different writers in the same pass.
"""

import json
import os
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import dashboard_data
import scanner
from cli import calc_cost
from dashboard import get_dashboard_data

from tests.test_dashboard_js import emit, requires_node, run_js
from tests.test_local_day_bucketing import TZ_AHEAD, requires_tzset
from tests.timestamps import local_day_of, utc_ts_on_local_day

MILLION = 1_000_000

OPUS = "claude-opus-4-8"      # $5 / $25 per M in / out
HAIKU = "claude-haiku-4-5"    # $1 / $5
SONNET = "claude-sonnet-4-6"  # $3 / $15

# Drives the page's real applyFilter, then its real CSV exporter over the very
# array the table was rendered from — the finding is about both, and a fix
# applied to one alone would show up as a disagreement here.
_DRIVE = """(() => {
  rawData = payload;
  selectedSource = 'claude';
  selectedModels = new Set(models === null ? payload.all_models : models);
  // Either a named range the page knows, or an explicit {start, end} pair
  // injected by shadowing getRangeBounds — the page has no vocabulary for
  // "just this one historical day".
  if (range && typeof range === 'object') {
    selectedRange = 'all';
    getRangeBounds = () => range;
  } else {
    selectedRange = range;
  }
  let totals = null;
  renderStats = (t) => { totals = t; };
  applyFilter();
  let csv = null;
  downloadCSV = (name, header, rows) => { csv = {name: name, header: header, rows: rows}; };
  exportSessionsCSV();
  return {
    totals: totals,
    sessions: lastFilteredSessions.map(s => ({
      session_id: s.session_id, model: s.model, cost: s.cost, turns: s.turns,
      input: s.input, output: s.output, cache_read: s.cache_read,
      cache_creation: s.cache_creation, cache_creation_1h: s.cache_creation_1h,
      duration_min: s.duration_min, last: s.last, billable: !!s.billable })),
    byProject: lastByProject.map(p => ({
      project: p.project, cost: p.cost, turns: p.turns, sessions: p.sessions })),
    byBranch: lastByProjectBranch.map(p => ({
      project: p.project, branch: p.branch, sessions: p.sessions,
      cost: p.cost, turns: p.turns })),
    csv: csv,
  };
})()"""


def _assistant(session_id, model, ts, message_id, inp, out, cache_read,
               cache_creation, cache_creation_1h, cwd="/home/u/myproj",
               branch="main"):
    """One assistant record, with every money-bearing column non-zero.

    None of the cache arguments has a default, deliberately. With them at zero
    three of the five columns compare 0 == 0 in every assertion below, and the
    new per-day query could drop cache reads or the 1-hour write tier with this
    file green — which is exactly the trap `test_project_cost_attribution.py`
    documents for the rollup it guards.

    `usage.cache_creation` is the transcript's own split of the flat
    `cache_creation_input_tokens` into its two TTL tiers, with the 5-minute part
    as the remainder — what invariant 6 stores.
    """
    return json.dumps({
        "type": "assistant", "sessionId": session_id, "timestamp": ts,
        "cwd": cwd, "gitBranch": branch,
        "message": {"id": message_id, "model": model, "content": [],
                    "usage": {
                        "input_tokens": inp, "output_tokens": out,
                        "cache_read_input_tokens": cache_read,
                        "cache_creation_input_tokens": cache_creation,
                        "cache_creation": {
                            "ephemeral_1h_input_tokens": cache_creation_1h,
                            "ephemeral_5m_input_tokens":
                                cache_creation - cache_creation_1h,
                        }}},
    })


# The branches the fixture's turns were produced on. `s-crosser` checks out
# BRANCH_NEW between its two days, so its stored session label — first non-empty
# wins, invariant 8 — is BRANCH_OLD while half its money was spent elsewhere.
# That is the shape the Sessions column of `Cost by Project & Branch` has to
# survive, and with one branch here it could not be seen at all.
BRANCH_OLD = "main"
BRANCH_NEW = "feature"

# The three turns of the fixture, as (model, tokens) pairs, so the expected
# costs below are computed from the same numbers the transcript carries.
CROSSER_ON_THE_OLD_DAY = (HAIKU, (4 * MILLION, 400_000, MILLION, 200_000, 80_000))
CROSSER_ON_THE_NEW_DAY = (OPUS, (100_000, 10_000, 50_000, 20_000, 8_000))
OTHER_ON_THE_NEW_DAY = (SONNET, (2 * MILLION, 200_000, 300_000, 60_000, 25_000))


def _cost(turn):
    model, tokens = turn
    return calc_cost(model, *tokens)


class _ScannedFixture(unittest.TestCase):
    """Writes a real transcript, runs the real scanner, reads the real API.

    `s-crosser` spans two local days two days apart and uses a different model
    on each, so a range covering one of them has a different cost AND a
    different top-token model from the session's lifetime. `s-today` gives the
    newer day a second session, so a count of "sessions in range" is not
    trivially the count of all of them.

    `s-crosser` also changes BRANCH between those days, and `s-today` stays on
    the first one. That is the third dimension of the same defect: the money in
    `Cost by Project & Branch` is per turn, so a count keyed on the session's
    single label puts a session on a branch it left and misses the branch it
    moved to. With one branch in the transcript the two keys agree by
    construction and the column is untested — which is exactly how it came to
    render money against zero sessions with this file green.
    """

    @classmethod
    def setUpClass(cls):
        old_ts = utc_ts_on_local_day(2, 10)
        new_ts = utc_ts_on_local_day(0, 10)
        today_other_ts = utc_ts_on_local_day(0, 11)
        cls.DAY_OLD = local_day_of(old_ts)
        cls.DAY_NEW = local_day_of(new_ts)
        cls._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmpdir.name)
        projects = tmp / "projects" / "u" / "proj"
        projects.mkdir(parents=True)
        (projects / "sess.jsonl").write_text("\n".join([
            _assistant("s-crosser", CROSSER_ON_THE_OLD_DAY[0],
                       old_ts, "m-c1",
                       *CROSSER_ON_THE_OLD_DAY[1], branch=BRANCH_OLD),
            _assistant("s-crosser", CROSSER_ON_THE_NEW_DAY[0],
                       new_ts, "m-c2",
                       *CROSSER_ON_THE_NEW_DAY[1], branch=BRANCH_NEW),
            _assistant("s-today", OTHER_ON_THE_NEW_DAY[0],
                       today_other_ts, "m-t1",
                       *OTHER_ON_THE_NEW_DAY[1], branch=BRANCH_OLD),
        ]) + "\n", encoding="utf-8")
        db = tmp / "usage.db"
        scanner.scan(projects_dir=tmp / "projects", db_path=db, verbose=False)
        cls.payload = get_dashboard_data(db)

    @classmethod
    def tearDownClass(cls):
        dashboard_data.reset_payload_cache()
        cls._tmpdir.cleanup()

    # What each slice of the fixture really costs, per turn and per model,
    # exactly as cli.py would compute it.
    OLD_ONLY = _cost(CROSSER_ON_THE_OLD_DAY)
    CROSSER_NEW = _cost(CROSSER_ON_THE_NEW_DAY)
    TODAY_OTHER = _cost(OTHER_ON_THE_NEW_DAY)
    CROSSER_LIFETIME = OLD_ONLY + CROSSER_NEW

    def drive(self, date_range="all", models=None):
        return run_js(emit(_DRIVE, payload=self.payload, range=date_range,
                           models=models))

    def session(self, result, session_id):
        rows = [s for s in result["sessions"] if s["session_id"] == session_id]
        self.assertEqual(len(rows), 1,
                         f"expected exactly one {session_id} row, got {rows}")
        return rows[0]

    # The export writes each row's cost to four decimals, so a sum of N rows
    # can sit up to N/2 x 1e-4 from the exact figure. Three places is inside
    # that for this fixture and far outside the defect, which is a factor.
    CSV_PLACES = 3

    def csv_cost(self, result):
        """The `Est. Cost` column of the export, summed as a spreadsheet would."""
        header = result["csv"]["header"]
        column = header.index("Est. Cost")
        return sum(float(row[column]) for row in result["csv"]["rows"]
                   if row[column] != "")


class TestFixtureSetupAcrossMidnight(unittest.TestCase):
    """Expected days follow the timestamps even when setup's clock advances."""

    def test_expected_days_are_derived_from_the_written_instants(self):
        class Probe(_ScannedFixture):
            pass

        with patch("tests.timestamps.date") as mocked_date:
            mocked_date.today.side_effect = [
                date(2026, 8, 18),
                date(2026, 8, 19),
                date(2026, 8, 19),
            ]
            Probe.setUpClass()
        try:
            days = sorted({
                row["day"] for row in Probe.payload["project_by_day_model"]
            })
            self.assertEqual(days, [Probe.DAY_OLD, Probe.DAY_NEW])
            self.assertEqual(days, ["2026-08-16", "2026-08-19"])
        finally:
            Probe.tearDownClass()


@requires_node
class TestTheFixtureIsDiscriminating(_ScannedFixture):
    """Every assertion below rests on these; none of them is free."""

    def test_the_crossing_session_really_spans_two_local_days(self):
        days = sorted({r["day"] for r in self.payload["project_by_day_model"]})
        self.assertEqual(days, [self.DAY_OLD, self.DAY_NEW])

    def test_its_last_active_day_is_the_newer_one(self):
        """Which is what made `last_date` a plausible-looking selector."""
        row = next(s for s in self.payload["sessions_all"]
                   if s["session_id"] == "s-crosser")
        self.assertEqual(row["last_date"], self.DAY_NEW)

    def test_the_older_day_carries_most_of_the_money(self):
        """So crediting it to the newer day is a factor, not a rounding error."""
        self.assertGreater(self.OLD_ONLY, 5 * self.CROSSER_NEW)

    def test_every_money_bearing_column_is_exercised(self):
        """Guard the guard. Three of the five compare 0 == 0 in every
        assertion below if the fixture leaves the cache columns empty."""
        for column in ("input", "output", "cache_read", "cache_creation",
                       "cache_creation_1h"):
            with self.subTest(column=column):
                self.assertGreater(
                    sum(r[column] for r in self.payload["daily_by_model"]), 0)

    def test_each_day_uses_a_different_model(self):
        """So the row's Model column has to be recomputed from the slice.

        It also makes this fixture blind to half of the split it exercises:
        with one model per day, `(session_id, model)` separates the days on its
        own and the `day` term of the GROUP BY does nothing here. That is why
        `TestOneModelOnTwoDaysStaysTwoRows` below carries a second fixture —
        it is not redundant with this one, it is the only thing that can fail
        when the day key is dropped.
        """
        by_day = {}
        for r in self.payload["project_by_day_model"]:
            by_day.setdefault(r["day"], set()).add(r["model"])
        self.assertEqual(by_day[self.DAY_OLD], {HAIKU})
        self.assertEqual(by_day[self.DAY_NEW], {OPUS, SONNET})

    def test_the_crossing_session_leaves_the_branch_it_is_labelled_with(self):
        """So the Sessions column of the branch table cannot pass by luck.

        The money rows already carry the branch each TURN was produced on. The
        session carries one label — the first branch it saw — so here the two
        disagree: `s-crosser` is labelled BRANCH_OLD and spent its newer day on
        BRANCH_NEW. Hold the fixture to one branch and a count keyed on the
        label and a count keyed on the turns give the same answer for every
        range, which is why the column shipped broken.
        """
        by_branch = {}
        for r in self.payload["project_by_day_model"]:
            by_branch.setdefault(r["branch"], set()).add(r["day"])
        self.assertEqual(by_branch,
                         {BRANCH_OLD: {self.DAY_OLD, self.DAY_NEW},
                          BRANCH_NEW: {self.DAY_NEW}})
        labels = {s["session_id"]: s["branch"]
                  for s in self.payload["sessions_all"]}
        self.assertEqual(labels, {"s-crosser": BRANCH_OLD,
                                  "s-today": BRANCH_OLD})


@requires_node
class TestSessionRowsAreScopedToTheRange(_ScannedFixture):
    """The over-report half: lifetime totals under a one-day label."""

    def setUp(self):
        self.result = self.drive({"start": self.DAY_NEW,
                                  "end": self.DAY_NEW})

    def test_the_row_carries_only_the_days_in_range(self):
        row = self.session(self.result, "s-crosser")
        self.assertAlmostEqual(row["cost"], self.CROSSER_NEW, places=6)
        self.assertEqual(row["turns"], 1)
        for column, expected in zip(
                ("input", "output", "cache_read", "cache_creation",
                 "cache_creation_1h"), CROSSER_ON_THE_NEW_DAY[1]):
            with self.subTest(column=column):
                self.assertEqual(row[column], expected)

    def test_the_row_is_not_the_whole_lifetime(self):
        """The defect, stated as a number so it cannot creep back."""
        row = self.session(self.result, "s-crosser")
        self.assertLess(row["cost"], self.CROSSER_LIFETIME / 5)

    def test_the_model_column_names_a_model_whose_tokens_are_in_the_row(self):
        """Lifetime it is haiku; on this day the session only used opus."""
        self.assertEqual(self.session(self.result, "s-crosser")["model"], OPUS)

    def test_the_csv_exports_the_same_figures(self):
        self.assertAlmostEqual(self.csv_cost(self.result),
                               self.CROSSER_NEW + self.TODAY_OTHER,
                               places=self.CSV_PLACES)


@requires_node
class TestSessionsActiveInRangeAreListed(_ScannedFixture):
    """The under-report half, which no caption could have fixed."""

    def setUp(self):
        self.result = self.drive({"start": self.DAY_OLD,
                                  "end": self.DAY_OLD})

    def test_a_session_last_active_later_is_still_listed(self):
        row = self.session(self.result, "s-crosser")
        self.assertAlmostEqual(row["cost"], self.OLD_ONLY, places=6)
        self.assertEqual(row["turns"], 1)

    def test_the_model_column_names_that_days_model(self):
        self.assertEqual(self.session(self.result, "s-crosser")["model"], HAIKU)

    def test_a_session_with_no_turns_in_range_is_not_listed(self):
        """The selection is still a selection: s-today ran only on DAY_NEW."""
        self.assertEqual([s["session_id"] for s in self.result["sessions"]],
                         ["s-crosser"])


@requires_node
class TestTheSessionsTableReconcilesWithTheStatTiles(_ScannedFixture):
    """Two independent aggregations of one dataset, under one range label.

    The tiles are costed from `daily_by_model`, the table from the sessions
    rollup, so an equality here is money on screen agreeing with money on
    screen — the property the reader assumes and the one that failed.
    """

    RANGES = ("all", "today", "7d", "30d")

    def test_the_cost_column_sums_to_the_est_cost_tile(self):
        for date_range in self.RANGES:
            with self.subTest(range=date_range):
                result = self.drive(date_range)
                summed = sum(s["cost"] for s in result["sessions"])
                self.assertAlmostEqual(summed, result["totals"]["cost"],
                                       places=6)

    def test_the_turn_counts_sum_to_the_turns_tile(self):
        for date_range in self.RANGES:
            with self.subTest(range=date_range):
                result = self.drive(date_range)
                self.assertEqual(sum(s["turns"] for s in result["sessions"]),
                                 result["totals"]["turns"])

    def test_the_csv_totals_what_the_page_displays(self):
        for date_range in self.RANGES:
            with self.subTest(range=date_range):
                result = self.drive(date_range)
                self.assertAlmostEqual(self.csv_cost(result),
                                       result["totals"]["cost"],
                                       places=self.CSV_PLACES)

    def test_two_different_ranges_do_not_render_the_same_table(self):
        """Three ranges rendering an identical sessions table is what a
        `last_date` selection looks like from the reader's chair."""
        one_day = self.drive({"start": self.DAY_NEW, "end": self.DAY_NEW})
        every = self.drive("all")
        self.assertNotAlmostEqual(sum(s["cost"] for s in one_day["sessions"]),
                                  sum(s["cost"] for s in every["sessions"]),
                                  places=4)


@requires_node
class TestSessionCountsFollowTurnsInRange(_ScannedFixture):
    """`filteredSessions` also feeds the Sessions tile and two table columns.

    Re-keying the selection moves all three, and it moves them the right way:
    they now count sessions that ran in the range rather than sessions that
    happened to STOP in it. Nothing pinned that before, so it could have
    changed silently either way.
    """

    def test_the_sessions_tile_counts_sessions_active_in_range(self):
        old = self.drive({"start": self.DAY_OLD, "end": self.DAY_OLD})
        self.assertEqual(old["totals"]["sessions"], 1)
        new = self.drive({"start": self.DAY_NEW, "end": self.DAY_NEW})
        self.assertEqual(new["totals"]["sessions"], 2)
        self.assertEqual(self.drive("all")["totals"]["sessions"], 2)

    def test_the_project_table_counts_them_the_same_way(self):
        """This column already paired a range-correct dollar figure with a
        `last_date`-scoped count, inside one row."""
        old = self.drive({"start": self.DAY_OLD, "end": self.DAY_OLD})
        self.assertEqual([p["sessions"] for p in old["byProject"]], [1])
        new = self.drive({"start": self.DAY_NEW, "end": self.DAY_NEW})
        self.assertEqual([p["sessions"] for p in new["byProject"]], [2])

    def test_the_branch_table_counts_them_the_same_way(self):
        """Once per branch a session had turns on IN RANGE, not once per label.

        The money in this table is per turn, so the count has to be too. Keyed
        on `sessions_all[].branch` — the session's single first-non-empty label
        — `s-crosser` was counted on BRANCH_OLD in every range including the one
        where it only ever ran on BRANCH_NEW, and BRANCH_NEW's row, holding real
        money, fell through to zero.

        This assertion was `[1]` on a one-branch fixture, which both keys
        satisfy: see `test_the_crossing_session_leaves_the_branch_it_is_
        labelled_with`.
        """
        old = self.drive({"start": self.DAY_OLD, "end": self.DAY_OLD})
        self.assertEqual({p["branch"]: p["sessions"] for p in old["byBranch"]},
                         {BRANCH_OLD: 1})
        new = self.drive({"start": self.DAY_NEW, "end": self.DAY_NEW})
        self.assertEqual({p["branch"]: p["sessions"] for p in new["byBranch"]},
                         {BRANCH_OLD: 1, BRANCH_NEW: 1})
        every = self.drive("all")
        self.assertEqual({p["branch"]: p["sessions"] for p in every["byBranch"]},
                         {BRANCH_OLD: 2, BRANCH_NEW: 1})

    def test_no_branch_row_shows_money_against_no_sessions(self):
        """The defect stated as a shape, so it cannot come back in another
        fixture's numbers: a row with turns in it was produced by at least one
        session, so a Sessions cell of 0 beside a cost is self-contradictory."""
        for date_range in (
                "all",
                {"start": self.DAY_OLD, "end": self.DAY_OLD},
                {"start": self.DAY_NEW, "end": self.DAY_NEW}):
            with self.subTest(range=date_range):
                rows = self.drive(date_range)["byBranch"]
                self.assertTrue(rows)
                for row in rows:
                    self.assertGreater(row["turns"], 0, row)
                    self.assertGreater(row["cost"], 0, row)
                    self.assertGreater(row["sessions"], 0, row)


@requires_node
class TestSessionMetadataStaysWholeSession(_ScannedFixture):
    """Duration and Last Active are properties of the session, not the window.

    A deliberate choice, recorded here because the alternative — clipping them
    to the range — is equally defensible and would otherwise look like a bug to
    the next reader. What must NOT happen is either one silently becoming a
    per-range figure while the columns beside it are per-session, or vice versa.
    """

    def test_duration_and_last_active_do_not_change_with_the_range(self):
        every = self.session(self.drive("all"), "s-crosser")
        one_day = self.session(
            self.drive({"start": self.DAY_NEW, "end": self.DAY_NEW}),
            "s-crosser")
        self.assertEqual(one_day["duration_min"], every["duration_min"])
        self.assertEqual(one_day["last"], every["last"])
        self.assertGreater(every["duration_min"], 24 * 60)

    def test_the_lifetime_columns_say_so_where_the_reader_sees_them(self):
        """The row mixes two semantics, so the two lifetime cells carry a
        tooltip naming which one they are."""
        html = run_js(emit(
            "(() => {"
            "  const html = {};"
            "  const els = new Map();"
            "  document.getElementById = (id) => {"
            "    if (!els.has(id)) {"
            "      const el = stubEl();"
            "      Object.defineProperty(el, 'innerHTML', {"
            "        get() { return html[id] === undefined ? '' : html[id]; },"
            "        set(v) { html[id] = v; } });"
            "      els.set(id, el);"
            "    }"
            "    return els.get(id);"
            "  };"
            "  renderSessionsTable([session]);"
            "  return html;"
            "})()",
            session={"session_id": "abcdef0123", "project": "p", "topic": "t",
                     "last": "2026-08-06 10:00", "duration_min": 7,
                     "model": OPUS, "turns": 3, "input": 10, "output": 20,
                     "cost": 1.0, "billable": True}))
        body = html["sessions-body"]
        self.assertEqual(body.count('title="Whole session'), 2, body)


@requires_node
class TestPayloadsWithoutADaySplitStillRender(_ScannedFixture):
    """The two older shapes `sessionForSelection` has always accepted.

    A session row with a per-model split but no per-day one, and a row with
    neither, both still select on their last-active day and show their lifetime
    totals. Dropping these would turn a degraded payload into an empty table.
    """

    SESSION = {
        "session_id": "s-legacy", "source": "claude", "project": "p",
        "branch": "main", "topic": "", "last": "2026-04-08 10:00",
        "last_date": "2026-04-08", "duration_min": 5, "model": OPUS,
        "turns": 3, "input": 1_000_000, "output": 100_000, "cache_read": 0,
        "cache_creation": 0, "cache_creation_1h": 0,
    }

    def _drive(self, session, date_range):
        return run_js(emit(
            "(() => {"
            "  rawData = {sessions_all: [session], all_models: models,"
            "             daily_by_model: [], hourly_by_model: [],"
            "             top_dispatches: [], subagent_by_type: [],"
            "             project_by_day_model: []};"
            "  selectedSource = 'claude';"
            "  selectedModels = new Set(models);"
            "  if (range && typeof range === 'object') {"
            "    selectedRange = 'all'; getRangeBounds = () => range;"
            "  } else { selectedRange = range; }"
            "  renderStats = () => {};"
            "  applyFilter();"
            "  return lastFilteredSessions.map(s => ({id: s.session_id,"
            "     cost: s.cost, turns: s.turns}));"
            "})()",
            session=session, range=date_range, models=[OPUS, HAIKU]))

    def test_a_per_model_split_alone_still_selects_on_the_last_active_day(self):
        session = dict(self.SESSION, by_model=[
            {"model": OPUS, "input": 1_000_000, "output": 100_000,
             "cache_read": 0, "cache_creation": 0, "cache_creation_1h": 0,
             "turns": 3}])
        got = self._drive(session, {"start": "2026-04-08", "end": "2026-04-08"})
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0]["cost"],
                               calc_cost(OPUS, 1_000_000, 100_000, 0, 0),
                               places=6)
        self.assertEqual(
            self._drive(session, {"start": "2026-04-09", "end": "2026-04-09"}),
            [])

    def test_a_row_with_no_split_at_all_still_renders(self):
        got = self._drive(dict(self.SESSION), "all")
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0]["cost"],
                               calc_cost(OPUS, 1_000_000, 100_000, 0, 0),
                               places=6)


class TestTheSessionDaySplitAccountsForEveryTurn(_ScannedFixture):
    """The server side, checked without a browser.

    `by_day_model` is what the client now prices, so a column dropped from this
    query is money that quietly leaves the page — the same failure
    `TestSessionRowsCarryEveryTokenColumn` guards for the row totals and the
    per-model split.
    """

    TOKEN_COLUMNS = ("input", "output", "cache_read", "cache_creation",
                     "cache_creation_1h", "turns")

    def rows(self):
        return [d for s in self.payload["sessions_all"] for d in s["by_day_model"]]

    def test_every_session_carries_one(self):
        for s in self.payload["sessions_all"]:
            with self.subTest(session=s["session_id"]):
                self.assertTrue(s["by_day_model"])

    def test_the_day_split_matches_the_daily_rollup(self):
        for column in self.TOKEN_COLUMNS:
            with self.subTest(column=column):
                self.assertEqual(
                    sum(r[column] for r in self.rows()),
                    sum(r[column] for r in self.payload["daily_by_model"]))

    def test_it_costs_the_same_as_the_daily_rollup(self):
        def cost(rows):
            return sum(calc_cost(r["model"], r["input"], r["output"],
                                 r["cache_read"], r["cache_creation"],
                                 r["cache_creation_1h"]) for r in rows)

        self.assertGreater(cost(self.rows()), 0)
        self.assertAlmostEqual(cost(self.rows()),
                               cost(self.payload["daily_by_model"]), places=9)

    def test_its_days_are_the_days_the_charts_use(self):
        self.assertEqual(sorted({r["day"] for r in self.rows()}),
                         [self.DAY_OLD, self.DAY_NEW])

    def test_the_per_model_split_is_still_the_same_totals(self):
        """`by_model` is `by_day_model` summed over days; both ship, and the
        contract AGENTS.md records for `by_model` has not moved."""
        for column in self.TOKEN_COLUMNS:
            with self.subTest(column=column):
                self.assertEqual(
                    sum(m[column] for s in self.payload["sessions_all"]
                        for m in s["by_model"]),
                    sum(r[column] for r in self.rows()))


# ── The day half of that key, which no fixture above can see ───────────────
#
# `s-crosser` spends a different model on each of its two days — deliberately,
# and `test_each_day_uses_a_different_model` pins it — so `(session_id, model)`
# already separates its turns by day, and the `day` term of
# `rollups.sessions_all`'s GROUP BY does no work for it. Delete that term and
# every assertion above this line still passes, the server-side class included:
# the split collapses only for a session that spends ONE model on more than one
# local day, which nothing above is.
#
# Adding that turn to the transcript above is not an option: OLD_ONLY,
# CROSSER_NEW, TODAY_OTHER and CROSSER_LIFETIME are computed from exactly those
# three records and the stat-tile reconciliations sum to them. So this is a
# second, deliberately minimal fixture — one project, one session, one model,
# two local days — and the money is stacked on the newer day so a collapse is
# visible from either side.

SAME_MODEL_OLD_DAY = (OPUS, (200_000, 20_000, 90_000, 40_000, 15_000))
SAME_MODEL_NEW_DAY = (OPUS, (5 * MILLION, 500_000, 2 * MILLION,
                             400_000, 150_000))

DAY_SPLIT_COLUMNS = ("input", "output", "cache_read", "cache_creation",
                     "cache_creation_1h")


class TestOneModelOnTwoDaysStaysTwoRows(unittest.TestCase):
    """The `day` term of the session split, isolated from the model term.

    Collapsed, the session's whole lifetime lands on ONE row stamped with an
    arbitrary one of its days. `sessionForSelection` filters these rows on
    `b.day`, so the range covering the other day finds nothing — the session
    and 96% of its money leave the Sessions table, the Sessions tile and both
    project tables — while the range covering the surviving day prices the
    entire lifetime under a one-day label. That is the failure AGENTS.md
    records as "101x on a session crossing local midnight", and the day key is
    the whole of what prevents it.

    No browser: this asserts the payload the client is handed, so it still runs
    where `@requires_node` skips.
    """

    @classmethod
    def setUpClass(cls):
        old_ts = utc_ts_on_local_day(2, 10)
        new_ts = utc_ts_on_local_day(0, 10)
        cls.DAY_OLD = local_day_of(old_ts)
        cls.DAY_NEW = local_day_of(new_ts)
        cls._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmpdir.name)
        projects = tmp / "projects" / "u" / "same"
        projects.mkdir(parents=True)
        (projects / "sess.jsonl").write_text("\n".join([
            _assistant("s-same", SAME_MODEL_OLD_DAY[0],
                       old_ts, "m-s1",
                       *SAME_MODEL_OLD_DAY[1], cwd="/home/u/sameproj"),
            _assistant("s-same", SAME_MODEL_NEW_DAY[0],
                       new_ts, "m-s2",
                       *SAME_MODEL_NEW_DAY[1], cwd="/home/u/sameproj"),
        ]) + "\n", encoding="utf-8")
        db = tmp / "usage.db"
        scanner.scan(projects_dir=tmp / "projects", db_path=db, verbose=False)
        cls.payload = get_dashboard_data(db)

    @classmethod
    def tearDownClass(cls):
        dashboard_data.reset_payload_cache()
        cls._tmpdir.cleanup()

    OLD_DAY_COST = _cost(SAME_MODEL_OLD_DAY)
    NEW_DAY_COST = _cost(SAME_MODEL_NEW_DAY)
    LIFETIME_COST = OLD_DAY_COST + NEW_DAY_COST

    def session(self):
        rows = self.payload["sessions_all"]
        self.assertEqual([s["session_id"] for s in rows], ["s-same"])
        return rows[0]

    def rows(self):
        return self.session()["by_day_model"]

    def test_the_fixture_spends_one_model_on_two_local_days(self):
        """Guard the guard, from a rollup that is not the one under test.

        Vary the model across the days and the model term starts separating
        them on its own — every assertion below then passes with the day key
        deleted, which is the state this class exists to end.
        """
        daily = self.payload["daily_by_model"]
        self.assertEqual(sorted(r["day"] for r in daily),
                         [self.DAY_OLD, self.DAY_NEW])
        self.assertEqual({r["model"] for r in daily}, {OPUS})

    def test_the_collapse_would_be_a_factor_not_a_rounding_error(self):
        """The defect as money, so it cannot creep back as a small one."""
        self.assertGreater(self.LIFETIME_COST, 20 * self.OLD_DAY_COST)

    def test_each_local_day_gets_its_own_row(self):
        self.assertEqual(sorted(r["day"] for r in self.rows()),
                         [self.DAY_OLD, self.DAY_NEW])

    def test_each_row_carries_only_that_days_turns(self):
        """Not the row count alone: a column dropped from the SELECT would
        keep both rows and still empty the table's money columns."""
        by_day = {r["day"]: r for r in self.rows()}
        for day, turn in ((self.DAY_OLD, SAME_MODEL_OLD_DAY),
                          (self.DAY_NEW, SAME_MODEL_NEW_DAY)):
            with self.subTest(day=day):
                self.assertIn(day, by_day,
                              f"{day} has no row of its own: {self.rows()}")
                self.assertEqual(by_day[day]["model"], turn[0])
                self.assertEqual(by_day[day]["turns"], 1)
                for column, expected in zip(DAY_SPLIT_COLUMNS, turn[1]):
                    with self.subTest(column=column):
                        self.assertEqual(by_day[day][column], expected)

    def test_each_row_prices_only_its_own_day(self):
        """What the page actually sums, costed the way `calc_cost` does."""
        by_day = {r["day"]: r for r in self.rows()}
        for day, expected in ((self.DAY_OLD, self.OLD_DAY_COST),
                              (self.DAY_NEW, self.NEW_DAY_COST)):
            with self.subTest(day=day):
                self.assertIn(day, by_day,
                              f"{day} has no row of its own: {self.rows()}")
                row = by_day[day]
                self.assertAlmostEqual(
                    calc_cost(row["model"], row["input"], row["output"],
                              row["cache_read"], row["cache_creation"],
                              row["cache_creation_1h"]),
                    expected, places=6)

    def test_the_per_model_split_is_those_two_days_summed(self):
        """`by_model` is `by_day_model` summed over days. With one model on two
        days the two views genuinely differ — the case `_ScannedFixture` cannot
        express — so this is also what says the day split is not just `by_model`
        under another name."""
        by_model = self.session()["by_model"]
        self.assertEqual([m["model"] for m in by_model], [OPUS])
        self.assertEqual(by_model[0]["turns"], 2)
        for column, old, new in zip(DAY_SPLIT_COLUMNS,
                                    SAME_MODEL_OLD_DAY[1],
                                    SAME_MODEL_NEW_DAY[1]):
            with self.subTest(column=column):
                self.assertEqual(by_model[0][column], old + new)


@requires_tzset
class TestTheDaySplitBucketsByTheLocalDay(unittest.TestCase):
    """And that day is the viewer's calendar day, not the UTC prefix.

    `tests/test_local_day_bucketing.py` pins that for the daily chart, the
    subagent views and the session row's `last_date`, and
    `test_session_row_day_matches_the_daily_chart` says why: the sessions table
    filters on that day, so it has to agree with the bars. The filter has since
    moved onto `by_day_model[].day` and nothing followed it there. Bucketed by
    the raw UTC prefix, a turn made at 23:00 local lands on the next day's row —
    invisible to a range naming the day the user was working, and double-counted
    into one they were not.

    Invisible in UTC, which is where CI runs, so the zone is forced; `tzset` is
    POSIX-only, hence the skip.
    """

    # 09:00Z and 11:00Z share a UTC day; at UTC+14 they are 23:00 and 01:00
    # local, i.e. two different local days. One session, one model, so the day
    # term is again the only thing that can separate them — and only if it is
    # the local day.
    SHARED_UTC_DAY = "2026-04-08"
    LOCAL_DAYS = ["2026-04-08", "2026-04-09"]
    BEFORE_LOCAL_MIDNIGHT = ("2026-04-08T09:00:00Z", SAME_MODEL_OLD_DAY[1])
    AFTER_LOCAL_MIDNIGHT = ("2026-04-08T11:00:00Z", SAME_MODEL_NEW_DAY[1])

    def setUp(self):
        original = os.environ.get("TZ")
        os.environ["TZ"] = TZ_AHEAD
        time.tzset()
        self.addCleanup(self._restore_tz, original)
        tmp = Path(tempfile.mkdtemp())
        projects = tmp / "projects" / "u" / "tz"
        projects.mkdir(parents=True)
        (projects / "sess.jsonl").write_text("\n".join([
            _assistant("s-tz", OPUS, ts, f"m-z{i}", *tokens, cwd="/home/u/tz")
            for i, (ts, tokens) in enumerate(
                (self.BEFORE_LOCAL_MIDNIGHT, self.AFTER_LOCAL_MIDNIGHT))
        ]) + "\n", encoding="utf-8")
        db = tmp / "usage.db"
        scanner.scan(projects_dir=tmp / "projects", db_path=db, verbose=False)
        self.payload = get_dashboard_data(db)

    @staticmethod
    def _restore_tz(original):
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()

    def rows(self):
        session, = self.payload["sessions_all"]
        return session["by_day_model"]

    def test_the_two_turns_really_share_a_utc_day(self):
        """Guard the guard, read from the one rollup that stays UTC on
        purpose: if they did not, UTC bucketing would split them too and this
        class would pass against it."""
        self.assertEqual({r["day"] for r in self.payload["hourly_by_model"]},
                         {self.SHARED_UTC_DAY})

    def test_the_split_lands_on_the_local_days(self):
        self.assertEqual(sorted(r["day"] for r in self.rows()), self.LOCAL_DAYS)

    def test_it_puts_them_on_the_same_days_as_the_daily_chart(self):
        """One range label, two aggregations: the tiles read the chart's rollup
        and the table reads this one."""
        self.assertEqual(
            sorted({r["day"] for r in self.rows()}),
            sorted({r["day"] for r in self.payload["daily_by_model"]}))

    def test_each_local_day_keeps_its_own_tokens(self):
        by_day = {r["day"]: r for r in self.rows()}
        for day, (_, tokens) in zip(self.LOCAL_DAYS,
                                    (self.BEFORE_LOCAL_MIDNIGHT,
                                     self.AFTER_LOCAL_MIDNIGHT)):
            with self.subTest(day=day):
                self.assertIn(day, by_day, self.rows())
                for column, expected in zip(DAY_SPLIT_COLUMNS, tokens):
                    with self.subTest(column=column):
                        self.assertEqual(by_day[day][column], expected)


# ── Two smaller defects in the same file ───────────────────────────────────

@requires_node
class TestTheModelFilterNamesThePartitionItActuallyUses(unittest.TestCase):
    """`renderModelCheckboxes` splits on `isBillable` and called it "Anthropic".

    `isBillable` is `getPricing(model) !== null`, which resolves OpenAI ids too,
    so "has a published rate" stopped being a proxy for "is Anthropic" the
    moment Codex support landed: a Codex dashboard filed `gpt-5.6-sol` under a
    heading reading "Anthropic", beneath a title reading "Codex Usage".

    The partition is not a vendor split and must not be renamed into one — a
    vendor classifier keyed on the pricing families could not classify the very
    ids that trigger this (an unpriced `gpt-oss-120b` IS an OpenAI model), and
    it would be the second source of truth `isBillable` exists to forbid. It is
    literally priced vs unpriced, and it is the same partition
    `defaultModelSelection` uses to decide what starts checked. So the strings
    say that, and they are true for both sources and both directions.
    """

    def render(self, models, selected):
        return run_js(emit(
            "(() => {"
            "  const html = {};"
            "  const els = new Map();"
            "  document.getElementById = (id) => {"
            "    if (!els.has(id)) {"
            "      const el = stubEl();"
            "      Object.defineProperty(el, 'innerHTML', {"
            "        get() { return html[id] === undefined ? '' : html[id]; },"
            "        set(v) { html[id] = v; } });"
            "      els.set(id, el);"
            "    }"
            "    return els.get(id);"
            "  };"
            "  allModelsList = models;"
            "  selectedModels = new Set(selected);"
            "  renderModelCheckboxes();"
            "  return {panel: html['model-checkboxes'],"
            "          label: els.get('model-trigger-label').textContent};"
            "})()", models=models, selected=selected))

    def test_a_codex_dashboard_does_not_call_openai_models_anthropic(self):
        got = self.render(["gpt-5.6-sol", "gpt-oss-120b"], ["gpt-5.6-sol"])
        self.assertNotIn("Anthropic", got["panel"])
        self.assertNotIn("Anthropic", got["label"])

    def test_the_headings_name_the_partition(self):
        got = self.render(["gpt-5.6-sol", "gpt-oss-120b"], ["gpt-5.6-sol"])
        self.assertIn("Priced", got["panel"])
        self.assertIn("No published rate", got["panel"])
        self.assertEqual(got["label"], "All priced")

    def test_the_same_strings_serve_a_claude_dashboard(self):
        """The symmetric error a source-aware label would have left behind: an
        unpriced ANTHROPIC id under a heading meaning "not Anthropic"."""
        got = self.render([OPUS, "claude-quartz-1-local"], [OPUS])
        self.assertNotIn("Other providers", got["panel"])
        self.assertIn("Priced", got["panel"])
        self.assertEqual(got["label"], "All priced")

    def test_the_overflow_form_is_renamed_too(self):
        got = self.render([OPUS, HAIKU, "gemma-3-27b-local"],
                          [OPUS, HAIKU, "gemma-3-27b-local"])
        # Everything selected is "All models"; drop one priced model so the
        # "+N others" branch is the one under test.
        got = self.render([OPUS, HAIKU, "gemma-3-27b-local"],
                          [OPUS, HAIKU])
        self.assertEqual(got["label"], "All priced")
        got = self.render([OPUS, HAIKU, "gemma-3-27b-local", "glm-4-local"],
                          [OPUS, HAIKU, "gemma-3-27b-local"])
        self.assertEqual(got["label"], "All priced +1")

    def test_the_grouping_itself_did_not_move(self):
        """Rename only: the first group is still exactly what starts checked,
        which is what makes the panel's "these are on, these are off" legible."""
        got = run_js(emit(
            "(() => {"
            "  const models = list;"
            "  const chosen = defaultModelSelection(models);"
            "  return {priced: models.filter(m => isBillable(m)),"
            "          checked: [...chosen]};"
            "})()", list=[OPUS, "gemma-3-27b-local", "gpt-5.6-sol"]))
        self.assertEqual(got["priced"], got["checked"])


def _cache_row(cache_creation, cache_creation_1h):
    """One payload row whose only tokens are cache writes, at a chosen tier."""
    return {"model": OPUS, "input": 0, "output": 0, "cache_read": 0,
            "cache_creation": cache_creation,
            "cache_creation_1h": cache_creation_1h, "turns": 1}


@requires_node
class TestADerivedCacheWriteRateIsStableAcrossRowSizes(unittest.TestCase):
    """`columnRate` computed the cache-write rate as a quotient of the row.

    Cache writes bill at two tiers, so that column has no single list price and
    the rate a row contributed is the rate its own mix came to. Computing it as
    `((total - long) * cw + long * cw_1h) / total` is not float-stable across
    different `total`s: for a non-dyadic `cw`, two rows of the SAME model at the
    SAME all-5-minute tier yield two distinct doubles, the rate set reports
    `size > 1`, and the cell prints ` avg` beside that model's own single list
    price — the claim `mixedTiers` exists three lines away to avoid making.

    Every rate PRICING ships is dyadic, so the shipped table cannot exercise
    this at all; the path is reached through `CLAUDE_USAGE_RATES`, which this
    same campaign wired through to the browser. The test therefore overrides a
    rate, and keeps a dyadic control beside it.
    """

    # A user-supplied rate for a real Claude model. Codex cannot reach this
    # column at all — its cache_creation is structurally zero — so a gpt id
    # would make the test vacuous.
    NON_DYADIC = {"input": 5.0, "output": 25.0, "cache_read": 0.5,
                  "cache_write": 6.4, "cache_write_1h": 10.24}
    DYADIC = {"input": 5.0, "output": 25.0, "cache_read": 0.5,
              "cache_write": 6.25, "cache_write_1h": 10.0}

    def rates(self, override, rows):
        return run_js(emit(
            "(() => {"
            "  applyRateOverrides({[model]: override});"
            "  const bucket = newCostBucket({});"
            "  for (const r of rows) accumulateCostRow(bucket, r);"
            "  return {size: bucket.rates.cache_creation.size,"
            "          values: [...bucket.rates.cache_creation],"
            "          blended: blendedRate(bucket, 'cache_creation'),"
            "          mixed: mixedTiers(bucket),"
            "          cell: effortCostCell(bucket, 'cache_creation'),"
            "          naive: rows.map(r => ((r.cache_creation - r.cache_creation_1h)"
            "             * override.cache_write + r.cache_creation_1h"
            "             * override.cache_write_1h) / r.cache_creation)};"
            "})()", model=OPUS, override=override, rows=rows))

    # Row totals chosen so the naive quotient really is unstable at the rate
    # above: (3 * 6.4) / 3 !== 6.4 while (10 * 6.4) / 10 does, and (29 *
    # 10.24) / 29 !== 10.24 while (30 * 10.24) / 30 does. Two distinct doubles
    # in the set is exactly what prints the spurious marker.
    PURE_5M = [_cache_row(3, 0), _cache_row(10, 0)]
    PURE_1H = [_cache_row(29, 29), _cache_row(30, 30)]

    def test_the_override_really_is_float_unstable(self):
        """Guard the fixture: with a dyadic rate the quotient is exact and this
        whole class would pass against the unfixed code."""
        for rows in (self.PURE_5M, self.PURE_1H):
            with self.subTest(rows=rows):
                got = self.rates(self.NON_DYADIC, rows)
                self.assertEqual(len(set(got["naive"])), 2, got["naive"])
        control = self.rates(self.DYADIC, self.PURE_5M)
        self.assertEqual(len(set(control["naive"])), 1, control["naive"])

    def test_one_model_at_one_tier_is_not_an_average(self):
        got = self.rates(self.NON_DYADIC, self.PURE_5M)
        self.assertEqual(got["size"], 1, got["values"])
        self.assertFalse(got["blended"])
        self.assertFalse(got["mixed"])
        self.assertNotIn("avg", got["cell"])

    def test_the_all_one_hour_tier_reproduces_its_list_price(self):
        got = self.rates(self.NON_DYADIC, self.PURE_1H)
        self.assertEqual(got["size"], 1, got["values"])
        self.assertEqual(got["values"], [self.NON_DYADIC["cache_write_1h"]])
        self.assertNotIn("avg", got["cell"])

    def test_a_genuine_blend_is_still_marked(self):
        """The direction that actually misleads a reader is the missing marker,
        so the fix must not buy its silence by collapsing real blends."""
        got = self.rates(self.NON_DYADIC,
                         [_cache_row(10, 0), _cache_row(10, 10)])
        self.assertTrue(got["mixed"])
        self.assertIn("avg", got["cell"])

    def test_a_row_mixing_both_tiers_is_still_marked(self):
        got = self.rates(self.NON_DYADIC, [_cache_row(10, 4)])
        self.assertTrue(got["mixed"])
        self.assertIn("avg", got["cell"])

    def test_the_shipped_dyadic_table_is_unchanged(self):
        got = self.rates(self.DYADIC, self.PURE_5M)
        self.assertEqual(got["values"], [self.DYADIC["cache_write"]])
        self.assertNotIn("avg", got["cell"])


if __name__ == "__main__":
    unittest.main()
