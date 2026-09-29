"""Private, disposable startup snapshots must never become live data."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_usage import dashboard_cache as cache
from claude_usage.db import get_db, init_db


class TestDashboardSnapshots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "usage.db"
        with get_db(self.db) as conn:
            init_db(conn, self.db)
        conn.close()
        self.target = self.db.with_name("usage.db.dashboard-claude.json")
        self.data = {"all_models": ["test-model"], "daily_by_model": [],
                     "generated_at": "2026-09-05 12:00:00", "unscanned": False}

    def save(self):
        return cache.save_snapshot(self.db, "claude", self.data, "test",
                                   expected_identity=cache.snapshot_identity(self.db))

    def read(self):
        return cache.read_snapshot(self.db, "claude", "test")

    def test_saved_response_survives_without_opening_sqlite(self):
        self.assertTrue(self.save())
        with mock.patch("sqlite3.connect", side_effect=AssertionError("opened SQLite")):
            saved = self.read()
        self.assertEqual(saved["data"], self.data)
        self.assertGreater(saved["saved_at"], 0)
        if os.name == "posix":
            self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_missing_corrupt_oversized_and_incompatible_are_misses(self):
        self.assertIsNone(self.read())
        for raw in (b"{broken", b"null", b"[]", b'{"saved_at":NaN}'):
            self.target.write_bytes(raw)
            self.assertIsNone(self.read())
        self.assertTrue(self.save())
        with mock.patch.object(cache, "MAX_SNAPSHOT_BYTES", 10):
            self.assertIsNone(self.read())
        self.assertIsNone(cache.read_snapshot(self.db, "claude", "new-version"))
        self.assertIsNone(cache.read_snapshot(self.db, "codex", "test"))
        saved = json.loads(self.target.read_text(encoding="utf-8"))
        saved["key"] = "codex"
        self.target.write_text(json.dumps(saved), encoding="utf-8")
        self.assertIsNone(self.read())

    def test_failed_empty_or_oversized_result_preserves_previous_snapshot(self):
        self.save()
        for data in ({"error": "failed"}, {"unscanned": True}):
            self.assertFalse(cache.save_snapshot(self.db, "claude", data, "test",
                expected_identity=cache.snapshot_identity(self.db)))
        with mock.patch.object(cache, "MAX_SNAPSHOT_BYTES", 10):
            self.assertFalse(self.save())
        self.assertEqual(self.read()["data"], self.data)

    def test_database_replacement_or_removal_cannot_reuse_previous_history(self):
        self.save()
        previous = self.db.with_name("previous.db")
        self.db.rename(previous)
        self.assertIsNone(self.read())
        self.db.write_bytes(previous.read_bytes())
        self.assertIsNone(self.read())

    def test_replacement_after_calculation_cannot_save_old_data_for_the_new_database(self):
        identity = cache.snapshot_identity(self.db)
        self.db.rename(self.db.with_name("original.db"))
        self.db.write_bytes(b"different file")
        self.assertFalse(cache.save_snapshot(self.db, "claude", self.data, "test",
                                            expected_identity=identity))
        self.assertFalse(self.target.exists())

    def test_write_failure_is_optional_and_cleans_the_temporary_file(self):
        self.save()
        with mock.patch.object(cache.os, "replace", side_effect=OSError("read only")):
            self.assertFalse(self.save())
        self.assertEqual(self.read()["data"], self.data)
        self.assertEqual(list(self.db.parent.glob(".dashboard-snapshot-*")), [])

    def test_replacement_while_reading_does_not_return_the_old_databases_snapshot(self):
        self.save()
        read = cache.read_bounded_regular_file

        def read_then_replace(*args, **kwargs):
            raw = read(*args, **kwargs)
            self.db.rename(self.db.with_name("original.db"))
            self.db.write_bytes(b"replacement")
            return raw

        with mock.patch.object(cache, "read_bounded_regular_file", read_then_replace):
            self.assertIsNone(self.read())

    @unittest.skipUnless(os.name == "posix", "symlinks require privileges on Windows")
    def test_symlink_reads_are_refused_and_writes_never_follow_the_link(self):
        victim = self.db.with_name("victim")
        victim.write_bytes(b"private unrelated file")
        self.target.symlink_to(victim)
        self.assertIsNone(self.read())
        self.assertTrue(self.save())
        self.assertEqual(victim.read_bytes(), b"private unrelated file")
        self.assertFalse(self.target.is_symlink())

    def test_hard_link_reads_are_refused(self):
        self.save()
        os.link(self.target, self.db.with_name("linked"))
        self.assertIsNone(self.read())

    def test_unsafe_database_path_is_not_a_cache_bypass(self):
        self.save()
        with mock.patch.object(cache, "secure_db_permissions",
                               side_effect=cache.UnsafeDatabasePathError("unsafe")):
            self.assertIsNone(self.read())
            self.assertFalse(self.save())
