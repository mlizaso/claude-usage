"""Dashboard chronology must compare ISO timestamps by instant, not spelling."""

import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import dashboard_data
import db


class DashboardTimestampFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.conn = sqlite3.connect(self.tmp / "usage.db")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def snapshot(self, *, resets_key, resets_at, percent, observed_at,
                 scope="pro", group="5m"):
        self.conn.execute(
            "INSERT INTO usage_limits_snapshots "
            "(kind, grp, scope, resets_key, percent, severity, is_active, "
            " resets_at, observed_at) VALUES ('codex', ?, ?, ?, ?, '', 1, ?, ?)",
            (group, scope, resets_key, percent, resets_at, observed_at))

    def turn(self, timestamp, message_id="codex-message", source="codex"):
        self.conn.execute(
            "INSERT INTO turns (session_id, message_id, timestamp, model, "
            "source, input_tokens, output_tokens) "
            "VALUES ('codex-session', ?, ?, 'gpt-5.6-sol', ?, 1, 1)",
            (message_id, timestamp, source))

    @staticmethod
    def with_offset(instant, hours):
        return instant.astimezone(timezone(timedelta(hours=hours))).isoformat()


class TestCodexDashboardTimestampOrdering(DashboardTimestampFixture):
    @patch("dashboard_data.limits_core.read_thresholds", return_value={})
    def test_projection_uses_latest_instants_and_latest_reset(self, _thresholds):
        """Offsets can make an earlier value lexically greater than the latest."""
        self.snapshot(
            resets_key="old", resets_at="2026-08-05T01:00:00+02:00",
            percent=10, observed_at="2026-08-05T01:00:00+02:00")
        self.snapshot(
            resets_key="new", resets_at="2026-08-05T00:40:00Z",
            percent=20, observed_at="2026-08-05T00:30:00Z")
        # This turn is earlier than the newest snapshot by instant but is
        # lexically greater, so SELECT MAX(timestamp) used to pick it.
        self.turn("2026-08-05T01:00:00+02:00")
        self.conn.commit()

        projection = dashboard_data.codex_limits_projection(
            self.conn,
            now=datetime(2026, 8, 5, 0, 30, 10, tzinfo=timezone.utc))

        self.assertEqual(projection["age_seconds"], 10)
        self.assertEqual([w["percent"] for w in projection["windows"]], [20])
        self.assertFalse(projection["windows"][0]["expired"])

    @patch("dashboard_data.limits_core.read_thresholds", return_value={})
    def test_history_orders_mixed_offsets_by_instant(self, _thresholds):
        earlier = "2026-08-05T01:00:00+02:00"  # 23:00Z on the prior day
        later = "2026-08-05T00:30:00Z"
        self.snapshot(resets_key="same", resets_at=later, percent=10,
                      observed_at=earlier)
        self.snapshot(resets_key="same", resets_at=later, percent=20,
                      observed_at=later)
        self.conn.commit()

        history = dashboard_data.codex_limit_history(self.conn)

        self.assertEqual([row["percent"] for row in history], [10, 20])


class TestClaudeWindowStartTimestampOrdering(DashboardTimestampFixture):
    def test_window_start_uses_earliest_instant_not_lexical_min(self):
        reset = datetime.now(timezone.utc).replace(microsecond=0) \
            - timedelta(hours=3)
        earlier = reset + timedelta(minutes=10)
        later = reset + timedelta(minutes=30)
        self.turn(self.with_offset(earlier, 2), "claude-earlier", "claude")
        self.turn(later.isoformat(), "claude-later", "claude")
        self.conn.commit()

        window = {"kind": "session", "group": "", "resets_at": reset.isoformat(),
                  "window_start": "PROJECTED"}
        dashboard_data._correct_window_start(self.conn, window)

        self.assertEqual(window["window_start"], earlier.isoformat())

    def test_window_start_rejects_earlier_turn_that_is_lexically_after_boundary(self):
        reset = datetime.now(timezone.utc).replace(microsecond=0) \
            - timedelta(hours=3)
        earlier = reset - timedelta(minutes=30)
        self.turn(self.with_offset(earlier, 2), "claude-earlier", "claude")
        self.conn.commit()

        window = {"kind": "session", "group": "", "resets_at": reset.isoformat(),
                  "window_start": "PROJECTED"}
        dashboard_data._correct_window_start(self.conn, window)

        self.assertEqual(window["window_start"], "PROJECTED")


if __name__ == "__main__":
    unittest.main()
