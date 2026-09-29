"""`rollups.INCIDENT_GAP_SECONDS` is the value that is actually in force.

One incident can contain many retry and subagent notices. INCIDENT_GAP_SECONDS
must control grouping rather than an identical shadowed local value.

These tests move the constant and require the grouping to move with it, in both
directions, so the name and the behaviour can never drift apart again.
"""

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import rollups
from db import init_db


class TestTheDocumentedGapIsTheOneInForce(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        self.conn.execute(
            "INSERT INTO sessions (session_id, project_name, first_timestamp, "
            "last_timestamp, model, source) VALUES ('sess-1', 'user/proj', "
            "'2026-08-01T12:00:00Z', '2026-08-01T12:00:00Z', 'claude-opus-5', "
            "'claude')")

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def notice(self, uuid, timestamp):
        self.conn.execute(
            "INSERT INTO limit_events (event_uuid, kind, session_id, timestamp, "
            "status, message, reset_hint, reset_zone) VALUES (?, 'rate_limit', "
            "'sess-1', ?, 429, 'limit reached', '5pm', 'UTC')",
            (uuid, timestamp))
        self.conn.commit()

    def test_a_smaller_gap_splits_notices_the_default_would_collapse(self):
        """20 minutes apart is one incident at the shipped 30-minute gap."""
        self.notice("u1", "2026-08-01T10:00:00Z")
        self.notice("u2", "2026-08-01T10:20:00Z")
        self.assertEqual(len(rollups.limit_incidents(self.conn)), 1)

        with mock.patch.object(rollups, "INCIDENT_GAP_SECONDS", 60):
            incidents = rollups.limit_incidents(self.conn)
        self.assertEqual([i["notices"] for i in incidents], [1, 1])

    def test_a_larger_gap_collapses_notices_the_default_would_split(self):
        """70 minutes apart is two incidents at the shipped 30-minute gap."""
        self.notice("u1", "2026-08-01T10:00:00Z")
        self.notice("u2", "2026-08-01T11:10:00Z")
        self.assertEqual(len(rollups.limit_incidents(self.conn)), 2)

        with mock.patch.object(rollups, "INCIDENT_GAP_SECONDS", 2 * 60 * 60):
            incidents = rollups.limit_incidents(self.conn)
        self.assertEqual([i["notices"] for i in incidents], [2])


if __name__ == "__main__":
    unittest.main()
