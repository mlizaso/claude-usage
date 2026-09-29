"""The rebuild is licensed by OWNERSHIP, not by mismatch.

`db.schema_mismatches` answers one question — "is this the schema this build
writes" — and until 2026-08-15 `init_db` treated its answer as licence to drop
every table in the file. That conflates two states which look identical to it
and are not remotely alike:

* a `usage.db` written by another version of this tool, which is ours to throw
  away because `scan()` rebuilds it from the transcripts; and
* a SQLite file this product has never written, whose contents it has no claim
  on at all.

The second was reproduced end to end: an unrelated database holding `notes` and
`invoices`, pointed at by `CLAUDE_USAGE_DB`, handed to the READ-ONLY command
`cli.py stats`. Both tables and all four rows were dropped, under a stderr
notice reading "The database is only a cache of your transcripts, so no usage
history is lost". The drop runs with `PRAGMA secure_delete` on and is followed
by a VACUUM, so the pages are zeroed — there is nothing to recover. The
pre-removal build left that file untouched, so it was a regression rather than a
standing hazard.

Reaching it needs no exotic setup. The `Dockerfile` sets `CLAUDE_USAGE_DB`, this
repository's own documentation tells users to give each installed version its
own, and a typo or a reused path is the whole of it.

**The guard reads complete table/column fingerprints, not familiar names or
column subsets, and its first two versions did not.** The first accepted any
file holding an object called `turns`, `sessions` or `processed_files` — and
`sessions` is one of the commonest table names in the SQL world, shipped by
Laravel, Rails' ActiveRecord::SessionStore and CakePHP among others. A
Laravel-shaped database handed to `cli.py stats` had every table dropped under
the same notice this whole file exists to prevent, with `strings` finding 0
bytes of a stored IBAN afterwards. A VIEW counted too, because `stored_tables`
reports views and triggers. The intermediate guard required only one table with
a few common columns; an unrelated `turns(session_id, message_id,
input_tokens, note)` table still passed and was rebuilt. Current adoption
requires every table and every column of one complete released shape.

The tightening costs no real upgrade:
`test_every_released_version_is_still_recognised` builds each of the 21
released tags' complete declared shape as a real SQLite file and asserts the
gate accepts it. A database caught mid-`executescript` is accepted only after
the durable application id has already been claimed; a partial unmarked schema
is correctly refused.

**That test read the tags with `git show` until 2026-08-16 and skipped on every
CI leg**, because `actions/checkout` fetches no tags and `git tag` came back
empty. So the one guard against a too-tight gate — the failure that would brick
every existing user with "Refusing to rebuild a database this tool did not
write" — was answered by five green runs that had never executed it. The census
is checked in as `_RELEASED_SIGNATURE_TABLES` instead, which runs with no `.git`
at all; the git read survives as `test_the_snapshot_still_describes_this_
checkout_s_tags`, whose job is now only to catch a release nobody recorded.
"""

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path, PureWindowsPath
from unittest import mock

import cli
import db
from db import (ForeignDatabaseError, connect_existing_db, get_db, init_db,
                looks_like_our_database)
from tests.legacy_database import (
    LEGACY_BASE_SCHEMA as _V1_0_THROUGH_V1_4,
    LEGACY_WITH_AGENTS_SCHEMA as _V1_5_0_THROUGH_V1_5_3,
    LEGACY_WITH_LIMITS_SCHEMA as _V1_6_0_THROUGH_V1_6_1,
    LEGACY_WITH_TOPICS_SCHEMA as _V1_5_4_THROUGH_V1_5_5,
    RELEASED_SCHEMAS as _RELEASED_SIGNATURE_TABLES,
    create_schema as _create_schema,
)


def _unrelated_database(path):
    """A SQLite file this product has never written."""
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE notes (id INTEGER, body TEXT);"
                       "CREATE TABLE invoices (id INTEGER, amount REAL);")
    conn.executemany("INSERT INTO notes VALUES (?, ?)",
                     [(1, "quarterly planning"), (2, "do not lose this")])
    conn.executemany("INSERT INTO invoices VALUES (?, ?)",
                     [(1, 4200.0), (2, 1337.0)])
    conn.commit()
    conn.close()
    return path


class TestAForeignFileIsRefusedRatherThanRebuilt(unittest.TestCase):
    def setUp(self):
        self.path = _unrelated_database(
            Path(tempfile.mkdtemp()) / "someone-elses.db")

    def test_init_db_raises_instead_of_dropping(self):
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        with self.assertRaises(ForeignDatabaseError):
            init_db(conn, self.path)

    def test_the_rows_are_still_there_afterwards(self):
        """The assertion that matters. Everything else is about the message."""
        conn = sqlite3.connect(self.path)
        with contextlib.suppress(ForeignDatabaseError):
            init_db(conn, self.path)
        conn.close()
        check = sqlite3.connect(self.path)
        self.addCleanup(check.close)
        self.assertEqual(check.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 2)
        self.assertEqual(
            check.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 2)

    def test_get_db_refuses_before_changing_the_foreign_journal_mode(self):
        check = sqlite3.connect(self.path)
        self.assertEqual(check.execute("PRAGMA journal_mode").fetchone()[0],
                         "delete")
        check.close()

        with self.assertRaises(ForeignDatabaseError):
            get_db(self.path)

        check = sqlite3.connect(self.path)
        self.addCleanup(check.close)
        self.assertEqual(check.execute("PRAGMA journal_mode").fetchone()[0],
                         "delete")
        self.assertEqual(check.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
                         2)
        self.assertFalse(self.path.with_name(self.path.name + "-wal").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits only")
    def test_get_db_refusal_does_not_chmod_the_foreign_file(self):
        """Refusal covers metadata as well as rows and journal settings."""
        os.chmod(self.path, 0o644)
        with self.assertRaises(ForeignDatabaseError):
            get_db(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)

    @unittest.skipUnless(os.name == "posix", "POSIX ownership only")
    def test_a_precreated_file_owned_by_another_uid_is_never_adopted(self):
        """An empty SQLite file is not fresh-install evidence by itself."""
        current_uid = os.getuid()
        with mock.patch.object(db.os, "getuid", return_value=current_uid + 1):
            with self.assertRaisesRegex(RuntimeError, "foreign-owned"):
                get_db(self.path)
        check = sqlite3.connect(self.path)
        self.addCleanup(check.close)
        self.assertEqual(check.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 2)

    def test_the_message_names_the_path_and_the_remedy(self):
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        with self.assertRaises(ForeignDatabaseError) as caught:
            init_db(conn, self.path)
        text = str(caught.exception)
        self.assertIn(str(self.path), text)
        self.assertIn("CLAUDE_USAGE_DB", text)
        # The tables it found, so the reader can recognise their own file.
        self.assertIn("notes", text)

    def test_the_remedy_survives_as_separate_lines(self):
        """`terminal_safe` escapes Cc and a newline is Cc, so routing the whole
        message through it — rather than the path and the table names
        individually — would fold the remedy onto one line as `\\x0a`. The same
        trap `dashboard.port_in_use_lines` documents."""
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        with self.assertRaises(ForeignDatabaseError) as caught:
            init_db(conn, self.path)
        text = str(caught.exception)
        self.assertNotIn("\\x0a", text)
        self.assertGreater(len(text.splitlines()), 3)

    def test_the_cli_exits_1_with_a_message_not_a_traceback(self):
        original, cli.DB_PATH = cli.DB_PATH, self.path
        buf, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(buf), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as caught:
                    cli.cmd_stats()
        finally:
            cli.DB_PATH = original
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(buf.getvalue(), "", "no report over someone else's data")
        self.assertIn("did not write", err.getvalue())


class TestOurOwnDatabasesAreStillRebuilt(unittest.TestCase):
    """Anti-vacuity. A guard that refused everything would 'fix' this defect and
    break every upgrade, and every assertion above would still pass."""

    def test_a_database_from_an_older_version_still_rebuilds(self):
        path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = sqlite3.connect(path)
        _create_schema(conn, _V1_5_4_THROUGH_V1_5_5)
        conn.execute("INSERT INTO turns (session_id, message_id, input_tokens)"
                     " VALUES ('s1', 'm1', 100)")
        conn.commit()
        self.assertTrue(looks_like_our_database(conn, db.stored_tables(conn)))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertTrue(init_db(conn, path), "an old usage.db must rebuild")
        conn.close()
        self.assertIn("different version", err.getvalue())

    def test_a_current_database_is_left_alone(self):
        path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(path)
        self.addCleanup(conn.close)
        init_db(conn, path)
        self.assertFalse(init_db(conn, path), "a matching schema must not rebuild")

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits only")
    def test_a_nonprivate_owned_database_fails_closed_if_chmod_fails(self):
        path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(path)
        init_db(conn, path)
        conn.close()
        os.chmod(path, 0o644)
        self.addCleanup(os.chmod, path, 0o600)
        with mock.patch.object(db.os, "fchmod",
                               side_effect=PermissionError("read-only")):
            with self.assertRaisesRegex(RuntimeError, "non-private"):
                get_db(path)

    @unittest.skipUnless(os.name == "posix", "POSIX directory policy only")
    def test_a_shared_writable_nonsticky_parent_is_refused(self):
        path = Path(tempfile.mkdtemp()) / "usage.db"
        os.chmod(path.parent, 0o777)
        self.addCleanup(os.chmod, path.parent, 0o700)
        with self.assertRaisesRegex(RuntimeError, "shared writable"):
            get_db(path)

    @unittest.skipUnless(os.name == "posix", "POSIX parent-link policy only")
    def test_a_user_owned_parent_symlink_is_refused_before_sqlite_opens(self):
        base = Path(tempfile.mkdtemp())
        target = base / "target"
        target.mkdir(mode=0o700)
        linked_parent = base / "database-dir"
        linked_parent.symlink_to(target, target_is_directory=True)
        path = linked_parent / "usage.db"

        with self.assertRaisesRegex(RuntimeError,
                                    "symbolic-link database directory"):
            get_db(path)
        self.assertFalse((target / "usage.db").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX sticky directories only")
    def test_a_private_custom_directory_under_sticky_tmp_remains_valid(self):
        parent = Path(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(lambda: parent.rmdir())
        path = parent / "usage.db"
        self.addCleanup(
            lambda: [candidate.unlink(missing_ok=True) for candidate in (
                path, path.with_name(path.name + "-wal"),
                path.with_name(path.name + "-shm"),
                path.with_name(path.name + ".rebuild.lock"))])
        conn = get_db(path)
        self.addCleanup(conn.close)
        self.assertFalse(init_db(conn, path))

    @unittest.skipUnless(os.name == "posix", "POSIX sticky directories only")
    def test_a_database_directly_in_sticky_tmp_is_refused(self):
        path = Path("/tmp") / f"claude-usage-sticky-{os.getpid()}-{id(self)}.db"
        self.addCleanup(
            lambda: [candidate.unlink(missing_ok=True) for candidate in (
                path, path.with_name(path.name + "-wal"),
                path.with_name(path.name + "-shm"),
                path.with_name(path.name + ".rebuild.lock"))])
        with self.assertRaisesRegex(RuntimeError, "shared writable"):
            get_db(path)

    def test_a_fresh_database_carries_a_durable_application_identity(self):
        path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(path)
        self.addCleanup(conn.close)
        init_db(conn, path)
        self.assertEqual(
            conn.execute("PRAGMA application_id").fetchone()[0],
            db.APPLICATION_ID)

    def test_a_released_unmarked_database_is_adopted(self):
        """The marker must not strand every database from v1.0.0--v1.6.1."""
        path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = sqlite3.connect(path)
        _create_schema(conn, _V1_0_THROUGH_V1_4)
        self.assertEqual(conn.execute(
            "PRAGMA application_id").fetchone()[0], 0)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(init_db(conn, path))
        self.assertEqual(conn.execute(
            "PRAGMA application_id").fetchone()[0], db.APPLICATION_ID)
        conn.close()

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits only")
    def test_a_released_database_is_tightened_only_after_adoption(self):
        path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = sqlite3.connect(path)
        _create_schema(conn, _V1_0_THROUGH_V1_4)
        os.chmod(path, 0o644)
        with contextlib.redirect_stderr(io.StringIO()):
            init_db(conn, path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        conn.close()

    def test_an_owned_database_is_recognised_while_every_table_is_absent(self):
        """A rebuild window retains the header identity even after all DROPs."""
        path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(path)
        self.addCleanup(conn.close)
        init_db(conn, path)
        for name in db.stored_tables(conn):
            kind = conn.execute(
                "SELECT type FROM sqlite_master WHERE name = ?", (name,)
            ).fetchone()[0]
            conn.execute(f'DROP {kind.upper()} IF EXISTS "{name}"')
        conn.commit()
        self.assertEqual(db.stored_tables(conn), [])
        self.assertFalse(init_db(conn, path))
        self.assertIn("turns", db.stored_tables(conn))

    def test_an_empty_file_is_not_foreign(self):
        """It has no tables at all, so the ownership question does not arise —
        and `init_db` must still create the schema in it, which is every fresh
        install."""
        path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        self.assertFalse(init_db(conn, path))
        self.assertIn("turns", db.stored_tables(conn))

    def test_a_partial_release_shape_is_not_ownership_evidence(self):
        """A common-column `turns` table is not a whole released database."""
        path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE turns (session_id TEXT, message_id TEXT, "
            "input_tokens INTEGER, note TEXT)"
        )
        conn.execute("INSERT INTO turns VALUES ('s', 'm', 1, 'keep me')")
        conn.commit()
        self.assertFalse(looks_like_our_database(conn, db.stored_tables(conn)))
        with self.assertRaises(ForeignDatabaseError):
            init_db(conn, path)
        self.assertEqual(conn.execute("SELECT note FROM turns").fetchone()[0],
                         "keep me")
        self.assertEqual(conn.execute("PRAGMA application_id").fetchone()[0], 0)
        conn.close()

    def test_a_foreign_table_that_merely_shares_a_name_is_not_ours(self):
        """`sessions` is one of the commonest table names in the SQL world --
        Laravel, Rails' ActiveRecord::SessionStore, CakePHP -- and the first
        ownership gate matched names alone. A Laravel-shaped database handed to
        the READ-ONLY `cli.py stats` had every table dropped under the notice
        this gate exists to prevent, with `strings` finding 0 bytes of the
        stored IBAN afterwards.

        A VIEW is included because `stored_tables` reports views and triggers,
        so a view called `sessions` satisfied the old gate too."""
        shapes = {
            "sessions": "CREATE TABLE sessions (id INTEGER PRIMARY KEY,"
                        " user_id INTEGER, payload TEXT, expires INTEGER)",
            "turns": "CREATE TABLE turns (id INTEGER PRIMARY KEY, note TEXT)",
            "processed_files": "CREATE TABLE processed_files (id INTEGER, name TEXT)",
            "view named sessions": "CREATE TABLE login (id INTEGER, tok TEXT);"
                                   "CREATE VIEW sessions AS SELECT * FROM login",
        }
        for label, ddl in shapes.items():
            with self.subTest(shape=label):
                path = Path(tempfile.mkdtemp()) / "usage.db"
                conn = sqlite3.connect(path)
                conn.executescript(ddl)
                conn.commit()
                self.assertFalse(
                    looks_like_our_database(conn, db.stored_tables(conn)),
                    f"a foreign {label} was accepted as ours and would be dropped")
                conn.close()

    def test_every_released_version_is_still_recognised(self):
        """Anti-vacuity, and the half that a tightening can silently break: a
        gate that refused everything would 'fix' the defect and reject every
        real upgrade, while every assertion above still passed.

        Driven through the real gate rather than by re-implementing it: each
        release's declared columns are built as an actual SQLite file and handed
        to `looks_like_our_database`, so a change to what "ours" means is
        measured rather than restated here.

        **This read the tags with `git show` until 2026-08-16, and therefore did
        not run in CI at all.** `actions/checkout` fetches no tags by default,
        so `git tag` came back empty on every runner and the test self-skipped
        -- replicated that day with the action's own fetch (`git init`, `git
        fetch --no-tags --depth=1 <sha>`, `git checkout FETCH_HEAD`): `skipped
        'no tags in this checkout'`. A skipped guard reads as a pass, which is
        the same hazard `CLAUDE_USAGE_REQUIRE_JS` and
        `CLAUDE_USAGE_REQUIRE_BROWSER` exist for, and this one had no such
        escape hatch. The workflow file is not the only fix and was not the one
        taken: the snapshot below runs everywhere, including in a tarball with
        no `.git` at all.

        The floor below is the same hazard one layer down, and it is not
        hypothetical: measured 2026-08-16, emptying `_RELEASED_SIGNATURE_TABLES`
        leaves this test iterating nothing and reporting `ok`, in exactly the
        no-tags checkout every CI leg uses. Released tags only ever accumulate,
        so the number never needs raising to stay true -- an unrecorded NEW
        release is the sibling test's job, and a LOST one is this one's.
        """
        self.assertGreaterEqual(
            len(_RELEASED_SIGNATURE_TABLES), 21,
            "the census has lost releases it once recorded; a truncated one "
            "makes this guard pass by checking nothing")
        for tag, tables in sorted(_RELEASED_SIGNATURE_TABLES.items()):
            with self.subTest(tag=tag):
                path = Path(tempfile.mkdtemp()) / "usage.db"
                conn = sqlite3.connect(path)
                self.addCleanup(conn.close)
                _create_schema(conn, tables)
                self.assertTrue(
                    looks_like_our_database(conn, db.stored_tables(conn)),
                    f"{tag} would be refused as foreign -- the gate is too tight")

    def test_the_snapshot_still_describes_this_checkout_s_tags(self):
        """What the snapshot CAN get wrong, checked wherever tags exist.

        A released tag's bytes never change, so the recorded columns cannot rot;
        what can is a new release being cut and never recorded. This re-derives
        every tag's own `CREATE TABLE` and compares, so an unrecorded release
        fails here rather than silently narrowing the guard above.

        Still skips where there are no tags -- a shallow CI checkout, a source
        tarball -- and that is now a freshness check going unverified rather
        than the ownership gate going unchecked.
        """
        import re
        import subprocess
        root = str(Path(__file__).resolve().parent.parent)
        tags = subprocess.run(["git", "tag"], capture_output=True, text=True,
                              encoding="utf-8", cwd=root).stdout.split()
        if not tags:
            self.skipTest("no tags in this checkout")
        self.assertEqual(sorted(tags), sorted(_RELEASED_SIGNATURE_TABLES),
                         "a release is missing from the snapshot above")
        for tag in tags:
            with self.subTest(tag=tag):
                text = ""
                for name in ("db.py", "scanner.py"):
                    got = subprocess.run(["git", "show", f"{tag}:{name}"],
                                         capture_output=True, text=True,
                                         encoding="utf-8", cwd=root)
                    if got.returncode == 0:
                        text += got.stdout
                declared = {}
                for found in re.finditer(
                        r"CREATE TABLE(?: IF NOT EXISTS)? ([a-z_]+)\s*\((.*?)\n\s*\)",
                        text, re.S):
                    declared.setdefault(found.group(1), tuple(sorted(
                        {m.group(1) for m in
                         re.finditer(r"^\s*([a-z_]+)", found.group(2), re.M)})))
                for table, column in re.findall(
                        r'_ensure_column\(conn,\s*"([a-z0-9_]+)",\s*'
                        r'"([a-z0-9_]+)"',
                        text):
                    declared[table] = tuple(sorted(
                        set(declared.get(table, ())) | {column}))
                self.assertEqual(declared, _RELEASED_SIGNATURE_TABLES[tag])


class TestConnectingToAnExistingDatabaseDoesNotCreateOrSwapIt(unittest.TestCase):
    def test_identity_checks_also_apply_without_posix_permissions(self):
        class NonPosixOS:
            name = "nt"

            def __getattr__(self, name):
                return getattr(os, name)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.db"
            replacement = Path(tmp) / "replacement.db"
            path.write_bytes(b"original")
            replacement.write_bytes(b"replacement")
            expected = db._database_file_identity(path.stat())
            with mock.patch.object(db, "os", NonPosixOS()):
                db._verify_database_path_identity(path, expected)
                with self.assertRaisesRegex(RuntimeError, "Database path changed"):
                    db._verify_database_path_identity(replacement, expected)

    def test_a_windows_unc_path_uses_an_authority_free_sqlite_uri(self):
        uri = db._sqlite_existing_file_uri(
            PureWindowsPath(r"\\server\share\usage.db")
        )
        self.assertEqual(uri, "file:////server/share/usage.db?mode=rw")

    def test_a_missing_database_stays_missing(self):
        path = Path(tempfile.mkdtemp()) / "missing.db"
        self.assertIsNone(connect_existing_db(path))
        self.assertFalse(path.exists())

    def test_removal_after_validation_does_not_recreate_the_file(self):
        path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(path)
        init_db(conn, path)
        conn.close()
        real_guard = db.secure_db_permissions

        def remove_after_validation(*args, **kwargs):
            result = real_guard(*args, **kwargs)
            path.unlink()
            return result

        with mock.patch.object(db, "secure_db_permissions",
                               remove_after_validation):
            with self.assertRaises(sqlite3.OperationalError):
                connect_existing_db(path)
        self.assertFalse(path.exists(),
                         "sqlite3.connect recreated the removed database")

    @unittest.skipUnless(os.name == "posix", "POSIX inode verification only")
    def test_replacement_after_validation_is_rejected_and_closed(self):
        root = Path(tempfile.mkdtemp())
        path = root / "usage.db"
        replacement = root / "replacement.db"
        for candidate in (path, replacement):
            conn = get_db(candidate)
            init_db(conn, candidate)
            conn.close()

        real_guard = db.secure_db_permissions
        real_connect = db.sqlite3.connect
        opened = []

        def replace_after_validation(*args, **kwargs):
            result = real_guard(*args, **kwargs)
            os.replace(replacement, path)
            return result

        def track_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened.append(conn)
            return conn

        with mock.patch.object(db, "secure_db_permissions",
                               replace_after_validation), \
                mock.patch.object(db.sqlite3, "connect", track_connect):
            with self.assertRaisesRegex(RuntimeError,
                                        "Database path changed during open"):
                connect_existing_db(path)

        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    @unittest.skipUnless(os.name == "posix", "POSIX inode verification only")
    def test_admission_rejects_a_swap_after_the_connection_was_opened(self):
        root = Path(tempfile.mkdtemp())
        path = root / "usage.db"
        replacement = root / "replacement.db"
        for candidate in (path, replacement):
            setup = get_db(candidate)
            init_db(setup, candidate)
            setup.close()

        conn = connect_existing_db(path)
        self.addCleanup(conn.close)
        os.replace(replacement, path)

        with self.assertRaisesRegex(RuntimeError,
                                    "Database path changed during admission"):
            with db.database_admission(conn, path):
                pass

    @unittest.skipUnless(os.name == "posix", "POSIX inode verification only")
    def test_admission_rejects_a_swap_after_its_locked_schema_check(self):
        """A stale connection must not publish a result after admission."""
        root = Path(tempfile.mkdtemp())
        path = root / "usage.db"
        replacement = root / "replacement.db"
        for candidate in (path, replacement):
            setup = get_db(candidate)
            init_db(setup, candidate)
            setup.close()

        conn = connect_existing_db(path)
        self.addCleanup(conn.close)
        real_admitted = db._init_db_admitted

        def admit_then_replace(*args, **kwargs):
            result = real_admitted(*args, **kwargs)
            os.replace(replacement, path)
            return result

        with mock.patch.object(db, "_init_db_admitted", admit_then_replace):
            with self.assertRaisesRegex(
                    RuntimeError, "Database path changed during admission"):
                with db.database_admission(conn, path):
                    conn.execute("SELECT 1").fetchone()


class TestATablelessForeignDatabaseIsNotMistakenForAFreshFile(unittest.TestCase):
    """A foreign owner's DDL window must never become our ownership claim."""

    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "foreign.db"
        owner = sqlite3.connect(self.path)
        owner.execute("CREATE TABLE invoices (id INTEGER, amount INTEGER)")
        owner.execute("INSERT INTO invoices VALUES (1, 4200)")
        owner.commit()
        owner.execute("DROP TABLE invoices")
        owner.commit()
        owner.close()
        self.assertGreater(self.path.stat().st_size, 0)

    def test_the_tableless_window_is_refused_instead_of_claimed(self):
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        with self.assertRaisesRegex(
                ForeignDatabaseError, "non-empty, table-less"):
            init_db(conn, self.path)
        self.assertEqual(conn.execute(
            "PRAGMA application_id").fetchone()[0], 0)
        self.assertEqual(db.stored_tables(conn), [])

    def test_recreated_foreign_rows_survive_every_later_open(self):
        first = sqlite3.connect(self.path)
        with contextlib.suppress(ForeignDatabaseError):
            init_db(first, self.path)
        first.close()

        owner = sqlite3.connect(self.path)
        owner.execute("CREATE TABLE invoices (id INTEGER, amount INTEGER)")
        owner.execute("INSERT INTO invoices VALUES (1, 4200)")
        owner.commit()
        owner.close()

        second = sqlite3.connect(self.path)
        with self.assertRaises(ForeignDatabaseError):
            init_db(second, self.path)
        second.close()
        check = sqlite3.connect(self.path)
        self.addCleanup(check.close)
        self.assertEqual(check.execute(
            "SELECT id, amount FROM invoices").fetchall(), [(1, 4200)])

    def test_a_create_drop_between_the_guarded_open_and_sqlite_is_refused(self):
        """The guarded descriptor closes before SQLite opens the path.

        A stale zero-byte observation must not survive that gap: the connected
        database is rechecked under ``BEGIN IMMEDIATE`` before it is claimed.
        """
        path = Path(tempfile.mkdtemp()) / "raced.db"
        real_secure = db.secure_db_permissions

        def interleaved(target, *args, **kwargs):
            result = real_secure(target, *args, **kwargs)
            owner = sqlite3.connect(path)
            owner.execute("CREATE TABLE invoices (id INTEGER, amount INTEGER)")
            owner.execute("INSERT INTO invoices VALUES (1, 4200)")
            owner.commit()
            owner.execute("DROP TABLE invoices")
            owner.commit()
            owner.close()
            return result

        with mock.patch.object(db, "secure_db_permissions",
                               side_effect=interleaved):
            with self.assertRaisesRegex(
                    ForeignDatabaseError, "table-less"):
                get_db(path)

        owner = sqlite3.connect(path)
        self.addCleanup(owner.close)
        self.assertEqual(owner.execute("PRAGMA application_id").fetchone()[0], 0)
        owner.execute("CREATE TABLE invoices (id INTEGER, amount INTEGER)")
        owner.execute("INSERT INTO invoices VALUES (1, 4200)")
        owner.commit()
        with self.assertRaises(ForeignDatabaseError):
            init_db(owner, path)
        self.assertEqual(owner.execute(
            "SELECT id, amount FROM invoices").fetchall(), [(1, 4200)])

    def test_another_applications_nonzero_identity_is_always_refused(self):
        owner = sqlite3.connect(self.path)
        owner.execute("PRAGMA application_id = 123456")
        owner.commit()
        owner.close()
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        with self.assertRaisesRegex(ForeignDatabaseError, "123456"):
            init_db(conn, self.path)


class TestTheSchemaIsPublishedWithoutTakingTheWriteLock(unittest.TestCase):
    """`init_db` must not make a reader into a writer.

    executescript can commit a transaction before executing its statements.
    Database admission must avoid an unnecessary write lock while a scanner is
    reconciling sessions."""

    def test_a_matching_schema_leaves_no_transaction_open(self):
        path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(path)
        self.addCleanup(conn.close)
        init_db(conn, path)
        init_db(conn, path)
        self.assertFalse(conn.in_transaction)

    def test_another_connection_can_write_while_init_db_runs_on_a_current_db(self):
        """The property the removed wrap broke, asserted directly: opening a
        database whose schema already matches must not block a writer."""
        path = Path(tempfile.mkdtemp()) / "usage.db"
        setup = get_db(path)
        init_db(setup, path)
        setup.close()

        writer = sqlite3.connect(path, timeout=0)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO turns (session_id) VALUES ('held')")
        try:
            reader = sqlite3.connect(path, timeout=0)
            self.addCleanup(reader.close)
            # Would raise `database is locked` if init_db demanded the write
            # lock while another connection held it.
            init_db(reader, path)
        finally:
            writer.rollback()


if __name__ == "__main__":
    unittest.main()
