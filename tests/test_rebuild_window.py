"""Two historical races in concurrent schema initialization and rebuilding.

Initialization is concurrent BY DESIGN — `cli.cmd_dashboard` binds and serves
before it starts its background scan thread, the server is a
`ThreadingHTTPServer`, and both `dashboard_data` entry points enter
`database_admission` on the request thread. So "another opener is rebuilding right now" is the ordinary
case, not the exotic one, and these are the two ways that went wrong.

**The ownership gate was a TOCTOU across two unlocked reads.** `init_db` read
`stored_tables`, then `looks_like_our_database` read it again. Between them the
winner of a rebuild race drops all six tables and has not yet recreated them, so
the first read saw a populated file and the second an empty one — no signature
matched, and `init_db` raised `ForeignDatabaseError` about the product's OWN
database, moments after emptying it, killing the scan that was about to refill
it. One observation cannot disagree with itself, so there is now one.

**The hygiene used to run while the database had no tables.** The rebuild now
marks the file, drops and recreates the complete schema in one transaction, and
only then runs `VACUUM` plus `wal_checkpoint(TRUNCATE)`. The marker stays set
until both finish; a pinned reader therefore makes the opener fail closed
instead of exposing a table-less or uncleared database.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import db


def _older_schema(path):
    """A complete v1.0--v1.4 database, not a look-alike fragment."""
    conn = sqlite3.connect(path)
    for table, columns in db._LEGACY_BASE_SCHEMA.items():
        declared = ", ".join(f'"{column}" TEXT' for column in columns)
        conn.execute(f'CREATE TABLE "{table}" ({declared})')
    conn.execute("INSERT INTO turns (session_id, message_id, input_tokens)"
                 " VALUES ('s', 'm', 1)")
    conn.commit()
    conn.close()
    return path


class TestTheOwnershipGateAnswersForOneObservation(unittest.TestCase):
    def test_a_stale_schema_observation_is_not_ownership_evidence(self):
        """An unmarked database must match now, not in an earlier reading."""
        path = _older_schema(Path(tempfile.mkdtemp()) / "usage.db")
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        present = db.stored_tables(conn)
        self.assertTrue(present, "fixture should start populated")
        # The winner drops everything.
        for name in present:
            conn.execute(f'DROP TABLE IF EXISTS "{name}"')
        conn.commit()
        self.assertFalse(
            db.looks_like_our_database(conn, present),
            "a stale pre-drop fingerprint claimed a now-ambiguous database")

    def test_a_genuinely_foreign_file_is_still_disowned(self):
        """Anti-vacuity. The re-read must not turn the gate into `return True`:
        a foreign file that does NOT change under the check is still refused,
        which is the whole reason the gate exists."""
        path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        conn.executescript("CREATE TABLE sessions (id INTEGER, payload TEXT);"
                           "CREATE TABLE customers (id INTEGER, iban TEXT);")
        conn.commit()
        self.assertFalse(
            db.looks_like_our_database(conn, db.stored_tables(conn)))

    def test_a_foreign_file_that_moves_under_the_check_is_still_disowned(self):
        """The case the test above cannot reach, and the one that mattered.

        Its fixture does not change under the check, so it never exercises the
        re-read branch at all -- it passed the whole time the branch answered
        `True` for every moving file. A foreign database whose own application
        commits DDL inside the window is the reachable version of that, and
        blanket-True fed it to `rebuild_database`: measured at 9 destroyed in 30
        runs of the READ-ONLY `cli.py stats` against a `notes`/`invoices` file.
        """
        path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        conn.executescript("CREATE TABLE notes (id INTEGER, body TEXT);"
                           "CREATE TABLE invoices (id INTEGER, iban TEXT);")
        conn.commit()
        present = db.stored_tables(conn)
        conn.execute("CREATE TABLE session_cache (k TEXT)")   # the owning app
        conn.commit()
        self.assertNotEqual(set(db.stored_tables(conn)), set(present),
                            "fixture must move, or this repeats the test above")
        self.assertFalse(
            db.looks_like_our_database(conn, present),
            "a foreign file was claimed as ours because it moved under the check")

    def test_a_name_of_ours_on_foreign_columns_does_not_survive_the_re_read(self):
        """`sessions` is one of the commonest table names in the SQL world, so a
        subset test on NAMES is not enough: a Laravel-shaped file that
        transiently holds only `sessions` is a subset of ours and must still be
        refused. The re-read therefore checks columns, not just names."""
        path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        conn.executescript("CREATE TABLE sessions (id TEXT PRIMARY KEY,"
                           " payload TEXT);"
                           "CREATE TABLE users (id INTEGER, email TEXT);")
        conn.commit()
        present = db.stored_tables(conn)
        conn.execute("DROP TABLE users")          # now holds only `sessions`
        conn.commit()
        self.assertEqual(db.stored_tables(conn), ["sessions"])
        self.assertFalse(
            db.looks_like_our_database(conn, present),
            "a foreign `sessions` was claimed as ours by a name-only subset test")

    def test_a_foreign_file_that_goes_completely_empty_is_still_disowned(self):
        """The third generation of this defect, and the one the empty set hides.

        The re-check tests `now <= EXPECTED_COLUMNS`, and the empty set is a
        subset of everything -- so for a file caught between its owning
        application's `DROP` and its `CREATE` that test is VACUOUSLY true, the
        signature loop below it iterates zero times, and the function falls
        straight through to `return True`. A foreign database was claimed as
        ours because it was momentarily table-less.

        What separates that from the race this branch exists for is the reading
        we actually hold: in the race `present` is OUR six table names, and here
        it is somebody else's. Hence the `present` test beside the `now` one.
        """
        path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        conn.executescript("CREATE TABLE notes (id INTEGER, body TEXT);"
                           "CREATE TABLE invoices (id INTEGER, iban TEXT);")
        conn.commit()
        present = db.stored_tables(conn)
        for name in present:                       # the owning app's teardown
            conn.execute(f'DROP TABLE "{name}"')
        conn.commit()
        self.assertEqual(db.stored_tables(conn), [],
                         "fixture must be table-less, or it misses the branch")
        self.assertFalse(
            db.looks_like_our_database(conn, present),
            "a table-less FOREIGN file was claimed as ours by a vacuous subset")

    def test_the_durable_identity_survives_a_tableless_rebuild_window(self):
        """Once adopted, ownership no longer depends on transient tables."""
        path = _older_schema(Path(tempfile.mkdtemp()) / "usage.db")
        conn = db.get_db(path)
        self.addCleanup(conn.close)
        present = db.stored_tables(conn)
        for name in present:
            conn.execute(f'DROP TABLE "{name}"')
        conn.commit()
        self.assertEqual(db._application_id(conn), db.APPLICATION_ID)
        self.assertFalse(db.init_db(conn, path))
        self.assertEqual(set(db.stored_tables(conn)), set(db.EXPECTED_COLUMNS))

    def test_ownership_is_re_asked_under_the_write_lock(self):
        """Every unlocked answer has been wrong once. `rebuild_database` asks
        again with `BEGIN IMMEDIATE` held, where the file cannot move, and
        refuses rather than dropping if the answer is no."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(db._rebuild_database_unlocked))
        names = [n.func.id for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        self.assertIn("looks_like_our_database", names,
                      "the rebuild drops without re-asking whether the file is ours")

    def test_init_db_takes_exactly_one_reading_of_the_file(self):
        """Structural, because the race it prevents is timing-dependent and a
        second reading would reintroduce it invisibly.

        Counts `stored_tables` calls in the admitted implementation — not in
        the functions it calls, which take theirs under the write lock.
        """
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(db._init_db_admitted))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "stored_tables"]
        self.assertEqual(
            len(calls), 1,
            "init_db must observe the file once and pass that reading on")


class TestTheDatabaseIsNotLeftTableLessWhileHygieneRuns(unittest.TestCase):
    def test_the_rebuild_leaves_the_schema_in_place(self):
        """The marked rebuild recreates the schema before cleanup begins."""
        path = _older_schema(Path(tempfile.mkdtemp()) / "usage.db")
        conn = db.get_db(path)
        self.addCleanup(conn.close)
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(db.init_db(conn, path))
        self.assertEqual(sorted(db.stored_tables(conn)),
                         sorted(db.EXPECTED_COLUMNS))

    def test_rebuild_cleanup_runs_after_the_marked_schema_transaction(self):
        """Structural guard for the in-place fail-closed cleanup ordering."""
        import ast
        import inspect
        source = inspect.getsource(db._rebuild_database_unlocked)
        rebuild = ast.parse(source)
        self.assertIn("_cleanup_rebuild", source)
        self.assertLess(source.index("for statement in SCHEMA_STATEMENTS"),
                        source.index("_cleanup_rebuild(conn)"),
                        "cleanup must follow complete schema creation")
        names = [n.func.id for n in ast.walk(rebuild)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        self.assertNotIn("_checkpoint_wal", names,
                         "cleanup owns checkpointing after the schema commit")

        init = ast.parse(inspect.getsource(db._init_db_admitted))
        init_names = [n.func.id for n in ast.walk(init)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        self.assertNotIn("_checkpoint_wal", init_names,
                         "init_db must not duplicate rebuild cleanup")


class TestFreshSchemaPublicationIsAtomic(unittest.TestCase):
    def test_every_table_commits_together(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        traced = []
        conn.set_trace_callback(traced.append)

        db.init_db(conn)

        first_create = next(i for i, sql in enumerate(traced)
                            if "CREATE TABLE" in sql.upper())
        begin = max(i for i, sql in enumerate(traced[:first_create])
                    if sql.strip().upper() == "BEGIN IMMEDIATE")
        commit = next(i for i, sql in enumerate(traced[begin + 1:], begin + 1)
                      if sql.strip().upper() == "COMMIT")
        transaction = traced[begin:commit + 1]
        creates = [sql for sql in transaction if "CREATE TABLE" in sql.upper()]
        self.assertEqual(len(creates), len(db.EXPECTED_COLUMNS))


if __name__ == "__main__":
    unittest.main()
