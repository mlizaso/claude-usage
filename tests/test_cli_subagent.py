"""Tests for the CLI subagent summary lines in `today` and `stats`."""

import contextlib
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import cli
from scanner import get_db, init_db, insert_turns, upsert_sessions
from tests.legacy_database import create_schema as create_legacy_schema
from tests.timestamps import utc_ts_on_local_day


def _turn(message_id, inp, out, is_subagent, agent_id, ts):
    return {
        "session_id": "sess-1", "timestamp": ts, "model": "claude-opus-4-8",
        "input_tokens": inp, "output_tokens": out,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "tool_name": None, "cwd": "/home/user/proj",
        "message_id": message_id, "is_subagent": is_subagent, "agent_id": agent_id,
    }


class TestCliSubagentLines(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        today_ts = utc_ts_on_local_day()
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-1", "project_name": "user/proj",
            "first_timestamp": today_ts, "last_timestamp": today_ts,
            "git_branch": "main", "model": "claude-opus-4-8",
            "total_input_tokens": 400, "total_output_tokens": 130,
            "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 2,
        }])
        insert_turns(conn, [
            _turn("m-main", 100, 50, 0, None, today_ts),
            _turn("m-sub", 300, 80, 1, "agent-1", today_ts),
        ])
        conn.commit()
        conn.close()
        self._orig_db = cli.DB_PATH
        cli.DB_PATH = self.db_path

    def tearDown(self):
        cli.DB_PATH = self._orig_db

    def test_today_shows_subagent_tokens(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_today()
        out = buf.getvalue()
        self.assertIn("Subagent tokens:", out)
        # 300 + 80 = 380 subagent tokens, 1 turn
        self.assertIn("(1 turns)", out)

    def test_stats_shows_subagent_turns_and_tokens(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_stats()
        out = buf.getvalue()
        self.assertIn("Subagent turns:", out)
        self.assertIn("Subagent tokens:", out)



class TestCliReadCommandsRefuseToReportOnARebuiltDatabase(unittest.TestCase):
    """A database created before the subagent columns existed must make the read
    commands REFUSE, not report.

    The contract this class pins was inverted by the removal of the migrations,
    and the old name for it — "survive an old schema" — is exactly the reading
    that made the defect invisible. `require_db` calls `init_db` on open, which
    now REBUILDS a database whose schema does not match rather than migrating it
    (AGENTS.md invariant 6). The pre-existing row does not survive that; it is
    re-derived by the next scan from the transcripts it came from. So a `today`
    that renders a complete, correct report of an empty database is **not** the
    success case. It is the failure: a reviewer reproduced `cli.py stats`
    against a real older database printing `Total turns: 0`,
    `Est. total cost: $0.0000` and exiting 0 — a confidently wrong final answer,
    under a stderr notice that had just promised "the next scan re-reads every
    transcript", on a path where there is no next scan.

    What is asserted instead, for every read command: **exit 1, nothing at all on
    stdout, and both the rebuild notice and the recovery instruction on stderr.**
    Empty stdout specifically rather than "no numbers", because a header printed
    over nothing is the same lie in a smaller font — and because `today` is what
    a terminal pipes.

    **A REBUILD and an EMPTY DATABASE are two conditions, and only the first
    refuses.** That distinction is load-bearing and was learned the hard way:
    refusing on emptiness alone reds `TestCliReportsWithNoData`, whose contract
    — "an empty (but valid) DB must report emptiness, not crash or invent rows"
    — predates all of this and is right, because zeroes are the correct answer
    for a machine with no transcripts. What both conditions share is the notice
    on stderr, and it is UNCONDITIONAL rather than tied to the rebuild: keyed to
    the rebuild it was exactly one invocation wide, and the next command printed
    a confident `$0.0000` in silence.

    `cmd_scan` is deliberately not in this class. It is the one read-free caller,
    and a rebuild there is the ordinary case rather than a refusal: an emptied
    `processed_files` is what makes the very next scan re-read every transcript.
    """

    def setUp(self):
        self.today_ts = utc_ts_on_local_day()
        self.db_path = self._old_schema_database()
        self._orig_db = cli.DB_PATH
        cli.DB_PATH = self.db_path

    def tearDown(self):
        cli.DB_PATH = self._orig_db

    def _old_schema_database(self):
        """A fresh database on a schema this build does not write.

        Built per command, not once per class, and that is not fixture hygiene
        -- it is what the product does. The refusal is ONE-SHOT by construction:
        the first open rebuilds the file, so by the second command the schema
        matches and nothing refuses. Each real invocation is its own process
        against its own first open, and sharing one file across three commands
        in one test tested `today` and then tested nothing.
        """
        import sqlite3
        db_path = Path(tempfile.mkdtemp()) / "usage.db"
        today_ts = self.today_ts
        # A complete released schema this build does not write: `turns` has no
        # is_subagent/agent_id and there is no `agents` table. A partial
        # hand-made `turns` table is intentionally not ownership evidence.
        conn = sqlite3.connect(db_path)
        create_legacy_schema(conn)
        conn.execute(
            "INSERT INTO turns (session_id, timestamp, model, input_tokens, "
            "output_tokens, cache_read_tokens, cache_creation_tokens, message_id) "
            "VALUES ('s1', ?, 'claude-opus-4-8', 100, 200, 0, 0, 'm1')",
            (today_ts,))
        conn.commit()
        conn.close()
        return db_path

    def _run_fresh(self, attr):
        """Run one read command against its own untouched old-schema file."""
        original, cli.DB_PATH = cli.DB_PATH, self._old_schema_database()
        try:
            return self._run(getattr(cli, attr))
        finally:
            cli.DB_PATH = original

    COMMANDS = (("today", "cmd_today"), ("week", "cmd_week"),
                ("stats", "cmd_stats"))

    def _run(self, command):
        """Run a read command, returning (exit code or None, stdout, stderr).

        `SystemExit` is caught rather than allowed to escape because it is the
        contract, not an accident: `require_db` exits 1 on a rebuild. An earlier
        version of this helper did not catch it, which turned the whole class
        into five ERRORs the moment the refusal shipped.
        """
        buf = io.StringIO()
        code = None
        with redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                command()
            except SystemExit as exc:
                code = 1 if exc.code is None else exc.code
        return code, buf.getvalue(), err.getvalue()

    def test_every_read_command_exits_nonzero(self):
        for name, attr in self.COMMANDS:
            with self.subTest(command=name):
                code, _, _ = self._run_fresh(attr)
                self.assertEqual(code, 1)

    def test_no_read_command_writes_anything_to_stdout(self):
        """Not "no numbers" — nothing. A header over an empty table is the same
        wrong answer with a smaller blast radius, and it is what a shell
        redirect captures as the report."""
        for name, attr in self.COMMANDS:
            with self.subTest(command=name):
                _, out, _ = self._run_fresh(attr)
                self.assertEqual(out, "")

    def test_the_rebuild_is_announced_on_stderr_only(self):
        for name, attr in self.COMMANDS:
            with self.subTest(command=name):
                _, out, err = self._run_fresh(attr)
                self.assertIn("written by a different version", err)
                self.assertNotIn("written by a different version", out)

    def test_stderr_names_the_recovery_and_not_just_the_problem(self):
        """The notice alone leaves the reader with an emptied database and no
        instruction. `cli.py scan` is the whole remedy, so it has to be said."""
        for name, attr in self.COMMANDS:
            with self.subTest(command=name):
                _, _, err = self._run_fresh(attr)
                self.assertIn("holds no turns", err)
                self.assertIn("cli.py scan", err)

    def test_the_refusal_survives_the_invocation_that_rebuilt(self):
        """**The refusal is a property of the DATABASE, not of one process.**

        This is the regression test for the shape the first fix had. Keying it
        to `init_db`'s rebuild return made it true for exactly one invocation:
        the second `stats` found a matching, empty database, sailed past the
        check and printed `Total turns: 0`, `Est. total cost: $0.0000` at exit
        0 — the same confidently wrong answer, one command later. Two
        independent reviewers reproduced it.

        So: run every command TWICE against the same file. The first rebuilds
        it; the second must refuse just as firmly, and on its own evidence.
        """
        for name, attr in self.COMMANDS:
            with self.subTest(command=name):
                original, cli.DB_PATH = cli.DB_PATH, self._old_schema_database()
                try:
                    first = self._run(getattr(cli, attr))
                    second = self._run(getattr(cli, attr))
                finally:
                    cli.DB_PATH = original
                self.assertEqual(first[0], 1, "the rebuilding invocation")
                # The rebuild announcement fires once, because only one
                # invocation rebuilds -- but the emptiness notice must not be
                # tied to it. Keyed to the rebuild alone it was exactly one
                # command wide, and the next one printed a confident $0.0000
                # in silence.
                self.assertIn("written by a different version", first[2])
                self.assertNotIn("written by a different version", second[2])
                self.assertIn("holds no turns", second[2])
                self.assertIn("cli.py scan", second[2])

    def test_an_empty_database_nobody_rebuilt_is_still_announced(self):
        """A database this build wrote, holding nothing, REPORTS -- and says so.

        Not refused. The two conditions are separate and only the rebuild
        refuses, which the class docstring above states in bold. This method was
        named `..._is_refused_too` until 2026-08-15, stating the opposite of its
        own `assertIsNone(code)`; names in this repository are read as the
        contract, so a maintainer reconciling the two could have made emptiness
        exit 1 -- the change that reds `TestCliReportsWithNoData`.

        Reachable without any rebuild at all: a first scan interrupted before it
        committed, or a machine with no transcripts. The old event-keyed check
        reported zeroes here with total confidence.
        """
        fresh = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(fresh)
        try:
            init_db(conn, fresh)
            conn.commit()
        finally:
            conn.close()
        original, cli.DB_PATH = cli.DB_PATH, fresh
        try:
            code, out, err = self._run(cli.cmd_stats)
        finally:
            cli.DB_PATH = original
        # Reports normally -- an empty database is a STATE, and zeroes are the
        # correct answer for a machine with no transcripts. What it must not do
        # is stay silent about it.
        self.assertIsNone(code)
        self.assertIn("holds no turns", err)
        self.assertIn("cli.py scan", err)
        self.assertNotIn("written by a different version", err)

    def test_the_row_really_is_gone(self):
        """The refusal is only honest if the data it refuses to report on is in
        fact no longer there — otherwise the right fix would have been to report
        it. Read back through a fresh connection, not the closed one."""
        import sqlite3
        self._run(cli.cmd_stats)
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 0)
        finally:
            conn.close()

    def test_a_matching_database_still_reports(self):
        """Anti-vacuity, and the half that would otherwise rot silently: if the
        refusal fired on every database rather than on a rebuilt one, every
        assertion above would still pass and the tool would be useless.

        **It checks the NOTICE as well as the refusal, and until 2026-08-15 it
        checked only the refusal.** The unconditional emptiness notice is this
        change's headline fix, and nothing distinguished it from a guard that
        fires on every database: replacing `require_db`'s `if empty:` with
        `if True:` left the whole suite green (measured 2026-08-15) while every
        `today` / `week` / `stats` against a healthy, populated database printed
        "This usage database holds no turns" on stderr for the rest of time.
        A warning a working install cannot stop emitting is one users learn to
        pipe away, which spends the credibility the rebuilt-database case needs.
        """
        fresh = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(fresh)
        try:
            init_db(conn, fresh)
            upsert_sessions(conn, [{
                "session_id": "sess-1", "project_name": "user/proj",
                "first_timestamp": self.today_ts, "last_timestamp": self.today_ts,
                "git_branch": "main", "model": "claude-opus-4-8",
                "total_input_tokens": 100, "total_output_tokens": 200,
                "total_cache_read": 0, "total_cache_creation": 0,
                "turn_count": 1,
            }])
            insert_turns(conn, [_turn("m1", 100, 200, 0, None, self.today_ts)])
            conn.commit()
        finally:
            conn.close()
        original, cli.DB_PATH = cli.DB_PATH, fresh
        try:
            code, out, err = self._run(cli.cmd_stats)
        finally:
            cli.DB_PATH = original
        self.assertIsNone(code, f"a matching database must not refuse: {err!r}")
        self.assertIn("Subagent turns:", out)
        self.assertNotIn("written by a different version", err)
        self.assertNotIn("holds no turns", err,
                         "the emptiness notice must stay conditional on "
                         "emptiness")


if __name__ == "__main__":
    unittest.main()
