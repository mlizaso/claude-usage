"""`cli.require_db` — the two things it does before any read command sees a row.

`today`, `week` and `stats` all open the database through this one function, and
two of its behaviours were asserted by nothing at all. Both were measured on
2026-08-15 by mutating `cli.py` and running the whole suite, which stayed green
for the pair of them together.

**The missing-database diagnostic goes to stderr.** Put it back on stdout and
`claude-usage stats > report.txt` on a machine that has never scanned writes
"Database not found. Run: python cli.py scan" into the report file, where the
reader finds a sentence instead of a table and the terminal shows nothing at
all. That is the same rule `TestTheScanRootWarningReachesOnlyTheCommandsThatScan`
pins one function further down in tests/test_cli.py — stdout is the report, and
a diagnostic is not part of it.

**The connection waits `db.BUSY_TIMEOUT_MS` for a lock.** `require_db` calls
`init_db`, which can hold the write lock for a whole rebuild, and this product
creates concurrent openers by design: a dashboard per VS Code window, each with
a background scan, plus `cli.py scan` in a terminal. sqlite3's default five
seconds is shorter than one `/api/data` rollup on a real database, so without
the PRAGMA the losing reader does not wait — it raises `database is locked` at
someone who typed `stats`.

Nothing here re-tests the refusal contract: that is
`TestCliReadCommandsRefuseToReportOnARebuiltDatabase` in
tests/test_cli_subagent.py, and the foreign-file refusal is in
tests/test_foreign_database.py.
"""

import contextlib
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import cli
from db import BUSY_TIMEOUT_MS, get_db, init_db


def _run_require_db():
    """Call it the way a command does, returning (exit code, stdout, stderr).

    The connection is returned too when there is one, so the caller can read the
    settings it was opened with. `SystemExit` is caught because on this path it
    is the contract rather than an accident.
    """
    out, err = io.StringIO(), io.StringIO()
    code, conn = None, None
    with redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            conn = cli.require_db()
        except SystemExit as exc:
            code = 1 if exc.code is None else exc.code
    return code, out.getvalue(), err.getvalue(), conn


class TestThereIsNoDatabaseAtAll(unittest.TestCase):
    """The first thing `require_db` checks, and the first thing a new user hits.

    Not an error case in the usual sense: a machine that has never run `scan` is
    the ordinary state of a fresh install, so the answer has to be an
    instruction rather than a traceback — and it has to arrive somewhere a
    redirected report will not swallow.
    """

    def setUp(self):
        self._orig = cli.DB_PATH
        cli.DB_PATH = Path(tempfile.mkdtemp()) / "nothing-here.db"
        self.assertFalse(cli.DB_PATH.exists(), "the fixture created the file")

    def tearDown(self):
        cli.DB_PATH = self._orig

    def test_it_exits_one(self):
        code, _, _, _ = _run_require_db()
        self.assertEqual(code, 1)

    def test_the_instruction_is_on_stderr(self):
        _, _, err, _ = _run_require_db()
        self.assertIn("Database not found", err)
        self.assertIn("cli.py scan", err)

    def test_stdout_stays_empty(self):
        """The whole point of the stream it is on. A shell redirect captures
        stdout, so a diagnostic printed there IS the report on the one machine
        where there is nothing else to print."""
        _, out, _, _ = _run_require_db()
        self.assertEqual(out, "")


class TestTheConnectionWaitsForAConcurrentRebuild(unittest.TestCase):
    """`require_db` opens with a bare `sqlite3.connect`, so it inherits none of
    `get_db`'s settings and has to set this one itself."""

    def setUp(self):
        self._orig = cli.DB_PATH
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(self.db_path)
        try:
            init_db(conn, self.db_path)
            conn.commit()
        finally:
            conn.close()
        cli.DB_PATH = self.db_path

    def tearDown(self):
        cli.DB_PATH = self._orig

    def test_the_connection_carries_the_products_busy_timeout(self):
        code, out, _, conn = _run_require_db()
        self.assertIsNone(code, f"a current database must not refuse: {out!r}")
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0],
                         BUSY_TIMEOUT_MS)
        self.assertGreater(BUSY_TIMEOUT_MS, 5000,
                           "5000 is the default this exists to replace")


if __name__ == "__main__":
    unittest.main()
