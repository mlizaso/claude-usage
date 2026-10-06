"""What each CLI command writes to stdout, and what it does not.

Three findings converge here, and the rule they share is one sentence: **stdout
is the answer the command was asked for; everything else is stderr.** A
diagnostic on stdout is not untidiness, it is a wrong answer delivered on the
channel the caller reads.

`url` is the case that makes it concrete. It exists to be substituted --
`open "$(python cli.py url)"` -- so its stdout is a URL or nothing at all. Its
six failure lines went to stdout, which handed a launcher two lines of English
where it expected a link, and `2>/dev/null` hid nothing because there was
nothing on stderr to hide. The docstring and test added alongside asserted that
"url's stdout is a single URL" while the failure paths still broke it.

The last class covers the one file state declare-and-rebuild cannot answer: a
`usage.db` that is not a SQLite database at all. `stored_tables` raises before
`schema_mismatches` can be asked anything, so it surfaced as a six-frame
traceback where every other database problem gets one line -- and unlike a
foreign schema or a stale one, nothing here can repair it, so the remedy has to
be handed to the user.
"""

import contextlib
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from unittest import mock

import cli
import db


class _Streams(unittest.TestCase):
    def run_command(self, command, *args, **kwargs):
        """(exit code or None, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        code = None
        with redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                command(*args, **kwargs)
            except SystemExit as exc:
                code = 1 if exc.code is None else exc.code
        return code, out.getvalue(), err.getvalue()


class TestUrlWritesOnlyAUrlToStdout(_Streams):
    """`url`'s stdout is a URL or nothing — on every path, not just the happy one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = cli.DB_PATH
        cli.DB_PATH = Path(self.tmp) / "usage.db"
        # `cmd_url` reads `dashboard.URL_FILE`, probes whatever it finds, and
        # UNLINKS it when the probe proves the link dead. Patching only
        # `cli.DB_PATH` left that pointed at the developer's real
        # `~/.claude/dashboard-url`, so running the suite deleted the one way
        # back into a dashboard they had open -- and if it was still live,
        # `cmd_url` printed its URL and this test failed on `out == ""`, which
        # is a suite that only passes for users who have never run `dashboard`.
        import dashboard
        self._orig_url_file = dashboard.URL_FILE
        dashboard.URL_FILE = Path(self.tmp) / "dashboard-url"

    def tearDown(self):
        cli.DB_PATH = self._orig
        import dashboard
        dashboard.URL_FILE = self._orig_url_file

    def test_the_real_dashboard_url_is_not_what_we_are_driving(self):
        """Asserted rather than assumed, because nothing else here would fail if
        the redirection were dropped -- it would just quietly operate on the
        developer's own file again."""
        import dashboard
        self.assertEqual(dashboard.URL_FILE.parent, Path(self.tmp))
        self.assertNotEqual(dashboard.URL_FILE, self._orig_url_file)

    def test_no_running_dashboard_says_so_on_stderr(self):
        code, out, err = self.run_command(cli.cmd_url)
        self.assertEqual(code, 1)
        self.assertEqual(out, "",
                         "a launcher substituting this would open the message")
        self.assertIn("No running dashboard", err)
        self.assertIn("cli.py dashboard", err)


class TestAnUnreadableDatabaseIsExplainedNotRaised(_Streams):
    """A file that SQLite cannot open at all.

    Distinct from both other database refusals, and reachable the same way they
    are: a truncated copy, an interrupted download, a path collision with
    something that is not a database.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "usage.db"
        self.path.write_bytes(b"not a database\n")
        self._orig = cli.DB_PATH
        cli.DB_PATH = self.path

    def tearDown(self):
        cli.DB_PATH = self._orig

    def test_it_exits_1_without_a_traceback(self):
        code, out, err = self.run_command(cli.cmd_stats)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)

    def test_the_message_names_the_file_and_the_remedy(self):
        _, _, err = self.run_command(cli.cmd_stats)
        self.assertIn(str(self.path), err)
        self.assertIn("cli.py scan", err)
        # The remedy is the USER's here — a rebuild needs a schema to read and
        # there is none — so it has to say to move the file.
        self.assertTrue("Move" in err or "delete" in err.lower(),
                        f"no actionable remedy in: {err!r}")

    def test_every_read_command_is_covered(self):
        for name in ("cmd_today", "cmd_week", "cmd_stats"):
            with self.subTest(command=name):
                code, out, err = self.run_command(getattr(cli, name))
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertNotIn("Traceback", err)


class TestALockedOrReadOnlyDatabaseIsNotCalledUnusable(_Streams):
    """`sqlite3.OperationalError` is a SUBCLASS of `sqlite3.DatabaseError`.

    So a single `except sqlite3.DatabaseError` catches `database is locked`,
    `attempt to write a readonly database` and `unable to open database file`
    alongside the corruption it was written for — and answers all of them with
    "Move or delete that file". Told that about a database another window was
    merely scanning, a reader would throw away a perfectly good file.

    The clause order is load-bearing and cannot be asserted by reading: the
    subclass must be caught first or it never runs at all.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "usage.db"
        conn = sqlite3.connect(self.path)
        # Ours by ownership, but on a schema this build does not write, so
        # `init_db` tries to rebuild — which is what needs the write lock.
        # Real signature columns: the ownership gate reads columns, not names,
        # so a stub `sessions (session_id)` is somebody else's file and would be
        # refused before the lock is ever reached.
        conn.executescript(
            "CREATE TABLE turns (id INTEGER PRIMARY KEY, s TEXT,"
            " session_id TEXT, message_id TEXT, input_tokens INTEGER);"
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY,"
            " total_input_tokens INTEGER);")
        conn.executemany("INSERT INTO turns (s) VALUES (?)", [("a",), ("b",)])
        conn.execute(f"PRAGMA application_id = {db.APPLICATION_ID}")
        conn.commit()
        conn.close()
        self._orig = cli.DB_PATH
        cli.DB_PATH = self.path

    def tearDown(self):
        cli.DB_PATH = self._orig
        os.chmod(self.tmp, 0o755)

    @unittest.skipUnless(os.name == "posix", "POSIX modes only")
    def test_a_read_only_location_is_not_reported_as_a_broken_file(self):
        os.chmod(self.tmp, 0o555)
        code, out, err = self.run_command(cli.cmd_stats)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertNotIn("Move or delete", err,
                         "told the user to delete an intact database")
        self.assertIn("looks fine", err)

    @unittest.skipUnless(os.name == "posix", "POSIX modes only")
    def test_the_intact_database_survives_being_refused(self):
        os.chmod(self.tmp, 0o555)
        self.run_command(cli.cmd_stats)
        os.chmod(self.tmp, 0o755)
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 2)

    def test_a_lock_is_answered_the_same_way_on_every_platform(self):
        """The same refusal as the two tests above, with no POSIX mode in it.

        Both of those manufacture the error with `os.chmod(dir, 0o555)`, so both
        are POSIX-only, and for a while they were this branch's only cover at
        all. Measured 2026-08-16 on a copy of the tree as it stood then, with
        `database_refusal`'s `OperationalError` branch deleted outright and
        every `os.name == "posix"` predicate under `tests/` forced false — which
        is what the windows-latest leg runs: the full suite came back OK, with
        29 skips and not one failure.
        The branch that keeps `database is locked` from being answered with
        "move or delete that file" could be removed and no leg but POSIX would
        notice, on the platform whose file locking is the stricter of the two.

        `TestOneDefinitionOfWhichDatabaseRefusalIsWhich` now closes the wording
        half of that portably, and this is deliberately NOT a second copy of it:
        that class calls `database_refusal` directly, and what is left unpinned
        off-POSIX is everything between the raise and the terminal — that
        `require_db` prints the refusal rather than raising, exits 1, keeps
        stdout empty for a piped report, and passes SQLite's own words through.

        Raising the error directly is what makes it portable, and it is a
        FAITHFUL substitute rather than a convenience: `require_db` catches what
        `init_db` raises and hands it straight to `database_refusal`, so what
        this injects is exactly the object a real lock delivers. What it does
        not reproduce is the lock itself — the sibling above still owns "the
        intact database survives being refused".
        """
        import scanner
        for message in ("database is locked",
                        "attempt to write a readonly database",
                        "unable to open database file"):
            with self.subTest(sqlite_said=message):
                with mock.patch.object(
                        scanner, "init_db",
                        side_effect=sqlite3.OperationalError(message)):
                    code, out, err = self.run_command(cli.cmd_stats)
                self.assertEqual(code, 1)
                self.assertEqual(out, "", "a diagnostic reached stdout")
                self.assertNotIn("Traceback", err)
                self.assertNotIn("Move or delete", err,
                                 "told the user to delete an intact database")
                self.assertIn("looks fine", err)
                self.assertIn(message, err, "SQLite's own words are dropped")

    def test_operational_error_is_caught_before_its_parent(self):
        """Structural cover for the except clauses that still carry both.

        The docstring here used to say the ordering "cannot be observed once it
        is wrong". That was refuted by its own siblings above, which read the
        message and see the difference on every platform; what is true is
        narrower — an except-clause order is invisible to a *reader*, and this
        walk is cheap insurance against a future clause list regaining it.

        Counted rather than assumed, because a loop over "every Try carrying
        both handlers" asserts nothing at all when none does. Measured
        2026-08-16, exactly one qualifies: `main`'s guard around the report.
        """
        import ast
        source = (Path(__file__).resolve().parent.parent / "codex_claude_usage" /
                  "cli.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            names = []
            for h in node.handlers:
                if h.type is None:
                    continue
                names.append(ast.unparse(h.type))
            # `sqlite3.DatabaseError` exactly -- NOT anything merely ending in
            # those letters. `ForeignDatabaseError` is this repository's own and
            # is not in the sqlite3 hierarchy at all; matching it here made this
            # test fail against correctly ordered code, which is its own small
            # lesson about suffix matching.
            def is_sqlite(name, leaf):
                return name in (f"sqlite3.{leaf}", leaf)

            if any(is_sqlite(n, "OperationalError") for n in names) and \
               any(is_sqlite(n, "DatabaseError") for n in names):
                op = next(i for i, n in enumerate(names)
                          if is_sqlite(n, "OperationalError"))
                db = next(i for i, n in enumerate(names)
                          if is_sqlite(n, "DatabaseError"))
                checked += 1
                self.assertLess(op, db,
                                "OperationalError is a subclass and must be "
                                "caught first, or its branch is dead code")
        self.assertGreater(
            checked, 0,
            "no try in cli.py carries both clauses -- either the branch this "
            "pins is gone, or it moved and took the cover with it")


class TestACorruptDatabaseIsExplainedFromTheReadToo(_Streams):
    """Page 1 intact, later pages damaged — the commonest corruption there is.

    Such a file OPENS cleanly, so `require_db`'s handler never fires; it fails
    on the first read past the corruption, inside the report. The guard ended
    one statement before the failure it was written for, so `stats` still
    produced the six-frame traceback the fix was meant to remove.
    """

    def _corrupt_database(self):
        from db import get_db, init_db
        from scanner import insert_turns, upsert_sessions
        path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(path)
        init_db(conn, path)
        upsert_sessions(conn, [{
            "session_id": "s", "project_name": "p",
            "first_timestamp": "2026-08-01T00:00:00Z",
            "last_timestamp": "2026-08-01T00:00:00Z", "git_branch": "m",
            "model": "claude-opus-4-8", "total_input_tokens": 1,
            "total_output_tokens": 1, "total_cache_read": 0,
            "total_cache_creation": 0, "turn_count": 1}])
        insert_turns(conn, [{
            "session_id": "s", "timestamp": "2026-08-01T00:00:00Z",
            "model": "claude-opus-4-8", "input_tokens": i, "output_tokens": 1,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_name": None, "cwd": None, "message_id": f"m{i}",
            "is_subagent": 0, "agent_id": None} for i in range(3000)])
        conn.commit()
        conn.close()
        with contextlib.closing(sqlite3.connect(path)) as checkpoint:
            checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with open(path, "r+b") as handle:
            handle.seek(4096 * 30)
            handle.write(b"\xff" * 4096 * 10)
        return path

    def test_the_report_path_explains_it_rather_than_raising(self):
        path = self._corrupt_database()
        original, cli.DB_PATH = cli.DB_PATH, path
        try:
            with mock.patch.object(cli.sys, "argv", ["cli.py", "stats"]):
                code, out, err = self.run_command(cli.main)
        finally:
            cli.DB_PATH = original
        self.assertEqual(code, 1)
        self.assertIn("Not a usable usage database", err)
        self.assertIn("cli.py scan", err)

    def test_a_lock_taken_mid_report_is_not_called_corruption_either(self):
        """The same subclass trap, at the second boundary.

        A scan can take the write lock AFTER `require_db` has handed back a
        connection and while the report is still reading, so `main` needs the
        split `require_db` already has. Raised directly rather than arranged
        with a real writer: what is asserted is which message the handler
        chooses, and a timing race would test the fixture instead.
        """
        def locked(**kwargs):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.dict(cli.COMMANDS, {"stats": locked}), \
                mock.patch.object(cli.sys, "argv", ["cli.py", "stats"]):
            code, out, err = self.run_command(cli.main)
        self.assertEqual(code, 1)
        self.assertNotIn("Move or delete", err,
                         "a lock is not a reason to delete the database")
        self.assertIn("Try again", err)


class TestTheDisplayedClockAgreesWithTheDayItIsFiledUnder(unittest.TestCase):
    """`local_minute_expr` and `local_day_expr` are siblings over one column.

    `rollups.sessions_all` states the contract outright — "both are local so a
    session lands on the same day the charts put its turns on" — and gating only
    the day key broke it for exactly the value class the gate was added for. Two
    sessions three days apart printed identically while sitting in different day
    buckets; a stored literal `now` printed the moment the payload was built,
    recomputed on every poll, so a session that never happened read as active
    right now.
    """

    def _both(self, value):
        from localdays import local_day_expr, local_minute_expr
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE t (ts TEXT)")
            conn.execute("INSERT INTO t VALUES (?)", (value,))
            return conn.execute(
                f"SELECT {local_day_expr('ts')}, {local_minute_expr('ts')} FROM t"
            ).fetchone()
        finally:
            conn.close()

    def test_a_calendar_impossible_date_is_binned_by_both(self):
        day, minute = self._both("2026-02-31T12:00:00Z")
        self.assertEqual(day, "2026-02-31")
        self.assertTrue(minute.startswith("2026-02-31"),
                        f"the clock re-interpreted a day the bucket did not: {minute!r}")

    def test_a_non_iso_time_value_is_binned_by_both(self):
        """SQLite accepts `now`; its calendar day is not in the string at all,
        so neither expression may hand it to `date()`/`strftime()`."""
        day, minute = self._both("now")
        self.assertEqual(day, "now")
        self.assertEqual(minute, "now")

    @contextlib.contextmanager
    def _in_utc(self):
        """Set TZ for this block and put it back, whatever happens.

        **Restoring is not tidiness here.** `time.tzset()` changes the process's
        timezone for every test that runs after it, in whatever order the loader
        happens to pick — and this suite is full of tests whose whole subject is
        the viewer's local day. An unrestored `TZ=UTC` makes those pass for the
        wrong reason, and a developer in any other zone gets a different suite
        from CI. The first version of this test set it and never put it back.
        """
        import time
        previous = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        if hasattr(time, "tzset"):
            time.tzset()
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            if hasattr(time, "tzset"):
                time.tzset()

    def test_a_real_timestamp_is_unaffected(self):
        """Anti-vacuity: a gate that binned everything would pass every
        assertion above and destroy the product."""
        with self._in_utc():
            day, minute = self._both("2026-03-03T12:34:56Z")
        self.assertEqual(day, "2026-03-03")
        self.assertEqual(minute, "2026-03-03 12:34")


class _EntryPoint(unittest.TestCase):
    """Run a shipped entry point in a child process, isolated from the user.

    A child rather than an in-process call because what is under test lives in
    an `if __name__ == "__main__":` block, which no import ever executes, and
    because the exit CODE is half of every contract below.

    **`HOME` and `CODEX_CLAUDE_USAGE_DB` are redirected as a safety property, not as
    hygiene.** In several cases below the thing under test is precisely what
    stops the child from scanning; mutate it away and an un-isolated child
    walks the developer's real `~/.claude/projects` and writes their real
    `usage.db`. `USERPROFILE` goes with `HOME` because that is what
    `Path.home()` reads on Windows.
    """

    ROOT = Path(__file__).resolve().parent.parent

    def entry_point(self, *argv, db=None, extra_env=None):
        """(returncode, stdout, stderr).

        `extra_env` is applied last, so a caller can put back the one variable
        this harness deliberately clears -- `CODEX_CLAUDE_USAGE_PROJECTS_DIRS`, whose
        absence is the whole subject of one test below.
        """
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        Path(home, ".claude", "projects").mkdir(parents=True)
        env = dict(os.environ)
        env["HOME"] = home
        env["USERPROFILE"] = home
        env["CODEX_CLAUDE_USAGE_DB"] = str(db or Path(home, ".claude", "usage.db"))
        env.pop("CODEX_CLAUDE_USAGE_PROJECTS_DIRS", None)
        # Both ends pinned by construction. `encoding=` alone is half the fix
        # for a child that is itself Python: on a windows-latest runner the
        # child encodes its stdout with the runner's codepage, and decoding
        # that as UTF-8 here is a NEW mismatch rather than a repair -- a worse
        # one, because cp1252 decoding never raises.
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(extra_env or {})
        proc = subprocess.run([sys.executable, *argv], cwd=self.ROOT, env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=180)
        return proc.returncode, proc.stdout, proc.stderr

    def foreign_database(self):
        """Somebody else's SQLite file, by column signature and not by name."""
        path = Path(tempfile.mkdtemp()) / "usage.db"
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        conn = sqlite3.connect(path)
        conn.executescript("CREATE TABLE sessions (id INTEGER, note TEXT);"
                           "CREATE TABLE invoices (id INTEGER);")
        conn.commit()
        conn.close()
        return path

    def unreadable_database(self):
        """Not a SQLite database at all -- a truncated copy, a path collision."""
        path = Path(tempfile.mkdtemp()) / "usage.db"
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        path.write_bytes(b"not a database\n")
        return path


class TestScanAnswersTheSameRefusalsTheReportsDo(_EntryPoint):
    """The command the refusal message NAMES answered it with a traceback.

    `stats` on a mis-pointed `CODEX_CLAUDE_USAGE_DB` printed three lines ending in
    "Move or delete that file, then run: python cli.py scan". Follow that
    literally without moving the file first -- which is what a reader who has
    not yet understood the message does -- and `scan` produced a bare
    `sqlite3.DatabaseError` traceback. (No frame count: the two this campaign
    wrote down were both wrong, and the depth is zero now anyway.) A foreign file did the same through
    `db.ForeignDatabaseError`, raised out of `scanner._scan_unlocked`.

    Both are a typo in an environment variable, and a typo that tracebacks from
    one command and prints one line from another teaches the reader that the two
    commands disagree about the file.
    """

    def test_a_foreign_database_stops_the_scan_with_a_message(self):
        code, out, err = self.entry_point("cli.py", "scan",
                                          db=self.foreign_database())
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)
        self.assertIn("Refusing to rebuild", err)

    def test_an_unreadable_database_stops_the_scan_with_a_message(self):
        code, out, err = self.entry_point("cli.py", "scan",
                                          db=self.unreadable_database())
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)
        self.assertIn("Not a usable usage database", err)

    def test_scan_and_stats_say_the_same_thing_about_the_same_file(self):
        """Equality, not similarity, because the point is ONE definition.

        Two hand-kept copies of these messages would pass every assertion above
        while drifting apart on the next edit; byte equality is what makes a
        second copy fail here the day it is written.
        """
        for fixture in ("foreign_database", "unreadable_database"):
            with self.subTest(fixture=fixture):
                path = getattr(self, fixture)()
                _, _, scanned = self.entry_point("cli.py", "scan", db=path)
                _, _, reported = self.entry_point("cli.py", "stats", db=path)
                self.assertNotEqual(scanned.strip(), "",
                                    "anti-vacuity: two silent commands are equal too")
                self.assertEqual(scanned, reported)

    def test_the_remedy_survives_as_separate_lines(self):
        """`terminal_safe` escapes Cc and a newline is Cc, so a multi-line
        exception routed through it arrives as one `\\x0a`-run of a line."""
        _, _, err = self.entry_point("cli.py", "scan", db=self.foreign_database())
        self.assertNotIn("\\x0a", err)
        self.assertGreaterEqual(len(err.strip().splitlines()), 4)


class TestAPathTheGuardRefusesIsAMessageNotATraceback(_EntryPoint):
    """`db.secure_db_permissions` refuses four PATH shapes, and none reached
    `database_refusal` at all.

    Measured 2026-08-16 against a copy of the tree before the fix, each of the
    four on `stats` and on `scan`: eight invocations, eight multi-frame
    tracebacks, exit 1. Three of them ended in the guard's own `RuntimeError`;
    the fourth -- a database inside a directory that cannot be stat'd -- ended
    in a `PermissionError` raised by `require_db`'s own `DB_PATH.exists()`
    probe, one line ABOVE the first handler in that function, which is why
    widening the caught tuple alone would not have reached it. Every other
    database problem gets one refusal and an exit; a mis-pointed
    `CODEX_CLAUDE_USAGE_DB` is a typo, not a crash.

    The two commands do not print identical words for a directory, and that is
    the guard's doing rather than a second copy of a message: `require_db` asks
    with `create=False` and is told "not a regular file", while `scan` reaches
    it through `db.get_db` with `create=True`, where the `os.open` fails first
    and becomes "Refusing unsafe database path". Both are the guard's sentence,
    quoted rather than re-worded, so neither can drift from it here.
    """

    def refuses(self, db, *guard_said):
        for command in ("stats", "scan"):
            with self.subTest(command=command):
                code, out, err = self.entry_point("cli.py", command, db=db)
                self.assertEqual(code, 1)
                self.assertEqual(out, "", "a diagnostic reached stdout")
                self.assertNotIn("Traceback", err)
                self.assertIn("Cannot use that usage database path", err)
                self.assertIn(str(db), err, "the refusal does not name the file")
                self.assertIn("CODEX_CLAUDE_USAGE_DB", err,
                              "nothing points at what to correct")
                self.assertTrue(
                    any(said in err for said in guard_said),
                    f"none of {guard_said} in the refusal: {err!r}")

    def test_a_path_that_is_not_a_regular_file(self):
        """Portable: no symlink, no mode bits, so the Windows leg runs it."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "usage.db").mkdir()
        self.refuses(tmp / "usage.db",
                     "Database path is not a regular file",
                     "Refusing unsafe database path")

    def test_an_unsafe_rebuild_lock_is_the_same_path_refusal(self):
        """Admission's adjacent lock belongs to the database trust boundary."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "usage.db"
        conn = db.get_db(path)
        db.init_db(conn, path)
        conn.commit()
        conn.close()
        lock_path = path.with_name(path.name + ".rebuild.lock")
        lock_path.unlink()
        lock_path.mkdir()

        self.refuses(path, "Refusing unsafe rebuild lock")

    @unittest.skipUnless(os.name == "posix", "needs symlinks and POSIX modes")
    def test_the_three_conditions_that_need_a_posix_filesystem(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)

        real = tmp / "real.db"
        sqlite3.connect(real).close()
        link = tmp / "link.db"
        link.symlink_to(real)
        with self.subTest(path="symbolic link"):
            self.refuses(link, "Refusing symbolic-link database path")

        hard = tmp / "hard.db"
        os.link(real, hard)
        with self.subTest(path="hard link"):
            self.refuses(hard, "Refusing hard-linked database path")

        shut = tmp / "shut"
        shut.mkdir()
        inside = shut / "usage.db"
        sqlite3.connect(inside).close()
        os.chmod(shut, 0o000)
        self.addCleanup(os.chmod, shut, 0o755)
        with self.subTest(path="parent directory that cannot be stat'd"):
            # The guard's sentence here is the OSError's own -- there is no
            # `RuntimeError` to quote, because the stat that would have decided
            # which refusal applies is the call that failed.
            self.refuses(inside, "Permission denied")

    @unittest.skipUnless(os.name == "posix", "POSIX inode verification only")
    def test_a_swap_after_validation_is_still_a_path_refusal(self):
        """The post-connect identity guard belongs to the same CLI contract.

        ``connect_existing_db`` validates a descriptor, opens SQLite by path,
        and then rejects an inode swap.  That last rejection happens after
        ``secure_db_permissions`` has returned, so the outer connection guard
        must raise the same explicit path-refusal exception.
        """
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "usage.db"
        replacement = tmp / "replacement.db"
        for candidate in (path, replacement):
            conn = db.get_db(candidate)
            db.init_db(conn, candidate)
            conn.commit()
            conn.close()

        real_guard = db.secure_db_permissions

        def replace_after_validation(*args, **kwargs):
            result = real_guard(*args, **kwargs)
            os.replace(replacement, path)
            return result

        with mock.patch.object(cli, "DB_PATH", path), \
                mock.patch.object(db, "secure_db_permissions",
                                  replace_after_validation):
            code, out, err = _Streams.run_command(self, cli.cmd_stats)

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)
        self.assertIn("Cannot use that usage database path", err)
        self.assertIn("Database path changed during open", err)

    @unittest.skipUnless(os.name == "posix", "POSIX inode verification only")
    def test_scan_classifies_the_shared_post_open_identity_guard(self):
        """The create-capable opener reaches the verifier through ``get_db``."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "usage.db"
        replacement = tmp / "replacement.db"
        for candidate in (path, replacement):
            conn = db.get_db(candidate)
            db.init_db(conn, candidate)
            conn.commit()
            conn.close()

        real_guard = db.secure_db_permissions

        def replace_after_validation(*args, **kwargs):
            result = real_guard(*args, **kwargs)
            os.replace(replacement, path)
            return result

        import scanner
        with mock.patch.object(cli, "DB_PATH", path), \
                mock.patch.object(scanner, "DB_PATH", path), \
                mock.patch.object(db, "secure_db_permissions",
                                  replace_after_validation):
            code, out, err = _Streams.run_command(self, cli.cmd_scan)

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)
        self.assertIn("Cannot use that usage database path", err)
        self.assertIn("Database path changed during open", err)

    @unittest.skipUnless(os.name == "posix", "POSIX inode verification only")
    def test_read_classifies_an_identity_change_before_admission(self):
        """A safe open can become stale before ``init_db`` takes its lock."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "usage.db"
        replacement = tmp / "replacement.db"
        for candidate in (path, replacement):
            conn = db.get_db(candidate)
            db.init_db(conn, candidate)
            conn.commit()
            conn.close()

        real_connect = db.connect_existing_db

        def connect_then_replace(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            os.replace(replacement, path)
            return conn

        with mock.patch.object(cli, "DB_PATH", path), \
                mock.patch.object(db, "connect_existing_db",
                                  connect_then_replace):
            code, out, err = _Streams.run_command(self, cli.cmd_stats)

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)
        self.assertIn("Cannot use that usage database path", err)
        self.assertIn("Database path changed during admission", err)

    def test_an_error_the_guard_did_not_raise_is_re_raised_not_relabelled(self):
        """The other half of widening the caught tuple.

        `_refusable()` now catches `OSError` and bare `RuntimeError`, which also
        carry a disk that filled up. `database_refusal` answers `None` for
        those and the caller re-raises, so nothing is printed and the original
        traceback survives -- delete that `raise` and a full disk is answered
        with advice about `CODEX_CLAUDE_USAGE_DB`.
        """
        import scanner
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "usage.db"
        sqlite3.connect(path).close()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "DB_PATH", path), \
                mock.patch.object(scanner, "init_db",
                                  side_effect=OSError("no space left on device")), \
                redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(OSError):
                cli.cmd_stats()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "",
                         "a disk that filled up was given a database remedy")

    def test_every_caller_re_raises_not_just_the_one_with_a_test(self):
        """The claim is about EVERY caller, so assert it at every caller.

        There are three `if not _refused(exc): raise` sites and the test above
        drives one. Deleting the `raise` at either of the others -- the one-line
        `_refused(exc); sys.exit(1)` a reader who assumes `_refused` always
        exits would write -- left the whole suite green, so `cli.py scan` on a
        full disk would exit 1 with both streams empty and nothing in CI would
        notice. The shipped behaviour is right; only the coverage was missing.
        """
        import scanner
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "usage.db"
        sqlite3.connect(path).close()
        # `scanner.scan` is what `cmd_scan` calls, and `OSError` is a member of
        # the widened tuple that `database_refusal` deliberately answers None
        # for -- the combination that must reach the caller unlabelled.
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "DB_PATH", path), \
                mock.patch.object(scanner, "scan",
                                  side_effect=OSError("no space left on device")), \
                redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(OSError):
                cli.cmd_scan()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "",
                         "a scan that failed on a full disk said nothing at all")


class TestTheAdviceNamesACommandThatExists(_Streams):
    """Four of the five delivery surfaces have no usable `cli.py` to run.

    pip, Homebrew and the .vsix put a `codex-claude-usage` console script on PATH and
    ship no file the reader can point `python` at, so every "run: python cli.py
    scan" printed there named a command that does not exist -- in the messages
    that matter most, the ones printed when something has already gone wrong.
    The checkout is the one where it exists. The Docker image carries the
    package but not that wrapper, and a host-side command would target the wrong
    database anyway, so its recovery spelling enters the running container.
    """

    def _spelling(self, argv0):
        import safetext
        with mock.patch.object(sys, "argv", [argv0, "url"]):
            return safetext.invocation()

    def test_the_console_script_is_named_when_that_is_how_we_were_run(self):
        self.assertEqual(self._spelling("/usr/local/bin/codex-claude-usage"),
                         "codex-claude-usage")

    def test_the_homebrew_shim_is_believed_over_argv(self):
        """Homebrew is the surface `argv[0]` cannot answer for.

        Its shim runs `cli.py` through `runpy` having done
        `sys.argv = sys.argv[2:]`, so inside Python `argv[0]` is
        `<libexec>/cli.py` — indistinguishable from a git checkout. The advice
        therefore read "run: python cli.py scan" on an install where `cli.py`
        sits inside libexec and cannot be run at all, which is one of the three
        surfaces this whole helper exists for.

        The shim exports `CODEX_CLAUDE_USAGE_INVOKED_AS` because only it knows the
        name on PATH; `tests/test_brew_shim_env.py` holds it in UNCONDITIONAL.
        """
        import safetext
        env = dict(os.environ)
        env["CODEX_CLAUDE_USAGE_INVOKED_AS"] = "codex-claude-usage"
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(
                    sys, "argv",
                    ["/opt/homebrew/opt/codex-claude-usage/libexec/cli.py", "url"]):
            self.assertEqual(safetext.invocation(), "codex-claude-usage")

    def test_the_docker_spelling_targets_its_mounted_database(self):
        import safetext
        env = dict(os.environ)
        env.update({
            "CODEX_CLAUDE_USAGE_INVOKED_AS": "docker",
            "CODEX_CLAUDE_USAGE_DOCKER_CONTAINER": "usage-app_1",
        })
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "argv", ["cli.py", "url"]):
            self.assertEqual(
                safetext.invocation(),
                "docker exec usage-app_1 python3 -m codex_claude_usage.cli",
            )

    def test_an_unsafe_docker_container_name_is_never_rendered_as_a_command(self):
        import safetext
        env = dict(os.environ)
        env.update({
            "CODEX_CLAUDE_USAGE_INVOKED_AS": "docker",
            "CODEX_CLAUDE_USAGE_DOCKER_CONTAINER": "usage; touch /tmp/owned",
        })
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "argv", ["cli.py", "url"]):
            self.assertEqual(safetext.invocation(), "python cli.py")

    def test_an_unset_or_unknown_declaration_falls_back_to_argv(self):
        """Anti-vacuity, and the safety property: the variable is read from the
        environment, so an arbitrary value must not become the advice."""
        import safetext
        for declared in (None, "", "   ", "rm -rf /", "python cli.py"):
            with self.subTest(declared=declared):
                env = {k: v for k, v in os.environ.items()
                       if k != "CODEX_CLAUDE_USAGE_INVOKED_AS"}
                if declared is not None:
                    env["CODEX_CLAUDE_USAGE_INVOKED_AS"] = declared
                with mock.patch.dict(os.environ, env, clear=True), \
                        mock.patch.object(sys, "argv", ["cli.py", "url"]):
                    self.assertEqual(safetext.invocation(), "python cli.py")

    def test_the_checkout_spelling_survives_everywhere_else(self):
        for argv0 in ("cli.py", "/src/codex-claude-usage/cli.py",
                      "/usr/lib/python3.13/unittest/__main__.py"):
            with self.subTest(argv0=argv0):
                self.assertEqual(self._spelling(argv0), "python cli.py")

    def test_a_real_message_follows_the_invocation(self):
        """Anti-vacuity: the helper is only worth anything if the printed text
        actually uses it. Drive a real refusal both ways."""
        import dashboard
        with mock.patch.object(dashboard, "read_url_file", return_value=None):
            with mock.patch.object(sys, "argv", ["/usr/bin/codex-claude-usage", "url"]):
                _, _, script = self.run_command(cli.cmd_url)
            with mock.patch.object(sys, "argv", ["cli.py", "url"]):
                _, _, checkout = self.run_command(cli.cmd_url)
        self.assertIn("codex-claude-usage dashboard", script)
        self.assertNotIn("python cli.py", script)
        self.assertIn("python cli.py dashboard", checkout)

    def test_the_help_text_follows_it_too(self):
        self.assertIn("python cli.py scan", cli.USAGE,
                      "USAGE is the template and keeps the checkout spelling")
        with mock.patch.object(sys, "argv", ["/usr/bin/codex-claude-usage"]):
            rendered = cli.usage_text()
        self.assertIn("codex-claude-usage scan", rendered)
        self.assertNotIn("python cli.py", rendered)


class TestOneDefinitionOfTheLockedDatabaseMessage(_Streams):
    """A lock met during the OPEN and a lock met during the REPORT.

    Both are `sqlite3.OperationalError`, and they were answered by two
    hand-written copies -- the second three lines where the first was four. The
    line it dropped was the diagnosis, "The file itself looks fine", which is
    the entire substance of the change that introduced it: a reader who hits
    the lock one statement later was told to retry without being told the file
    is intact or what is likely holding it.

    Judged rather than assumed, because the two reviewers split on it. The
    dissent was that the read path also answers `no such table: turns` from
    inside another process's rebuild window, where "or it may be on a read-only
    mount" cannot apply -- true, and it is a hedged possibility beside SQLite's
    own words on the line above, while the sentence the second copy dropped is
    true on both paths. One definition, with the verb as the only parameter, is
    what makes a third copy fail here on the day it is written.
    """

    def test_the_two_paths_differ_only_in_the_verb(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)

        def locked(**kwargs):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(cli, "DB_PATH", tmp / "usage.db"):
            opened = cli.database_refusal(
                sqlite3.OperationalError("database is locked"))
            with mock.patch.dict(cli.COMMANDS, {"stats": locked}), \
                    mock.patch.object(cli.sys, "argv", ["cli.py", "stats"]):
                code, out, err = self.run_command(cli.main)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        read = err.rstrip("\n")
        self.assertIn("looks fine", read,
                      "the read path dropped the diagnosis again")
        self.assertEqual(opened.splitlines()[1:], read.splitlines()[1:],
                         "the two messages have drifted apart")
        self.assertEqual(opened.splitlines()[0].replace("open", "read", 1),
                         read.splitlines()[0],
                         "the first lines differ by more than the verb")


class TestOneDefinitionOfWhichDatabaseRefusalIsWhich(unittest.TestCase):
    """`cli.database_refusal` drives all three commands, so its ordering is too.

    The ordering used to live in `require_db`'s except-clause list, where a
    structural test could read it. It now lives in an `isinstance` chain, which
    no AST walk can judge -- so it is asserted by execution instead, which is
    the stronger check of the two: it fails on a wrong order AND on a wrong
    message.
    """

    def test_a_lock_is_not_answered_with_move_or_delete(self):
        """`sqlite3.OperationalError` is a SUBCLASS of `sqlite3.DatabaseError`.

        Tested second, so swapping the two `isinstance` calls makes this branch
        unreachable and tells a reader to throw away a database that another
        window was merely scanning.
        """
        message = cli.database_refusal(sqlite3.OperationalError("database is locked"))
        self.assertNotIn("Move or delete", message)
        self.assertIn("Try again", message)

    def test_a_damaged_file_is(self):
        message = cli.database_refusal(sqlite3.DatabaseError("file is not a database"))
        self.assertIn("Move or delete", message)

    def test_a_foreign_file_keeps_the_lines_db_gave_it(self):
        from db import ForeignDatabaseError
        message = cli.database_refusal(ForeignDatabaseError("one\n  two\n  three"))
        self.assertEqual(message, "one\n  two\n  three")

    def test_anything_else_is_not_claimed(self):
        """`None`, so a caller re-raises what this cannot label rather than
        printing a database remedy for a disk that filled up."""
        self.assertIsNone(cli.database_refusal(RuntimeError("disk gone")))

    def test_a_path_refusal_needs_no_private_guard_frames(self):
        refusal = db.UnsafeDatabasePathError("Refusing unsafe rebuild lock: test.lock")
        self.assertIsInstance(refusal, RuntimeError)
        message = cli.database_refusal(refusal)
        self.assertIn("Cannot use that usage database path", message)
        self.assertIn(str(refusal), message)


class _InlineThread:
    """A `threading.Thread` stand-in that runs its target on `start()`."""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class TestTheBackgroundScanExplainsItselfOnStderr(_Streams):
    """`dashboard`'s ingestion runs on a daemon thread, and it reported to stdout.

    Two defects in one line. It printed the failure on STDOUT, the stream this
    command uses for the authenticated URL a reader is expected to copy; and it
    routed the exception through `terminal_safe`, which escapes Cc -- so
    `db.ForeignDatabaseError`, whose whole value is a seven-line remedy, arrived
    as one `\\x0a`-run. Measured against a foreign database before this changed:
    the whole remedy on one line of stdout, stderr empty, `/api/data` failing for
    the life of the process, and exit 0 on Ctrl-C.
    """

    def _dashboard_with_a_failing_scan(self, failure):
        def fake_serve(**kwargs):
            kwargs["on_ready"]()

        with mock.patch.object(cli, "cmd_scan", side_effect=failure), \
                mock.patch("threading.Thread", _InlineThread), \
                mock.patch("dashboard.serve", fake_serve):
            return self.run_command(cli.cmd_dashboard, host="127.0.0.1",
                                    port=8123, no_browser=True)

    def test_the_background_scan_does_not_bury_the_url_in_paths(self):
        """`cli.py dashboard`'s ingestion ran at the default `verbose=True`.

        Keep rebuild diagnostics on stderr so stdout remains the authenticated
        dashboard URL. Exercise both fresh and existing databases.

        `dashboard._background_scan`, the OTHER way into the same thread,
        already passed `verbose=False`; the two entry points simply disagreed.
        The foreground `cli.py scan` stays verbose on purpose -- it is the
        command the reader typed, and showing its work is the point.
        """
        import dashboard
        import json
        import scanner
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        proj = root / "projects" / "p"
        proj.mkdir(parents=True)
        (proj / "s.jsonl").write_text(json.dumps({
            "type": "assistant", "sessionId": "s", "uuid": "u",
            "timestamp": "2026-08-01T12:00:00.000Z", "cwd": "/w/p",
            "gitBranch": "main",
            "message": {"id": "m1", "model": "claude-opus-4-8",
                        "usage": {"input_tokens": 1, "output_tokens": 1}},
        }) + "\n", encoding="utf-8")
        db = root / "usage.db"

        def fake_serve(**kwargs):
            kwargs["on_ready"]()

        with mock.patch.object(cli, "DB_PATH", db), \
                mock.patch.object(scanner, "DB_PATH", db), \
                mock.patch.object(scanner, "DEFAULT_PROJECTS_DIRS", []), \
                mock.patch.object(dashboard, "DB_PATH", db), \
                mock.patch.dict(
                    os.environ, {scanner.EXTRA_PROJECTS_DIRS_ENV: ""}), \
                mock.patch("threading.Thread", _InlineThread), \
                mock.patch("dashboard.serve", fake_serve):
            _, out, _ = self.run_command(
                cli.cmd_dashboard, host="127.0.0.1", port=8123,
                no_browser=True, projects_dirs=[str(proj)])
        self.assertNotIn(
            str(proj), out,
            "a transcript path reached the stream that carries the URL")

    def test_the_command_the_reader_typed_still_shows_its_work(self):
        """The control: quieting the background thread must not quiet `scan`."""
        import inspect
        source = inspect.getsource(cli.cmd_scan)
        self.assertIn("verbose=True", source.split("\n")[0] + "verbose=True"
                      if "verbose" in source.split("\n")[0] else source,
                      "cmd_scan should still default to verbose")
        self.assertEqual(
            inspect.signature(cli.cmd_scan).parameters["verbose"].default, True)

    def test_an_ordinary_failure_goes_to_stderr(self):
        _, out, err = self._dashboard_with_a_failing_scan(RuntimeError("disk gone"))
        self.assertIn("Background scan failed", err)
        self.assertIn("disk gone", err)
        self.assertNotIn("Background scan failed", out)

    def test_a_refused_database_says_which_half_of_the_process_stopped(self):
        """`cmd_scan` has already written the reason to stderr and asked to
        exit; on a daemon thread that ends the thread and nothing else, so the
        server goes on serving. Saying so is the stated limit, not a fix."""
        _, out, err = self._dashboard_with_a_failing_scan(SystemExit(1))
        self.assertIn("Background scan stopped", err)
        self.assertIn("still serving", err)
        self.assertNotIn("Background scan stopped", out)

    def _dashboard_over_a_refused_database(self, failure):
        """The REAL `cmd_scan` on the thread, with the database refused under it.

        The sibling helper patches `cmd_scan` itself, which can only deliver a
        bare `SystemExit`; what the branch below keys on is the exception
        `cmd_scan` actually raises, so this one leaves `cmd_scan` in place and
        fails the scan beneath it.
        """
        def fake_serve(**kwargs):
            kwargs["on_ready"]()

        with mock.patch("scanner.scan", side_effect=failure), \
                mock.patch("threading.Thread", _InlineThread), \
                mock.patch("dashboard.serve", fake_serve):
            return self.run_command(cli.cmd_dashboard, host="127.0.0.1",
                                    port=8123, no_browser=True)

    def test_only_a_lock_is_told_the_page_still_holds_something(self):
        """The reassurance is true of exactly one of the three refusals.

        A lock clears; the database behind the page is intact and readable the
        moment the other process lets go. The other two do not, and the line
        was printed for all three until 2026-08-16. Measured that day against a
        foreign `CODEX_CLAUDE_USAGE_DB`, through a real server on a real socket with
        a real token: `GET /api/data` and `GET /api/sources` both answered
        `500 {"error": "Failed to read the usage database"}`, again on the next
        request, while `/` and `/healthz` answered 200 — the page loads and can
        never fill.
        """
        _, out, err = self._dashboard_over_a_refused_database(
            sqlite3.OperationalError("database is locked"))
        self.assertIn("whatever the database already held", err)
        self.assertNotIn("whatever the database already held", out)

    def test_a_rebuild_window_is_not_told_the_page_still_holds_something(self):
        """The one case where `_is_a_lock` and `db.a_retry_could_succeed`
        must DISAGREE, and until this test nothing held them apart.

        `no such table: turns` is another process sitting between the DROP and
        the CREATE of a schema rebuild (invariant 6). Two different questions
        get two different answers about it:

        * the PAGE should retry -- the rebuild finishes and the next request
          succeeds, which is what `db.a_retry_could_succeed` says; and
        * the TERMINAL must not reassure -- the tables really are dropped, so
          "still serving whatever the database already held" is false, which is
          what `_is_a_lock` says.

        Collapsing the two predicates into one is the tempting simplification:
        they read alike, and one of them is newer. Measured by mutation before
        this test existed -- delegating `_is_a_lock` to `a_retry_could_succeed`
        left `test_cli_streams`, `test_dashboard` and the new notice suite all
        green, while making this branch print the same false reassurance that
        round 19 removed for the foreign case and 2026-08-16 removed for the
        read-only one. That is the third time this sentence has had to be
        reclaimed, so this time it is asserted.
        """
        from db import a_retry_could_succeed
        rebuilding = sqlite3.OperationalError("no such table: turns")
        self.assertTrue(a_retry_could_succeed(rebuilding),
                        "the page must keep asking: the rebuild finishes")
        self.assertFalse(cli._is_a_lock(rebuilding),
                         "the terminal must not claim the data is still there")
        _, out, err = self._dashboard_over_a_refused_database(rebuilding)
        self.assertNotIn("whatever the database already held", err)
        self.assertNotIn("whatever the database already held", out)

    def test_a_database_that_will_not_clear_says_the_page_is_broken(self):
        """The two `OperationalError` cases are the ones this class was missing.

        `transient` was `isinstance(exc, sqlite3.OperationalError)` -- a wider
        set than the sentence it gates, because `attempt to write a readonly
        database` and `unable to open database file` are in that family and
        neither clears itself. A read-only `~/.claude` therefore got the
        reassuring line while every data request answered 500. The class read
        only `foreign` and `not a database`, so the two members that separate
        the class test from the lock test were exactly the two absent from it.
        """
        from db import ForeignDatabaseError
        cases = {
            "foreign": ForeignDatabaseError("one\n  two"),
            "not a database": sqlite3.DatabaseError("file is not a database"),
            "read-only": sqlite3.OperationalError(
                "attempt to write a readonly database"),
            "cannot open": sqlite3.OperationalError(
                "unable to open database file"),
        }
        for label, failure in cases.items():
            with self.subTest(refusal=label):
                _, out, err = self._dashboard_over_a_refused_database(failure)
                self.assertIn("Background scan stopped", err)
                self.assertNotIn("whatever the database already held", err,
                                 "promised a page that answers 500 to everything")
                self.assertIn("every data request will fail", err)

    def test_cmd_scan_is_what_raises_that_system_exit(self):
        """The coupling the branch above depends on, asserted rather than
        assumed: `cmd_scan` prints the refusal itself and exits, so what reaches
        the thread is a `SystemExit` and not the original exception."""
        from db import ForeignDatabaseError
        with mock.patch("scanner.scan",
                        side_effect=ForeignDatabaseError("one\n  two")):
            code, out, err = self.run_command(cli.cmd_scan)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, "one\n  two\n")

    def test_the_other_entry_points_own_scan_keeps_the_lines_too(self):
        """`dashboard._background_scan` is the same thread on the OTHER way in.

        It has no `cmd_scan` in front of it -- it calls `scanner.scan` directly
        -- so it carried its own copy of the `terminal_safe` fold, and a
        `CODEX_CLAUDE_USAGE_DB` pointed at somebody else's SQLite file is exactly what
        this thread finds out about. Its stream stays stdout: unlike `cli.py
        dashboard`, this entry point prints no URL for a reader to copy, and
        `test_a_failing_scan_does_not_take_the_server_down_silently` in
        `tests/test_port_in_use.py` reads that stream.
        """
        import dashboard
        from db import ForeignDatabaseError
        with mock.patch("scanner.scan",
                        side_effect=ForeignDatabaseError("one\n  two")):
            _, out, _ = self.run_command(dashboard._background_scan)
        self.assertIn("one\n  two", out)
        self.assertNotIn("\\x0a", out)


class TestALossyScanDoesNotNameTheFileUnasked(_Streams):
    """EGRESS-2. The sibling of the warning the class above quieted.

    "skipped N unreadable record(s) in <absolute path>" went to STDOUT and was
    gated on nothing, so the one code path that deliberately asks for silence --
    `cmd_dashboard`'s background scan, and `/api/rescan`, both `verbose=False`
    -- still emitted the reader's project topology, one line per damaged file.
    Its immediate sibling three lines above, the read-error warning in the same
    function, had already been moved to stderr and this one was left behind.

    Two rules, and stderr alone is NOT enough for the second: the extension
    appends `[server:err]` lines to the same output channel it directs users to
    open and share, and `docker logs` captures both streams, so the path has to
    be withheld rather than merely rerouted.

    Both parsers, because a rule that held on one of them would be a hole shaped
    like whichever assistant the reader happens to use.
    """

    def _lossy(self, kind):
        """A transcript whose second record is a torn write, and its directory."""
        import json
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        proj = root / "projects" / "-Users-victim-Developer-secret-client"
        proj.mkdir(parents=True)
        if kind == "claude":
            good = json.dumps({
                "type": "assistant", "sessionId": "s", "uuid": "u",
                "timestamp": "2026-08-01T12:00:00.000Z", "cwd": "/w/p",
                "gitBranch": "main",
                "message": {"id": "m1", "model": "claude-opus-4-8",
                            "usage": {"input_tokens": 5, "output_tokens": 7}}})
            name = "s.jsonl"
            # A torn assistant record: decodes to nothing, costs a whole
            # API response.
            torn = '{"type": "assistant", "messa'
        else:
            good = json.dumps({
                "type": "session_meta",
                "payload": {"id": "t-1", "timestamp": "2026-08-01T12:00:00.000Z"}})
            name = "rollout-2026-08-01T12-00-00-019fcf22aaaa.jsonl"
            # It has to carry a `_INTERESTING` token to reach the decoder at
            # all: the Codex prefilter drops a line it was never going to read,
            # deliberately, so a rollout full of prose cannot warn about prose.
            # `token_count` is also the record that actually carries usage.
            torn = '{"type": "token_count", "payload": {"total_token_usa'
        path = proj / name
        path.write_text(good + "\n" + torn, encoding="utf-8")
        return proj, path

    def test_a_quiet_scan_reports_the_loss_without_naming_the_file(self):
        import scanner
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind):
                proj, path = self._lossy(kind)
                db = path.parent.parent.parent / "usage.db"
                _, out, err = self.run_command(
                    scanner.scan, projects_dirs=[proj], db_path=db,
                    verbose=False)
                self.assertIn("unreadable record", err,
                              "the loss must still be reported")
                self.assertNotIn(str(path), out + err,
                                 "the transcript path reached a captured stream")
                self.assertNotIn(proj.name, out + err,
                                 "the project directory name leaked")

    def test_the_command_the_reader_typed_still_names_it(self):
        """The control. Withholding it from a log nobody asked for must not
        withhold it from `cli.py scan`, where knowing WHICH file is the point."""
        import scanner
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind):
                proj, path = self._lossy(kind)
                db = path.parent.parent.parent / "usage.db"
                _, out, err = self.run_command(
                    scanner.scan, projects_dirs=[proj], db_path=db,
                    verbose=True)
                self.assertIn(str(path), err, "a verbose scan must name it")
                self.assertNotIn(
                    "unreadable record", out,
                    "the warning belongs on stderr on every path")

    def test_neither_parser_puts_the_warning_on_stdout(self):
        """Directly on the parsers, so the rule is pinned where it is written
        rather than only through the one caller that passes the flag."""
        import codex_transcripts
        import transcripts
        for kind, parser in (("claude", transcripts.parse_jsonl_file),
                             ("codex", codex_transcripts.parse_jsonl_file)):
            for verbose in (True, False):
                with self.subTest(kind=kind, verbose=verbose):
                    _proj, path = self._lossy(kind)
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), contextlib.redirect_stderr(err):
                        parser(path, verbose=verbose)
                    self.assertEqual(out.getvalue(), "")
                    self.assertIn("unreadable record", err.getvalue())
                    self.assertEqual(str(path) in err.getvalue(), verbose)


class TestTheMissingDatabaseRefusalNamesTheFile(_Streams):
    """The one refusal in `require_db` that did not say which file it meant.

    "Database not found. Run: python cli.py scan" is credible for a machine
    that has never scanned and misleading for a typo'd `CODEX_CLAUDE_USAGE_DB` -- and
    following its instruction literally then CREATES a second database at the
    typo'd path, leaving the real history untouched and unreachable with
    nothing on either stream naming either file. Every sibling refusal in the
    same function interpolates the path.
    """

    def test_it_names_the_path_it_looked_at(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "typo" / "usage.db"
        with mock.patch.object(cli, "DB_PATH", path):
            code, out, err = self.run_command(cli.cmd_stats)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn(str(path), err)
        self.assertIn("cli.py scan", err)


class TestTheMissingRootWarningIsADiagnostic(_EntryPoint):
    """`scanner`'s two warnings about roots, on the stream a diagnostic belongs on.

    `cli.scan_roots_for` was added with its copy of the missing-root line
    already on stderr and `scanner._scan_unlocked`'s two were left where they
    were, so ONE `cli.py scan` reported an absent `CODEX_CLAUDE_USAGE_PROJECTS_DIRS`
    root on both streams at once -- `scan_roots_for` hands on only the roots
    that exist, but `scanner.resolve_scan_roots` re-reads the environment for
    itself. The same stdout copy landed in `cli.py dashboard`'s stdout, beside
    the authenticated URL a reader is told to copy.

    A stated limit, not a fix: the environment root is still reported twice for
    one `cli.py scan` -- measured 2026-08-16, two lines on stderr and none on
    stdout. Neither copy can be removed on the strength of the other: this one
    is the only report for `python scanner.py` and `python dashboard.py`, and
    the CLI's is the only report for the roots it filters out before dispatch.
    """

    def test_an_absent_environment_root_keeps_stdout_clean(self):
        # The EXPECTED RENDERING, not the POSIX spelling. Both warning copies
        # print `terminal_safe(d)` where `d` is a `pathlib.Path`, so on Windows
        # the child emits `\\nope\\env-root` and a hard-coded "/nope/env-root"
        # appears nowhere in its output -- a Windows-only red, on the one leg
        # `tag-on-merge.yml` refuses to release without. Round-tripping the
        # value through `Path` here makes the test assert what the code prints
        # on whatever platform is running it.
        root = str(Path("/nope/env-root"))
        code, out, err = self.entry_point(
            "cli.py", "scan",
            extra_env={"CODEX_CLAUDE_USAGE_PROJECTS_DIRS": root})
        self.assertIn(code, (None, 0), f"the scan itself failed: {err!r}")
        self.assertIn(root, err)
        self.assertNotIn("Warning:", out,
                         "a diagnostic landed in the report output")
        # The stated limit above, made executable: if the duplicate is ever
        # removed this fails, and the paragraph that describes it has to go too.
        self.assertEqual(err.count(root), 2,
                         "the two copies of the warning have changed in number "
                         "-- update the class docstring with them")

    def test_no_roots_at_all_keeps_stdout_clean_too(self):
        """The sibling line, which the same round left on stdout beside it.

        In process, because the state is "every root is missing" and the child
        harness deliberately creates one.
        """
        import scanner
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), contextlib.redirect_stderr(err):
            scanner.scan(db_path=tmp / "usage.db",
                         projects_dirs=[tmp / "absent"], verbose=False)
        self.assertIn("no transcript directories to scan", err.getvalue())
        self.assertNotIn("Warning:", out.getvalue())


class TestTheEntryPointsSayWhatTheyDidWithTheArguments(_EntryPoint):
    """Two siblings, one class, because the asymmetry between them WAS the bug.

    `dashboard.py` was given a guard that rejects argv, and its message said
    `Ignoring:` while the code beneath it exited 1 without binding anything --
    the opposite of what it did, and credible precisely because "ignoring" is
    the behaviour the previous release actually had. A reader told the argument
    was ignored browses to the default port and finds nothing listening.

    `scanner.py` was not given the guard at all and kept its own parser: it
    matched one spelling of one flag (`--projects-dir=/x` was dropped), took no
    value when none followed, could not express the repeatable form `cli.py
    scan` accepts, and discarded every other token at exit 0. Measured before
    this changed: `python scanner.py --porjects-dir /nope --source codex`
    scanned the defaults and printed an ordinary `Scan complete:` block.
    """

    CASES = {
        "dashboard.py": ("--port", "9000"),
        "scanner.py": ("--porjects-dir", "/nope", "--source", "codex"),
    }

    POINTS_AT = {"dashboard.py": "cli.py dashboard", "scanner.py": "cli.py scan"}

    def test_neither_claims_to_have_ignored_what_it_refused(self):
        for module, argv in self.CASES.items():
            with self.subTest(module=module):
                code, _, err = self.entry_point(module, *argv)
                self.assertEqual(code, 1)
                self.assertNotIn("ignor", err.lower(),
                                 "the message says it carried on; it did not")
                self.assertIn("Refused:", err)
                self.assertIn("Nothing was", err)

    def test_neither_does_the_work_it_refused_to_configure(self):
        for module, argv in self.CASES.items():
            with self.subTest(module=module):
                _, out, _ = self.entry_point(module, *argv)
                self.assertEqual(out, "")

    def test_each_points_at_the_entry_point_that_does_parse(self):
        for module, argv in self.CASES.items():
            with self.subTest(module=module):
                _, _, err = self.entry_point(module, *argv)
                self.assertIn(self.POINTS_AT[module], err)

    def test_the_bare_form_of_the_scanner_still_scans(self):
        """Anti-vacuity, and it is not a formality: a guard that refused
        everything would satisfy every assertion above while deleting the entry
        point. Against an isolated HOME the roots are empty, so this asserts the
        scan RAN, not what it found."""
        code, out, _ = self.entry_point("scanner.py")
        self.assertEqual(code, 0)
        self.assertIn("Scan complete", out)


class TestARefusalPutsNothingOnStdout(_Streams):
    """This file's rule, asserted AS a rule instead of one command at a time.

    The rule is the first line of the module docstring: stdout is the answer the
    command was asked for; everything else is stderr. Every test in this file
    asserts an instance of it, and an instance is not the rule — a diagnostic
    moved back to stdout on a path nobody wrote a case for breaks the contract
    with nothing going red.

    That is not hypothetical. Six helpers across `tests/` merge stderr INTO the
    stdout buffer — `test_dashboard.TestUrlFileRecovery._run_cmd_url`,
    `TestCliRejectsArgumentsItWouldDrop._run`, two in `test_cli.py`, one in
    `test_port_in_use.py` and one in `test_cli_reports.py` — each with a comment
    saying the merge is deliberate because "these assertions are about the
    MESSAGE, not the stream". Each of those comments is defensible on its own;
    together they meant the STREAM was pinned by almost nothing. Reported by
    sweeping cli.py's `file=sys.stderr` sites one at a time: five could be
    reverted to `print(...)` with the full suite green, including both lines of
    `url`'s `url_is_live is None` branch — byte for byte the break the commit
    that wrote this rule exists to remove.

    Re-run against this class on 2026-08-16, same sweep, one site at a time and
    `tests.test_cli_streams` alone as the judge: **18 of cli.py's 20 sites now
    red it**. The two that do not are the scan-root warning and the `--source`
    value error, and both are caught by `tests.test_cli` — checked
    module-by-module rather than assumed, because five modules named on one
    `unittest` command line load as a single failing test and print `Ran 1`,
    which reads exactly like a pass being counted.

    Derived from `cli.COMMANDS` and `cli.COMMAND_FLAGS` rather than listed, so a
    seventh command is covered the day it is added, which is the other half of
    what "as a rule" has to mean.

    Its own limits, stated rather than implied: it walks the argument refusals,
    a busy port, `url`'s three failure paths and `require_db`'s two notices. It
    says nothing about the refusals that need a damaged or foreign database —
    the classes above own those — and nothing about the happy paths of
    `today`/`week`/`stats`, whose stdout IS the report.
    """

    def main_with_no_command_able_to_run(self, argv):
        """(exit code, stdout, stderr) from `cli.main`, commands stubbed out.

        Stubbed because the point is what the argument layer writes, not what a
        report would print; an unstubbed `today` would put a whole report on the
        stdout this class asserts is empty.
        """
        ran = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(
                cli.COMMANDS, {n: (lambda *a, **k: ran.append(1))
                               for n in cli.COMMANDS}))
            for name in ("cmd_dashboard", "cmd_scan", "cmd_url"):
                stack.enter_context(mock.patch.object(
                    cli, name, lambda *a, **k: ran.append(1)))
            stack.enter_context(mock.patch.object(
                cli.sys, "argv", ["cli.py"] + list(argv)))
            code, out, err = self.run_command(cli.main)
        return code, out, err

    def test_no_command_puts_its_argument_refusal_on_stdout(self):
        """A flag no command reads, walked over every command there is."""
        for command in sorted(cli.COMMANDS):
            with self.subTest(command=command):
                code, out, err = self.main_with_no_command_able_to_run(
                    [command, "--definitely-not-a-flag"])
                self.assertEqual(code, 1)
                self.assertEqual(out, "", "the refusal reached stdout")
                self.assertNotEqual(err.strip(), "",
                                    "anti-vacuity: two silent streams are equal too")

    def test_a_dangling_value_flag_is_refused_on_stderr_too(self):
        """The other shape of the same mistake: a flag whose value is missing.

        Derived from `COMMAND_FLAGS` so it covers whatever each command actually
        takes, and skipped for a command declaring only bare switches, which
        have no dangling form.
        """
        for command, flags in sorted(cli.COMMAND_FLAGS.items()):
            takes_value = [f for f, kind in flags.items() if kind != "flag"]
            if not takes_value:
                continue
            with self.subTest(command=command, flag=takes_value[0]):
                code, out, err = self.main_with_no_command_able_to_run(
                    [command, takes_value[0]])
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertNotEqual(err.strip(), "")

    def test_an_unknown_command_is_refused_on_stderr(self):
        code, out, err = self.main_with_no_command_able_to_run(["stat"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "", "the usage banner reached stdout")
        self.assertIn("stat", err)

    def test_a_busy_port_puts_its_whole_remedy_on_stderr(self):
        """The fourth refusal `main` answers, and the one with the most to say.

        `serve` calls `on_ready` only after the bind succeeds, so on this path
        nothing has reached stdout yet — which is exactly why a remedy printed
        there would be the only thing a reader piping this command sees.
        """
        import dashboard
        lines = ["Port 8080 is already in use.", "  Try: python cli.py url --open"]
        failure = dashboard.PortInUseError("127.0.0.1", 8080, lines)
        with mock.patch.object(dashboard, "serve", side_effect=failure), \
                mock.patch.object(cli.sys, "argv",
                                  ["cli.py", "dashboard", "--no-browser"]):
            code, out, err = self.run_command(cli.main)
        self.assertEqual(code, 1)
        self.assertEqual(out, "", "the remedy reached stdout")
        for line in lines:
            self.assertIn(line, err)

    def test_a_valid_invocation_still_reaches_its_command(self):
        """Anti-vacuity for the walk above: `main` can still run something.

        A `main` that refused everything satisfies all three assertions above,
        and this is what fails on it.
        """
        ran = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(
                cli.COMMANDS, {n: (lambda *a, **k: ran.append(n))
                               for n in cli.COMMANDS}))
            stack.enter_context(mock.patch.object(
                cli.sys, "argv", ["cli.py", "stats"]))
            code, out, err = self.run_command(cli.main)
        self.assertEqual(len(ran), 1, f"stats did not run: {err!r}")
        self.assertIn(code, (None, 0))

    def test_a_missing_database_is_refused_on_stderr(self):
        """`require_db`'s other two notices, which the sweep also left unpinned.

        This one is a refusal like the four above: nothing to report, so nothing
        on stdout.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.object(cli, "DB_PATH", Path(tmp) / "absent.db"):
            code, out, err = self.run_command(cli.cmd_stats)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("cli.py scan", err)

    def test_the_empty_database_notice_does_not_contaminate_the_report(self):
        """The one case where stdout is NOT empty, and the rule still decides it.

        An empty database is a state rather than a refusal — zeroes are the
        correct answer for a machine with no transcripts — so `stats` reports
        normally and exits 0. The notice beside it is a diagnostic, and
        `require_db`'s docstring makes that a contract in as many words: "stdout
        stays exactly what it was, which is what keeps a piped report honest".
        Nothing asserted the stream until this test.
        """
        from db import get_db, init_db
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = Path(tmp) / "usage.db"
        conn = get_db(path)
        init_db(conn, path)
        conn.commit()
        conn.close()
        with mock.patch.object(cli, "DB_PATH", path):
            code, out, err = self.run_command(cli.cmd_stats)
        self.assertIn(code, (None, 0))
        self.assertIn("Total turns:", out, "the report itself went missing")
        self.assertIn("holds no turns", err)
        self.assertNotIn("holds no turns", out,
                         "the notice landed in the piped report")

    def test_every_failure_path_of_url_keeps_stdout_empty(self):
        """`url` is the command the rule was written for, so walk all three.

        `open "$(python cli.py url)"` is the documented use, so a line of
        English on stdout is a link a launcher will try to open. The
        `url_is_live is None` branch is the one the sweep found revertible with
        the suite green; the other two are its siblings and were not covered as
        stream assertions either.
        """
        import dashboard
        cases = {
            "no file at all": dict(read=[None]),
            "probe did not finish": dict(read=["http://127.0.0.1:8080/#t"],
                                         live=None),
            "link changed under the probe": dict(
                read=["http://127.0.0.1:8080/#t", "http://127.0.0.1:9999/#u"],
                live=False),
        }
        for label, spec in cases.items():
            with self.subTest(path=label):
                with mock.patch.object(dashboard, "read_url_file",
                                       side_effect=spec["read"]), \
                        mock.patch.object(dashboard, "remove_url_file"), \
                        mock.patch.object(dashboard, "url_is_live",
                                          return_value=spec.get("live")):
                    code, out, err = self.run_command(cli.cmd_url)
                self.assertEqual(code, 1)
                self.assertEqual(out, "",
                                 "a launcher substituting this would open the message")
                self.assertNotEqual(err.strip(), "")

    def test_a_live_link_is_the_one_thing_stdout_does_carry(self):
        """The control the three above need: stdout is not simply always empty."""
        import dashboard
        url = "http://127.0.0.1:8080/#token"
        with mock.patch.object(dashboard, "read_url_file", return_value=url), \
                mock.patch.object(dashboard, "url_is_live", return_value=True):
            code, out, err = self.run_command(cli.cmd_url)
        self.assertIn(code, (None, 0))
        self.assertEqual(out, url + "\n", "stdout is the URL and nothing else")
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
