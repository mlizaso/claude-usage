"""Two ways a payload rollup can be quietly wrong: the GROUP BY and the clock.

`rollups.py`'s own docstring states the rule the whole module exists to enforce
— every row knows its own model, source and label, because SQLite will happily
return an *arbitrary* row's value for a non-aggregated column. Getting that
wrong produces no error and no empty table; it produces a table with the right
totals under the wrong labels, which is the failure mode nobody notices.

The subtlety these tests pin is that a GROUP BY term which merely *looks* like
the SELECT alias is not the alias: SQLite resolves a bare name against the input
columns first, so `... AS agent_type ... GROUP BY agent_type` groups by
`agents.agent_type`, not by the COALESCE/CASE expression above it.

`limit_incidents` is the other shape: the scanner stores whatever string a
transcript carried in `timestamp` (see localdays.py), so two records in one
session can parse to one aware and one naive datetime — and subtracting those
raises `TypeError`, which the incident loop did not catch. Nothing in
`dashboard_data._collect_dashboard_data` has a try/except, so that escapes
`/api/data` and blanks the entire dashboard rather than one card.
"""

import os
import sqlite3
import tempfile
import unittest

import rollups
from db import init_db


class _RollupFixture(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def add_session(self, session_id="sess-1", project="user/proj",
                    source="claude", ts="2026-08-01T12:00:00Z"):
        self.conn.execute(
            "INSERT INTO sessions (session_id, project_name, first_timestamp, "
            "last_timestamp, model, source) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, project, ts, ts, "claude-opus-5", source))

    def add_subagent_turn(self, agent_id, output, ts="2026-08-01T12:00:00Z",
                          model="claude-opus-5", source="claude",
                          session_id="sess-1"):
        self.conn.execute(
            "INSERT INTO turns (session_id, timestamp, model, output_tokens, "
            "is_subagent, agent_id, source) VALUES (?, ?, ?, ?, 1, ?, ?)",
            (session_id, ts, model, output, agent_id, source))

    def add_agent(self, agent_id, agent_type, source="claude"):
        self.conn.execute(
            "INSERT INTO agents (source, agent_id, agent_type) VALUES (?, ?, ?)",
            (source, agent_id, agent_type))


class TestSubagentByTypeGrouping(_RollupFixture):
    """`subagent_by_type` must group by the label it displays."""

    def test_auto_compact_and_unknown_are_not_merged(self):
        """Two dispatches with no `agents` row are two different types.

        `a.agent_type` is NULL for both, so grouping on that real column put
        them in one bucket and the SELECT's CASE then labelled the whole bucket
        with one arbitrary row's answer — attributing an unrecognised dispatch's
        tokens to Claude Code's auto-compaction agent and deleting the
        `unknown` bucket from the chart entirely.
        """
        self.add_session()
        self.add_subagent_turn("acompact-aaa", output=100)
        self.add_subagent_turn("orphan-bbb", output=200)
        self.conn.commit()

        rows = rollups.subagent_by_type(self.conn)
        by_type = {r["agent_type"]: r for r in rows}
        self.assertEqual(sorted(by_type), ["auto-compact", "unknown"])
        self.assertEqual(by_type["auto-compact"]["output"], 100)
        self.assertEqual(by_type["auto-compact"]["dispatches"], 1)
        self.assertEqual(by_type["unknown"]["output"], 200)
        self.assertEqual(by_type["unknown"]["dispatches"], 1)

    def test_named_types_still_separate_from_the_fallbacks(self):
        self.add_session()
        self.add_agent("agent-ccc", "code-reviewer")
        self.add_subagent_turn("acompact-aaa", output=100)
        self.add_subagent_turn("orphan-bbb", output=200)
        self.add_subagent_turn("agent-ccc", output=400)
        self.conn.commit()

        by_type = {r["agent_type"]: r for r in rollups.subagent_by_type(self.conn)}
        self.assertEqual(sorted(by_type),
                         ["auto-compact", "code-reviewer", "unknown"])
        self.assertEqual(by_type["code-reviewer"]["output"], 400)

    def test_equal_agent_ids_do_not_share_metadata_across_sources(self):
        self.add_agent("shared-agent", "claude-reviewer", "claude")
        self.add_agent("shared-agent", "codex-guardian", "codex")
        self.add_subagent_turn(
            "shared-agent", output=100, source="claude",
            model="claude-opus-5", session_id="shared")
        self.add_subagent_turn(
            "shared-agent", output=200, source="codex",
            model="gpt-5.4", session_id="shared")
        self.conn.commit()

        rows = rollups.subagent_by_type(self.conn)
        by_source = {row["source"]: row for row in rows}
        self.assertEqual(by_source["claude"]["agent_type"], "claude-reviewer")
        self.assertEqual(by_source["codex"]["agent_type"], "codex-guardian")

    def test_legacy_blank_metadata_sources_still_join_their_claude_turn(self):
        """Every side of a source-qualified join applies the legacy fold."""
        self.add_session(project="legacy/project", source="")
        self.add_agent("legacy-agent", "legacy-reviewer", source="")
        self.add_subagent_turn(
            "legacy-agent", output=100, source="claude", session_id="sess-1")
        self.conn.commit()

        projects = rollups.project_by_day_model(self.conn, source="claude")
        self.assertEqual([row["project"] for row in projects], ["legacy/project"])
        by_type = rollups.subagent_by_type(self.conn, source="claude")
        self.assertEqual([row["agent_type"] for row in by_type],
                         ["legacy-reviewer"])
        dispatches = rollups.top_dispatches(self.conn, source="claude")
        self.assertEqual([row["agent_type"] for row in dispatches],
                         ["legacy-reviewer"])

    def test_init_db_canonicalizes_duplicate_legacy_source_identities(self):
        """A read-path initialization must not leave duplicate Claude rows."""
        self.add_session(project="legacy/project", source="")
        self.add_session(project="canonical/project", source="claude")
        self.add_agent("shared-agent", "legacy-reviewer", source="")
        self.add_agent("shared-agent", "canonical-reviewer", source="claude")
        self.add_subagent_turn(
            "shared-agent", output=100, source="claude", session_id="sess-1")
        self.conn.execute(
            "INSERT INTO turns (session_id, timestamp, model, output_tokens, "
            "is_subagent, agent_id, message_id, source) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            ("sess-1", "2026-08-01T12:00:00Z", "claude-opus-5", 999,
             "shared-agent", "duplicate-message", ""),
        )
        self.conn.execute(
            "INSERT INTO turns (session_id, timestamp, model, output_tokens, "
            "is_subagent, agent_id, message_id, source) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            ("sess-1", "2026-08-01T12:00:00Z", "claude-opus-5", 200,
             "shared-agent", "duplicate-message", "claude"),
        )
        self.conn.commit()

        init_db(self.conn)

        sessions = self.conn.execute(
            "SELECT source, project_name FROM sessions WHERE session_id = ?",
            ("sess-1",),
        ).fetchall()
        agents = self.conn.execute(
            "SELECT source, agent_type FROM agents WHERE agent_id = ?",
            ("shared-agent",),
        ).fetchall()
        duplicate_turns = self.conn.execute(
            "SELECT source, output_tokens FROM turns WHERE message_id = ?",
            ("duplicate-message",),
        ).fetchall()
        self.assertEqual([(row["source"], row["project_name"])
                          for row in sessions],
                         [("claude", "canonical/project")])
        self.assertEqual([(row["source"], row["agent_type"])
                          for row in agents],
                         [("claude", "canonical-reviewer")])
        self.assertEqual([(row["source"], row["output_tokens"])
                          for row in duplicate_turns],
                         [("claude", 200)])

        by_type = rollups.subagent_by_type(self.conn, source="claude")
        self.assertEqual(len(by_type), 1, by_type)
        self.assertEqual(by_type[0]["agent_type"], "canonical-reviewer")
        self.assertEqual(by_type[0]["output"], 300)

    def test_null_and_empty_source_collapse_into_one_claude_row(self):
        """The same alias collision on `source`, which splits a row in two.

        Rows written before the `source` column existed carry NULL and rows a
        parser left blank carry ''. Both are Claude's — the SELECT normalises
        them to 'claude' — but grouping on the real `t.source` kept them apart,
        emitting two rows with the identical label and counting the same
        dispatch total twice over.
        """
        self.add_session()
        self.add_subagent_turn("orphan-aaa", output=10, source=None)
        self.add_subagent_turn("orphan-bbb", output=20, source="")
        self.conn.commit()

        rows = rollups.subagent_by_type(self.conn)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["source"], "claude")
        self.assertEqual(rows[0]["output"], 30)
        self.assertEqual(rows[0]["dispatches"], 2)

    def test_null_and_empty_model_collapse_into_one_unknown_row(self):
        self.add_session()
        self.add_subagent_turn("orphan-aaa", output=10, model=None)
        self.add_subagent_turn("orphan-bbb", output=20, model="")
        self.conn.commit()

        rows = rollups.subagent_by_type(self.conn)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["model"], "unknown")
        self.assertEqual(rows[0]["output"], 30)

    def test_two_models_stay_two_rows_so_each_can_be_priced(self):
        """The rule the module exists for: one model per row."""
        self.add_session()
        self.add_subagent_turn("orphan-aaa", output=10, model="claude-opus-5")
        self.add_subagent_turn("orphan-bbb", output=20, model="claude-haiku-4-5")
        self.conn.commit()

        rows = rollups.subagent_by_type(self.conn)
        self.assertEqual({r["model"] for r in rows},
                         {"claude-opus-5", "claude-haiku-4-5"})

    def test_source_scoping_still_selects_one_assistant(self):
        self.add_session(session_id="sess-claude", source="claude")
        self.add_session(session_id="sess-codex", source="codex")
        self.add_subagent_turn("acompact-aaa", output=100,
                               session_id="sess-claude", source="claude")
        self.add_subagent_turn("orphan-bbb", output=200,
                               session_id="sess-codex", source="codex",
                               model="gpt-5.6-sol")
        self.conn.commit()

        claude_rows = rollups.subagent_by_type(self.conn, source="claude")
        self.assertEqual([r["output"] for r in claude_rows], [100])
        codex_rows = rollups.subagent_by_type(self.conn, source="codex")
        self.assertEqual([r["output"] for r in codex_rows], [200])


class TestLimitIncidentTimestamps(_RollupFixture):
    """A malformed timestamp must cost one notice, never the whole payload."""

    def add_limit_event(self, uuid, timestamp, reset_hint="5pm",
                        session_id="sess-1", kind="rate_limit"):
        self.conn.execute(
            "INSERT INTO limit_events (event_uuid, kind, session_id, timestamp, "
            "status, message, reset_hint, reset_zone) "
            "VALUES (?, ?, ?, ?, 429, 'limit reached', ?, 'UTC')",
            (uuid, kind, session_id, timestamp, reset_hint))

    def test_mixed_aware_and_naive_timestamps_do_not_raise(self):
        """`fromisoformat` accepts both; subtracting them raises TypeError.

        The guard was `except ValueError`, so this escaped `limit_incidents`,
        `_collect_dashboard_data` and `get_dashboard_data` — GET /api/data
        returned a 500 and the page rendered nothing at all.
        """
        self.add_session()
        self.add_limit_event("u1", "2026-08-01T10:00:00Z")
        self.add_limit_event("u2", "2026-08-01T10:05:00")
        self.conn.commit()

        incidents = rollups.limit_incidents(self.conn)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["notices"], 2)
        self.assertEqual(incidents[0]["blocked_min"], 5.0)

    def test_bare_date_is_treated_as_utc_midnight(self):
        self.add_session()
        self.add_limit_event("u1", "2026-08-01T00:00:00Z")
        self.add_limit_event("u2", "2026-08-01")
        self.conn.commit()

        incidents = rollups.limit_incidents(self.conn)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["notices"], 2)
        self.assertEqual(incidents[0]["blocked_min"], 0.0)

    def test_unparseable_timestamp_skips_only_that_notice(self):
        self.add_session()
        self.add_limit_event("u1", "2026-08-01T10:00:00Z")
        self.add_limit_event("u2", "not a timestamp at all")
        self.add_limit_event("u3", "2026-08-01T10:10:00Z")
        self.conn.commit()

        incidents = rollups.limit_incidents(self.conn)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["notices"], 2)

    def test_all_aware_timestamps_are_unchanged(self):
        """The existing behaviour, pinned so the normalisation cannot shift it."""
        self.add_session()
        self.add_limit_event("u1", "2026-08-01T10:00:00Z")
        self.add_limit_event("u2", "2026-08-01T10:20:00Z")
        # More than INCIDENT_GAP_SECONDS later: a second incident.
        self.add_limit_event("u3", "2026-08-01T11:30:00Z")
        self.conn.commit()

        incidents = rollups.limit_incidents(self.conn)
        self.assertEqual(len(incidents), 2)
        self.assertEqual(incidents[0]["notices"], 2)
        self.assertEqual(incidents[0]["blocked_min"], 20.0)
        self.assertEqual(incidents[1]["notices"], 1)


if __name__ == "__main__":
    unittest.main()
