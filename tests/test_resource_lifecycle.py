"""Tests that a failed read or scan releases its SQLite connection.

The dashboard process is long-lived — the VS Code extension keeps it running for
the whole session, polling /api/data and rescanning on demand. Every one of
those paths used to close its connection only by falling off the end of the
happy path, so any query that raised (a locked database during a concurrent
scan, a damaged page, a schema-admission/rebuild failure after an upgrade) leaked
a connection and a file descriptor for the life of the process.

These tests force the error path and then prove the connection is closed, by
using it: a closed sqlite3.Connection raises ProgrammingError.
"""

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cli
import dashboard
import db as database
import reports
import dashboard_data
import scanner


def _is_closed(conn):
    """True if `conn` is closed — the only honest way to ask sqlite3."""
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


class _TrackedConnections:
    """Context manager recording every sqlite3 connection a module opens."""

    def __init__(self, module, attr="sqlite3"):
        self.module = module
        self.attr = attr
        self.opened = []

    def __enter__(self):
        real_connect = sqlite3.connect

        def tracking_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            self.opened.append(conn)
            return conn

        self._patch = mock.patch.object(
            getattr(self.module, self.attr), "connect", tracking_connect)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False

    def per_request(self):
        """Everything opened except the payload cache's version probe.

        The probe is the one connection in this module that is SUPPOSED to
        outlive the call that opened it, and it has to be: `PRAGMA data_version`
        only reports another connection's commits when two reads are compared on
        the SAME connection. Measured — a freshly opened connection returns 1
        every time, however many commits have landed in between, so a probe that
        was closed and reopened per request would report "unchanged" forever and
        the cache would never invalidate. Closing it would not tighten this
        guard; it would silently convert the cache into a staleness bug.

        So the invariant this class enforces is narrower than it used to be, and
        is stated rather than quietly relaxed: *every per-request connection is
        closed, and the only connection allowed to outlive a call is the single
        version probe*. `test_the_version_probe_is_the_only_survivor_and_there
        _is_one` is what stops that exception widening into a licence to leak.
        """
        probe = getattr(dashboard_data, "_VERSION_PROBE", None)
        live = probe[1] if probe else None
        return [c for c in self.opened if c is not live]

    def assert_all_closed(self, test):
        test.assertTrue(self.opened, "no connection was opened — test proves nothing")
        per_request = self.per_request()
        test.assertTrue(per_request,
                        "only the probe was opened — this test proves nothing")
        for conn in per_request:
            test.assertTrue(_is_closed(conn),
                            "a connection outlived the call that opened it")


class TestDashboardDataReleasesItsConnection(unittest.TestCase):
    def setUp(self):
        dashboard_data.reset_payload_cache()
        self.addCleanup(dashboard_data.reset_payload_cache)
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = scanner.get_db(self.db_path)
        scanner.init_db(conn)
        conn.commit()
        conn.close()

    def test_connection_closed_on_the_success_path(self):
        with _TrackedConnections(dashboard_data) as tracked:
            dashboard_data.get_dashboard_data(self.db_path)
        tracked.assert_all_closed(self)

    def test_connection_closed_when_a_query_raises(self):
        """The original defect: close() sat after the last query, so it never ran."""
        @contextlib.contextmanager
        def malformed(*_args, **_kwargs):
            raise sqlite3.DatabaseError("database disk image is malformed")
            yield  # pragma: no cover - makes this a context manager

        with _TrackedConnections(dashboard_data) as tracked:
            with mock.patch.object(dashboard_data, "database_admission", malformed):
                with self.assertRaises(sqlite3.DatabaseError):
                    dashboard_data.get_dashboard_data(self.db_path)
        tracked.assert_all_closed(self)

    def test_connection_closed_when_timestamp_registration_raises(self):
        """Setup performed after connect belongs inside the same close guard."""
        with _TrackedConnections(dashboard_data) as tracked:
            with mock.patch.object(
                    dashboard_data, "_register_timestamp_order",
                    side_effect=sqlite3.DatabaseError("registration failed")):
                with self.assertRaises(sqlite3.DatabaseError):
                    dashboard_data.get_dashboard_data(self.db_path)
        tracked.assert_all_closed(self)
        self.assertIsNone(dashboard_data._VERSION_PROBE)

    def test_repeated_failures_do_not_accumulate_connections(self):
        """A long-lived server must not lose a handle per failed poll."""
        @contextlib.contextmanager
        def locked(*_args, **_kwargs):
            raise sqlite3.OperationalError("locked")
            yield  # pragma: no cover - makes this a context manager

        with _TrackedConnections(dashboard_data) as tracked:
            with mock.patch.object(dashboard_data, "database_admission", locked):
                for _ in range(25):
                    with self.assertRaises(sqlite3.OperationalError):
                        dashboard_data.get_dashboard_data(self.db_path)
        # 25 request connections, and at most one probe however many polls ran —
        # which is the whole claim: a failing poll must not cost a handle, and
        # the probe must be reused rather than reopened per call.
        self.assertEqual(len(tracked.per_request()), 25)
        self.assertLessEqual(len(tracked.opened), 26)
        tracked.assert_all_closed(self)

    def test_a_failed_build_releases_an_idle_version_probe(self):
        """A probe only has a purpose while a cached payload refers to it.

        The failure happens after ``_database_version`` opens the probe but
        before any cache entry can be stored. On Windows that otherwise keeps
        the database file open for the rest of the dashboard process even
        though there is nothing for the probe to validate.
        """
        with _TrackedConnections(dashboard_data) as tracked:
            with mock.patch.object(
                    dashboard_data, "_collect_dashboard_data",
                    side_effect=sqlite3.DatabaseError("forced build failure")):
                with self.assertRaises(sqlite3.DatabaseError):
                    dashboard_data.get_dashboard_data(self.db_path)

        self.assertEqual(dashboard_data._PAYLOAD_CACHE, {})
        self.assertIsNone(dashboard_data._VERSION_PROBE)
        self.assertTrue(all(_is_closed(conn) for conn in tracked.opened),
                        "the failed request retained a SQLite connection")

    def test_a_failed_rebuild_discards_stale_cache_before_releasing_probe(self):
        """An old cache entry cannot justify keeping a changed probe open.

        A successful payload is made stale by another connection's commit.
        The next request misses that cache and fails while rebuilding it. The
        old entry is now unreachable, so retaining its probe is a handle leak
        that can prevent database replacement on Windows.
        """
        with _TrackedConnections(dashboard_data) as tracked, \
                mock.patch.object(
                    dashboard_data, "PAYLOAD_CACHE_MIN_BUILD_SECONDS", 0.0):
            dashboard_data.get_dashboard_data(self.db_path)
            self.assertTrue(dashboard_data._PAYLOAD_CACHE,
                            "the fixture did not create a cache entry")

            writer = sqlite3.connect(self.db_path)
            try:
                writer.execute(
                    "INSERT INTO turns(session_id, message_id, source) "
                    "VALUES ('cache-bump', 'cache-bump', 'claude')"
                )
                writer.commit()
            finally:
                writer.close()

            with mock.patch.object(
                    dashboard_data, "_collect_dashboard_data",
                    side_effect=sqlite3.DatabaseError("forced rebuild failure")):
                with self.assertRaises(sqlite3.DatabaseError):
                    dashboard_data.get_dashboard_data(self.db_path)

        self.assertEqual(dashboard_data._PAYLOAD_CACHE, {})
        self.assertIsNone(dashboard_data._VERSION_PROBE)
        self.assertTrue(all(_is_closed(conn) for conn in tracked.opened),
                        "stale cache state retained a SQLite connection")

    def test_the_version_probe_is_the_only_survivor_and_there_is_one(self):
        """Bounds the exception `per_request` makes, so it cannot widen.

        Without this, excluding the probe from the leak check would be a hole
        big enough to hide a real leak in: anything this module chose to stash on
        `_VERSION_PROBE` would become invisible to `assert_all_closed`.
        """
        with _TrackedConnections(dashboard_data) as tracked:
            for _ in range(5):
                dashboard_data.get_dashboard_data(self.db_path)
        survivors = [c for c in tracked.opened if not _is_closed(c)]
        self.assertLessEqual(len(survivors), 1, "more than one connection outlived its call")
        probe = getattr(dashboard_data, "_VERSION_PROBE", None)
        if survivors:
            self.assertIsNotNone(probe, "a connection survived and it is not the probe")
            self.assertIs(survivors[0], probe[1],
                          "the surviving connection is not the version probe")


class TestDatabaseOpenReleasesItsConnection(unittest.TestCase):
    def test_timestamp_registration_failure_closes_the_new_connection(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "usage.db"

        with _TrackedConnections(database) as tracked, \
                mock.patch.object(
                    database, "register_timestamp_order",
                    side_effect=sqlite3.DatabaseError("registration failed")):
            with self.assertRaisesRegex(sqlite3.DatabaseError,
                                        "registration failed"):
                database.get_db(path)

        self.assertEqual(len(tracked.opened), 1)
        self.assertTrue(_is_closed(tracked.opened[0]),
                        "failed database setup leaked its SQLite handle")


class TestScanReleasesItsConnection(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.projects_dir = tmp / "projects"
        self.projects_dir.mkdir()
        self.db_path = tmp / "usage.db"

    def test_connection_closed_on_the_success_path(self):
        # scanner imports get_db from the database module; track the connection
        # at its real owner instead of requiring scanner to retain an otherwise
        # dead sqlite3 compatibility import.
        with _TrackedConnections(database) as tracked:
            scanner.scan(projects_dir=self.projects_dir,
                         db_path=self.db_path, verbose=False)
        tracked.assert_all_closed(self)

    def test_connection_closed_when_the_scan_raises(self):
        with _TrackedConnections(database) as tracked:
            with mock.patch.object(
                    scanner, "discover_jsonl_files",
                    side_effect=OSError("disk went away mid-scan")):
                with self.assertRaises(OSError):
                    scanner.scan(projects_dir=self.projects_dir,
                                 db_path=self.db_path, verbose=False)
        tracked.assert_all_closed(self)

    def test_a_failed_scan_does_not_block_the_next_one(self):
        """The scan lock must be released too, or the server can never rescan."""
        with mock.patch.object(scanner, "discover_jsonl_files",
                               side_effect=OSError("transient")):
            with self.assertRaises(OSError):
                scanner.scan(projects_dir=self.projects_dir,
                             db_path=self.db_path, verbose=False)
        # Must not deadlock or raise: the lock and the connection are both free.
        result = scanner.scan(projects_dir=self.projects_dir,
                              db_path=self.db_path, verbose=False)
        self.assertEqual(result["new"], 0)


class TestCliReadCommandsReleaseTheirConnection(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = scanner.get_db(self.db_path)
        scanner.init_db(conn)
        conn.commit()
        conn.close()
        self._orig = cli.DB_PATH
        cli.DB_PATH = self.db_path
        self.addCleanup(lambda: setattr(cli, "DB_PATH", self._orig))

    def test_each_command_closes_even_when_rendering_raises(self):
        import io
        from contextlib import redirect_stdout

        for name in ("cmd_today", "cmd_week", "cmd_stats"):
            with self.subTest(command=name):
                with _TrackedConnections(cli) as tracked:
                    with mock.patch.object(
                            reports, "hr", side_effect=RuntimeError("render blew up")):
                        with redirect_stdout(io.StringIO()):
                            with self.assertRaises(RuntimeError):
                                getattr(cli, name)()
                tracked.assert_all_closed(self)

    def test_require_db_closes_when_connection_setup_raises(self):
        """A connection is owned as soon as the guarded open succeeds."""
        real_connect = sqlite3.connect
        opened = []

        class SetupFailureConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.startswith("PRAGMA busy_timeout"):
                    raise sqlite3.OperationalError("forced setup failure")
                return super().execute(sql, *args, **kwargs)

        def failing_connect(*args, **kwargs):
            kwargs["factory"] = SetupFailureConnection
            conn = real_connect(*args, **kwargs)
            opened.append(conn)
            return conn

        with mock.patch.object(database.sqlite3, "connect", failing_connect):
            with self.assertRaisesRegex(sqlite3.OperationalError,
                                        "forced setup failure"):
                cli.require_db()

        self.assertEqual(len(opened), 1)
        self.assertTrue(_is_closed(opened[0]),
                        "failed connection setup leaked its SQLite handle")


class TestHttpHandlersAnswerInsteadOfDroppingTheSocket(unittest.TestCase):
    """A failing query must produce a 500, not a reset connection.

    The page's fetch() and the VS Code webview cannot tell a dropped socket from
    a dead server, so a transiently locked database looked like a crash.
    """

    def test_api_data_returns_500_when_the_read_fails(self):
        handler = dashboard.DashboardHandler.__new__(dashboard.DashboardHandler)
        sent = {}
        handler._send_json = lambda status, value: sent.update(
            status=status, value=value)
        handler._authorize_host = lambda: True
        handler._api_request_is_authorized = lambda: True
        handler.path = "/api/data"
        with mock.patch.object(dashboard, "get_dashboard_data",
                               side_effect=sqlite3.OperationalError("locked")):
            handler.do_GET()
        self.assertEqual(sent["status"], 500)
        self.assertIn("error", sent["value"])

    def test_rescan_returns_500_when_the_scan_fails(self):
        handler = dashboard.DashboardHandler.__new__(dashboard.DashboardHandler)
        sent = {}
        handler._send_json = lambda status, value: sent.update(
            status=status, value=value)
        handler._authorize_host = lambda: True
        handler._api_request_is_authorized = lambda: True
        handler.path = "/api/rescan"
        try:
            with mock.patch.object(
                    scanner, "scan", side_effect=OSError("disk full")):
                handler.do_POST()
            self.assertEqual(sent["status"], 500)
            self.assertIn("error", sent["value"])
            self.assertEqual(dashboard.scan_status()["state"], "failed")
        finally:
            # The lifecycle is process-wide by design. Recover it so this
            # direct-handler failure fixture cannot make unrelated later
            # server classes reject every data read with HTTP 503.
            dashboard._scan_activity_started()
            dashboard._scan_activity_finished(True)

    def test_a_failed_rescan_invalidates_the_server_database_cache(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(dashboard_data.reset_payload_cache)
        db_path = Path(tmp.name) / "usage.db"
        conn = scanner.get_db(db_path)
        scanner.init_db(conn)
        conn.commit()
        conn.close()
        with mock.patch.object(
                dashboard_data, "PAYLOAD_CACHE_MIN_BUILD_SECONDS", 0.0):
            dashboard_data.get_dashboard_data(db_path)
        self.assertTrue(dashboard_data._PAYLOAD_CACHE)
        self.assertIsNotNone(dashboard_data._VERSION_PROBE)

        handler = dashboard.DashboardHandler.__new__(dashboard.DashboardHandler)
        sent = {}
        handler._send_json = lambda status, value: sent.update(
            status=status, value=value)
        handler._authorize_host = lambda: True
        handler._api_request_is_authorized = lambda: True
        handler.path = "/api/rescan"
        try:
            with mock.patch.object(dashboard, "DB_PATH", db_path), \
                    mock.patch.object(
                        scanner, "scan", side_effect=OSError("disk full")):
                handler.do_POST()
            self.assertEqual(sent["status"], 500)
            self.assertEqual(dashboard_data._PAYLOAD_CACHE, {})
            self.assertIsNone(dashboard_data._VERSION_PROBE)
        finally:
            dashboard._scan_activity_started()
            dashboard._scan_activity_finished(True)

    def test_the_rescan_lock_is_released_after_a_failure(self):
        """Otherwise one failed rescan makes every later one return 409."""
        self.test_rescan_returns_500_when_the_scan_fails()
        self.assertTrue(dashboard.RESCAN_LOCK.acquire(blocking=False),
                        "RESCAN_LOCK was still held after a failed rescan")
        dashboard.RESCAN_LOCK.release()


if __name__ == "__main__":
    unittest.main()
