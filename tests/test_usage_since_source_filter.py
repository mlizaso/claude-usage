"""Which assistant's turns count toward a Claude plan window.

`dashboard_data.usage_since` answers "N turns · M tokens recorded here" for the
plan panel, and it scopes that count with
`COALESCE(NULLIF(source, ''), 'claude') = ?`. Nothing exercised that predicate:
replacing it with `WHERE (? IS NOT NULL) AND timestamp >= ?` — same parameter
arity, filter gone — left the whole suite green, while its sibling filter inside
`_correct_window_start` twenty lines below is covered by
`test_another_source_s_turns_never_open_a_claude_window`. The standard existed;
it had simply not been applied here.

After a quota window rolls over, usage-since must still select the requested
assistant. Source-unqualified queries would mix unrelated usage into the new
window.

The blank-source half is tested too. `turns.source` defaults to `'claude'` but
the scanner writes `t.get("source", "claude")`, so a record carrying an explicit
`''` or NULL still lands blank; the COALESCE/NULLIF wrapper is what keeps those
rows counted as Claude, and a test that only checks `= ?` does not see it go.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import dashboard_data
import db


class UsageSinceFixture(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        fd, self.path = tempfile.mkstemp(dir=self._tmpdir.name, suffix=".db")
        os.close(fd)
        self.conn = db.get_db(self.path)
        db.init_db(self.conn)
        self.now = datetime.now(timezone.utc)
        self.start = self.now - timedelta(hours=1)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def turn(self, minutes_ago, tokens, source="claude"):
        """One turn, `minutes_ago` before now. `source` is written verbatim, so
        `None` and `''` reproduce the rows the COALESCE wrapper exists for."""
        when = (self.now - timedelta(minutes=minutes_ago)).isoformat()
        self.conn.execute(
            "INSERT INTO turns (session_id, timestamp, model, input_tokens, "
            "output_tokens, cache_read_tokens, cache_creation_tokens, "
            "message_id, source) VALUES ('s', ?, 'm', ?, 0, 0, 0, ?, ?)",
            (when, tokens, f"m-{minutes_ago}-{source}", source))
        self.conn.commit()

    def recorded(self, source="claude"):
        return dashboard_data.usage_since(self.conn, self.start.isoformat(), source)


class TestOnlyTheRequestedSourceIsCounted(UsageSinceFixture):
    def test_a_codex_turn_does_not_count_toward_a_claude_window(self):
        self.turn(30, 10)
        self.turn(20, 1000, source="codex")
        self.assertEqual(self.recorded(), {"turns": 1, "tokens": 10})

    def test_a_claude_turn_does_not_count_toward_a_codex_window(self):
        """The predicate has to be a filter, not a Claude-shaped constant."""
        self.turn(30, 10)
        self.turn(20, 1000, source="codex")
        self.assertEqual(self.recorded("codex"), {"turns": 1, "tokens": 1000})

    def test_a_blank_source_still_counts_as_claude(self):
        """Rows written before the column existed, and rows the scanner wrote
        with an explicit blank, are Claude's — that is what the COALESCE/NULLIF
        wrapper says. Dropping the wrapper and keeping `source = ?` silently
        loses them from every Claude window."""
        self.turn(40, 5, source="")
        self.turn(35, 6, source=None)
        self.turn(30, 10)
        self.turn(20, 1000, source="codex")
        self.assertEqual(self.recorded(), {"turns": 3, "tokens": 21})

    def test_only_one_source_present_is_still_filtered(self):
        """A Codex-only database must report zero for a Claude window rather
        than the Codex figure — `{turns: 0}` is truthy, so `claude_limits`
        assigns it and the panel shows it."""
        self.turn(30, 1000, source="codex")
        self.assertEqual(self.recorded(), {"turns": 0, "tokens": 0})


class TestTheWindowBoundIsStillApplied(UsageSinceFixture):
    """The other half of the same WHERE clause, so a repair to one cannot be
    made by deleting the other."""

    def test_a_turn_before_the_window_is_excluded(self):
        self.turn(120, 999)
        self.turn(30, 10)
        self.assertEqual(self.recorded(), {"turns": 1, "tokens": 10})


class TestTheContractAroundIt(UsageSinceFixture):
    """`claude_limits` calls this on every /api/data and /api/limits and only
    checks that the result is truthy, so the shape and the never-raises rule are
    both load-bearing."""

    def test_no_start_means_no_answer(self):
        self.assertIsNone(dashboard_data.usage_since(self.conn, ""))
        self.assertIsNone(dashboard_data.usage_since(self.conn, None))

    def test_a_broken_connection_returns_none_rather_than_raising(self):
        self.conn.execute("DROP TABLE turns")
        self.assertIsNone(self.recorded())

    def test_claude_is_the_default_source(self):
        self.turn(30, 10)
        self.turn(20, 1000, source="codex")
        self.assertEqual(
            dashboard_data.usage_since(self.conn, self.start.isoformat()),
            self.recorded("claude"))


if __name__ == "__main__":
    unittest.main()
