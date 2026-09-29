"""Tests for the dashboard's subagent data layer (get_dashboard_data)."""

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scanner import get_db, init_db, insert_turns, upsert_agents, upsert_sessions
import dashboard
from tests.legacy_database import create_schema as create_legacy_schema
from tests.test_dashboard_js import emit, requires_node, run_js


def _turn(session_id, message_id, model="claude-opus-4-8",
          inp=100, out=50, is_subagent=0, agent_id=None,
          timestamp="2026-04-08T10:00:00Z"):
    return {
        "session_id": session_id, "timestamp": timestamp, "model": model,
        "input_tokens": inp, "output_tokens": out,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "tool_name": None, "cwd": "/home/user/proj",
        "message_id": message_id, "is_subagent": is_subagent, "agent_id": agent_id,
    }


class TestDashboardSubagentData(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-1", "project_name": "user/proj",
            "first_timestamp": "2026-04-08T10:00:00Z",
            "last_timestamp": "2026-04-08T10:30:00Z",
            "git_branch": "main", "model": "claude-opus-4-8",
            "total_input_tokens": 400, "total_output_tokens": 210,
            "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 3,
        }])
        insert_turns(conn, [
            _turn("sess-1", "m-main", inp=100, out=50, is_subagent=0),
            _turn("sess-1", "m-sub1", inp=300, out=80, is_subagent=1, agent_id="agent-1"),
            _turn("sess-1", "m-sub2", inp=200, out=40, is_subagent=1, agent_id="acompact-xyz"),
        ])
        upsert_agents(conn, [{
            "agent_id": "agent-1", "agent_type": "Explore",
            "dispatched_in_session": "sess-1", "completed_at": "2026-04-08T10:20:00Z",
            "status": "completed", "total_tokens": 380,
            "total_duration_ms": 4200, "tool_use_count": 5,
        }])
        conn.commit()
        conn.close()

    def test_returns_subagent_keys(self):
        d = dashboard.get_dashboard_data(self.db_path)
        self.assertIn("subagent_by_type", d)
        self.assertIn("top_dispatches", d)

    def test_subagent_by_type_resolves_agent_type(self):
        d = dashboard.get_dashboard_data(self.db_path)
        types = {r["agent_type"] for r in d["subagent_by_type"]}
        # agent-1 -> Explore (from agents table); acompact-* -> auto-compact
        self.assertIn("Explore", types)
        self.assertIn("auto-compact", types)

    def test_top_dispatches_carries_dispatch_metadata(self):
        d = dashboard.get_dashboard_data(self.db_path)
        explore = [r for r in d["top_dispatches"] if r["agent_type"] == "Explore"]
        self.assertEqual(len(explore), 1)
        self.assertEqual(explore[0]["tool_uses"], 5)
        self.assertEqual(explore[0]["duration_ms"], 4200)
        self.assertEqual(explore[0]["turns"], 1)

    def test_main_turn_excluded_from_subagent_data(self):
        d = dashboard.get_dashboard_data(self.db_path)
        # Only the 2 subagent turns contribute; the main turn must not appear.
        total_turns = sum(r["turns"] for r in d["subagent_by_type"])
        self.assertEqual(total_turns, 2)

    def test_agent_upsert_is_scoped_by_source(self):
        conn = get_db(self.db_path)
        self.addCleanup(conn.close)
        upsert_agents(conn, [{
            "source": "codex", "agent_id": "agent-1",
            "agent_type": "guardian", "dispatched_in_session": "sess-1",
            "completed_at": "", "status": "", "total_tokens": None,
            "total_duration_ms": None, "tool_use_count": None,
        }])
        rows = conn.execute(
            "SELECT source, agent_type FROM agents WHERE agent_id = 'agent-1' "
            "ORDER BY source").fetchall()
        self.assertEqual([tuple(row) for row in rows],
                         [("claude", "Explore"), ("codex", "guardian")])



class TestDashboardOnAnOldSchema(unittest.TestCase):
    """`get_dashboard_data` must not crash on a pre-v1.5.0 schema.

    `cmd_dashboard` binds and serves *before* its background scan runs
    `init_db`, so on the first load after upgrading, a pre-existing database may
    still lack the `agents` table and the `is_subagent`/`agent_id` columns the
    subagent queries use. `get_dashboard_data` enters `database_admission`, which
    REBUILDS that database rather than migrating it (AGENTS.md invariant 6).

    So the payload must come back complete and empty rather than raising
    "no such table" — and the row that was there does not survive, which is the
    decision rather than a regression. A later successful scan re-reads every
    transcript because a rebuilt database has an empty `processed_files`; this
    test does not assume the one-shot startup scan necessarily wins a race with
    a request-triggered rebuild.
    """

    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = sqlite3.connect(self.db_path)
        # Complete released old schema: turns WITHOUT is_subagent/agent_id and
        # no agents table. A partial hand-made schema is not ownership evidence.
        create_legacy_schema(conn)
        conn.execute(
            "INSERT INTO turns (session_id, timestamp, model, input_tokens, "
            "output_tokens, cache_read_tokens, cache_creation_tokens, "
            "tool_name, cwd, message_id) VALUES "
            "('s1','2026-04-08T10:00:00Z','claude-opus-4-8',"
            "100,50,0,0,NULL,'/home/user/proj','m1')"
        )
        conn.commit()
        conn.close()

    def _data(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            payload = dashboard.get_dashboard_data(self.db_path)
        return payload, err.getvalue()

    def test_does_not_crash_and_returns_empty_subagent_data(self):
        d, _ = self._data()
        self.assertNotIn("error", d)
        self.assertEqual(d["subagent_by_type"], [])
        self.assertEqual(d["top_dispatches"], [])

    def test_the_rebuild_is_announced_and_the_stale_row_is_gone(self):
        d, err = self._data()
        self.assertIn("written by a different version", err)
        self.assertEqual(d["all_models"], [])
        self.assertEqual(d["daily_by_model"], [])

    def test_a_second_load_neither_rebuilds_nor_announces(self):
        """The dashboard polls. A rebuild on every request would throw away the
        background scan's work as fast as it landed."""
        self._data()
        d, err = self._data()
        self.assertEqual(err, "")
        self.assertNotIn("error", d)


class TestABlankTimestampDoesNotBlankADispatchStart(unittest.TestCase):
    """A turn with no timestamp must not erase its dispatch's start.

    `top_dispatches` groups by a hidden `pricing_day` so a rate change inside one
    local day is priced per period, then merges those subgroups back. A turn
    carrying no timestamp lands in its own `pricing_day IS NULL` subgroup, and the
    merge picked the earliest `start_ts` with `timestamp_compare`, which sorts a
    blank below every real instant. The blank therefore won, and the dispatch's
    `start`, `start_date` and `start_local` came back empty while every one of its
    tokens stayed in the row — a dispatch that reported 1,010 input tokens and no
    date. Both orderings are pinned because the merge keeps whichever subgroup the
    query returned first, and the query orders by token volume.
    """

    def _dispatch(self, blank_tokens, real_tokens):
        db_path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(db_path)
        try:
            init_db(conn)
            real = "2026-04-08T10:00:00Z"
            upsert_sessions(conn, [{
                "session_id": "sess-1", "project_name": "user/proj",
                "first_timestamp": real, "last_timestamp": real,
                "git_branch": "main", "model": "claude-opus-4-8",
                "total_input_tokens": blank_tokens + real_tokens,
                "total_output_tokens": 0, "total_cache_read": 0,
                "total_cache_creation": 0, "turn_count": 2,
            }])
            insert_turns(conn, [
                _turn("sess-1", "m-real", inp=real_tokens, out=0, is_subagent=1,
                      agent_id="agent-1", timestamp=real),
                _turn("sess-1", "m-blank", inp=blank_tokens, out=0, is_subagent=1,
                      agent_id="agent-1", timestamp=""),
            ])
            conn.commit()
        finally:
            conn.close()
        with contextlib.redirect_stdout(io.StringIO()):
            data = dashboard.get_dashboard_data(db_path=db_path)
        rows = [r for r in data["top_dispatches"] if r["agent_id"] == "agent-1"]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_the_real_start_survives_when_the_blank_turn_sorts_second(self):
        row = self._dispatch(blank_tokens=10, real_tokens=1000)
        self.assertEqual(row["start_date"], "2026-04-08")
        self.assertTrue(row["start"], "the dispatch lost its start entirely")
        self.assertEqual(row["input"], 1010)

    def test_the_real_start_is_adopted_when_the_blank_turn_sorts_first(self):
        row = self._dispatch(blank_tokens=1000, real_tokens=10)
        self.assertEqual(row["start_date"], "2026-04-08")
        self.assertTrue(row["start"], "the dispatch lost its start entirely")
        self.assertEqual(row["input"], 1010)


_DISPATCH_STARTS = """(() => {
  rawData = payload;
  selectedModels = new Set(payload.all_models);
  selectedRange = 'all';
  renderStats = () => {};
  applyFilter();
  return lastFilteredDispatches.map(d => ({ agent_id: d.agent_id,
    start_date: d.start_date, start: d.start, input: d.input }));
})()"""


def _dispatch_row(model, start_date, start, inp):
    return {"agent_id": "a1", "source": "claude", "agent_type": "Explore",
            "model": model, "start": start, "start_date": start_date,
            "input": inp, "output": 0, "cache_read": 0, "cache_creation": 0,
            "cache_creation_1h": 0, "turns": 1, "cost": 0.0,
            "cost_parts": {"input": 0.0, "output": 0.0, "cache_read": 0.0,
                           "cache_creation": 0.0},
            "duration_ms": None, "tool_uses": None, "status": "completed",
            "by_day": []}


def _dispatch_payload(rows):
    return {"all_models": ["claude-opus-4-8", "claude-haiku-4-5"],
            "available_sources": ["claude"], "daily_by_model": [],
            "sessions_all": [], "project_by_day_model": [],
            "effort_by_day_model": [], "stop_reason_by_day_model": [],
            "hourly_by_model": [], "limit_incidents": [], "subagent_daily": [],
            "subagent_totals": [], "top_dispatches": rows}


@requires_node
class TestTheClientKeepsARealDispatchStart(unittest.TestCase):
    """The browser collapses a dispatch's per-model rows and picks the earliest.

    It did so with a bare `r.start_date < d.start_date`, and `''` sorts below
    every real day — so a dispatch spanning two models, where one model's turns
    carried no timestamp, rendered with an empty Start cell while both models'
    tokens stayed in the row. The server-side half of this rule lives in
    `rollups._merge_pricing_rows`; this is the same defect one layer up, and it
    is reachable independently because these rows are per (dispatch, model).
    """

    def _starts(self, rows):
        return run_js(emit(_DISPATCH_STARTS, payload=_dispatch_payload(rows)))

    def test_a_blank_model_row_does_not_erase_the_dispatch_start(self):
        got = self._starts([
            _dispatch_row("claude-opus-4-8", "2026-04-08", "2026-04-08 10:00", 1000),
            _dispatch_row("claude-haiku-4-5", "", "", 10),
        ])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["start_date"], "2026-04-08")
        self.assertEqual(got[0]["start"], "2026-04-08 10:00")
        self.assertEqual(got[0]["input"], 1010)

    def test_a_real_start_is_adopted_when_the_blank_row_comes_first(self):
        got = self._starts([
            _dispatch_row("claude-haiku-4-5", "", "", 10),
            _dispatch_row("claude-opus-4-8", "2026-04-08", "2026-04-08 10:00", 1000),
        ])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["start_date"], "2026-04-08")
        self.assertEqual(got[0]["input"], 1010)

    def test_the_earliest_of_two_real_starts_still_wins(self):
        got = self._starts([
            _dispatch_row("claude-opus-4-8", "2026-04-08", "2026-04-08 10:00", 1000),
            _dispatch_row("claude-haiku-4-5", "2026-04-07", "2026-04-07 09:00", 10),
        ])
        self.assertEqual(got[0]["start_date"], "2026-04-07")


if __name__ == "__main__":
    unittest.main()
