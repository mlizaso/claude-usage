"""Persisted timestamp ordering stays independent of raw ISO spelling."""

import os
import sqlite3
import tempfile
import unittest

from claude_usage import db, rollups, scanner
from claude_usage.timestamps import timestamp_compare, timestamp_order


class TimestampStorageTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def turn(timestamp, message_id="m-1", session_id="s-1"):
        return {
            "session_id": session_id,
            "timestamp": timestamp,
            "model": "claude-sonnet-5",
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "tool_name": "",
            "message_id": message_id,
            "is_subagent": 0,
            "agent_id": None,
        }

    def test_order_key_matches_instant_and_malformed_fallback_order(self):
        values = [
            "2026-08-22T11:00:00+02:00",  # 09:00Z
            "2026-08-22T10:00:00Z",
            "not-a-timestamp",
            "",
        ]
        for left in values:
            for right in values:
                self.assertEqual(
                    (timestamp_order(left) > timestamp_order(right))
                    - (timestamp_order(left) < timestamp_order(right)),
                    timestamp_compare(left, right),
                    (left, right),
                )

    def test_turn_and_session_order_keys_are_written_with_raw_values(self):
        scanner.insert_turns(self.conn, [
            self.turn("2026-08-22T10:00:00Z", "m-1"),
            self.turn("2026-08-22T11:00:00+02:00", "m-2"),
        ])
        scanner.upsert_sessions(self.conn, [{
            "session_id": "s-1", "project_name": "p",
            "first_timestamp": "2026-08-22T11:00:00+02:00",
            "last_timestamp": "2026-08-22T10:00:00Z",
            "git_branch": "", "model": "claude-sonnet-5",
            "total_input_tokens": 2, "total_output_tokens": 2,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 2,
        }])
        turn = self.conn.execute(
            "SELECT timestamp, timestamp_order FROM turns "
            "WHERE message_id = 'm-1'").fetchone()
        self.assertEqual(turn["timestamp_order"], timestamp_order(turn["timestamp"]))
        session = self.conn.execute(
            "SELECT first_timestamp, first_timestamp_order, last_timestamp, "
            "last_timestamp_order FROM sessions WHERE session_id = 's-1'").fetchone()
        self.assertEqual(session["first_timestamp_order"],
                         timestamp_order(session["first_timestamp"]))
        self.assertEqual(session["last_timestamp_order"],
                         timestamp_order(session["last_timestamp"]))

        rows = rollups.sessions_all(self.conn)
        self.assertEqual(rows[0]["duration_min"], 60.0)

    def test_limit_incident_sql_orders_by_persisted_instant(self):
        scanner.upsert_limit_events(self.conn, [
            {"event_uuid": "later", "kind": "rate_limit", "session_id": "s-1",
             "timestamp": "2026-08-22T10:00:00Z", "status": 429},
            {"event_uuid": "earlier", "kind": "rate_limit", "session_id": "s-1",
             "timestamp": "2026-08-22T23:30:00+14:00", "status": 429},
        ])
        rows = self.conn.execute(
            "SELECT timestamp, timestamp_order FROM limit_events "
            "ORDER BY timestamp_order").fetchall()
        self.assertEqual([row["timestamp"] for row in rows], [
            "2026-08-22T23:30:00+14:00", "2026-08-22T10:00:00Z",
        ])
        self.assertEqual([row["timestamp_order"] for row in rows], [
            timestamp_order(row["timestamp"]) for row in rows
        ])
        incidents = rollups.limit_incidents(self.conn)
        self.assertEqual(incidents[0]["blocked_min"], 30.0)

    def test_snapshot_order_key_keeps_earliest_mixed_offset_observation(self):
        row = ("codex", "5h", "pro", "reset", 10, "", 1, "", 0,
               "2026-08-22T10:00:00Z")
        scanner.record_limit_snapshot(self.conn, [row])
        earlier = (*row[:9], "2026-08-22T23:30:00+14:00")
        scanner.record_limit_snapshot(self.conn, [earlier])
        stored = self.conn.execute(
            "SELECT observed_at, observed_at_order FROM usage_limits_snapshots"
        ).fetchone()
        self.assertEqual(stored["observed_at"], earlier[-1])
        self.assertEqual(stored["observed_at_order"], timestamp_order(earlier[-1]))

    def test_rollups_register_ordering_on_an_ordinary_reopened_connection(self):
        """Public rollups cannot depend on a UDF installed by another opener."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "usage.db")
            writer = sqlite3.connect(path)
            writer.row_factory = sqlite3.Row
            db.init_db(writer)
            writer.execute(
                "INSERT INTO sessions "
                "(session_id, project_name, first_timestamp, last_timestamp, "
                "model, source) VALUES (?, ?, ?, ?, ?, ?)",
                ("s-1", "project", "2026-08-22T11:00:00+02:00",
                 "2026-08-22T10:00:00Z", "claude-sonnet-5", "claude"),
            )
            writer.execute(
                "INSERT INTO turns "
                "(session_id, timestamp, model, output_tokens, source) "
                "VALUES (?, ?, ?, ?, ?)",
                ("s-1", "2026-08-22T10:00:00Z", "claude-sonnet-5", 1,
                 "claude"),
            )
            writer.executemany(
                "INSERT INTO limit_events "
                "(event_uuid, kind, session_id, timestamp, status, reset_hint) "
                "VALUES (?, 'rate_limit', 's-1', ?, 429, 'soon')",
                [
                    ("earlier", "2026-08-22T23:30:00+14:00"),
                    ("later", "2026-08-22T10:00:00Z"),
                ],
            )
            writer.commit()
            writer.close()

            reader = sqlite3.connect(path)
            reader.row_factory = sqlite3.Row
            try:
                sessions = rollups.sessions_all(reader)
                incidents = rollups.limit_incidents(reader)
            finally:
                reader.close()

        self.assertEqual([row["session_id"] for row in sessions], ["s-1"])
        self.assertEqual(sessions[0]["duration_min"], 60.0)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["notices"], 2)
        self.assertEqual(incidents[0]["blocked_min"], 30.0)


if __name__ == "__main__":
    unittest.main()
