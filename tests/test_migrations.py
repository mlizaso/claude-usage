"""There are no migrations, and this file is what pins that.

`db.SCHEMA_SQL` declares the whole schema inline and `init_db` does exactly one
thing beyond running it: if the stored database does not match, it drops
everything and recreates it. Nothing is converted, nothing is repaired in place,
and no `schema_meta` marker records that any of it happened — the file is a
derived cache of ~/.claude/projects and ~/.codex/sessions, and the next scan
refills it.

Rebuild owned incompatible schemas instead of keeping migration-complete
markers that can outlive failed work. Verify the current schema on every open
and reclaim pages that may still hold discarded transcript-derived data.

Initialization is still concurrent *by design* — `cli.cmd_dashboard` binds and
serves before it starts its background `scan()` thread, the server is a
`ThreadingHTTPServer`, and dashboard request threads enter `database_admission`
— so the rebuild re-checks the schema under the write lock before dropping
anything, and that is pinned below.
"""

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
import unittest
from pathlib import Path
from unittest import mock

import scanner
import db
from db import (BUSY_TIMEOUT_MS, EXPECTED_COLUMNS, _enable_wal, get_db,
                init_db, rebuild_database, schema_mismatches, stored_tables)


class _MigrationTempDb(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "usage.db"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def columns(self, table):
        """Read a table's columns without leaving the file open: Windows cannot
        remove a directory while a handle into it is live."""
        conn = get_db(self.db_path)
        try:
            return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        finally:
            conn.close()


_LOCK_HOLDER = """
import sqlite3, sys, time
mode, path, hold = sys.argv[1], sys.argv[2], float(sys.argv[3])
conn = sqlite3.connect(path, timeout=0)
if mode == "write":
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO processed_files (path, mtime, lines)"
                 " VALUES ('holder', 1.0, 1)")
else:
    conn.execute("BEGIN")
    conn.execute("SELECT COUNT(*) FROM turns").fetchone()
print("HELD", flush=True)
time.sleep(hold)
conn.rollback()
conn.close()
"""


_KILL_DURING_REBUILD_CLEANUP = """
import os, sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
import db

path = Path(sys.argv[1])
conn = db.get_db(path)
db._checkpoint_wal = lambda ignored: os._exit(97)
db.init_db(conn, path)
"""


@contextlib.contextmanager
def _lock_held_by_another_process(test, mode, db_path, hold_seconds):
    """Hold a real SQLite lock from a second OS process.

    A second *process*, not a second connection in this one, because that is
    the shape the product creates and the only one that exercises the file
    locks: one dashboard per VS Code window, its background scan thread,
    `/api/rescan`, and `cli.py scan` in a terminal, all on one usage.db.
    `scanner._SCAN_LOCK` serialises only what is inside a single interpreter.

    Written to a file rather than passed as `python -c`: the script is several
    lines and Windows re-parses the command line in the child, which is a
    quoting hazard this test has no reason to take on.
    """
    script = Path(db_path).with_name("lock_holder.py")
    script.write_text(_LOCK_HOLDER, encoding="utf-8")
    # Both ends of the pipe pinned: `encoding=` here, PYTHONIOENCODING in the
    # child. A Python child left on `locale.getencoding()` writes cp1252 on
    # windows-latest, and decoding that as UTF-8 is a mismatch of its own.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    holder = subprocess.Popen(
        [sys.executable, str(script), mode, str(db_path), str(hold_seconds)],
        stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env)
    try:
        ready = holder.stdout.readline().strip()
        if ready != "HELD":
            test.fail(f"the lock holder never took the lock: {ready!r}")
        yield
    finally:
        holder.terminate()
        holder.wait(timeout=30)
        holder.stdout.close()


def _transcript(path, session_id, message_ids):
    """A minimal Claude transcript: one usage-bearing assistant record each."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps({
        "type": "assistant", "sessionId": session_id,
        "timestamp": f"2026-08-01T10:{n:02d}:00Z",
        "cwd": "/home/u/proj", "gitBranch": "main",
        "message": {"id": mid, "model": "claude-opus-4-8", "content": [],
                    "usage": {"input_tokens": 100, "output_tokens": 50,
                              "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0}},
    }) for n, mid in enumerate(message_ids)) + "\n", encoding="utf-8")


class TestTheConnectionSettingsScansDependOn(_MigrationTempDb):
    """`get_db` is the product opener that configures WAL and the busy timeout
    up front. Bare CLI/dashboard connections can initialize or rebuild through
    path-supplied admission, but inherit the file's persistent journal mode.
    Both settings are asserted because each covers a different race below."""

    def test_the_writer_waits_far_longer_than_sqlite_s_default(self):
        """Five seconds is shorter than one `/api/data` rollup on a real
        database, which is why the scan's `conn.commit()` raised rather than
        waited."""
        conn = get_db(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0],
                         BUSY_TIMEOUT_MS)
        self.assertGreater(BUSY_TIMEOUT_MS, 5000,
                           "5000 is the default this exists to replace")

    def test_the_database_is_write_ahead_logged(self):
        """Journal mode is a property of the FILE, so this is also what the
        bare `sqlite3.connect` read paths in `dashboard_data` and `cli`
        inherit without knowing about it."""
        conn = get_db(self.db_path)
        init_db(conn)
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    @unittest.skipUnless(os.name == "posix", "POSIX modes only")
    def test_the_write_ahead_sidecars_are_owner_only_too(self):
        """`secure_db_permissions` hardens one path and WAL adds two more
        beside it. SQLite derives their mode from the database's own, so this
        asserts a property we inherit rather than one we set — which is
        exactly why it is worth pinning."""
        conn = get_db(self.db_path)
        init_db(conn)
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO processed_files (path, mtime, lines)"
                     " VALUES ('k', 1.0, 1)")
        conn.commit()
        sidecars = sorted(p.name for p in self.db_path.parent.iterdir()
                          if p.name.startswith("usage.db-"))
        self.assertEqual(sidecars, ["usage.db-shm", "usage.db-wal"])
        for name in sidecars:
            self.assertEqual(
                (self.db_path.parent / name).stat().st_mode & 0o777, 0o600,
                f"{name} is readable by someone other than its owner")

    def test_a_filesystem_that_cannot_map_the_wal_index_is_walked_back(self):
        """The dangerous shape, and the reason the mode is proved with a read.

        Journal mode is persistent, so a conversion that succeeds and only
        then cannot map the `-shm` wal-index (reported on some network and
        container-bind filesystems) would not break one run — it would break
        every run after it, with the failure baked into the file header.
        """
        conn = get_db(self.db_path)
        self.addCleanup(conn.close)

        class _TheWalIndexCannotBeMapped:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args):
                if sql.strip().lower().startswith("select count(*) from sqlite_master"):
                    raise sqlite3.OperationalError("disk I/O error")
                return self._real.execute(sql, *args)

        self.assertEqual(_enable_wal(_TheWalIndexCannotBeMapped(conn)), "delete")
        self.assertEqual(
            conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "delete",
            "the database was left in a mode nothing on this filesystem can read")

    def test_a_conversion_that_cannot_run_leaves_the_database_usable(self):
        """The PRAGMA takes a lock to convert, so it can raise `database is
        locked` under exactly the contention it exists to remove. Opening the
        database must not start failing for that."""
        conn = get_db(self.db_path)
        init_db(conn)
        self.addCleanup(conn.close)

        class _TheConversionIsLockedOut:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args):
                if "journal_mode" in sql.lower():
                    raise sqlite3.OperationalError("database is locked")
                return self._real.execute(sql, *args)

        self.assertIsNone(_enable_wal(_TheConversionIsLockedOut(conn)))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 0)


class TestScanSurvivesConcurrentAccess(_MigrationTempDb):
    """The product creates this concurrency itself, and `scan()` used to die of
    it with an unhandled `sqlite3.OperationalError: database is locked`.

    Both races below are two real OS processes against one database file. They
    are separate tests because the two halves of the remedy are not
    interchangeable: measured here, write-ahead logging alone still fails the
    writer race at 5.4s — indistinguishable from no fix at all — and the busy
    timeout alone survives the reader race only by *waiting out* the reader,
    which a page that keeps polling can extend indefinitely.

    What the scan does on failure is why this is worth two subprocesses:
    `cli.main` dispatches `cmd_scan` outside its only try/except, so
    `cli.py scan` prints a raw traceback and exits non-zero; `/api/rescan`
    answers 500; and `cmd_dashboard`'s background scan — the VS Code
    extension's only ingestion path — prints one line to its output channel and
    then serves stale data for the rest of the session.
    """

    def setUp(self):
        super().setUp()
        self.projects = Path(self.tmpdir) / "projects"
        _transcript(self.projects / "first.jsonl", "s1", ["m1", "m2", "m3"])
        self._scan()
        _transcript(self.projects / "second.jsonl", "s2", ["m4", "m5", "m6"])

    def _scan(self):
        return scanner.scan(projects_dir=str(self.projects),
                            db_path=self.db_path, verbose=False)

    def test_a_concurrent_reader_does_not_stop_the_scan(self):
        """The headline shape: one page building `/api/data`, one scan.

        A rollback-journal reader can block a writer. The scanner must use the
        shared timeout contract during commits as well as initial connection
        setup."""
        held = 20.0
        started = time.monotonic()
        with _lock_held_by_another_process(self, "read", self.db_path, held):
            result = self._scan()                      # must not raise
            elapsed = time.monotonic() - started
        self.assertEqual(result["new"], 1)
        self.assertLess(elapsed, held / 2,
                        "the scan waited for the reader instead of ignoring it")
        conn = get_db(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 6)

    def test_a_second_writer_delays_the_scan_rather_than_killing_it(self):
        """The half write-ahead logging does not cover.

        WAL permits exactly one writer, so a second scanner process — a second
        VS Code window's background scan, `/api/rescan`, or a terminal
        `cli.py scan` — still has to wait, and with the 5s default it did not
        wait, it raised. Six seconds, because five is the default being
        replaced; the scan is expected to spend most of them blocked.
        """
        with _lock_held_by_another_process(self, "write", self.db_path, 6.0):
            result = self._scan()                      # must not raise
        self.assertEqual(result["new"], 1)
        conn = get_db(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 6)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM processed_files "
                         "WHERE path = 'holder'").fetchone()[0], 0,
            "the blocking writer's rolled-back row was committed")

    def test_waiting_for_a_lock_is_all_it_does_constraints_still_raise(self):
        """A busy timeout was chosen over a retry loop and over a blanket
        `INSERT OR IGNORE` for this reason: it makes SQLite *wait* for a lock
        and changes nothing else, so a genuine constraint violation still comes
        out of the same connection instead of being retried into silence or
        ignored. A landmine for any future remedy that catches more.
        """
        conn = get_db(self.db_path)
        init_db(conn)
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO processed_files (path, mtime, lines)"
                     " VALUES ('k', 1.0, 1)")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO processed_files (path, mtime, lines)"
                         " VALUES ('k', 2.0, 2)")


def _snapshot_row(observed_at, percent=83, severity="warning"):
    """One `usage_limits_snapshots` row in `codex_snapshot_rows`' tuple order."""
    return ("codex", "10080m", "pro", "2026-08-08T12:00", percent,
            severity, 1, "2026-08-08T12:00:00+00:00", 0, observed_at)


class TestWhatRecordLimitSnapshotActuallyKeeps(_MigrationTempDb):
    """The landmine that was here has been defused: this now asserts the RIGHT
    answer, and it is what proves the storage half of the fix arrived.

    A quota level keeps its earliest observed timestamp regardless of file
    order. INSERT OR IGNORE would retain whichever row arrived first.

    Three changes were needed together and all three have landed:

    - `codex_transcripts.parse_jsonl_file` keeps the FIRST record per key
      instead of collapsing a level's run last-wins before it ever arrives
      here (the dominant cause);
    - this function upserts the earlier instant over the later one, so the
      answer no longer depends on the order `discover_jsonl_files` happens to
      produce the rollouts in;
    - `dashboard_data.codex_limits_projection` no longer derives the plan
      panel's `age_seconds` from `max(observed_at)` over this table, which
      would otherwise have started aging from when a level was reached rather
      than from the last thing Codex recorded.

    The persisted `observed_at_order` guard is what keeps the other promise in
    that docstring true — a rescan that re-reads an unchanged cache still
    writes nothing — and `test_a_later_observation_writes_nothing_at_all`
    below is what pins it.
    """

    def test_the_earliest_observation_wins_whatever_order_rows_arrive_in(self):
        conn = get_db(self.db_path)
        init_db(conn)
        self.addCleanup(conn.close)
        # Offered in the order `discover_jsonl_files` produces them: the rollout
        # that STARTED first sorts first and is still running, so its copy of
        # this level is the *later* observation of the two.
        scanner.record_limit_snapshot(conn, [_snapshot_row("2026-08-05T09:13:11Z")])
        scanner.record_limit_snapshot(conn, [_snapshot_row("2026-08-05T03:06:31Z")])
        stored = [r["observed_at"] for r in conn.execute(
            "SELECT observed_at FROM usage_limits_snapshots")]
        self.assertEqual(stored, ["2026-08-05T03:06:31Z"])

    def test_mixed_offsets_are_compared_as_instants_not_text(self):
        conn = get_db(self.db_path)
        init_db(conn)
        self.addCleanup(conn.close)
        # 11:00+02:00 is 09:00Z, earlier than the stored 10:00Z even though
        # its raw spelling sorts later.
        scanner.record_limit_snapshot(
            conn, [_snapshot_row("2026-08-22T10:00:00+00:00")])
        scanner.record_limit_snapshot(
            conn, [_snapshot_row("2026-08-22T11:00:00+02:00")])
        stored = conn.execute(
            "SELECT observed_at FROM usage_limits_snapshots").fetchone()[0]
        self.assertEqual(stored, "2026-08-22T11:00:00+02:00")

        # A later Z observation must not replace that earlier instant merely
        # because its hour has a lexically smaller spelling.
        before = conn.total_changes
        scanner.record_limit_snapshot(
            conn, [_snapshot_row("2026-08-22T09:30:00Z")])
        self.assertEqual(conn.total_changes, before)

    def test_a_later_observation_writes_nothing_at_all(self):
        """Not merely "keeps the earlier value" — writes no row.

        `record_limit_snapshot` runs on every scan against a cache that has
        usually not moved, so the upsert has to be a genuine no-op on a later
        instant rather than a rewrite that happens to store the same bytes.
        Counting `total_changes` is what tells those two apart.
        """
        conn = get_db(self.db_path)
        init_db(conn)
        self.addCleanup(conn.close)
        scanner.record_limit_snapshot(conn, [_snapshot_row("2026-08-05T03:06:31Z")])
        before = conn.total_changes
        scanner.record_limit_snapshot(conn, [_snapshot_row("2026-08-05T09:13:11Z")])
        self.assertEqual(conn.total_changes, before, "rewrote an unchanged row")

    def test_lowering_the_instant_moves_the_whole_row_with_it(self):
        """A MIN on `observed_at` alone would leave one observation's instant
        beside another observation's severity — a row that never existed.
        """
        conn = get_db(self.db_path)
        init_db(conn)
        self.addCleanup(conn.close)
        scanner.record_limit_snapshot(
            conn, [_snapshot_row("2026-08-05T09:13:11Z", severity="warning")])
        scanner.record_limit_snapshot(
            conn, [_snapshot_row("2026-08-05T03:06:31Z", severity="")])
        row = conn.execute(
            "SELECT observed_at, severity FROM usage_limits_snapshots").fetchone()
        self.assertEqual((row["observed_at"], row["severity"]),
                         ("2026-08-05T03:06:31Z", ""))

    def test_a_row_no_caller_can_build_now_raises_instead_of_vanishing(self):
        """The one behaviour the upsert gives up, pinned deliberately.

        `INSERT OR IGNORE` ignored EVERY constraint, not just the primary key,
        so a malformed row disappeared with no error and no row — the silent
        limit loss `route_limit_records` prints a warning to prevent. The upsert
        raises instead, which is better surfacing but turns a dropped row into a
        failed scan, so it is only acceptable while no caller can produce one:
        `codex_snapshot_rows` defaults every column out of validated fields
        (`observed_at` comes from `_bounded_text`, which returns "" for a
        missing or non-string timestamp), and `account.snapshot_rows` runs
        inside `scan()`'s own try/except.
        """
        conn = get_db(self.db_path)
        init_db(conn)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.IntegrityError):
            scanner.record_limit_snapshot(conn, [_snapshot_row(None)])
        self.assertEqual(scanner.codex_snapshot_rows(
            [{"kind": "codex", "percent": 62}])[0][9], "",
            "the Codex row builder must not be able to offer a NULL instant")


# A SIGKILL subprocess harness stood here: it monkeypatched
# `scanner._model_priority_sql` to kill its own process while the end-of-scan
# reconciliation SQL was being built, so the damage below was produced by the
# shipped code path rather than manufactured. Nothing ever ran it - it was
# assigned and referenced nowhere - so it is deleted rather than left as
# evidence that does not execute. Five module-level names went with it --
# `hashlib`, `signal`, `threading`, `SCHEMA_SQL`, `_processed_file_key` -- and
# all five were ALREADY unused, which is not what an earlier version of this
# comment said: it claimed `signal` and `threading` were imported for the
# harness alone. Measured on the pre-deletion blob, `threading` appears nowhere
# inside the constant at all, and `signal` appears only inside its
# triple-quoted child source, which carries its own `import signal`. Deleting
# the constant made nothing unused. If that state is ever worth reproducing
# through the product again, write it back as a test, not as a constant.


# ---------------------------------------------------------------------------
# There are no migrations. What replaced them.
# ---------------------------------------------------------------------------


def _stderr():
    """Capture what `init_db` announces. It writes to stderr, not to a logger."""
    return contextlib.redirect_stderr(io.StringIO())


class _RebuildFixture(_MigrationTempDb):
    """A temp database plus the two things every rebuild test needs to say."""

    def connect(self):
        conn = get_db(self.db_path)
        self.addCleanup(conn.close)
        return conn

    def current(self):
        """A database at the current schema, with one row in it to lose."""
        conn = get_db(self.db_path)
        try:
            init_db(conn)
            conn.execute(
                "INSERT INTO turns (session_id, timestamp, model, message_id, "
                "input_tokens) VALUES ('s1', '2026-08-01T10:00:00Z', "
                "'claude-opus-4-8', 'm1', 7)")
            conn.execute("INSERT INTO sessions (session_id, project_name) "
                         "VALUES ('s1', '~/proj')")
            conn.commit()
        finally:
            conn.close()

    def init(self):
        """Run `init_db` the way a caller does, returning what it announced."""
        conn = get_db(self.db_path)
        try:
            with _stderr() as out:
                init_db(conn, self.db_path)
        finally:
            conn.close()
        return out.getvalue()

    def turn_count(self):
        conn = get_db(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        finally:
            conn.close()

    def assertUsable(self):
        """The database ends at the current schema and takes a row.

        Both halves matter: a rebuild that recreated nothing would also report
        no mismatches (there would be no tables to disagree), so the check that
        the schema is *right* is the insert, not the empty list.
        """
        conn = get_db(self.db_path)
        try:
            self.assertEqual(schema_mismatches(conn), [])
            conn.execute(
                "INSERT INTO turns (session_id, timestamp, model, message_id, "
                "reasoning_effort, stop_reason, git_branch, source, "
                "input_tokens, cache_creation_1h_tokens, "
                "reasoning_output_tokens) "
                "VALUES ('s9', '2026-08-02T10:00:00Z', 'claude-opus-4-8', 'm9',"
                " 'high', 'end_turn', 'main', 'claude', 3, 1, 2)")
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT reasoning_effort FROM turns "
                             "WHERE message_id = 'm9'").fetchone()[0], "high")
        finally:
            conn.close()


class TestTheSchemaIsDeclaredOnceAndInFull(_RebuildFixture):
    """`SCHEMA_SQL` is the whole schema, and `EXPECTED_COLUMNS` is derived from it.

    The second half is the point. A hand-written expectation beside the schema
    it checks is two sources of truth, and the check would go on passing while
    the two drifted — the same defect class as parallel packaging declarations
    that are not checked against files on disk. `EXPECTED_COLUMNS` is built by running
    `SCHEMA_SQL` into `:memory:` at import, so it cannot disagree.
    """

    EXPECTED_TABLE_COLUMN_COUNTS = {
        # The fully-migrated shape of the owner's live database, counted before
        # the migrations were deleted. Written out on purpose: this is the one
        # place a hand-kept number is worth having, because it is what pins
        # `SCHEMA_SQL` to the databases that already exist rather than to
        # itself. Dropping a column from `SCHEMA_SQL` would keep every derived
        # check green and fail here.
        # `size` catches same-mtime appends; `prefix_hash` proves an incremental
        # resume still starts after the bytes that were previously ingested;
        # `st_dev`/`st_ino` prove the path still names the same file.
        "turns": 20, "sessions": 16, "processed_files": 7, "agents": 9,
        "limit_events": 9, "usage_limits_snapshots": 12,
    }
    EXPECTED_INDEXES = {
        "idx_turns_session", "idx_turns_timestamp", "idx_sessions_first",
        "idx_agents_type", "idx_turns_subagent", "idx_turns_agent_id",
        "idx_turns_message_id", "idx_limit_events_ts",
        "idx_usage_limits_observed", "idx_turns_source",
        "idx_turns_timestamp_order", "idx_sessions_first_order",
        "idx_sessions_last_order", "idx_limit_events_ts_order",
        "idx_usage_limits_observed_order",
    }

    def test_a_fresh_database_holds_exactly_these_tables(self):
        self.init()
        conn = self.connect()
        self.assertEqual(sorted(stored_tables(conn)),
                         sorted(self.EXPECTED_TABLE_COLUMN_COUNTS))

    def test_every_table_has_the_column_count_the_live_database_has(self):
        self.init()
        for table, count in self.EXPECTED_TABLE_COLUMN_COUNTS.items():
            with self.subTest(table=table):
                self.assertEqual(len(self.columns(table)), count)

    def test_every_index_is_created(self):
        self.init()
        conn = self.connect()
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(names, self.EXPECTED_INDEXES)

    def test_the_message_id_index_is_unique_and_partial(self):
        """The partial key deduplicates within, never across, a source."""
        self.init()
        conn = self.connect()
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_turns_message_id'"
        ).fetchone()[0]
        self.assertIn("UNIQUE", sql)
        self.assertIn("WHERE", sql)
        conn.execute("INSERT INTO turns (message_id, source) VALUES ('dup', 'claude')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO turns (message_id, source) VALUES ('dup', 'claude')")
        conn.execute("INSERT INTO turns (message_id, source) VALUES ('dup', 'codex')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO turns (message_id, source) VALUES ('dup', 'codex')")
        # The partial clause: two blanks and two NULLs must still coexist.
        conn.execute("INSERT INTO turns (message_id) VALUES ('')")
        conn.execute("INSERT INTO turns (message_id) VALUES ('')")
        conn.execute("INSERT INTO turns (session_id) VALUES ('no-id')")
        conn.execute("INSERT INTO turns (session_id) VALUES ('no-id')")

    def test_source_qualifies_session_and_agent_primary_keys(self):
        self.assertEqual(db.EXPECTED_PRIMARY_KEYS["sessions"],
                         ("source", "session_id"))
        self.assertEqual(db.EXPECTED_PRIMARY_KEYS["agents"],
                         ("source", "agent_id"))

    def test_the_expectation_is_derived_from_the_schema_not_written_beside_it(self):
        """Mutation guard. Feed the deriving function a schema missing a column
        and the expectation must lose that column too — if it does not, it is a
        second hand-kept copy and this whole file is checking nothing."""
        self.assertIn("git_branch", EXPECTED_COLUMNS["turns"])
        with mock.patch.object(
                db, "SCHEMA_SQL",
                "CREATE TABLE turns (id INTEGER PRIMARY KEY, session_id TEXT);"):
            derived = db._expected_columns()
        self.assertEqual(derived, {"turns": frozenset({"id", "session_id"})})

    def test_schema_meta_is_gone(self):
        """The table every deleted migration recorded itself in. A build that
        recreated it would silently re-admit the whole mechanism."""
        self.init()
        conn = self.connect()
        self.assertNotIn("schema_meta", stored_tables(conn))
        self.assertNotIn("schema_meta", EXPECTED_COLUMNS)

    def test_sessions_use_a_non_nullable_composite_source_key(self):
        self.init()
        conn = self.connect()
        info = list(conn.execute("PRAGMA table_info(sessions)"))
        by_name = {row[1]: row for row in info}
        self.assertEqual(by_name["session_id"][3], 1,
                         "SQLite rowid tables do not infer NOT NULL from a PK")
        self.assertEqual(by_name["source"][3], 1)
        self.assertEqual(
            [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]],
            ["source", "session_id"])

    def test_source_normalization_maps_legacy_blanks_and_canonicalizes_names(self):
        self.assertEqual(db.normalize_source(None), "claude")
        self.assertEqual(db.normalize_source(""), "claude")
        self.assertEqual(db.normalize_source("  CODEX "), "codex")

    def test_an_unreadable_or_malformed_rebuild_marker_fails_closed(self):
        class Result:
            def __init__(self, value):
                self.value = value

            def fetchone(self):
                return self.value

        class BrokenConnection:
            def execute(self, _sql):
                raise sqlite3.DatabaseError("marker read failed")

        class MalformedConnection:
            def execute(self, _sql):
                return Result(("not-an-integer",))

        with self.assertRaises(sqlite3.DatabaseError):
            db._rebuild_in_progress(BrokenConnection())
        with self.assertRaises(sqlite3.DatabaseError):
            db._rebuild_in_progress(MalformedConnection())


class TestAFreshDatabaseIsNotTreatedAsAMismatch(_RebuildFixture):
    """An empty file is a fresh install, not a foreign schema.

    Every expected table is "missing" from a database with no tables at all, so
    the naive check announces a rebuild on the very first run of the very first
    install. `init_db` skips the check when the file holds no tables.
    """

    def test_a_fresh_database_announces_nothing(self):
        self.assertEqual(self.init(), "")

    def test_a_fresh_database_ends_usable(self):
        self.init()
        self.assertUsable()

    def test_running_it_again_announces_nothing_and_keeps_the_data(self):
        self.current()
        self.assertEqual(self.init(), "")
        self.assertEqual(self.turn_count(), 1)


class TestAMismatchedSchemaIsRebuilt(_RebuildFixture):
    """Any disagreement at all — and the database ends usable.

    Four shapes, because they fail the check in four different branches: a
    column this build needs and the file lacks; a column the file has and this
    build does not know; a whole table missing; and a file that is a database of
    something else entirely.
    """

    def _damage(self, *statements):
        self.current()
        conn = get_db(self.db_path)
        try:
            for statement in statements:
                conn.execute(statement)
            conn.commit()
        finally:
            conn.close()

    def _assert_rebuilt(self, expected_reason):
        announced = self.init()
        self.assertIn("written by a different version", announced)
        self.assertIn(expected_reason, announced)
        self.assertIn("cache", announced)
        self.assertEqual(self.turn_count(), 0,
                         "a rebuild that kept rows is a migration")
        self.assertUsable()

    def test_a_missing_column_rebuilds(self):
        # `ALTER TABLE ... DROP COLUMN` is how a previous release's file differs
        # from this one: it simply never had the column.
        self._damage("ALTER TABLE turns DROP COLUMN git_branch")
        self._assert_rebuilt("`turns.git_branch` is missing")

    def test_an_old_processed_cursor_is_rebuilt_then_repopulated(self):
        """The resume proof is schema, not a nullable best-effort cache.

        A row carrying only ``mtime`` and ``lines`` cannot prove that the bytes
        before its cursor are still the bytes previously ingested. The normal
        derived-cache rebuild must discard that row, and the next scan must
        replace it with the complete cursor shape, including the file identity
        needed to detect an atomic same-metadata replacement.
        """
        self.current()
        conn = get_db(self.db_path)
        try:
            conn.execute(
                "INSERT INTO processed_files (path, mtime, lines) "
                "VALUES ('old-cursor', 1.0, 7)")
            conn.execute("ALTER TABLE processed_files DROP COLUMN prefix_hash")
            conn.execute("ALTER TABLE processed_files DROP COLUMN size")
            conn.execute("ALTER TABLE processed_files DROP COLUMN st_dev")
            conn.execute("ALTER TABLE processed_files DROP COLUMN st_ino")
            conn.commit()
        finally:
            conn.close()

        announced = self.init()
        self.assertIn("written by a different version", announced)
        self.assertIn("`processed_files.size` is missing", announced)
        self.assertIn("`processed_files.st_dev` is missing", announced)
        self.assertIn("`processed_files.st_ino` is missing", announced)
        self.assertEqual(
            self.columns("processed_files"),
            {"path", "mtime", "lines", "size", "prefix_hash",
             "st_dev", "st_ino"})
        conn = self.connect()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM processed_files").fetchone()[0],
            0)

        projects = Path(self.tmpdir) / "projects"
        transcript = projects / "proj" / "session.jsonl"
        _transcript(transcript, "s-new", ["m-new"])
        with contextlib.redirect_stdout(io.StringIO()):
            result = scanner.scan(projects_dir=projects, db_path=self.db_path,
                                  verbose=False)
        self.assertEqual(result["new"], 1)
        row = conn.execute(
            "SELECT lines, size, prefix_hash, st_dev, st_ino "
            "FROM processed_files").fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], transcript.stat().st_size)
        self.assertRegex(row[2], r"^[0-9a-f]{64}$")
        self.assertEqual(row[3], str(transcript.stat().st_dev))
        self.assertEqual(row[4], str(transcript.stat().st_ino))

    def test_processed_identity_round_trips_unsigned_64_bit_values(self):
        """Device/inode values above SQLite INTEGER's signed limit stay exact."""
        self.current()
        value = 2**63 + 17
        conn = self.connect()
        try:
            columns = {
                row[1]: row[2]
                for row in conn.execute(
                    "PRAGMA table_info(\"processed_files\")")
            }
            self.assertEqual(columns["st_dev"], "TEXT")
            self.assertEqual(columns["st_ino"], "TEXT")
            conn.execute(
                "INSERT INTO processed_files "
                "(path, st_dev, st_ino) VALUES (?, ?, ?)",
                ("large-identity", db._processed_identity(value),
                 db._processed_identity(value + 1)))
            conn.commit()
            row = conn.execute(
                "SELECT st_dev, st_ino FROM processed_files "
                "WHERE path = ?", ("large-identity",)).fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(row), (str(value), str(value + 1)))
        self.assertEqual(db._processed_identity(row[0]), str(value))
        self.assertEqual(db._processed_identity(row[1]), str(value + 1))

    def test_old_integer_identity_columns_rebuild_before_they_can_round(self):
        """The prior INTEGER shape must not receive a large decimal string."""
        self.current()
        conn = self.connect()
        conn.execute("ALTER TABLE processed_files RENAME TO processed_files_old")
        conn.execute("""
            CREATE TABLE processed_files (
                path        TEXT PRIMARY KEY,
                mtime       REAL,
                lines       INTEGER,
                size        INTEGER,
                prefix_hash TEXT,
                st_dev      INTEGER,
                st_ino      INTEGER
            )
        """)
        conn.execute("DROP TABLE processed_files_old")
        conn.commit()

        reasons = schema_mismatches(conn)
        self.assertIn("`processed_files.st_dev` has type INTEGER; expected TEXT",
                      reasons)
        self.assertIn("`processed_files.st_ino` has type INTEGER; expected TEXT",
                      reasons)
        conn.close()

        announced = self.init()
        self.assertIn("`processed_files.st_dev` has type INTEGER; expected TEXT",
                      announced)
        self.assertEqual(self.columns("processed_files"),
                         {"path", "mtime", "lines", "size", "prefix_hash",
                          "st_dev", "st_ino"})

    def test_an_extra_column_rebuilds(self):
        """A FUTURE release's database, opened by this build. Downgrade is the
        realistic route: a user rolls back a version, or the VS Code extension's
        bundled copy is older than the one they ran from the terminal."""
        self._damage("ALTER TABLE turns ADD COLUMN turns_next_thing TEXT")
        self._assert_rebuilt("`turns.turns_next_thing` is not part of this schema")

    def test_a_missing_table_rebuilds(self):
        self._damage("DROP TABLE usage_limits_snapshots")
        self._assert_rebuilt("table `usage_limits_snapshots` is missing")

    def test_a_leftover_table_rebuilds(self):
        """`schema_meta` itself is the realistic instance: every database this
        build inherits from the migration era still carries it."""
        self._damage("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        self._assert_rebuilt("`schema_meta` is not part of this schema")

    def test_our_database_carrying_foreign_objects_rebuilds(self):
        """A stale `usage.db` with somebody's leftover table, view and index in
        it. Ours — `turns` and the rest are present — so the rebuild is licensed
        and everything foreign goes with it.

        This case used to be written WITHOUT our own tables, i.e. as a database
        that is not ours at all, and it asserted that we drop it. That is the
        defect `tests/test_foreign_database.py` now covers from the other side:
        a read command destroyed an unrelated SQLite file and reported "no usage
        history is lost". Ownership licenses the rebuild; a mismatch alone does
        not. What the two tests share is this one's real content — the foreign
        OBJECTS still have to go once we have established the file is ours."""
        self._damage(
            "CREATE TABLE contacts (id INTEGER PRIMARY KEY, name TEXT)",
            "CREATE INDEX idx_contacts_name ON contacts(name)",
            "CREATE VIEW loud AS SELECT upper(name) FROM contacts",
            "INSERT INTO contacts (name) VALUES ('someone')")
        conn = get_db(self.db_path)
        try:
            self.assertIn("`contacts` is not part of this schema",
                          schema_mismatches(conn))
            self.assertIn("`loud` is not part of this schema",
                          schema_mismatches(conn))
        finally:
            conn.close()
        self.assertIn("written by a different version", self.init())
        self.assertUsable()
        conn = self.connect()
        self.assertEqual(sorted(stored_tables(conn)), sorted(EXPECTED_COLUMNS))
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name IN "
            "('contacts', 'loud', 'idx_contacts_name')").fetchone(),
            "the foreign table, its view and its index all had to go")

    def test_quoted_foreign_object_names_rebuild_once_and_converge(self):
        """SQLite identifiers may contain quotes; schema metadata is data.

        Rebuild must quote every observed name as an identifier. Interpolating
        it between one pair of quote characters either raises halfway through
        the rebuild or names a different object, leaving every later open to
        rebuild and discard the freshly scanned cache again.
        """
        self._damage(
            'CREATE TABLE "extra"" table" (value TEXT)',
            'CREATE VIEW "extra"" view" AS SELECT session_id FROM turns',
            'CREATE TRIGGER "extra"" trigger" AFTER INSERT ON turns '
            'BEGIN SELECT 1; END',
        )

        announced = self.init()
        self.assertIn("written by a different version", announced)
        conn = self.connect()
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'extra%'")}
        self.assertEqual(names, set())
        self.assertEqual(self.init(), "", "the rebuild did not converge")
        self.assertUsable()

    def test_a_file_holding_none_of_our_tables_is_refused_not_rebuilt(self):
        """The other half, asserted here too so this class cannot drift back.

        `db.ForeignDatabaseError` rather than a rebuild, and the rows survive.
        The full treatment — the message, the CLI's exit, the anti-vacuity
        cases — is in `tests/test_foreign_database.py`."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE contacts (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("INSERT INTO contacts (name) VALUES ('someone')")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(db.ForeignDatabaseError):
            get_db(self.db_path)
        check = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                check.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 1)
        finally:
            check.close()

    def test_the_notice_names_both_losses_not_one(self):
        """The remedy paragraph, asserted -- it was not, until 2026-08-16.

        Until then the only thing any test looked at in that paragraph was
        `assertIn("cache", announced)`, which the wording it replaced also
        satisfies. Measured 2026-08-16 by restoring the pre-`a3bdbf5` sentence
        verbatim ("...so no usage history is lost except for sessions whose
        transcript has since been deleted..."): the full suite stayed green, so
        the product change that whole commit exists for could re-land unnoticed.

        Two losses, and only one of them is recoverable by scanning again. A
        session whose transcript has been pruned is gone; so is the Claude half
        of `usage_limits_snapshots`, which is sampled from `~/.claude.json` -- a
        cache Claude Code overwrites in place, so a refilling scan re-records
        only today's reading and every earlier window is gone for good. Codex's
        half is on the transcripts and does come back, which is why the notice
        names Claude specifically.

        The `assertNotIn` is a landmine for that one sentence rather than a rule
        about phrasing: an exception clause that reads as exhaustive is what
        made the old wording wrong, and it is the shape most likely to come back
        under a reflow.
        """
        self._damage("DROP TABLE usage_limits_snapshots")
        announced = self.init()
        self.assertIn("transcript has since been deleted", announced,
                      "the recoverable loss is still a loss and must be named")
        self.assertIn("plan-usage history", announced,
                      "the loss no rescan can undo was named by nothing")
        self.assertIn("Codex", announced,
                      "which assistant's quota history survives is the point")
        self.assertNotIn("no usage history is lost", announced)

    def test_the_notice_names_the_file_and_bounds_the_list(self):
        """A user reading it has to be able to find the database, and a foreign
        schema can produce dozens of reasons — the notice must not become a
        wall."""
        self.current()
        conn = get_db(self.db_path)
        try:
            for n in range(20):
                conn.execute(f"ALTER TABLE turns ADD COLUMN extra_{n} TEXT")
            conn.commit()
        finally:
            conn.close()
        announced = self.init()
        self.assertIn(str(self.db_path), announced)
        self.assertIn("and 15 more", announced)
        self.assertEqual(announced.count("\n  - "), 6)

    def test_the_notice_escapes_controls_in_paths_and_schema_names(self):
        """A rebuild diagnostic must not let metadata rewrite the terminal."""
        hostile_path = Path("usage\x1b[2J\nspoofed.db")
        hostile_reason = "`table\x1b]8;;https://example.invalid\x07\nname` is extra"
        with _stderr() as out:
            db._announce_rebuild([hostile_reason], hostile_path)
        announced = out.getvalue()
        self.assertNotIn("\x1b", announced)
        self.assertNotIn("\x07", announced)
        self.assertIn(r"\x1b", announced)
        self.assertIn(r"\x07", announced)
        self.assertIn(r"\x0a", announced)
        self.assertNotIn("\nspoofed.db", announced)
        self.assertNotIn("\nname`", announced)

    @unittest.skipUnless(os.name == "posix", "lock-link semantics are POSIX")
    def test_rebuild_refuses_a_symlinked_lock_file(self):
        self.current()
        conn = get_db(self.db_path)
        try:
            conn.execute("ALTER TABLE turns ADD COLUMN stale TEXT")
            conn.commit()
        finally:
            conn.close()
        victim = Path(self.tmpdir) / "lock-victim"
        victim.write_bytes(b"victim")
        Path(str(self.db_path) + ".rebuild.lock").symlink_to(victim)
        with self.assertRaisesRegex(RuntimeError, "unsafe rebuild lock"):
            self.init()

    @unittest.skipUnless(os.name == "posix", "lock-link semantics are POSIX")
    def test_rebuild_refuses_a_hard_linked_lock_file(self):
        self.current()
        conn = get_db(self.db_path)
        try:
            conn.execute("ALTER TABLE turns ADD COLUMN stale TEXT")
            conn.commit()
        finally:
            conn.close()
        victim = Path(self.tmpdir) / "lock-victim"
        victim.write_bytes(b"victim")
        os.link(victim, Path(str(self.db_path) + ".rebuild.lock"))
        with self.assertRaisesRegex(RuntimeError, "unsafe rebuild lock"):
            self.init()

    @unittest.skipUnless(os.name == "posix", "POSIX lock ownership only")
    def test_rebuild_refuses_a_foreign_owned_lock_file(self):
        lock_path = Path(str(self.db_path) + ".rebuild.lock")
        lock_path.write_bytes(b"0")
        fd = os.open(lock_path, os.O_RDWR)
        self.addCleanup(os.close, fd)
        info = lock_path.stat()
        foreign = mock.Mock(
            st_mode=info.st_mode,
            st_nlink=info.st_nlink,
            st_uid=os.getuid() + 1,
            st_dev=info.st_dev,
            st_ino=info.st_ino,
        )
        with mock.patch.object(db.os, "fstat", return_value=foreign):
            with self.assertRaisesRegex(RuntimeError, "foreign-owned rebuild lock"):
                db._validate_rebuild_lock_fd(fd, lock_path)

    @unittest.skipUnless(os.name == "posix", "POSIX lock permissions only")
    def test_rebuild_refuses_an_unprivate_lock_when_chmod_fails(self):
        lock_path = Path(str(self.db_path) + ".rebuild.lock")
        lock_path.write_bytes(b"0")
        os.chmod(lock_path, 0o644)
        fd = os.open(lock_path, os.O_RDWR)
        self.addCleanup(os.close, fd)
        with mock.patch.object(db.os, "fchmod",
                               side_effect=PermissionError("read-only")):
            with self.assertRaisesRegex(RuntimeError, "non-private rebuild lock"):
                db._validate_rebuild_lock_fd(fd, lock_path)


class TestARebuildDoesNotTouchAMatchingDatabase(_RebuildFixture):
    """The expensive, destructive branch must not fire on the ordinary open.

    Asserted two ways, because either alone is weak: the rows survive, and the
    file is byte-identical. The second is what catches a rebuild that dropped
    and recreated everything on a database that happened to be empty.
    """

    def test_the_rows_survive(self):
        self.current()
        self.init()
        self.init()
        self.assertEqual(self.turn_count(), 1)

    def test_the_file_is_unchanged(self):
        self.current()
        self.init()          # settle any WAL the first open produced
        before = self.db_path.read_bytes()
        self.assertEqual(self.init(), "")
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_a_missing_index_is_recreated_rather_than_rebuilt(self):
        """Deliberately NOT grounds to throw the data away. `CREATE INDEX IF NOT
        EXISTS` repairs it for free, and an index is not evidence that the rows
        were written by a different build — which is the only thing a rebuild is
        an answer to."""
        self.current()
        conn = get_db(self.db_path)
        try:
            conn.execute("DROP INDEX idx_turns_timestamp")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.init(), "")
        self.assertEqual(self.turn_count(), 1)
        conn = self.connect()
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'idx_turns_timestamp'"
        ).fetchone())


class TestTheRebuildRechecksUnderTheWriteLock(_RebuildFixture):
    """`init_db` is called concurrently by design, so two openers can both read
    a foreign schema. The loser must not drop the tables the winner has just
    rebuilt and the winner's scan may already be filling.
    """

    def test_a_stale_decision_drops_nothing(self):
        """Exactly the losing thread's state: it decided to rebuild from a read
        taken before the winner committed, and by the time it holds the write
        lock the schema is already correct."""
        self.current()
        conn = self.connect()
        with _stderr() as out:
            dropped = rebuild_database(conn, ["`turns.stale` is missing"],
                                       self.db_path)
        self.assertEqual(dropped, [])
        self.assertEqual(out.getvalue(), "",
                         "a rebuild that did nothing must not announce one")
        self.assertEqual(self.turn_count(), 1)

    def test_nothing_left_to_drop_drops_nothing(self):
        """The winner's OTHER shape, and it was covered by nothing until
        2026-08-16: the lock is granted after the winner's DROPs committed and
        before it recreated anything, so the schema still disagrees while there
        is no longer anything here to drop.

        Reachable with no threads at all -- a database with no tables is that
        state exactly -- which is what makes it testable. `init_db` exempts an
        empty file from the check for the same reason; that exemption was not
        carried through to `rebuild_database`, and the cost of the branch being
        absent was one real rebuild printing the product's loudest data-loss
        notice TWICE, the second time quoting the caller's now-stale reason
        list about a file that no longer contains anything it names.

        Measured 2026-08-16: forcing `if not stored_tables(conn)` false leaves
        `dropped` at `[]` either way, so the announcement is the whole of the
        observable difference and both assertions below are needed -- the empty
        list alone passes under the mutation.
        """
        conn = self.connect()
        stale = ["table `turns` is missing"]
        self.assertNotEqual(schema_mismatches(conn), [],
                            "the fixture has to reach the branch under test")
        with _stderr() as out:
            dropped = rebuild_database(conn, stale, self.db_path)
        self.assertEqual(dropped, [])
        self.assertEqual(out.getvalue(), "",
                         "someone else got here first; there is nothing to say")

    def test_a_real_mismatch_still_drops(self):
        """The guard above must not be what makes every rebuild a no-op."""
        self.current()
        conn = get_db(self.db_path)
        self.addCleanup(conn.close)
        conn.execute("ALTER TABLE turns ADD COLUMN stale TEXT")
        conn.commit()
        with _stderr():
            dropped = rebuild_database(conn, schema_mismatches(conn), self.db_path)
        self.assertEqual(sorted(dropped), sorted(EXPECTED_COLUMNS))

    def test_a_failure_mid_rebuild_leaves_the_old_database(self):
        """All-or-nothing. A half-dropped database is worse than a foreign one:
        the tables that survived would then be called a mismatch forever, and
        every later open would try again.

        **Assert the tables the fixture actually drops, and assert them twice.**
        Until 2026-08-15 the only assertion here was a row count on `turns`,
        which `stored_tables`' `ORDER BY name` puts fifth while the fixture
        raises on the SECOND drop -- so the one table it ever destroys,
        `agents`, was never looked at, and it was looked at through the very
        connection that had not committed. Measured that day, both mechanisms
        that provide the property could be deleted with the full suite green:
        removing the `BEGIN IMMEDIATE` (the drops then commit one by one, so the
        loss is permanent on disk) and removing the `conn.rollback()` from the
        `except BaseException` (the drops stay invisible to everyone else but
        the caller's own connection is handed back a half-dropped schema inside
        an open write transaction).

        The same-connection read below catches BOTH named mutations; measured
        2026-08-15 by blanking each read in turn. The on-disk read is kept for a
        different property — that nothing reached the FILE — and not because
        either mutation needs it. It can never be the unique catcher: what a
        second connection can see is a strict subset of what the caller's own
        connection sees. An earlier version of this docstring claimed each read
        caught one mutation, which would have made the on-disk block look
        deletable to anyone who re-measured it.
        """
        self.current()
        conn = get_db(self.db_path)
        self.addCleanup(conn.close)
        conn.execute("ALTER TABLE turns ADD COLUMN stale TEXT")
        conn.commit()
        seen = []

        class _TheDiskDiesPartWayThrough:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def execute(self, sql, *args):
                if sql.startswith("DROP "):
                    seen.append(sql)
                    if len(seen) == 2:
                        raise sqlite3.OperationalError("disk I/O error")
                return self._real.execute(sql, *args)

        with _stderr():
            with self.assertRaises(sqlite3.OperationalError):
                rebuild_database(_TheDiskDiesPartWayThrough(conn),
                                 schema_mismatches(conn), self.db_path)
        self.assertGreaterEqual(len(seen), 2, "the fixture dropped nothing")
        # The caller's own connection: this is what a missing rollback leaves
        # behind, and it is the connection `init_db` hands straight back.
        self.assertEqual(sorted(stored_tables(conn)), sorted(EXPECTED_COLUMNS))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 1)
        # And on disk, through a connection that shared none of that state:
        # this is what a missing `BEGIN IMMEDIATE` makes permanent. Ordered
        # after the read above on purpose -- a connection left holding the write
        # lock is exactly the state the read above fails in, so the failure
        # arrives before anything opens a second connection against it.
        conn.close()
        fresh = get_db(self.db_path)
        self.addCleanup(fresh.close)
        self.assertEqual(sorted(stored_tables(fresh)), sorted(EXPECTED_COLUMNS))
        self.assertEqual(
            fresh.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 1)

    def test_the_drops_run_under_the_write_lock(self):
        """The class's own name, asserted -- it was not, until 2026-08-15.

        `rebuild_database` re-reads the schema and drops under one write lock so
        that a second opener cannot land between the two. All three tests here
        passed with no lock taken at all, which makes the re-check a formality:
        the losing opener would re-read a schema the winner is still in the
        middle of changing.

        Probed from inside the drop loop, because that is the only moment the
        claim is about. The second connection asks for the write lock with no
        patience at all (`timeout=0`) rather than waiting out
        `BUSY_TIMEOUT_MS`, so a mutation that drops the lock fails this in
        milliseconds instead of hanging the suite for thirty seconds.
        """
        self.current()
        conn = get_db(self.db_path)
        self.addCleanup(conn.close)
        conn.execute("ALTER TABLE turns ADD COLUMN stale TEXT")
        conn.commit()
        contended = []

        class _AnotherOpenerTriesToWriteMidDrop:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def execute(inner, sql, *args):
                result = inner._real.execute(sql, *args)
                if sql.startswith("DROP ") and not contended:
                    rival = sqlite3.connect(self.db_path, timeout=0)
                    try:
                        rival.execute("BEGIN IMMEDIATE")
                        contended.append("granted")
                        rival.rollback()
                    except sqlite3.OperationalError as exc:
                        contended.append(str(exc))
                    finally:
                        rival.close()
                return result

        with _stderr():
            rebuild_database(_AnotherOpenerTriesToWriteMidDrop(conn),
                             schema_mismatches(conn), self.db_path)
        self.assertEqual(len(contended), 1, "the fixture never probed")
        self.assertIn("locked", contended[0],
                      "another opener took the write lock mid-rebuild")


class TestTheRebuildDoesNotLeaveThePlaintextInTheFile(_RebuildFixture):
    """Dropping a table detaches rows; it does not zero the pages they were on.

    Rebuilding discards transcript-derived tables. secure_delete, VACUUM and
    WAL checkpointing prevent their removed plaintext from surviving in pages.

    WHAT THIS TEST DOES AND DOES NOT PIN, measured by mutation on 2026-08-15
    rather than assumed, because the honest answer is not the obvious one.
    The three mechanisms are individually REDUNDANT on this SQLite build and
    `test_the_dropped_labels_are_not_recoverable_from_the_file` goes GREEN if
    you delete any ONE of them: with `secure_delete` forced OFF the VACUUM still
    rebuilds the file, with the VACUUM removed `secure_delete` still zeroes the
    pages the DROPs free, and the checkpoint is unobservable to it. Remove
    `secure_delete` AND the VACUUM together and it goes RED, so the pair is
    load-bearing jointly.

    The reason given here for the third one was wrong, which is worth keeping
    because it is the kind of reason that sounds sufficient: "the fixture's
    writes are already in `usage.db` when the rebuild starts". They are, and
    that is not what hides the checkpoint. What hides it is that the test opens
    ONE connection and closes it, and closing the last connection to a
    write-ahead-logged database checkpoints it anyway.
    `test_a_second_open_connection_does_not_leave_the_plaintext_behind` below
    removes exactly that, and reds on the checkpoint alone (2026-08-16), so the
    third mechanism is pinned after all — by a different fixture, not by this
    one.

    Do not read that as licence to delete one. They cover different inputs:
    `secure_delete` zeroes only the pages THIS rebuild frees, while the VACUUM
    also reclaims pages a PREVIOUS build already orphaned, even when nothing
    the current run deletes contains the stale plaintext.
    A fixture that reproduced that would need a database carrying orphaned pages
    before the test starts, and is not built here; that is the named gap.
    """

    NEEDLE = "zzsecretprojectname"

    def test_the_dropped_labels_are_not_recoverable_from_the_file(self):
        conn = get_db(self.db_path)
        try:
            init_db(conn)
            conn.executemany(
                "INSERT INTO sessions (session_id, project_name) VALUES (?, ?)",
                [(f"s{n}", f"{self.NEEDLE}{n}/proj") for n in range(400)])
            conn.execute("ALTER TABLE turns ADD COLUMN stale TEXT")
            conn.commit()
        finally:
            conn.close()
        self.assertIn(self.NEEDLE.encode(), self.db_path.read_bytes(),
                      "the fixture never wrote the plaintext")
        with _stderr():
            self.init()
        for suffix in ("", "-wal"):
            path = Path(str(self.db_path) + suffix)
            if path.exists():
                self.assertNotIn(self.NEEDLE.encode(), path.read_bytes(),
                                 f"plaintext survived in {path.name}")

    def test_a_second_open_connection_does_not_leave_the_plaintext_behind(self):
        """The third mechanism, made observable — this is the checkpoint's test.

        The class docstring records that the checkpoint is invisible to the test
        above, and it is: `init()` closes the only connection, and closing the
        LAST connection to a write-ahead-logged database checkpoints it anyway.
        So the assertion passes on a build with no `_checkpoint_wal` at all,
        measured by mutation 2026-08-16 (`pass` in its place: that test OK, full
        suite green).

        One more open connection is the whole difference, and it is not a
        contrived one: `cli.py dashboard` and the extension's server each hold
        one for the session, and `dashboard_data` opens another per request. Now
        nothing else can checkpoint, and what the rebuild leaves is decided by
        `_checkpoint_wal` alone. Measured the same day on the mutated build:
        `usage.db` came back 135,168 bytes WITH the needle in it — the VACUUM's
        result sitting unread in the sidecar while the main file kept the old
        pages verbatim — where the shipped build leaves 4,096 clean bytes.

        Note which file the plaintext survives in: the MAIN one, not the
        sidecar. `db._checkpoint_wal`'s docstring used to claim the test above
        pinned it and that it checked `usage.db-wal` "precisely because this
        call is the only thing that empties it"; both halves were wrong, and
        AGENTS.md said so correctly while the docstring said otherwise.
        """
        conn = get_db(self.db_path)
        try:
            init_db(conn)
            conn.executemany(
                "INSERT INTO sessions (session_id, project_name) VALUES (?, ?)",
                [(f"s{n}", f"{self.NEEDLE}{n}/proj") for n in range(400)])
            conn.execute("ALTER TABLE turns ADD COLUMN stale TEXT")
            conn.commit()
        finally:
            conn.close()
        self.assertIn(self.NEEDLE.encode(), self.db_path.read_bytes(),
                      "the fixture never wrote the plaintext")
        keeper = get_db(self.db_path)
        self.addCleanup(keeper.close)
        # A finished read, so the keeper holds no lock the checkpoint would
        # have to wait out — it is here only to stop the close below being the
        # last one.
        keeper.execute("SELECT COUNT(*) FROM sqlite_master").fetchall()
        with _stderr():
            self.init()
        for suffix in ("", "-wal"):
            path = Path(str(self.db_path) + suffix)
            if path.exists():
                self.assertNotIn(self.NEEDLE.encode(), path.read_bytes(),
                                 f"plaintext survived in {path.name}")

    def test_the_connection_gets_its_secure_delete_setting_back(self):
        """The envelope's other half: what the caller's connection is handed back.

        `rebuild_database` forces `secure_delete` ON for the drops and puts it
        back in an inner `finally`. Nothing asserted that restore until
        2026-08-15: measured that day, forcing the restore clause false left the
        full suite green while every connection `init_db` returns kept
        `secure_delete` ON for the rest of its life -- so a whole `scanner.scan`
        on that connection pays a freed-page overwrite it never asked for.

        Three states rather than one, because the read side is a tri-state
        (0 / 1 / 2) while the write side is a boolean plus the keyword `FAST`:
        restoring 2 by writing `= 2` lands on 1. That is the whole reason
        `db._secure_delete_literal` exists, and it is the second thing this
        pins -- it had no test of its own either.
        """
        for setting, expected in (("OFF", 0), ("ON", 1), ("FAST", 2)):
            with self.subTest(secure_delete=setting):
                self.current()
                conn = get_db(self.db_path)
                self.addCleanup(conn.close)
                conn.execute(f"PRAGMA secure_delete = {setting}")
                conn.execute("ALTER TABLE turns ADD COLUMN stale TEXT")
                conn.commit()
                self.assertEqual(
                    conn.execute("PRAGMA secure_delete").fetchone()[0], expected,
                    "this SQLite build does not hold the state the test sets")
                with _stderr():
                    dropped = rebuild_database(conn, schema_mismatches(conn),
                                               self.db_path)
                self.assertTrue(dropped, "the fixture dropped nothing")
                self.assertEqual(
                    conn.execute("PRAGMA secure_delete").fetchone()[0], expected)
                conn.close()


class TestInterruptedRebuildFailsClosedAroundPinnedReaders(_RebuildFixture):
    """A killed rebuild leaves a marker, never a usable plaintext snapshot."""

    NEEDLE = "held-reader-secret-project"

    def _plant_mismatch(self):
        conn = get_db(self.db_path)
        try:
            init_db(conn)
            conn.executemany(
                "INSERT INTO sessions (session_id, project_name) VALUES (?, ?)",
                [(f"s{n}", f"{self.NEEDLE}-{n}") for n in range(400)])
            conn.execute("ALTER TABLE turns ADD COLUMN stale TEXT")
            conn.commit()
        finally:
            conn.close()

    def test_killed_cleanup_stays_marked_until_a_wal_reader_releases(self):
        """The former drop/commit/VACUUM gap is exercised through abrupt death.

        The keeper is an actual WAL reader, not merely an open connection. It
        pins the old main pages while the child commits the marked replacement
        and dies exactly when checkpointing would have made those pages
        unreachable. A new opener must fail closed while the reader remains,
        then finish the same in-place cleanup after release.
        """
        self._plant_mismatch()
        keeper = get_db(self.db_path)
        try:
            keeper.execute("BEGIN")
            keeper.execute("SELECT project_name FROM sessions").fetchone()
            script = Path(self.tmpdir) / "kill_rebuild.py"
            script.write_text(_KILL_DURING_REBUILD_CLEANUP, encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            child = subprocess.run(
                [sys.executable, str(script), str(self.db_path),
                 str(Path(__file__).resolve().parents[1])],
                env=env, capture_output=True, text=True, encoding="utf-8",
                timeout=30)
            self.assertEqual(child.returncode, 97,
                             f"child did not exit at checkpoint: {child.stderr}")

            marker = sqlite3.connect(self.db_path, timeout=0)
            try:
                self.assertEqual(
                    marker.execute("PRAGMA user_version").fetchone()[0],
                    db.REBUILD_IN_PROGRESS)
            finally:
                marker.close()

            started = time.monotonic()
            with mock.patch.object(db, "BUSY_TIMEOUT_MS", 100):
                with self.assertRaises(sqlite3.OperationalError):
                    get_db(self.db_path)
            self.assertLess(time.monotonic() - started, 3,
                            "a pinned reader must produce a bounded refusal")

            keeper.rollback()
            keeper.close()
            keeper = sqlite3.connect(self.db_path, timeout=0,
                                     check_same_thread=False)
            keeper.execute("BEGIN")
            keeper.execute("SELECT project_name FROM sessions").fetchone()

            def release_reader():
                time.sleep(0.2)
                keeper.rollback()

            releaser = threading.Thread(target=release_reader)
            releaser.start()
            started = time.monotonic()
            with mock.patch.object(db, "BUSY_TIMEOUT_MS", 2000):
                recovered = get_db(self.db_path)
            releaser.join(timeout=3)
            self.assertFalse(releaser.is_alive(),
                             "the delayed reader did not release")
            self.assertGreater(time.monotonic() - started, 0.1,
                               "recovery did not wait for the reader")
            recovered.close()
        finally:
            keeper.close()

        recovered = get_db(self.db_path)
        try:
            self.assertEqual(db._rebuild_in_progress(recovered), False)
            self.assertEqual(schema_mismatches(recovered), [])
            for suffix in ("", "-wal"):
                path = Path(str(self.db_path) + suffix)
                if path.exists():
                    self.assertNotIn(self.NEEDLE.encode(), path.read_bytes(),
                                     f"plaintext survived in {path.name}")
        finally:
            recovered.close()

    def test_concurrent_opener_waits_for_marked_rebuild_cleanup(self):
        self._plant_mismatch()
        entered = threading.Event()
        release = threading.Event()
        results = {}
        real_cleanup = db._cleanup_rebuild

        def pause_cleanup(conn):
            entered.set()
            self.assertTrue(release.wait(5), "cleanup was not released")
            return real_cleanup(conn)

        def rebuild_worker():
            conn = get_db(self.db_path)
            try:
                with mock.patch.object(db, "_cleanup_rebuild", pause_cleanup):
                    with contextlib.redirect_stderr(io.StringIO()):
                        init_db(conn, self.db_path)
                results["rebuild"] = "done"
            except BaseException as exc:  # surfaced in the main assertion
                results["rebuild"] = exc
            finally:
                conn.close()

        def opener_worker():
            try:
                conn = get_db(self.db_path)
                results["opener"] = (
                    db._rebuild_in_progress(conn), schema_mismatches(conn))
                conn.close()
            except BaseException as exc:
                results["opener"] = exc

        rebuild = threading.Thread(target=rebuild_worker)
        rebuild.start()
        self.assertTrue(entered.wait(5), "rebuild never reached cleanup")
        opener = threading.Thread(target=opener_worker)
        opener.start()
        time.sleep(0.1)
        self.assertNotIn("opener", results,
                         "the opener used the marked database mid-cleanup")
        release.set()
        rebuild.join(timeout=10)
        opener.join(timeout=10)
        self.assertFalse(rebuild.is_alive())
        self.assertFalse(opener.is_alive())
        self.assertEqual(results.get("rebuild"), "done")
        self.assertEqual(results.get("opener"), (False, []))

    def test_preexisting_opener_serializes_marker_check_and_schema_admission(self):
        """No rebuild may commit after an opener reads "unmarked".

        The existing concurrent-opener test starts ``get_db`` after the marker
        exists. This is the opposite ordering: both SQLite connections already
        exist, opener A pauses immediately after its marker check, and opener B
        tries to rebuild. Without one rebuild-lock admission around A's marker
        and schema decisions, B reaches cleanup with the durable marker
        committed while A is still licensed by its stale answer.
        """
        self._plant_mismatch()
        opener = sqlite3.connect(
            self.db_path, timeout=5, check_same_thread=False)
        rebuilder = sqlite3.connect(
            self.db_path, timeout=5, check_same_thread=False)
        opener.row_factory = sqlite3.Row
        rebuilder.row_factory = sqlite3.Row
        self.addCleanup(opener.close)
        self.addCleanup(rebuilder.close)

        opener_checked = threading.Event()
        release_opener = threading.Event()
        rebuilder_attempted = threading.Event()
        rebuilder_marked = threading.Event()
        release_rebuilder = threading.Event()
        admission_gate = threading.Lock()
        admission_owner = {"thread": None}
        results = {}
        real_marker = db._rebuild_in_progress
        real_cleanup = db._cleanup_rebuild

        def pause_after_opener_check(conn):
            marked = real_marker(conn)
            if conn is opener:
                opener_checked.set()
                self.assertTrue(
                    release_opener.wait(5), "opener marker check was not released")
            return marked

        def pause_rebuilder_cleanup(conn):
            if conn is rebuilder:
                rebuilder_marked.set()
                self.assertTrue(
                    release_rebuilder.wait(5), "rebuilder cleanup was not released")
            return real_cleanup(conn)

        @contextlib.contextmanager
        def observed_admission_lock(_path):
            current = threading.current_thread()
            if current is second:
                rebuilder_attempted.set()
            admission_gate.acquire()
            admission_owner["thread"] = current
            try:
                yield
            finally:
                admission_owner["thread"] = None
                admission_gate.release()

        def run(name, conn):
            try:
                results[name] = db.init_db(conn, self.db_path)
            except BaseException as exc:  # surfaced by the main-thread assertion
                results[name] = exc

        first = threading.Thread(target=run, args=("opener", opener))
        second = threading.Thread(target=run, args=("rebuilder", rebuilder))
        try:
            with mock.patch.object(db, "_rebuild_in_progress",
                                   pause_after_opener_check), \
                    mock.patch.object(db, "_cleanup_rebuild",
                                      pause_rebuilder_cleanup), \
                    mock.patch.object(db, "_rebuild_lock",
                                      observed_admission_lock), \
                    mock.patch.object(db, "_announce_rebuild",
                                      lambda reasons, path: None):
                first.start()
                self.assertTrue(opener_checked.wait(5),
                                "opener never reached its marker check")
                second.start()
                self.assertTrue(rebuilder_attempted.wait(5),
                                "rebuilder never attempted admission")
                self.assertIs(
                    admission_owner["thread"], first,
                    "the opener did not retain admission after its marker check")
                self.assertFalse(rebuilder_marked.is_set(),
                                 "another rebuild committed during admission")
                release_opener.set()
                first.join(timeout=10)
                second.join(timeout=10)
        finally:
            release_opener.set()
            release_rebuilder.set()
            first.join(timeout=10)
            second.join(timeout=10)

        self.assertFalse(first.is_alive(), "pre-existing opener did not finish")
        self.assertFalse(second.is_alive(), "second opener did not finish")
        self.assertIs(results.get("opener"), True)
        self.assertIs(results.get("rebuilder"), False)
        self.assertFalse(db._rebuild_in_progress(opener))
        self.assertEqual(db.schema_mismatches(opener), [])


class TestDatabaseAdmissionContract(unittest.TestCase):
    def test_an_existing_transaction_is_refused_before_lock_acquisition(self):
        """Reverse lock order must fail fast instead of deadlocking."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "usage.db"
        conn = sqlite3.connect(db_path)
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE pending (value TEXT)")
        conn.commit()
        conn.execute("INSERT INTO pending VALUES ('held')")
        self.assertTrue(conn.in_transaction)

        with mock.patch.object(db, "_rebuild_lock") as rebuild_lock:
            with self.assertRaisesRegex(
                    sqlite3.ProgrammingError, "outside a transaction"):
                with db.database_admission(conn, db_path):
                    self.fail("admission yielded inside an existing transaction")
        rebuild_lock.assert_not_called()

        self.assertTrue(conn.in_transaction,
                        "the refusal silently changed the caller transaction")
        conn.rollback()


class TestSessionTotalsAreRepairedByTheVeryNextScan(_MigrationTempDb):
    """The unconditional end-of-scan sweep, and the defect it closes.

    `upsert_sessions` adds tokens to `sessions` additively while `insert_turns`
    merges duplicates against the `message_id` index, so the denormalised totals
    drift and the sweep at the end of `scan()` rewrites them from `turns`
    (AGENTS.md invariant 2).

    Reconciliation must also run when no input file changed. These tests
    deliberately corrupt stored totals and prove that the next scan repairs
    them. Concurrent upsert tests separately cover how partial totals arise.

    The flag also opened a worse hole than it closed, because it was ONE global
    key shared by every scanner PROCESS: one scanner's sweep cleared the flag
    another in-flight scanner was relying on, and if that second scanner then
    died its doubled totals were never reconciled by any later scan.
    Reproduced with six real concurrent processes (3 of 4 trials), confirmed to
    be exactly 2x and to survive two further full rescans.

    So the gate is gone. The scenario below is that damage, in its permanent
    form — a database whose `sessions` disagree with `turns` and whose
    `processed_files` says there is nothing left to read — and it must be
    repaired by the very next scan, with no flag, no marker and no new file.
    """

    def setUp(self):
        super().setUp()
        self.roots = Path(self.tmpdir) / "projects"
        _transcript(self.roots / "s1.jsonl", "s1", ["m1", "m2", "m3"])

    def _scan(self):
        return scanner.scan(projects_dir=self.roots, db_path=self.db_path)

    def _totals(self):
        conn = get_db(self.db_path)
        try:
            return conn.execute(
                "SELECT total_input_tokens, total_output_tokens, turn_count "
                "FROM sessions WHERE session_id = 's1'").fetchone()
        finally:
            conn.close()

    def _double_the_stored_totals(self):
        """Exactly what a scan that died before its sweep leaves behind: the
        additive halves of `upsert_sessions` applied twice, with every
        transcript still stamped as fully ingested."""
        conn = get_db(self.db_path)
        try:
            conn.execute(
                "UPDATE sessions SET total_input_tokens = total_input_tokens * 2,"
                " total_output_tokens = total_output_tokens * 2,"
                " turn_count = turn_count * 2 WHERE session_id = 's1'")
            conn.commit()
        finally:
            conn.close()

    def test_the_fixture_really_is_doubled(self):
        """Guard the setup: without this the repair below proves nothing."""
        self._scan()
        good = self._totals()
        self._double_the_stored_totals()
        bad = self._totals()
        self.assertEqual(tuple(bad), tuple(v * 2 for v in good))

    def test_the_damaged_database_has_nothing_left_to_read(self):
        """The other half of the fixture, and the reason a gated sweep could
        never heal this: every transcript is already stamped, so the next scan
        finds no new and no updated file at all."""
        self._scan()
        self._double_the_stored_totals()
        result = self._scan()
        self.assertEqual((result["new"], result["updated"]), (0, 0))

    def test_the_very_next_scan_repairs_the_totals(self):
        self._scan()
        good = self._totals()
        self._double_the_stored_totals()
        self._scan()
        self.assertEqual(tuple(self._totals()), tuple(good))

    def test_the_repair_needs_no_flag_and_no_marker(self):
        """It is a property of the code, not of a key in the database: there is
        no table left to hold one."""
        self._scan()
        self._double_the_stored_totals()
        conn = get_db(self.db_path)
        try:
            self.assertNotIn("schema_meta", stored_tables(conn))
        finally:
            conn.close()
        self._scan()
        self.assertEqual(self._totals()["turn_count"], 3)

    def test_the_totals_agree_with_turns_after_an_ordinary_scan(self):
        """The sweep must not have become a no-op in the other direction."""
        self._scan()
        conn = get_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT s.total_input_tokens, s.turn_count,"
                " (SELECT SUM(input_tokens) FROM turns WHERE session_id = 's1'),"
                " (SELECT COUNT(*) FROM turns WHERE session_id = 's1')"
                " FROM sessions s WHERE s.session_id = 's1'").fetchone()
        finally:
            conn.close()
        self.assertEqual((row[0], row[1]), (row[2], row[3]))
        self.assertEqual(row[3], 3)


class TestTheMigrationMachineryIsGoneFromTheSource(unittest.TestCase):
    """A mechanism guard, not a spelling one.

    Every deleted migration was reachable only through this handful of names, so
    a build that reintroduces any of them reintroduces the mechanism. Reading
    the source is the only way to assert an *absence*: a behavioural test can
    show that today's schema is right, and says nothing about a marker-gated
    rewrite added tomorrow.

    Searches every `*.py` at the repository ROOT, and nothing under `tests/`.
    The prose in AGENTS.md and in this file's own docstring names these keys on
    purpose — that is the record of why they were removed, and the guard must
    not force it to be deleted too. `#` comments in the modules themselves are
    exempt for the same reason; see `_code_without_comments`.

    **Nothing catches a stale reference under `tests/`, and widening the glob
    is not the answer.** A test docstring is where the reason a test exists is
    written down, and several of them can only be explained by naming the
    machinery that used to depend on them — so a glob over `tests/` would fail
    on this very file's `FORBIDDEN` tuple and would force the record out of the
    others. What is left is a hand sweep: those docstrings were rewritten into
    the past tense on 2026-08-15 after round 16 found several still describing
    the deleted machinery as live, and a future one will need the same pass.

    A DOCSTRING is deliberately NOT exempt, which is the one judgement call
    here. Three of the four live references the widened glob caught were
    docstrings, all of them describing machinery that no longer exists to a
    reader of `help()`. If history belongs in a module, it belongs in a `#`
    comment beside the code it explains.
    """

    ROOT = Path(__file__).resolve().parent.parent
    FORBIDDEN = (
        "_ensure_column", "_meta_get", "_meta_set",
        "_rekey_codex_turns", "codex_lineage_message_id_v1",
        "privacy_cwd_redacted_v1", "privacy_processed_paths_hashed_v1",
        "privacy_pages_reclaimed_v", "privacy_home_project_names_v1",
        "stream_tally_repair_v1", "cache_tier_backfill_v1",
        "reasoning_effort_backfill_v1", "turn_branch_backfill_v1",
        "limit_snapshot_earliest_v1", "topic_backfill_done",
        "session_totals_dirty_v1", "_backfill_topics",
    )

    @classmethod
    def _code_without_comments(cls, path):
        """The module's source with `#` comments removed, strings kept.

        Comments are where the history lives — `scanner.py` still explains why
        the merge rule is what it is by naming the repair that used to depend on
        it, and this guard must not force that record to be deleted. Strings are
        NOT removed, because a marker read is a string: `"SELECT value FROM
        schema_meta"` is exactly the shape being forbidden.

        `path` is a name relative to ROOT for the real modules, or an absolute
        path for the anti-vacuity probe, which must not be written into the
        checkout. `Path.__truediv__` returns its right operand unchanged when
        that operand is absolute, so one expression serves both and the probe
        needs no second reader.
        """
        source = (cls.ROOT / path).read_text(encoding="utf-8")
        out = []
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.COMMENT:
                out.append(token.string)
        return "\n".join(out)

    def test_no_live_code_in_any_package_module_names_any_of_them(self):
        """Every implementation module, not the two the removal touched.

        This scanned only `db.py` and `scanner.py` while its own name claimed
        the mechanism was gone from "the source", and a skeptic proved the gap
        by planting a full `_ensure_column` + `schema_meta` write + a
        `cache_tier_backfill_v1` marker into `rollups.py`: 113 tests stayed
        green. The removal really had left four live references behind, in
        `transcripts.py`, `codex_transcripts.py` and `rollups.py` (twice) --
        none of which the two-file version could ever have seen.

        Globbing ``codex_claude_usage/`` is the fix rather than a longer hand-kept
        list: a list that silently omits a file is worse than no list, because
        it is the thing a reader trusts.
        """
        modules = sorted(p.relative_to(self.ROOT) for p in
                         (self.ROOT / "codex_claude_usage").glob("*.py"))
        self.assertGreater(len(modules), 5,
                           "the glob found almost nothing; check ROOT")
        for name in modules:
            code = self._code_without_comments(name)
            for needle in self.FORBIDDEN:
                with self.subTest(module=name, needle=needle):
                    self.assertNotIn(needle, code)

    def test_schema_meta_survives_only_as_a_legacy_identity_literal(self):
        """The removed marker table is evidence, not migration machinery.

        v1.5.4--v1.6.1 really shipped ``schema_meta``. The exact legacy
        fingerprint must name it to keep those databases upgradeable, while a
        second occurrence would indicate that executable migration code has
        started reading or writing it again.
        """
        for path in sorted((self.ROOT / "codex_claude_usage").glob("*.py")):
            code = self._code_without_comments(path)
            expected = 1 if path.name == "db.py" else 0
            self.assertEqual(code.count("schema_meta"), expected, path.name)

    def test_neither_module_exports_them(self):
        for module in (db, scanner):
            for needle in self.FORBIDDEN:
                with self.subTest(module=module.__name__, needle=needle):
                    self.assertFalse(hasattr(module, needle))

    def test_the_guard_would_notice(self):
        """Refuse to pass vacuously, and prove the comment-stripping does not
        blind it: a needle planted in code is caught, the same needle in a
        comment is not, and the stripper leaves the rest of the file alone.

        The probe goes into a temp directory, not into `tests/`. It used to be
        written to `tests/_guard_probe_nomig.py` and removed by `addCleanup`,
        which does not run on a Ctrl-C or a SIGKILL — so an interrupted suite
        left an untracked `.py` file, named in no `.gitignore`, inside the one
        directory two other guards enumerate from disk.
        """
        code = self._code_without_comments("codex_claude_usage/scanner.py")
        self.assertIn("_model_priority_sql", code, "the stripper ate the source")
        with tempfile.TemporaryDirectory() as tmp:
            planted = Path(tmp) / "_guard_probe_nomig.py"
            planted.write_text(
                "# schema_meta in a comment is allowed\n"
                "MARKER = 'schema_meta'\n", encoding="utf-8")
            stripped = self._code_without_comments(planted)
        self.assertNotIn("in a comment is allowed", stripped)
        self.assertIn("schema_meta", stripped)
        self.assertFalse((self.ROOT / "tests" / "_guard_probe_nomig.py").exists(),
                         "the probe must not be written into the checkout")

    def test_the_end_of_scan_sweep_is_unconditional(self):
        """The one deletion that is a behaviour change rather than a removal.
        Pinned structurally as well as behaviourally (see
        `TestSessionTotalsAreRepairedByTheVeryNextScan`) because a future
        optimisation reaching for a gate is exactly what this must catch: the
        sweep's `UPDATE sessions` must sit at `scan()`'s own indentation, not
        inside an `if`."""
        source = (self.ROOT / "codex_claude_usage" / "scanner.py").read_text(
            encoding="utf-8")
        self.assertIn("\n    conn.execute(f\"\"\"\n        UPDATE sessions SET\n",
                      source)


if __name__ == "__main__":
    unittest.main()
