"""Regression tests for the seven findings that closed out the audit.

Each of these was reproduced by an adversarial verifier before being fixed, so
each test below states the wrong behaviour in the terms the reproduction used —
a number, not a description — and would fail if it returned.
"""

import csv
import io
import json
import unittest

from pricing import calc_cost
from tests.test_dashboard_js import emit, requires_node, run_js

MILLION = 1_000_000


@requires_node
class TestDispatchCostIsPerModel(unittest.TestCase):
    """The server emits one row per (dispatch, model); the client collapses them.

    Pricing a whole dispatch at one model was 5x out in either direction
    depending on which model happened to come first.
    """

    ROWS = [
        {"agent_id": "ag-1", "agent_type": "Explore", "model": "claude-opus-4-8",
         "start": "2026-04-08 10:00", "start_date": "2026-04-08",
         "input": 100, "output": 100, "cache_read": 0, "cache_creation": 0,
         "turns": 1, "duration_ms": 10, "tool_uses": 1, "status": "completed"},
        {"agent_id": "ag-1", "agent_type": "Explore", "model": "claude-haiku-4-5",
         "start": "2026-04-08 10:01", "start_date": "2026-04-08",
         "input": 2 * MILLION, "output": 400_000, "cache_read": 0,
         "cache_creation": 0, "turns": 10, "duration_ms": 10, "tool_uses": 1,
         "status": "completed"},
    ]

    def _collapse(self):
        return run_js(emit(
            "(() => {"
            "  rawData = {top_dispatches: rows, all_models: ['claude-opus-4-8','claude-haiku-4-5'],"
            "            daily_by_model: [], hourly_by_model: [], sessions_all: [],"
            "            subagent_by_type: [], project_by_day_model: []};"
            "  selectedModels = new Set(rawData.all_models);"
            "  selectedRange = 'all';"
            "  renderStats = () => {};"
            "  applyFilter();"
            "  return lastFilteredDispatches.map(d => ({agent_id: d.agent_id, model: d.model,"
            "     cost: d.cost, turns: d.turns, input: d.input, billable: !!d.billable}));"
            "})()", rows=self.ROWS))

    def test_the_two_rows_collapse_into_one_dispatch(self):
        got = self._collapse()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["turns"], 11)
        self.assertEqual(got[0]["input"], 2 * MILLION + 100)

    def test_cost_is_summed_per_model_not_priced_at_one(self):
        truth = (calc_cost("claude-opus-4-8", 100, 100, 0, 0)
                 + calc_cost("claude-haiku-4-5", 2 * MILLION, 400_000, 0, 0))
        wrong_all_opus = calc_cost("claude-opus-4-8", 2 * MILLION + 100, 400_100, 0, 0)
        got = self._collapse()
        self.assertAlmostEqual(got[0]["cost"], truth, places=6)
        self.assertLess(got[0]["cost"], wrong_all_opus / 2)

    def test_the_displayed_model_is_the_dominant_one(self):
        """Not whichever row the database happened to return first."""
        self.assertEqual(self._collapse()[0]["model"], "claude-haiku-4-5")


@requires_node
class TestSessionSurvivesFilteringBySecondaryModel(unittest.TestCase):
    """A session used opus once and haiku heavily; its primary model is opus."""

    SESSION = {
        "session_id": "s1", "project": "p", "branch": "main", "topic": "",
        "last": "2026-04-08 10:00", "last_date": "2026-04-08", "duration_min": 5,
        "model": "claude-opus-4-8", "turns": 11,
        "input": 2 * MILLION + 100, "output": 400_100,
        "cache_read": 0, "cache_creation": 0,
        "by_model": [
            {"model": "claude-opus-4-8", "input": 100, "output": 100,
             "cache_read": 0, "cache_creation": 0, "turns": 1},
            {"model": "claude-haiku-4-5", "input": 2 * MILLION, "output": 400_000,
             "cache_read": 0, "cache_creation": 0, "turns": 10},
        ],
    }

    def _sessions(self, selected):
        return run_js(emit(
            "(() => {"
            "  rawData = {sessions_all: [session], all_models: models,"
            "            daily_by_model: [], hourly_by_model: [], top_dispatches: [],"
            "            subagent_by_type: [], project_by_day_model: []};"
            "  selectedModels = new Set(selected);"
            "  selectedRange = 'all';"
            "  renderStats = () => {};"
            "  applyFilter();"
            "  return lastFilteredSessions.map(s => ({model: s.model, cost: s.cost,"
            "     turns: s.turns, input: s.input, billable: !!s.billable}));"
            "})()",
            session=self.SESSION, selected=selected,
            models=["claude-opus-4-8", "claude-haiku-4-5"]))

    def test_selecting_only_the_secondary_model_still_shows_the_session(self):
        """The defect: it disappeared, taking its project with it."""
        got = self._sessions(["claude-haiku-4-5"])
        self.assertEqual(len(got), 1, "session vanished when filtered by a model it used")
        self.assertEqual(got[0]["turns"], 10, "should report only the haiku turns")
        self.assertAlmostEqual(
            got[0]["cost"],
            calc_cost("claude-haiku-4-5", 2 * MILLION, 400_000, 0, 0), places=6)

    def test_selecting_only_the_primary_model_reports_only_its_turns(self):
        got = self._sessions(["claude-opus-4-8"])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["turns"], 1)
        self.assertAlmostEqual(got[0]["cost"],
                               calc_cost("claude-opus-4-8", 100, 100, 0, 0), places=6)

    def test_both_selected_sums_both_at_their_own_rates(self):
        got = self._sessions(["claude-opus-4-8", "claude-haiku-4-5"])
        truth = (calc_cost("claude-opus-4-8", 100, 100, 0, 0)
                 + calc_cost("claude-haiku-4-5", 2 * MILLION, 400_000, 0, 0))
        self.assertEqual(got[0]["turns"], 11)
        self.assertAlmostEqual(got[0]["cost"], truth, places=6)

    def test_selecting_an_unrelated_model_drops_the_session(self):
        self.assertEqual(self._sessions(["claude-sonnet-4-6"]), [])


@requires_node
class TestNewlySeenModelsAreMergedOnRefresh(unittest.TestCase):
    """/api/data is polled every 30s; the filter was only built on first load."""

    def test_a_new_unpriced_model_is_visible_when_all_models_are_unpriced(self):
        got = run_js(emit("""
          (() => {
            allModelsList = ['future-model-a'];
            selectedModels = defaultModelSelection(allModelsList);
            mergeNewlySeenModels(['future-model-a', 'future-model-b']);
            return [...selectedModels].sort();
          })()
        """))
        self.assertEqual(got, ['future-model-a', 'future-model-b'])

    def test_an_unpriced_model_refresh_preserves_a_custom_selection(self):
        got = run_js(emit("""
          (() => {
            allModelsList = ['future-model-a', 'future-model-b'];
            selectedModels = new Set(['future-model-a']);
            mergeNewlySeenModels([...allModelsList, 'future-model-c']);
            return [...selectedModels];
          })()
        """))
        self.assertEqual(got, ['future-model-a'])

    def test_a_model_first_seen_later_becomes_selected(self):
        got = run_js(emit("""
          (() => {
            allModelsList = ['claude-opus-4-8'];
            selectedModels = new Set(['claude-opus-4-8']);
            const changed = mergeNewlySeenModels(
              ['claude-opus-4-8', 'claude-haiku-4-5', 'gemma-3-27b']);
            return { changed,
                     known: allModelsList.slice().sort(),
                     selected: [...selectedModels].sort() };
          })()
        """))
        self.assertTrue(got["changed"])
        self.assertIn("claude-haiku-4-5", got["known"])
        self.assertIn("claude-haiku-4-5", got["selected"],
                      "a newly-seen priced model must not be silently excluded")
        # Unpriced models follow the same default as first load: known, unselected.
        self.assertIn("gemma-3-27b", got["known"])
        self.assertNotIn("gemma-3-27b", got["selected"])

    def test_merging_preserves_what_the_user_unchecked(self):
        got = run_js(emit("""
          (() => {
            allModelsList = ['claude-opus-4-8', 'claude-sonnet-4-6'];
            selectedModels = new Set(['claude-opus-4-8']);   // user unchecked sonnet
            mergeNewlySeenModels(['claude-opus-4-8', 'claude-sonnet-4-6', 'claude-haiku-4-5']);
            return [...selectedModels].sort();
          })()
        """))
        self.assertNotIn("claude-sonnet-4-6", got, "rebuilt the selection instead of merging")
        self.assertIn("claude-haiku-4-5", got)

    def test_load_data_actually_calls_the_merge_on_a_later_poll(self):
        """Covers the wiring, not just the helper.

        The helper can be perfect and still never run: the bug was that
        loadData only built the filter on first load. So drive loadData twice
        with a stubbed transport and watch the selection grow.
        """
        from tests.test_dashboard_js import _DOM_STUB, NODE, extract_app_script
        import subprocess
        import tempfile
        from pathlib import Path

        snippet = """
        (async () => {
          const payloads = [
            {all_models: ['claude-opus-4-8'], generated_at: 'x', daily_by_model: [],
             hourly_by_model: [], sessions_all: [], top_dispatches: [],
             subagent_by_type: [], project_by_day_model: []},
            {all_models: ['claude-opus-4-8', 'claude-haiku-4-5'], generated_at: 'y',
             daily_by_model: [], hourly_by_model: [], sessions_all: [],
             top_dispatches: [], subagent_by_type: [], project_by_day_model: []},
          ];
          let call = 0;
          apiFetch = async () => ({ ok: true, json: async () => payloads[call++] });
          renderStats = () => {};
          scheduleAutoRefresh = () => {};
          await loadData();
          const afterFirst = [...selectedModels].sort();
          await loadData();
          const afterSecond = [...selectedModels].sort();
          console.log(JSON.stringify({ afterFirst, afterSecond }));
        })();
        """
        source = _DOM_STUB + "\n" + extract_app_script() + "\n" + snippet
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.cjs"
            harness.write_text(source, encoding="utf-8")
            # node writes UTF-8; without encoding= the parent would decode with
            # locale.getencoding() — cp1252 on windows-latest.
            proc = subprocess.run([NODE, str(harness)], capture_output=True,
                                  text=True, encoding="utf-8", timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(got["afterFirst"], ["claude-opus-4-8"])
        self.assertIn(
            "claude-haiku-4-5", got["afterSecond"],
            "loadData did not merge the model the second poll reported, so its "
            "turns would be dropped from every table until a page reload")

    def test_no_new_models_is_a_no_op(self):
        got = run_js(emit("""
          (() => {
            allModelsList = ['claude-opus-4-8'];
            selectedModels = new Set(['claude-opus-4-8']);
            return { changed: mergeNewlySeenModels(['claude-opus-4-8']),
                     selected: [...selectedModels] };
          })()
        """))
        self.assertFalse(got["changed"])
        self.assertEqual(got["selected"], ["claude-opus-4-8"])


class TestServerSplitsDispatchesPerModel(unittest.TestCase):
    """Covers the SQL, which the client-side collapse test cannot reach.

    With a bare `model` column under `GROUP BY t.agent_id`, SQLite returns one
    arbitrary row's model for the whole dispatch — the value the client then
    priced every token at.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path

        import scanner
        from dashboard_data import get_dashboard_data

        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = scanner.get_db(self.db_path)
        scanner.init_db(conn)

        def turn(message_id, model, inp, out):
            return {
                "session_id": "s1", "timestamp": "2026-04-08T10:00:00Z",
                "model": model, "input_tokens": inp, "output_tokens": out,
                "cache_read_tokens": 0, "cache_creation_tokens": 0,
                "tool_name": None, "cwd": None, "message_id": message_id,
                "is_subagent": 1, "agent_id": "ag-1",
            }

        scanner.upsert_sessions(conn, [{
            "session_id": "s1", "project_name": "user/proj",
            "first_timestamp": "2026-04-08T10:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-opus-4-8",
            "total_input_tokens": 0, "total_output_tokens": 0,
            "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 0,
        }])
        scanner.insert_turns(conn, [
            turn("m-opus", "claude-opus-4-8", 100, 100),
            turn("m-haiku", "claude-haiku-4-5", 2 * MILLION, 400_000),
        ])
        conn.commit()
        conn.close()
        self.payload = get_dashboard_data(self.db_path)

    def test_one_row_per_model_not_one_per_dispatch(self):
        rows = [r for r in self.payload["top_dispatches"] if r["agent_id"] == "ag-1"]
        self.assertEqual(len(rows), 2,
                         "a dispatch spanning two models must produce two rows, "
                         "or the client cannot price each at its own rate")
        self.assertEqual({r["model"] for r in rows},
                         {"claude-opus-4-8", "claude-haiku-4-5"})

    def test_each_row_carries_only_its_own_models_tokens(self):
        rows = {r["model"]: r for r in self.payload["top_dispatches"]
                if r["agent_id"] == "ag-1"}
        self.assertEqual(rows["claude-opus-4-8"]["input"], 100)
        self.assertEqual(rows["claude-haiku-4-5"]["input"], 2 * MILLION)

    def test_sessions_carry_a_per_model_breakdown(self):
        session = self.payload["sessions_all"][0]
        by_model = {b["model"]: b for b in session["by_model"]}
        self.assertEqual(set(by_model), {"claude-opus-4-8", "claude-haiku-4-5"})
        self.assertEqual(by_model["claude-haiku-4-5"]["output"], 400_000)
        self.assertEqual(
            sum(b["turns"] for b in session["by_model"]), 2,
            "the breakdown must account for every turn in the session")


@requires_node
class TestMoneyFormattingIsLocaleIndependent(unittest.TestCase):
    """Tables used the browser locale while the axis and CSVs are dot-decimal.

    The comma-decimal RE-RUN of this used to live here as well, asserting
    `fmtCost` and `fmtCostBig` and nothing else — so `fmt`, `fmtRate` and
    `fmtPct` could each be unpinned with the suite green (verified: three
    separate unpinnings, `Ran … OK` every time). It is now
    `TestNoNumberFollowsTheViewersLocale.test_no_formatter_silently_loses_its_pinned_locale`
    in tests/test_ui_theme_and_loading.py, which runs the same subprocess under
    the same `LANG` and asserts all five formatters plus the one tile that
    formats its own number. Every mutation the version here caught, that one
    catches too; it also catches three the version here did not.

    What stays is the in-process reading below, which is the only assertion of
    these strings that is not `LANG`-dependent — node ignores `LANG` on Windows,
    where the subprocess guard passes vacuously.
    """

    def test_costs_use_a_dot_decimal_separator(self):
        got = run_js(emit(
            "({cost: fmtCost(1500.5), big: fmtCostBig(1500.5), tokens: fmt(999.5)})"))
        self.assertEqual(got["cost"], "$1,500.5000")
        self.assertEqual(got["big"], "$1,500.50")
        # fmt only reaches the locale-formatted branch below 1000, where the
        # decimal separator is the thing that used to vary.
        self.assertEqual(got["tokens"], "999.5")


@requires_node
class TestUnpricedModelsExportBlankNotZero(unittest.TestCase):
    """The tables render n/a; the CSVs asserted 0.0000 for the same rows."""

    def test_model_csv_leaves_an_unpriced_cost_empty(self):
        got = run_js(emit("""
          (() => {
            const captured = [];
            downloadCSV = (name, header, rows) => captured.push({name, header, rows});
            lastByModel = [
              {model: 'claude-opus-4-8', turns: 1, input: 1000000, output: 0,
               cache_read: 0, cache_creation: 0},
              {model: 'gemma-3-27b', turns: 1, input: 5000000, output: 900000,
               cache_read: 0, cache_creation: 0},
            ];
            exportModelCSV();
            return captured[0].rows;
          })()
        """))
        priced, unpriced = got
        self.assertEqual(priced[-1], "5.0000")
        self.assertEqual(unpriced[-1], "",
                         "an unpriced model must not export a confident 0.0000")

    def test_the_blank_survives_csv_encoding(self):
        got = run_js(emit("csvField('')"))
        parsed = next(csv.reader(io.StringIO("a," + got + ",b")))
        self.assertEqual(parsed, ["a", "", "b"])


@requires_node
class TestSortArrowsAreScopedToTheirTable(unittest.TestCase):
    """Sorting one table used to blank the direction arrows on the other three."""

    def test_session_sort_ids_do_not_collide_with_the_other_tables(self):
        got = run_js(emit("""
          (() => {
            const cleared = [];
            document.querySelectorAll = (sel) => { cleared.push(sel); return []; };
            document.getElementById = () => null;
            updateSortIcons();
            return cleared;
          })()
        """))
        self.assertEqual(got, ['[id^="sort-icon-"]'],
                         "the clear must be scoped by id prefix, not the shared "
                         ".sort-icon class every table uses")


@requires_node
class TestShortModelName(unittest.TestCase):
    """A trailing date stamp was supplying the version digits."""

    CASES = [
        ("claude-opus-4-20250514", "Opus 4"),
        ("claude-sonnet-4-20250514", "Sonnet 4"),
        ("claude-opus-4-8", "Opus 4.8"),
        ("claude-opus-4-7-20260215", "Opus 4.7"),
        ("claude-sonnet-4-6-20260101", "Sonnet 4.6"),
        ("claude-haiku-4-5", "Haiku 4.5"),
        ("claude-fable-5", "Fable 5"),
    ]

    def test_version_is_read_from_the_id_not_the_date(self):
        got = run_js(emit("ids.map(shortModelName)",
                          ids=[c[0] for c in self.CASES]))
        for (model, expected), actual in zip(self.CASES, got):
            with self.subTest(model=model):
                self.assertEqual(actual, expected)

    def test_non_anthropic_ids_are_unaffected(self):
        got = run_js(emit("ids.map(shortModelName)",
                          ids=["ollama/gemma3:27b", "glm-4-9b"]))
        self.assertEqual(got[0], "gemma3")
        self.assertTrue(got[1])


if __name__ == "__main__":
    unittest.main()
