"""Numeric tests for what `today` / `week` / `stats` actually report.

The existing CLI tests assert that labels like "Subagent tokens:" appear in the
output; none of them check a single number. Cost is the product's whole point,
and it is computed from a per-turn model attribution that AGENTS.md calls out as
easy to get wrong ("Aggregating tokens first and applying a single price is
wrong for sessions that span multiple models"). These tests pin the arithmetic
with figures chosen so that the correct per-turn result and the classic
aggregate-then-price mistake produce different strings.

It also covers the rest of what these three commands put on a terminal, which
was asserted nowhere: that both cache-write tiers reach every cost figure, that
the `Period:` header names the same local calendar days the body counts, that no
transcript-controlled value reaches the terminal unescaped, and that the Daily
Average section is decided by having days rather than by having input.
"""

import contextlib
import io
import os
import re
import tempfile
import time
import unicodedata
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import cli
import localdays
import reports
from scanner import get_db, init_db, insert_turns, upsert_sessions
from tests.test_local_day_bucketing import TZ_AHEAD, TZ_BEHIND, requires_tzset
from tests.timestamps import local_day, utc_ts_on_local_day

MILLION = 1_000_000


def _turn(message_id, model, ts, inp=0, out=0, cache_read=0, cache_creation=0,
          cache_creation_1h=0, session_id="sess-1", source=None):
    # `cache_creation` is the FULL write total and `cache_creation_1h` the part
    # of it written to the 1-hour cache, exactly as the two columns store them.
    # They bill at 2x and 1.25x input, so a fixture that leaves the 1-hour
    # figure at 0 can see neither a fix nor a break in that half of the rate.
    turn = {
        "session_id": session_id, "timestamp": ts, "model": model,
        "input_tokens": inp, "output_tokens": out,
        "cache_read_tokens": cache_read, "cache_creation_tokens": cache_creation,
        "cache_creation_1h_tokens": cache_creation_1h,
        "tool_name": None, "cwd": None, "message_id": message_id,
        "is_subagent": 0, "agent_id": None,
    }
    # Omitted entirely when unset, so the row exercises the DEFAULT 'claude'
    # column value the way every pre-Codex row in a real database does.
    if source is not None:
        turn["source"] = source
    return turn


class TestCliReportArithmetic(unittest.TestCase):
    """One session, one million input tokens on opus and one on haiku."""

    # Deliberate figures: 1M input tokens on opus is exactly $5.00 and 1M on
    # haiku exactly $1.00, so the correct per-turn total is $6.0000. Pricing the
    # aggregated 2M tokens at a single model's rate would give $10.0000 (opus)
    # or $2.0000 (haiku) — three outcomes no substring check could tell apart.
    EXPECTED_TOTAL = "$6.0000"
    WRONG_IF_PRICED_AS_OPUS = "$10.0000"
    WRONG_IF_PRICED_AS_HAIKU = "$2.0000"

    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        # A real (UTC) transcript timestamp for midday *local* time today, so
        # the row lands on the day the commands label no matter where this runs.
        self.today = local_day()
        ts = utc_ts_on_local_day()
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-1", "project_name": "user/proj",
            "first_timestamp": ts, "last_timestamp": ts,
            "git_branch": "main", "model": "claude-opus-4-8",
            "total_input_tokens": 2 * MILLION, "total_output_tokens": 0,
            "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 2,
        }])
        insert_turns(conn, [
            _turn("m-opus", "claude-opus-4-8", ts, inp=MILLION),
            _turn("m-haiku", "claude-haiku-4-5", ts, inp=MILLION),
        ])
        conn.commit()
        conn.close()
        self._orig_db = cli.DB_PATH
        cli.DB_PATH = self.db_path

    def tearDown(self):
        cli.DB_PATH = self._orig_db

    def _run(self, command):
        buf = io.StringIO()
        with redirect_stdout(buf):
            command()
        return buf.getvalue()

    def _assert_per_turn_pricing(self, out):
        self.assertIn(self.EXPECTED_TOTAL, out)
        self.assertNotIn(self.WRONG_IF_PRICED_AS_OPUS, out)
        self.assertNotIn(self.WRONG_IF_PRICED_AS_HAIKU, out)

    def test_today_totals_and_per_turn_cost(self):
        out = self._run(cli.cmd_today)
        self.assertIn(f"Today's Usage  ({self.today})", out)
        # Per-model rows, each priced with its own model.
        self.assertRegex(out, r"claude-opus-4-8\s+turns=1\s+in=1\.00M\s+out=0\s+cost=\$5\.0000")
        self.assertRegex(out, r"claude-haiku-4-5\s+turns=1\s+in=1\.00M\s+out=0\s+cost=\$1\.0000")
        self.assertRegex(out, r"TOTAL\s+turns=2\s+in=2\.00M")
        self._assert_per_turn_pricing(out)
        self.assertIn("Sessions today:   1", out)

    def test_week_totals_and_per_turn_cost(self):
        out = self._run(cli.cmd_week)
        start = local_day(6)
        self.assertIn(f"Weekly Usage  ({start} to {self.today})", out)
        # Every one of the 7 day rows is printed, including the empty ones.
        for days_ago in range(7):
            self.assertIn(local_day(days_ago), out)
        self.assertRegex(out, r"TOTAL\s+turns=2\s+in=2\.00M")
        self.assertIn("Sessions this week:  1", out)
        self._assert_per_turn_pricing(out)

    def test_stats_totals_and_per_turn_cost(self):
        out = self._run(cli.cmd_stats)
        self.assertIn("Total sessions:   1", out)
        self.assertIn("Est. total cost:  $6.0000", out)
        self.assertRegex(out, r"Input tokens:\s+2\.00M")
        self.assertRegex(out, r"claude-opus-4-8\s+sessions=1\s+turns=1\s+in=1\.00M\s+out=0\s+cost=\$5\.0000")
        self.assertIn("user/proj", out)
        self._assert_per_turn_pricing(out)

    def test_cache_tokens_are_priced_at_their_own_rates(self):
        """Cache reads are 10x cheaper than input; cache writes carry a premium."""
        ts = utc_ts_on_local_day(hour=13)
        conn = get_db(self.db_path)
        insert_turns(conn, [
            # opus: 1M cache_read = $0.50, 1M cache_creation = $6.25
            _turn("m-cache", "claude-opus-4-8", ts,
                  cache_read=MILLION, cache_creation=MILLION),
        ])
        conn.commit()
        conn.close()
        out = self._run(cli.cmd_today)
        # 5.00 + 1.00 + 0.50 + 6.25 = 12.75
        self.assertIn("$12.7500", out)
        self.assertRegex(out, r"Cache read:\s+1\.00M")
        self.assertRegex(out, r"Cache creation:\s+1\.00M")


class TestCliReportsWithNoData(unittest.TestCase):
    """An empty (but valid) DB must report emptiness, not crash or invent rows."""

    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(self.db_path)
        init_db(conn)
        conn.commit()
        conn.close()
        self._orig_db = cli.DB_PATH
        cli.DB_PATH = self.db_path

    def tearDown(self):
        cli.DB_PATH = self._orig_db

    def _run(self, command):
        buf = io.StringIO()
        with redirect_stdout(buf):
            command()
        return buf.getvalue()

    def test_today_reports_no_usage(self):
        self.assertIn("No usage recorded today.", self._run(cli.cmd_today))

    def test_week_reports_no_usage(self):
        self.assertIn("No usage recorded in the last 7 days.",
                      self._run(cli.cmd_week))

    def test_stats_reports_zeroes_without_crashing(self):
        out = self._run(cli.cmd_stats)
        self.assertIn("Total sessions:   0", out)
        self.assertIn("Est. total cost:  $0.0000", out)
        # No sessions means no bounds: the local-day expression over an empty
        # table is NULL, and the line prints blank rather than inventing a day.
        self.assertIn("Period:            to", out)
        # ...and with no days there is no average to state.
        self.assertNotIn("Daily Average", out)


class TestCliReportSourceScoping(unittest.TestCase):
    """The terminal reports cover ONE assistant, like every dashboard view.

    Claude is billed per token against published rates; a Codex plan is a
    subscription. Adding the two produces a figure neither billing regime can
    account for and that the dashboard cannot reproduce in either of its views —
    AGENTS.md calls one-source-at-a-time a correctness rule, not a UI
    preference. These figures are chosen so the three possible answers are three
    different strings: Claude $5.0000, Codex $5.2000, blended $10.2000.
    """

    CLAUDE_COST = "$5.0000"
    CODEX_COST = "$5.2000"
    BLENDED_COST = "$10.2000"

    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        self.today = local_day()
        ts = utc_ts_on_local_day()
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [
            {"session_id": "sess-claude", "project_name": "acme/claude-proj",
             "first_timestamp": ts, "last_timestamp": ts, "git_branch": "main",
             "model": "claude-opus-4-8", "total_input_tokens": MILLION,
             "total_output_tokens": 0, "total_cache_read": 0,
             "total_cache_creation": 0, "turn_count": 1, "source": "claude"},
            {"session_id": "sess-codex", "project_name": "acme/codex-proj",
             "first_timestamp": ts, "last_timestamp": ts, "git_branch": "main",
             "model": "gpt-5.4", "total_input_tokens": 2 * MILLION,
             "total_output_tokens": 0, "total_cache_read": 0,
             "total_cache_creation": 0, "turn_count": 2, "source": "codex"},
        ])
        insert_turns(conn, [
            # 1M input on opus is exactly $5.00.
            _turn("c-1", "claude-opus-4-8", ts, inp=MILLION,
                  session_id="sess-claude", source="claude"),
            # 1M input on gpt-5.4 crosses the 272K threshold ($5.00), while
            # gpt-5.4-nano is not eligible for the long-context tier ($0.20)
            # → $10.20 across the two turns.
            _turn("x-1", "gpt-5.4", ts, inp=MILLION,
                  session_id="sess-codex", source="codex"),
            _turn("x-2", "gpt-5.4-nano", ts, inp=MILLION,
                  session_id="sess-codex", source="codex"),
        ])
        conn.commit()
        conn.close()
        self._orig_db = cli.DB_PATH
        cli.DB_PATH = self.db_path

    def tearDown(self):
        cli.DB_PATH = self._orig_db

    def _run(self, command, **kwargs):
        buf = io.StringIO()
        with redirect_stdout(buf):
            command(**kwargs)
        return buf.getvalue()

    def _assert_claude_only(self, out):
        self.assertIn(self.CLAUDE_COST, out)
        self.assertNotIn(self.CODEX_COST, out)
        self.assertNotIn(self.BLENDED_COST, out)
        self.assertIn("claude-opus-4-8", out)
        self.assertNotIn("gpt-5.4", out)

    def _assert_codex_only(self, out):
        self.assertIn(self.CODEX_COST, out)
        self.assertNotIn(self.BLENDED_COST, out)
        self.assertIn("gpt-5.4", out)
        self.assertNotIn("claude-opus-4-8", out)

    # ── today ────────────────────────────────────────────────────────────────
    def test_today_defaults_to_claude(self):
        out = self._run(cli.cmd_today)
        self._assert_claude_only(out)
        self.assertRegex(out, r"TOTAL\s+turns=1\s+in=1\.00M")
        self.assertIn("Sessions today:   1", out)
        self.assertIn("Source: Claude Code", out)

    def test_today_source_codex(self):
        out = self._run(cli.cmd_today, source="codex")
        self._assert_codex_only(out)
        self.assertRegex(out, r"TOTAL\s+turns=2\s+in=2\.00M")
        self.assertIn("Sessions today:   1", out)
        self.assertIn("Source: Codex", out)

    def test_today_source_all_blends_and_says_so(self):
        out = self._run(cli.cmd_today, source="all")
        self.assertIn(self.BLENDED_COST, out)
        self.assertRegex(out, r"TOTAL\s+turns=3\s+in=3\.00M")
        self.assertIn("Sessions today:   2", out)
        self.assertIn("Source: all sources", out)
        # A blended total spans two billing regimes and must not be presented
        # as a single bill.
        self.assertIn("billing regimes", out)

    # ── week ─────────────────────────────────────────────────────────────────
    def test_week_defaults_to_claude(self):
        out = self._run(cli.cmd_week)
        self._assert_claude_only(out)
        self.assertIn("Sessions this week:  1", out)
        self.assertIn("Source: Claude Code", out)
        # The per-day line for today carries the scoped cost, not the blend.
        self.assertRegex(out, rf"{self.today}\s+turns=1\s+in=1\.00M\s+out=0\s+cost=\$5\.0000")

    def test_week_source_codex(self):
        out = self._run(cli.cmd_week, source="codex")
        self._assert_codex_only(out)
        self.assertIn("Sessions this week:  1", out)
        self.assertRegex(out, rf"{self.today}\s+turns=2\s+in=2\.00M\s+out=0\s+cost=\$5\.2000")

    def test_week_source_all(self):
        out = self._run(cli.cmd_week, source="all")
        self.assertIn(self.BLENDED_COST, out)
        self.assertIn("Sessions this week:  2", out)
        self.assertIn("billing regimes", out)

    # ── stats ────────────────────────────────────────────────────────────────
    def test_stats_defaults_to_claude(self):
        out = self._run(cli.cmd_stats)
        self._assert_claude_only(out)
        self.assertIn("Est. total cost:  $5.0000", out)
        self.assertIn("Total sessions:   1", out)
        self.assertIn("Total turns:      1", out)
        self.assertRegex(out, r"Input tokens:\s+1\.00M")
        # Top Projects is a turns-derived table and is scoped like the rest.
        self.assertIn("acme/claude-proj", out)
        self.assertNotIn("acme/codex-proj", out)
        self.assertIn("Claude Code Usage - All-Time Statistics", out)

    def test_stats_source_codex(self):
        out = self._run(cli.cmd_stats, source="codex")
        self._assert_codex_only(out)
        self.assertIn("Est. total cost:  $5.2000", out)
        self.assertIn("Total sessions:   1", out)
        self.assertIn("Total turns:      2", out)
        self.assertRegex(out, r"Input tokens:\s+2\.00M")
        self.assertIn("acme/codex-proj", out)
        self.assertNotIn("acme/claude-proj", out)
        self.assertIn("Codex Usage - All-Time Statistics", out)

    def test_stats_source_all(self):
        out = self._run(cli.cmd_stats, source="all")
        self.assertIn("Est. total cost:  $10.2000", out)
        self.assertIn("Total sessions:   2", out)
        self.assertIn("Total turns:      3", out)
        self.assertIn("acme/claude-proj", out)
        self.assertIn("acme/codex-proj", out)
        self.assertIn("billing regimes", out)
        # And it must not claim to be either assistant's report.
        self.assertIn("Combined Usage - All-Time Statistics", out)
        self.assertNotIn("Claude Code Usage - All-Time Statistics", out)

    def _add_claude_subagent_turn(self):
        conn = get_db(self.db_path)
        insert_turns(conn, [{
            "session_id": "sess-claude", "timestamp": utc_ts_on_local_day(hour=13),
            "model": "claude-haiku-4-5", "input_tokens": MILLION,
            "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_name": None, "cwd": None, "message_id": "c-sub",
            "is_subagent": 1, "agent_id": "agent-1", "source": "claude",
        }])
        conn.commit()
        conn.close()

    def test_subagent_lines_are_scoped(self):
        """Claude's subagent tokens must not be reported under Codex."""
        self._add_claude_subagent_turn()
        self.assertIn("Subagent tokens:  1.00M  (1 turns)", self._run(cli.cmd_today))
        self.assertIn("Subagent tokens:  0  (0 turns)",
                      self._run(cli.cmd_today, source="codex"))
        self.assertIn("Subagent turns:   0", self._run(cli.cmd_stats, source="codex"))
        self.assertIn("Subagent turns:   1", self._run(cli.cmd_stats))

    def test_an_empty_source_string_is_still_claude(self):
        """Rows written before the column existed carry '' and are Claude's.

        A bare `source = 'claude'` predicate would drop them out of every total
        — the same defaulting the migration and the dashboard client apply.
        """
        conn = get_db(self.db_path)
        conn.execute("UPDATE turns SET source = '' WHERE message_id = 'c-1'")
        conn.execute("UPDATE sessions SET source = '' WHERE session_id = 'sess-claude'")
        conn.commit()
        conn.close()
        self._assert_claude_only(self._run(cli.cmd_today))
        out = self._run(cli.cmd_stats)
        self.assertIn("Est. total cost:  $5.0000", out)
        self.assertIn("Total sessions:   1", out)
        self.assertIn("acme/claude-proj", out)

    def test_stats_daily_average_is_scoped(self):
        # 30-day average over a single day = that day's tokens for the source.
        out = self._run(cli.cmd_stats, source="codex")
        self.assertRegex(out, r"Daily Average \(last 30 days\):\s*\n\s*Input:\s+2\.00M")

    # ── validation ───────────────────────────────────────────────────────────
    def test_unknown_source_is_rejected(self):
        for command in (cli.cmd_today, cli.cmd_week, cli.cmd_stats):
            with self.subTest(command=command.__name__):
                with self.assertRaises(ValueError):
                    command(source="gemini")

    def test_main_threads_the_source_flag(self):
        buf = io.StringIO()
        with mock.patch.object(cli.sys, "argv", ["cli.py", "today", "--source", "codex"]):
            with redirect_stdout(buf):
                cli.main()
        self._assert_codex_only(buf.getvalue())

    def test_main_rejects_an_unknown_source(self):
        buf = io.StringIO()
        with mock.patch.object(cli.sys, "argv", ["cli.py", "today", "--source", "gemini"]):
            # stderr: the rejection is a diagnostic, and this command's stdout
            # is its report. Merged into one buffer because what is asserted
            # here is the message, not the stream.
            with redirect_stdout(buf), contextlib.redirect_stderr(buf):
                with self.assertRaises(SystemExit) as ctx:
                    cli.main()
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("--source", buf.getvalue())

    def test_usage_documents_the_flag(self):
        self.assertIn("--source", cli.USAGE)


class TestCliSingleSourceDatabase(unittest.TestCase):
    """A machine that only ever ran Codex must not be shown an empty report.

    Same rule the dashboard's picker applies: with exactly one assistant present
    there is nothing to choose between, so the reports cover it without a flag.
    """

    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        ts = utc_ts_on_local_day()
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-codex", "project_name": "acme/codex-proj",
            "first_timestamp": ts, "last_timestamp": ts, "git_branch": "main",
            "model": "gpt-5.4", "total_input_tokens": MILLION,
            "total_output_tokens": 0, "total_cache_read": 0,
            "total_cache_creation": 0, "turn_count": 1, "source": "codex",
        }])
        insert_turns(conn, [
            _turn("x-1", "gpt-5.4", ts, inp=MILLION,
                  session_id="sess-codex", source="codex"),
        ])
        conn.commit()
        conn.close()
        self._orig_db = cli.DB_PATH
        cli.DB_PATH = self.db_path

    def tearDown(self):
        cli.DB_PATH = self._orig_db

    def _run(self, command):
        buf = io.StringIO()
        with redirect_stdout(buf):
            command()
        return buf.getvalue()

    def test_today_reports_the_only_source_present(self):
        out = self._run(cli.cmd_today)
        self.assertIn("$5.0000", out)
        self.assertIn("Source: Codex", out)
        self.assertNotIn("No usage recorded today.", out)

    def test_stats_reports_the_only_source_present(self):
        out = self._run(cli.cmd_stats)
        self.assertIn("Est. total cost:  $5.0000", out)
        self.assertIn("Codex Usage - All-Time Statistics", out)


class TestResolveSource(unittest.TestCase):
    """Which assistant a report covers when no flag names one.

    `resolve_source` answers this with index seeks rather than a scan, so the
    NULL and '' spellings of "Claude" each need their own probe — a database
    written before the `source` column existed carries one of them for every
    row, and mistaking it for an unknown source would scope every report to
    nothing.
    """

    def _conn(self, raw_sources):
        db_path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(db_path)
        init_db(conn)
        ts = utc_ts_on_local_day()
        conn.executemany(
            "INSERT INTO turns (session_id, timestamp, model, input_tokens,"
            " output_tokens, cache_read_tokens, cache_creation_tokens,"
            " message_id, source) VALUES (?,?,?,?,?,?,?,?,?)",
            [("s1", ts, "claude-opus-4-8", 1, 1, 0, 0, f"m{i}", src)
             for i, src in enumerate(raw_sources)])
        conn.commit()
        self.addCleanup(conn.close)
        return conn

    def test_null_source_rows_are_claude(self):
        self.assertEqual(reports.resolve_source(self._conn([None, None])), "claude")

    def test_empty_string_source_rows_are_claude(self):
        self.assertEqual(reports.resolve_source(self._conn(["", ""])), "claude")

    def test_the_only_source_present_wins(self):
        self.assertEqual(reports.resolve_source(self._conn(["codex"])), "codex")

    def test_two_sources_default_to_claude(self):
        self.assertEqual(
            reports.resolve_source(self._conn(["codex", "claude"])), "claude")

    def test_legacy_rows_beside_codex_still_default_to_claude(self):
        # The '' rows are Claude's, so this database holds two sources even
        # though only one of them spells its name.
        self.assertEqual(
            reports.resolve_source(self._conn(["codex", ""])), "claude")

    def test_empty_database_defaults_to_claude(self):
        self.assertEqual(reports.resolve_source(self._conn([])), "claude")

    def test_an_explicit_request_is_honoured_against_the_evidence(self):
        # "Show me Codex" on a Claude-only machine means an empty Codex report,
        # never Claude's money under a Codex heading.
        conn = self._conn(["claude"])
        self.assertEqual(reports.resolve_source(conn, "codex"), "codex")

    def test_all_scopes_nothing(self):
        self.assertIsNone(reports.resolve_source(self._conn(["claude"]), "all"))

    def test_sources_present_normalises_every_spelling(self):
        conn = self._conn([None, "", "claude", "codex"])
        self.assertEqual(reports.sources_present(conn), ["claude", "codex"])


@requires_tzset
class TestStatsPeriodIsALocalDay(unittest.TestCase):
    """`stats`' Period line names the same calendar days its own body counts.

    Every other date in reports.py goes through `localdays.LOCAL_DAY`; the
    Period bounds were a raw `[:10]` slice of the stored UTC timestamp, so at
    UTC+14 the header read `2026-08-08` while `week`, the daily chart and the
    dashboard all filed the same turns under `2026-08-09` — a header naming a
    day on which its own body records nothing. The mismatch is invisible in UTC,
    which is where CI runs, so the zone is forced here; `tzset` is POSIX-only,
    hence the skip.

    Two sessions a day apart, so the two bounds are different strings and each
    is pinned on its own: a MIN/MAX swap prints them in the other order, which
    the full-line assertion catches. Nothing asserted the Period line at all
    before this class.
    """

    TZ = TZ_AHEAD                        # UTC+14
    EARLY = "2026-08-08T12:00:00.000Z"   # 2026-08-09 02:00 local
    LATE = "2026-08-10T12:00:00.000Z"    # 2026-08-11 02:00 local
    LOCAL_DAYS = ["2026-08-09", "2026-08-11"]
    EXPECTED = "  Period:           2026-08-09 to 2026-08-11"
    RAW_UTC_PREFIXES = "  Period:           2026-08-08 to 2026-08-10"

    def setUp(self):
        self._orig_tz = os.environ.get("TZ")
        os.environ["TZ"] = self.TZ
        time.tzset()
        self.addCleanup(self._restore_tz)
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [
            {"session_id": "s-early", "project_name": "user/proj",
             "first_timestamp": self.EARLY, "last_timestamp": self.EARLY,
             "git_branch": "main", "model": "claude-opus-4-8",
             "total_input_tokens": 0, "total_output_tokens": 0,
             "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 1},
            {"session_id": "s-late", "project_name": "user/proj",
             "first_timestamp": self.LATE, "last_timestamp": self.LATE,
             "git_branch": "main", "model": "claude-opus-4-8",
             "total_input_tokens": 0, "total_output_tokens": 0,
             "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 1},
        ])
        insert_turns(conn, [
            _turn("m-early", "claude-opus-4-8", self.EARLY, inp=MILLION,
                  session_id="s-early"),
            _turn("m-late", "claude-opus-4-8", self.LATE, inp=MILLION,
                  session_id="s-late"),
        ])
        conn.commit()
        conn.close()
        self._orig_db = cli.DB_PATH
        cli.DB_PATH = self.db_path
        self.addCleanup(self._restore_db)

    def _restore_tz(self):
        if self._orig_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._orig_tz
        time.tzset()

    def _restore_db(self):
        cli.DB_PATH = self._orig_db

    def _stats(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_stats()
        return buf.getvalue()

    def _period_line(self, out):
        return next(line for line in out.splitlines() if "Period:" in line)

    def test_period_bounds_are_local_days(self):
        out = self._stats()
        self.assertEqual(self._period_line(out), self.EXPECTED)
        self.assertNotIn(self.RAW_UTC_PREFIXES, out)

    def test_period_matches_the_days_the_turns_bucket_into(self):
        """The header must not name a day the report's own body leaves empty."""
        conn = get_db(self.db_path)
        days = sorted(r[0] for r in conn.execute(
            f"SELECT DISTINCT {localdays.LOCAL_DAY} FROM turns").fetchall())
        conn.close()
        self.assertEqual(days, self.LOCAL_DAYS)
        line = self._period_line(self._stats())
        self.assertIn(days[0], line)
        self.assertIn(days[-1], line)


@requires_tzset
class TestStatsPeriodIsALocalDayBehindUTC(TestStatsPeriodIsALocalDay):
    """At UTC-11 the same slice is wrong in the other direction."""

    TZ = TZ_BEHIND                       # UTC-11
    EARLY = "2026-08-08T02:00:00.000Z"   # 2026-08-07 15:00 local
    LATE = "2026-08-10T02:00:00.000Z"    # 2026-08-09 15:00 local
    LOCAL_DAYS = ["2026-08-07", "2026-08-09"]
    EXPECTED = "  Period:           2026-08-07 to 2026-08-09"
    RAW_UTC_PREFIXES = "  Period:           2026-08-08 to 2026-08-10"


# Exactly what `terminal_safe` escapes, stated as the hazard rather than by
# copying its implementation: control codes (Cc), the bidi and other format
# controls (Cf), and the lone surrogate (Cs). A newline is Cc, so the output is
# split on "\n" before this runs and a hit then means the report let a control
# code through on one line.
UNSAFE_CATEGORIES = ("Cc", "Cf", "Cs")


class TestReportsEscapeTranscriptControlledText(unittest.TestCase):
    """No report may hand a raw escape sequence to the terminal.

    Timestamps, model ids and project names are all transcript-controlled: the
    parser length-bounds them and stores whatever they contained, control codes
    included. `stats` printed its `Period:` bounds without `terminal_safe` while
    wrapping the model and project columns two lines below, so a single record
    carrying `"timestamp": "\x1b[2J\x1b[H..."` cleared the reader's screen and
    `\x1b[8m` concealed every figure printed after it.

    Two hostile sessions rather than one, because the bounds are separate call
    sites: a value starting below '2' wins `MIN(first_timestamp)` under SQLite's
    BINARY collation and one starting above it wins `MAX(last_timestamp)`, so a
    wrap applied to only one of them still fails here. Deleting all four
    `terminal_safe` calls from reports.py used to leave the whole suite green.
    """

    # Sized so the 10-character slice the report prints holds a COMPLETE escape
    # sequence and a readable marker: `ESC[2J CR` (clear screen, carriage
    # return) + "EARLY" on one side, `ESC[8m` (conceal) + "LATER" on the other.
    EVIL_FIRST = "\x1b[2J\rEARLY-T00:00:00Z"
    EVIL_LAST = "~\x1b[8mLATER-T00:00:00Z"
    EVIL_MODEL = "claude-opus-4-8\x1b[31m\u202e"
    EVIL_PROJECT = "acme/\x1b]0;pwned\x07proj"

    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        # Keep every endpoint malformed so this security fixture exercises the
        # terminal escaping of both selected raw bounds. The timestamp helper
        # deliberately ranks valid instants ahead of malformed fallback text.
        today_ts = "MIDDLE-T00:00:00Z"
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [
            {"session_id": "s-min", "project_name": "user/proj",
             "first_timestamp": self.EVIL_FIRST, "last_timestamp": self.EVIL_FIRST,
             "git_branch": "main", "model": "claude-opus-4-8",
             "total_input_tokens": 0, "total_output_tokens": 0,
             "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 1},
            {"session_id": "s-max", "project_name": "user/proj",
             "first_timestamp": self.EVIL_LAST, "last_timestamp": self.EVIL_LAST,
             "git_branch": "main", "model": "claude-opus-4-8",
             "total_input_tokens": 0, "total_output_tokens": 0,
             "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 1},
            {"session_id": "s-today", "project_name": self.EVIL_PROJECT,
             "first_timestamp": today_ts, "last_timestamp": today_ts,
             "git_branch": "main", "model": self.EVIL_MODEL,
             "total_input_tokens": MILLION, "total_output_tokens": 0,
             "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 1},
        ])
        insert_turns(conn, [
            _turn("m-min", "claude-opus-4-8", self.EVIL_FIRST, inp=1,
                  session_id="s-min"),
            _turn("m-max", "claude-opus-4-8", self.EVIL_LAST, inp=1,
                  session_id="s-max"),
            _turn("m-today", self.EVIL_MODEL, today_ts, inp=MILLION,
                  session_id="s-today"),
        ])
        conn.commit()
        conn.close()
        self._orig_db = cli.DB_PATH
        cli.DB_PATH = self.db_path

    def tearDown(self):
        cli.DB_PATH = self._orig_db

    def _run(self, command):
        buf = io.StringIO()
        with redirect_stdout(buf):
            command()
        # NOT splitlines(): it treats a hostile CR as a line break and would
        # hide the second half of the very line under test.
        return buf.getvalue().split("\n")

    def _assert_clean(self, text, label):
        leaked = sorted({f"U+{ord(c):04X}" for c in text
                         if unicodedata.category(c) in UNSAFE_CATEGORIES})
        self.assertEqual(leaked, [], f"{label} reached the terminal raw: {text!r}")

    def _period_line(self, lines):
        return next(line for line in lines if "Period:" in line)

    def test_stats_escapes_both_period_bounds(self):
        lines = self._run(cli.cmd_stats)
        period = self._period_line(lines)
        first_slot, separator, last_slot = period.partition(" to ")
        self.assertEqual(separator, " to ", f"unexpected Period line: {period!r}")
        # Each bound is its own print site; assert them apart so a fix applied
        # to only one of the two still fails.
        self._assert_clean(first_slot, "MIN(first_timestamp)")
        self._assert_clean(last_slot, "MAX(last_timestamp)")
        # The escaping must not swallow the value: an unparseable timestamp
        # still falls back to its raw prefix, which is what the reader sees.
        self.assertIn("EARLY", first_slot)
        self.assertIn("LATER", last_slot)

    def test_no_report_emits_a_control_character(self):
        """The model and project guards are covered by the same fixture."""
        for command in (cli.cmd_today, cli.cmd_week, cli.cmd_stats):
            for line in self._run(command):
                with self.subTest(command=command.__name__, line=line):
                    self._assert_clean(line, command.__name__)



class TestCacheWriteTiersInEveryReport(unittest.TestCase):
    """Every cost figure the three reports print bills both cache-write tiers.

    Exercise five-minute and one-hour cache writes separately. The one-hour
    field is a subset of total cache creation, so counting it again would
    inflate cost.

    Two turns, on two local days, on two models, so one fixture reaches all five
    sites: `today`, `week`'s By Day column and its By Model block, and `stats`'
    total and By Model block. The figures are chosen so that dropping the 1-hour
    tier, dropping the 5-minute tier, dropping cache reads, or billing the
    1-hour part twice each produce a different string.

        opus, today:  1M input $5.00 + 2M reads $1.00
                      + 3M 1-hour writes $30.00 + 1M 5-minute writes $6.25
        haiku, -3d:   1M 1-hour writes $2.00 + 1M 5-minute writes $1.25
    """

    OPUS_DAY_COST = "$42.2500"
    HAIKU_DAY_COST = "$3.2500"
    TOTAL_COST = "$45.5000"
    # What each report prints if the 1-hour tier is dropped and the whole write
    # bills at the 5-minute rate — the exact regression this class exists for.
    WRONG_OPUS_DAY = "$31.0000"
    WRONG_HAIKU_DAY = "$2.5000"
    WRONG_TOTAL = "$33.5000"

    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        self.today = local_day()
        self.earlier = local_day(3)
        today_ts = utc_ts_on_local_day()
        earlier_ts = utc_ts_on_local_day(days_ago=3)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-1", "project_name": "user/proj",
            "first_timestamp": earlier_ts, "last_timestamp": today_ts,
            "git_branch": "main", "model": "claude-opus-4-8",
            "total_input_tokens": MILLION, "total_output_tokens": 0,
            "total_cache_read": 2 * MILLION, "total_cache_creation": 6 * MILLION,
            "turn_count": 2,
        }])
        insert_turns(conn, [
            _turn("m-opus", "claude-opus-4-8", today_ts, inp=MILLION,
                  cache_read=2 * MILLION, cache_creation=4 * MILLION,
                  cache_creation_1h=3 * MILLION),
            _turn("m-haiku", "claude-haiku-4-5", earlier_ts,
                  cache_creation=2 * MILLION, cache_creation_1h=MILLION),
        ])
        conn.commit()
        conn.close()
        self._orig_db = cli.DB_PATH
        cli.DB_PATH = self.db_path

    def tearDown(self):
        cli.DB_PATH = self._orig_db

    def _run(self, command):
        buf = io.StringIO()
        with redirect_stdout(buf):
            command()
        return buf.getvalue()

    def _cost(self, value):
        return "cost=" + re.escape(value)

    def test_today_prices_both_write_tiers(self):
        out = self._run(cli.cmd_today)
        self.assertRegex(out, r"claude-opus-4-8\s+turns=1\s+in=1\.00M\s+out=0\s+"
                              + self._cost(self.OPUS_DAY_COST))
        self.assertRegex(out, r"TOTAL\s+turns=1\s+in=1\.00M\s+out=0\s+"
                              + self._cost(self.OPUS_DAY_COST))
        self.assertNotIn(self.WRONG_OPUS_DAY, out)

    def test_week_by_day_column_prices_both_write_tiers(self):
        """The By Day bucket is the only place `week` prices a day."""
        out = self._run(cli.cmd_week)
        self.assertRegex(out, self.today + r"\s+turns=1\s+in=1\.00M\s+out=0\s+"
                              + self._cost(self.OPUS_DAY_COST))
        self.assertRegex(out, self.earlier + r"\s+turns=1\s+in=0\s+out=0\s+"
                              + self._cost(self.HAIKU_DAY_COST))
        self.assertNotIn(self.WRONG_OPUS_DAY, out)
        self.assertNotIn(self.WRONG_HAIKU_DAY, out)

    def test_week_by_model_block_prices_both_write_tiers(self):
        out = self._run(cli.cmd_week)
        self.assertRegex(out, r"claude-opus-4-8\s+turns=1\s+in=1\.00M\s+out=0\s+"
                              + self._cost(self.OPUS_DAY_COST))
        self.assertRegex(out, r"claude-haiku-4-5\s+turns=1\s+in=0\s+out=0\s+"
                              + self._cost(self.HAIKU_DAY_COST))
        self.assertRegex(out, r"TOTAL\s+turns=2\s+in=1\.00M\s+out=0\s+"
                              + self._cost(self.TOTAL_COST))
        self.assertNotIn(self.WRONG_TOTAL, out)

    def test_week_by_day_column_sums_to_the_by_model_total(self):
        """A secondary check: the two panels sit on one screen and must agree.

        Deliberately not the load-bearing assertion — the identical mutation
        applied to both loops keeps them consistent while both are wrong, which
        is what the absolute figures above catch instead. Compared with a
        tolerance because each of the seven day rows is rounded to four decimals
        before the TOTAL beneath them is rounded once.
        """
        out = self._run(cli.cmd_week)
        by_day = [float(m) for m in re.findall(
            r"^ {4}\d{4}-\d{2}-\d{2}\s+turns=\S+\s+in=\S+\s+out=\S+\s+cost=\$(\d+\.\d+)$",
            out, re.MULTILINE)]
        self.assertEqual(len(by_day), 7, out)
        total = re.search(r"TOTAL\s+turns=2.*?cost=\$(\d+\.\d+)", out)
        self.assertAlmostEqual(sum(by_day), float(total.group(1)), delta=0.0005)

    def test_stats_total_and_by_model_price_both_write_tiers(self):
        out = self._run(cli.cmd_stats)
        self.assertIn(f"Est. total cost:  {self.TOTAL_COST}", out)
        self.assertRegex(out, r"claude-opus-4-8\s+sessions=1\s+turns=1\s+"
                              r"in=1\.00M\s+out=0\s+" + self._cost(self.OPUS_DAY_COST))
        self.assertRegex(out, r"claude-haiku-4-5\s+sessions=1\s+turns=1\s+"
                              r"in=0\s+out=0\s+" + self._cost(self.HAIKU_DAY_COST))
        self.assertNotIn(self.WRONG_TOTAL, out)
        # The reported write total is the full 6M, not the 1-hour part alone
        # nor the remainder: the column holds a subset, not a separate bucket.
        self.assertRegex(out, r"Cache creation:\s+6\.00M")



class _StatsTempDb(unittest.TestCase):
    """An empty temp database `cli.cmd_stats` reports on, and nothing else."""

    def setUp(self):
        # Captured before anything replaces it, and restored by a cleanup that
        # closes over the value rather than over the attribute -- see
        # `_fresh_database` for what re-reading it costs.
        original = cli.DB_PATH
        self.addCleanup(setattr, cli, "DB_PATH", original)
        self._fresh_database()

    def _fresh_database(self):
        """A new empty database for the next subTest, WITHOUT re-running setUp.

        A subTest loop that wants a clean database calls this. What it must not
        do is re-run the whole fixture under a `tearDown` that restores an
        attribute setUp itself records: the second pass records the first pass's
        temp path, and the module global is left pointing at a deleted temp file
        for the rest of the process. No victim exists today because every later
        module patches `cli.DB_PATH` in its own setUp; the same defect class in
        `dashboard.PROJECTS_DIRS` is what made `tests.test_port_in_use
        tests.test_dashboard` fail in that order and pass in the other, with the
        developer's whole corpus scanned into a temp database.

        The `addCleanup` in setUp closes over the value, and cleanups run LIFO,
        so the real path is restored last and a stray `self.setUp()` in a loop
        would be survivable here -- measured 2026-08-16, putting both calls back
        leaves `TestTheStatsFixtureLeavesTheModuleGlobalAlone` GREEN. That guard
        watches the shape this replaced, not the call.
        """
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(self.db_path)
        init_db(conn)
        conn.commit()
        conn.close()
        cli.DB_PATH = self.db_path

    def _insert(self, turns):
        conn = get_db(self.db_path)
        insert_turns(conn, turns)
        conn.commit()
        conn.close()

    def _stats(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_stats()
        return buf.getvalue()


class TestStatsDailyAverageSection(_StatsTempDb):
    """The Daily Average block appears when there are days, not when input is.

    `AVG()` returns NULL over no rows and `0.0` over rows that are all zero, and
    both are falsy — so a truthiness guard on the input average deleted the
    whole section, taking a perfectly good *output* average with it. A corpus
    whose uncached input is zero is what triggers it: `input_tokens` is stored as
    the uncached remainder, and a transcript whose `usage` omits it stores 0.

    Both branches are pinned here. Only the presence branch was asserted before
    (with a non-zero input), so a later regression to an unconditional print
    would have turned the guard into dead code unnoticed.
    """

    def test_zero_input_still_reports_the_output_average(self):
        self._insert([_turn("m-1", "claude-opus-4-8", utc_ts_on_local_day(),
                            inp=0, out=5 * MILLION, cache_read=40 * MILLION)])
        out = self._stats()
        self.assertIn("Daily Average (last 30 days):", out)
        self.assertRegex(out, r"Input:\s+0\n")
        self.assertRegex(out, r"Output:\s+5\.00M")
        # The section is the only thing at issue: the totals were always right.
        self.assertIn("Est. total cost:  $145.0000", out)

    def test_no_days_in_the_window_still_suppresses_the_section(self):
        """Nothing in the last 30 days means there is no average to state."""
        self._insert([_turn("m-old", "claude-opus-4-8",
                            utc_ts_on_local_day(days_ago=60), inp=MILLION)])
        out = self._stats()
        self.assertNotIn("Daily Average", out)
        # ...and the rest of the report is unaffected by the suppression.
        self.assertIn("Est. total cost:  $5.0000", out)


class TestStatsDailyAverageIsBoundedAtBothEnds(_StatsTempDb):
    """The window above the average, which had no upper bound until 2026-08-16.

    An average is the one figure `stats` prints where an extra GROUP BY row
    moves the answer instead of adding to it, and the query behind it asked only
    `timestamp >= ?` and `LOCAL_DAY >= date('now','localtime','-30 days')`. So
    any key sorting ABOVE today became a thirty-first day inside a thirty-day
    mean. The mirror image was already right -- the class above pins the past
    side with a 60-days-ago row -- which is what showed the bound was missing on
    one side only.

    Three inputs reach it, all reproduced on the shipped build before the fix.
    A well-formed FUTURE timestamp, which needs no corruption at all (clock
    skew, a snapshot-restored VM, a UTC/local RTC mismatch, transcripts scanned
    from another host); and the two raw-prefix buckets `local_day_expr`'s gate
    deliberately creates -- the literal text `now` and a bare Julian number --
    which sort above every `2026-` string in both comparisons.

    The fixture is three real days of 3,000 input tokens each, so the honest
    average is 3.0K and one 300,000-token intruder makes it 77.3K: a 25.75x
    error, and two figures no substring check could confuse.

    Sort order was only half the closure, and the class docstring used to claim
    the residue was asserted below when nothing here asserted it. Two of those
    three inputs are excluded because they sort past every `2026-` string, not
    because they are not days -- so a key equally not a day that sorts INSIDE
    the range printed 77.3K until 2026-08-16, when the query started asking
    whether the day key round-trips through date(). Both sides of that are
    pinned below: the in-range non-day is excluded, and a raw-prefix bucket
    whose prefix IS a real day still counts.
    """

    INTRUDER = 300_000

    def _three_real_days(self):
        return [_turn(f"m-{n}", "claude-opus-4-8",
                      utc_ts_on_local_day(days_ago=n), inp=3000)
                for n in range(3)]

    def _average_line(self):
        out = self._stats()
        self.assertIn("Daily Average (last 30 days):", out)
        return re.search(r"Input:\s+(\S+)", out.split("Daily Average")[1]).group(1)

    def test_three_real_days_average_to_the_figure_they_should(self):
        """The control. Without it every assertion below could pass vacuously."""
        self._insert(self._three_real_days())
        self.assertEqual(self._average_line(), "3.0K")

    def test_a_future_dated_turn_is_not_a_thirty_first_day(self):
        for days_ahead in (2, 200):
            with self.subTest(days_ahead=days_ahead):
                self._fresh_database()
                self._insert(self._three_real_days() + [
                    _turn("m-future", "claude-opus-4-8",
                          utc_ts_on_local_day(days_ago=-days_ahead),
                          inp=self.INTRUDER)])
                self.assertEqual(self._average_line(), "3.0K")

    def test_a_gated_out_raw_day_key_is_not_a_day_either(self):
        """`now` and a Julian number never reach date(); they must not average."""
        for stored in ("now", "2460000.5"):
            with self.subTest(timestamp=stored):
                self._fresh_database()
                self._insert(self._three_real_days() + [
                    _turn("m-raw", "claude-opus-4-8", stored,
                          inp=self.INTRUDER)])
                self.assertEqual(self._average_line(), "3.0K")

    def test_an_in_range_key_that_is_not_a_calendar_day_is_not_a_day_either(self):
        """The residue sort order left behind, closed 2026-08-16.

        The key has to be a non-day that BOTH lexicographic bounds admit, or the
        test passes for the wrong reason. This one is derived from a day inside
        the window -- 15 days back, with its units digit replaced by `a`, which
        sorts above every digit -- so it is strictly greater than that day and
        strictly less than today's key in every month, leap year and year roll.

        It used to be day `00` of the CURRENT month, and its docstring claimed
        that "always sorts above the boundary 30 days back". That is false on
        the 31st: `thirty_days_ago` is then the 1st of the same month, and
        `YYYY-MM-00` sorts below `YYYY-MM-01`, so the row was excluded by sort
        order -- the very half the conjunct exists to go beyond. On the seven
        such dates a year the conjunct could be deleted and the whole suite
        stayed green, while `_cmd_stats` went back to a 25.75x-wrong average.
        """
        key = local_day(days_ago=15)[:9] + "a"
        # The precondition the old fixture silently lost. Assert it, so a future
        # drift in either bound is loud rather than seven days of green a year.
        self.assertLess(local_day(days_ago=30), key)
        self.assertLess(key, local_day())
        self._insert(self._three_real_days() + [
            _turn("m-noday", "claude-opus-4-8",
                  key + "T12:00:00.000Z", inp=self.INTRUDER)])
        self.assertEqual(self._average_line(), "3.0K")

    def test_a_raw_bucket_whose_prefix_is_a_real_day_still_counts(self):
        """The control against over-tightening, and why the conjunct is written
        on the day key rather than on the timestamp.

        A garbage tail stops date() resolving the whole value, so the row falls
        into the raw-prefix bucket -- but that prefix IS a real calendar day and
        the row belongs on it. `AND date(timestamp) IS NOT NULL` closes the case
        above and drops this one, while passing every other assertion here.
        """
        stamped = utc_ts_on_local_day(days_ago=10)
        self._insert(self._three_real_days() + [
            _turn("m-tail", "claude-opus-4-8", stamped[:10] + "x" + stamped[10:],
                  inp=self.INTRUDER)])
        self.assertEqual(self._average_line(), "77.3K")

    def test_the_window_spans_exactly_thirty_inclusive_calendar_days(self):
        """Today and day 29 are the edges; day 30 is outside the label.

        The SQL uses inclusive bounds, so subtracting 30 admits 31 calendar
        keys. The large intruder makes that off-by-one visible while the two
        smaller rows prove neither legitimate edge was over-tightened.
        """
        self._insert([
            _turn("m-today", "claude-opus-4-8", utc_ts_on_local_day(), inp=1000),
            _turn("m-edge", "claude-opus-4-8",
                  utc_ts_on_local_day(days_ago=29), inp=3000),
            _turn("m-outside", "claude-opus-4-8",
                  utc_ts_on_local_day(days_ago=30), inp=self.INTRUDER),
        ])
        self.assertEqual(self._average_line(), "2.0K")


class TestTheStatsFixtureLeavesTheModuleGlobalAlone(unittest.TestCase):
    """A fixture that leaks `cli.DB_PATH` is a bug in every LATER test.

    The class above needs a clean database per subTest, and the shape it used to
    have -- setUp recording `self._orig_db = cli.DB_PATH`, a `tearDown`
    restoring it, and the loops calling `self.setUp()` again -- left the module
    global on a temp path: the second pass recorded the first pass's temp file
    as the "original". Nothing in the class notices; the damage is done to
    whatever runs next, and only when it happens not to patch `cli.DB_PATH`
    itself. That is a pass or fail decided by module ordering, which is exactly
    how a `dashboard.PROJECTS_DIRS` leak made `tests.test_port_in_use
    tests.test_dashboard` fail in that order and pass in the other, with the
    developer's entire corpus scanned into a temp database.

    Asserted by running the class rather than by reading it. What reds this,
    measured 2026-08-16, is restoring that whole shape -- `_orig_db` plus the
    `tearDown` plus the two `self.setUp()` calls. Restoring the `self.setUp()`
    calls ALONE does not, and the first version of this docstring claimed it
    did: `addCleanup` closes over the value and cleanups run LIFO, so the real
    path is restored last however many times setUp runs. The guard is therefore
    about the teardown shape, and a future fixture that goes back to restoring
    an attribute setUp records is what it catches.
    """

    def test_running_the_bounded_average_class_restores_db_path(self):
        before = cli.DB_PATH
        self.addCleanup(setattr, cli, "DB_PATH", before)
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            TestStatsDailyAverageIsBoundedAtBothEnds)
        self.assertGreater(suite.countTestCases(), 0, "nothing ran")
        result = unittest.TextTestRunner(stream=io.StringIO(),
                                         verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful(),
                        f"{result.errors}\n{result.failures}")
        self.assertEqual(cli.DB_PATH, before,
                         "the fixture left the module global on a temp path")


if __name__ == "__main__":
    unittest.main()
