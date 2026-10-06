"""How far a rebuild reaches, on the two paths where it reached too far.

`db.init_db` drops every table when the schema in front of it is not the one
this build declares. Two callers made that decision wrong in opposite
directions, and both were reproduced end to end on 2026-08-15 before this file
existed.

**`POST /api/rescan` narrowed the corpus.** `scanner.scan`'s `include_defaults`
defaults to False and the handler did not pass it, so a dashboard launched as
`cli.py dashboard --projects-dir X` rescanned X alone — harmless while a rescan
could only add rows, and destructive the moment it could drop them. Measured
against the arguments the handler used to compute: a database legitimately
holding six sessions from the default roots plus X, an older install touching
the file, one press of Rescan, and five of the six were gone. HTTP 200, a
success-shaped body, and no banner, because `dashboard_data._database_is_unscanned`
asks whether `processed_files` is empty and one refilled root is enough to make
it not. The only signal was one line on stderr.

**A fresh install called itself foreign.** Older builds published the six tables
one at a time and outside any transaction, so a second opener arriving
mid-creation could read a strict prefix of them. All five prefixes
printed the product's loudest data-loss notice and returned True, which is what
makes a read-only `cli.py stats` exit 1 against a database nobody but the other
opener had ever written. The window is observable rather than theoretical: 144
partial reads in 16,714 samples against one `init_db` on an empty file
(2026-08-15, this machine).

This build closes the table-prefix window by creating every table in one
transaction after claiming the application id. It still recognises and completes
a prefix left by an older process sharing the file. The four negative cases
below keep that compatibility exemption from becoming a data-loss hole: a
partial set WITH rows, an extra table, a column skew, and a file that is not ours
are all still rebuilt or refused.
"""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import dashboard
import dashboard_data
import db
import scanner


def create_statements():
    """The CREATE TABLE statements of `db.SCHEMA_SQL`, in the order it runs them.

    Read back out of a scratch database rather than split off the string: the
    schema carries `;` inside comments and inside a PRIMARY KEY clause, so any
    hand-rolled split gets a different — and silently shorter — list.
    """
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.executescript(db.SCHEMA_SQL)
        return [row[0] for row in scratch.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY rowid")]
    finally:
        scratch.close()


def write_transcript(root, session):
    """One Claude transcript holding one assistant turn."""
    project = root / "proj"
    project.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "assistant",
        "sessionId": session,
        "timestamp": "2026-08-10T01:00:00.000Z",
        "cwd": str(project),
        "gitBranch": "main",
        "message": {
            "id": f"msg_{session}",
            "model": "claude-opus-5",
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0},
        },
    }
    (project / f"{session}.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8")


def make_foreign(path):
    """The documented shared-database case: an older install's marker table."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    finally:
        conn.close()


def sessions_in(path):
    conn = sqlite3.connect(path)
    try:
        return sorted(row[0] for row in conn.execute(
            "SELECT session_id FROM sessions"))
    finally:
        conn.close()


class TestARescanNeverCoversFewerRootsThanTheScanThatFilledIt(unittest.TestCase):
    """DD-3. The Rescan button, on a dashboard started with `--projects-dir`.

    Behavioural rather than a check on the call's shape: the assertion is the
    set of sessions left in the database, so it holds however the roots are
    resolved. Dropping `include_defaults=True` from `do_POST` reds it with
    `['extra'] != ['extra', 'home0', ...]`.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.db_path = tmp / "usage.db"
        self.home_root = tmp / "home_projects"
        self.extra_root = tmp / "extra_projects"
        for i in range(3):
            write_transcript(self.home_root, f"home{i}")
        write_transcript(self.extra_root, "extra")
        # The startup scan `cli.cmd_dashboard` performs: the defaults AND the
        # root the process was launched with.
        with mock.patch.object(scanner, "DEFAULT_PROJECTS_DIRS", [self.home_root]):
            scanner.scan(db_path=self.db_path, projects_dirs=[self.extra_root],
                         include_defaults=True, verbose=False)
        self.assertEqual(sessions_in(self.db_path),
                         ["extra", "home0", "home1", "home2"],
                         "fixture: the startup scan must reach both roots")

    def rescan_over_a_foreign_database(self):
        make_foreign(self.db_path)
        handler = dashboard.DashboardHandler.__new__(dashboard.DashboardHandler)
        sent = {}
        handler._send_json = lambda status, value: sent.update(
            status=status, value=value)
        handler._authorize_host = lambda: True
        handler._api_request_is_authorized = lambda: True
        handler.path = "/api/rescan"
        with mock.patch.object(dashboard, "DB_PATH", self.db_path), \
                mock.patch.object(dashboard, "PROJECTS_DIRS", [self.extra_root]), \
                mock.patch.object(scanner, "DEFAULT_PROJECTS_DIRS", [self.home_root]), \
                redirect_stderr(io.StringIO()):
            handler.do_POST()
        return sent

    def test_a_rebuilding_rescan_refills_every_root_the_database_held(self):
        sent = self.rescan_over_a_foreign_database()
        self.assertEqual(sent["status"], 200)
        self.assertEqual(sessions_in(self.db_path),
                         ["extra", "home0", "home1", "home2"])

    def test_the_success_body_is_not_the_thing_that_tells_you(self):
        """Why the assertion above has to read the database.

        The endpoint answers 200 with a plausible count either way, and the
        page cannot see the stderr notice — so a body-shaped test would have
        passed against the defect it is here to catch.
        """
        sent = self.rescan_over_a_foreign_database()
        self.assertEqual(sent["status"], 200)
        self.assertIn("turns", sent["value"])
        self.assertNotIn("error", sent["value"])

    def test_the_banner_would_not_have_caught_it_either(self):
        """`unscanned` reads `processed_files`, which one refilled root fills.

        Kept as the record of why the fix is in the roots and not in the
        notice: this stays False on the repaired path and was False on the
        broken one too.
        """
        self.rescan_over_a_foreign_database()
        conn = db.get_db(self.db_path)
        try:
            self.assertFalse(dashboard_data._database_is_unscanned(conn))
        finally:
            conn.close()


class TestTheBackgroundScanCoversTheSameRoots(unittest.TestCase):
    """`python dashboard.py`'s ingestion thread, held to the same rule.

    Unobservable from that entry point today — it parses no arguments, so
    `PROJECTS_DIRS` is None and the resolved roots are the defaults either way —
    which is exactly why it is asserted here rather than left to reading: the
    property is meant to survive a later caller that does set it.
    """

    def test_it_scans_the_defaults_as_well_as_the_configured_root(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            db_path = tmp / "usage.db"
            home_root, extra_root = tmp / "home", tmp / "extra"
            write_transcript(home_root, "home0")
            write_transcript(extra_root, "extra")
            with mock.patch.object(dashboard, "DB_PATH", db_path), \
                    mock.patch.object(dashboard, "PROJECTS_DIRS", [extra_root]), \
                    mock.patch.object(scanner, "DEFAULT_PROJECTS_DIRS", [home_root]), \
                    redirect_stdout(io.StringIO()):
                dashboard._background_scan()
            self.assertEqual(sessions_in(db_path), ["extra", "home0"])


class TestAnOlderHalfBuiltDatabaseIsCompleted(unittest.TestCase):
    """C3. Every prefix an older schema publisher could leave behind."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.creates = create_statements()

    def test_the_prefixes_this_file_tests_are_the_ones_the_schema_creates(self):
        """Without this the loop below can pass by testing nothing at all."""
        self.assertEqual(len(self.creates), len(db.EXPECTED_COLUMNS))
        self.assertGreater(len(self.creates), 1)

    def half_built(self, n):
        """A database holding the first `n` tables `SCHEMA_SQL` creates."""
        path = self.dir / f"partial{n}.db"
        conn = sqlite3.connect(path)
        try:
            conn.execute(f"PRAGMA application_id = {db.APPLICATION_ID}")
            for statement in self.creates[:n]:
                conn.execute(statement)
            conn.commit()
        finally:
            conn.close()
        return path

    def test_no_prefix_of_the_creation_is_called_foreign(self):
        for n in range(1, len(self.creates)):
            with self.subTest(tables=n):
                path = self.half_built(n)
                conn = db.get_db(path)
                err = io.StringIO()
                try:
                    with redirect_stderr(err):
                        rebuilt = db.init_db(conn, path)
                    self.assertFalse(
                        rebuilt,
                        "a partly created database was reported as rebuilt, "
                        "which is what makes cli.py stats exit 1 on a fresh "
                        "install")
                    self.assertNotIn("different version", err.getvalue())
                    self.assertEqual(sorted(db.stored_tables(conn)),
                                     sorted(db.EXPECTED_COLUMNS),
                                     "the exemption must still finish the schema")
                finally:
                    conn.close()

    def test_the_predicate_answers_for_every_prefix_and_not_for_an_empty_file(self):
        """The direct question, so a failure above names which half broke.

        The complete set is excluded because `rebuild_database` returns before
        reaching the predicate when nothing mismatches — asserting it here is
        what stops someone `or`-ing that case in later.
        """
        for n in range(1, len(self.creates)):
            with self.subTest(tables=n):
                conn = db.get_db(self.half_built(n))
                try:
                    self.assertTrue(db.is_a_half_built_database(conn))
                finally:
                    conn.close()
        path = self.dir / "empty.db"
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA application_id = {db.APPLICATION_ID}")
        conn.commit()
        try:
            self.assertFalse(db.is_a_half_built_database(conn),
                             "a file with no tables is the OTHER exemption")
        finally:
            conn.close()


class TestTheExemptionStopsWhereTheDataStarts(unittest.TestCase):
    """The four cases that must still be rebuilt or refused.

    Each one deletes a different conjunct of `db.is_a_half_built_database`, so
    together they pin the predicate rather than its name.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def outcome(self, name, seed):
        path = self.dir / f"{name}.db"
        conn = db.get_db(path)
        try:
            seed(conn)
            conn.commit()
        finally:
            conn.close()
        conn = db.get_db(path)
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                rebuilt = db.init_db(conn, path)
            return rebuilt, err.getvalue(), conn
        finally:
            conn.close()

    def test_a_partial_table_set_holding_rows_is_still_rebuilt(self):
        """The row check. Without it the exemption eats a real database."""
        creates = create_statements()

        def seed(conn):
            for statement in creates[:3]:
                conn.execute(statement)
            conn.execute("INSERT INTO turns (session_id, message_id) "
                         "VALUES ('s', 'm')")

        rebuilt, notice, _ = self.outcome("partial_rows", seed)
        self.assertTrue(rebuilt)
        self.assertIn("different version", notice)

    def test_an_extra_table_is_still_rebuilt_even_with_no_rows_anywhere(self):
        """The subset check. An extra table is another build's, not an unfinished
        one — `schema_meta` is what four released versions declare."""
        def seed(conn):
            conn.executescript(db.SCHEMA_SQL)
            conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, "
                         "value TEXT)")

        rebuilt, notice, _ = self.outcome("extra_table", seed)
        self.assertTrue(rebuilt)
        self.assertIn("different version", notice)

    def test_a_column_skew_is_still_rebuilt_even_with_no_rows_anywhere(self):
        """The per-table column check. A `turns` missing a column is a different
        build's table, and a CREATE TABLE IF NOT EXISTS will not repair it."""
        def seed(conn):
            conn.executescript(db.SCHEMA_SQL)
            conn.execute("ALTER TABLE turns DROP COLUMN git_branch")

        rebuilt, notice, _ = self.outcome("column_skew", seed)
        self.assertTrue(rebuilt)
        self.assertIn("different version", notice)

    def test_a_file_that_is_not_ours_is_still_refused_outright(self):
        """DD-1's gate sits in front of all of this and must stay in front.

        Worth asserting here because the widened exemption is reached through
        the same branch: an empty foreign file must not slip past it as a
        half-built one.
        """
        path = self.dir / "notes.db"
        # A foreign application does not open its database through our
        # `get_db`: doing so claims the file with codex-claude-usage's durable SQLite
        # application id before its first table is created. Build this fixture
        # through SQLite itself so it has no product identity.
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(db.ForeignDatabaseError):
            db.get_db(path)
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(db.stored_tables(conn), ["notes"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
