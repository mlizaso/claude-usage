"""Where a plan window actually began, once the cached one has expired.

`~/.claude.json` is a cache that outlives its own windows (invariant 7), so after
a reset there is no figure for the window you are in. `account.current_window_bounds`
fills that hole by rolling the last known reset forward in whole steps — the only
thing a pure module with no database can do.

That projection is right only while you keep working. A five-hour window does not
sit on a fixed grid: it begins at your first message after the previous one
expired. Continuous activity can keep resets five hours apart; an idle stretch
can delay the next window's start beyond that projection.

So after any idle stretch the projected start is early, and `usage_since` counts
turns from before the window opened — attributing work billed to a closed window
to the open one. `dashboard_data._correct_window_start` replaces the projection
with the first turn the database actually holds.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import account
import dashboard_data
import db


def _iso(dt):
    return dt.isoformat()


class WindowStartFixture(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        fd, self.path = tempfile.mkstemp(dir=self._tmpdir.name, suffix=".db")
        os.close(fd)
        self.conn = db.get_db(self.path)
        db.init_db(self.conn)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def turn(self, when, tokens=10, source="claude"):
        self.conn.execute(
            "INSERT INTO turns (session_id, timestamp, model, input_tokens, "
            "output_tokens, cache_read_tokens, cache_creation_tokens, "
            "message_id, source) VALUES ('s', ?, 'claude-opus-5', ?, ?, 0, 0, ?, ?)",
            (_iso(when), tokens, tokens, f"m-{when.timestamp()}-{source}", source))
        self.conn.commit()

    def window(self, resets_at):
        return {"kind": "session", "group": "", "resets_at": _iso(resets_at),
                "expired": True, "window_start": None}


class TestIdleGapMovesTheWindowStart(WindowStartFixture):
    def test_the_start_follows_the_first_turn_after_the_reset(self):
        """The case the grid gets wrong: the machine sat idle for hours after the
        window expired, so the new window opened when work resumed — not on the
        five-hour grid the projection walks."""
        reset = self.now - timedelta(hours=9)
        resumed = self.now - timedelta(hours=2)     # 7h idle, then work
        self.turn(self.now - timedelta(hours=20))   # in a long-closed window
        self.turn(resumed)
        self.turn(self.now - timedelta(minutes=5))

        w = self.window(reset)
        dashboard_data._correct_window_start(self.conn, w)

        self.assertEqual(w["window_start"], _iso(resumed))
        self.assertEqual(w["window_start_source"], "transcripts")
        # The grid projection would have started the window at reset + 5h, which
        # is 2 hours before any work happened.
        grid_start, _ = account.current_window_bounds(self.window(reset), self.now)
        self.assertNotEqual(grid_start.isoformat(), w["window_start"])
        self.assertLess(grid_start, resumed)

    def test_the_earlier_window_s_turns_are_not_counted_in_this_one(self):
        """Why it matters. `usage_since` reads `window_start`; an early start
        sweeps in turns that were billed to a window that has already closed."""
        reset = self.now - timedelta(hours=9)
        resumed = self.now - timedelta(hours=2)
        self.turn(self.now - timedelta(hours=20), tokens=999)   # closed window
        self.turn(resumed, tokens=7)

        w = self.window(reset)
        dashboard_data._correct_window_start(self.conn, w)
        recorded = dashboard_data.usage_since(self.conn, w["window_start"], "claude")
        self.assertEqual(recorded["turns"], 1)
        # 7 in + 7 out; the 999-token turn from the closed window is excluded.
        self.assertEqual(recorded["tokens"], 14)

    def test_continuous_work_still_lands_on_the_first_turn(self):
        """No idle gap: the first turn after the reset is moments later, so the
        corrected start and the grid projection agree to within that gap. The
        correction must not disturb the case the projection already got right."""
        reset = self.now - timedelta(hours=3)
        first = reset + timedelta(minutes=1)
        self.turn(first)
        self.turn(self.now - timedelta(minutes=1))

        w = self.window(reset)
        dashboard_data._correct_window_start(self.conn, w)
        self.assertEqual(w["window_start"], _iso(first))

    def test_several_whole_windows_of_work_roll_forward(self):
        """Work spread over more than one window since the stale reset: the walk
        must land in the window containing NOW, not the first one it finds."""
        reset = self.now - timedelta(hours=12)
        self.turn(reset + timedelta(minutes=5))            # window 1
        current = self.now - timedelta(minutes=30)
        self.turn(current)                                  # the live window
        w = self.window(reset)
        dashboard_data._correct_window_start(self.conn, w)
        start = account._parse_instant(w["window_start"])
        end = account._parse_instant(w["window_end"])
        self.assertLessEqual(start, self.now)
        self.assertGreater(end, self.now)
        self.assertEqual(w["window_start"], _iso(current))


class TestTheCorrectionIsActuallyWired(WindowStartFixture):
    """The tests above drive `_correct_window_start` directly, so they all keep
    passing if the call is deleted from `claude_limits` — the helper would be
    correct and dead. These call the real payload builder instead."""

    def _limits_with(self, resets_at):
        """Stub the seam `claude_limits` ACTUALLY reads.

        This replaced `account.current_limits`, and that stub was dead: since
        `82d113e` the payload comes from `dashboard_data._limits_payload`, which
        calls `account.read_config` and `account.current_limits_from_config`.
        `account.current_limits` survives only in the exception fallback, so on
        this developer's machine the real `~/.claude.json` supplied the windows,
        `_correct_window_start` corrected them against the fixture's own turns,
        and both assertions passed on data the test never chose.

        It failed the moment the machine changed. Under a redirected HOME
        `read_config` finds nothing, the payload carries no windows, and both
        tests died on `IndexError: list index out of range` -- which is what CI
        would have shown, on any checkout with no subscription config, had CI
        been running.

        Stubbed at `_limits_payload` rather than deeper so the class keeps its
        stated point: `claude_limits` is the real payload builder here, and it
        is the correction loop inside it that these tests exist to pin.
        """
        payload = {"available": True, "plan": "max",
                   "windows": [{"kind": "session", "group": "",
                                "resets_at": _iso(resets_at), "expired": True,
                                "percent": 100, "severity": "critical",
                                "window_start": _iso(resets_at),
                                "window_end": _iso(resets_at + timedelta(hours=5))}]}
        original = dashboard_data._limits_payload
        dashboard_data._limits_payload = lambda env=None: dict(
            payload, windows=[dict(w) for w in payload["windows"]])
        self.addCleanup(setattr, dashboard_data, "_limits_payload", original)
        return payload

    def test_claude_limits_applies_the_correction(self):
        reset = self.now - timedelta(hours=9)
        resumed = self.now - timedelta(hours=2)
        self.turn(self.now - timedelta(hours=20), tokens=999)
        self.turn(resumed, tokens=5)
        self._limits_with(reset)

        out = dashboard_data.claude_limits(self.conn)
        window = out["windows"][0]
        self.assertEqual(window["window_start"], _iso(resumed))
        self.assertEqual(window["window_start_source"], "transcripts")

    def test_recorded_usage_follows_the_corrected_start(self):
        """End to end: the number the panel prints excludes the closed window's
        work. This is the user-visible consequence of the whole fix.

        The earlier turn has to sit INSIDE the stale projection for this to
        prove anything. Put it before the cached reset — as this test did — and
        `usage_since` excludes it under both starts, so the assertion held with
        the correction deleted outright. Here the stale reset is 12h old and the
        first turn is 11h old: the uncorrected start sweeps that turn in, and
        only the roll-forward walk (5h windows, so one whole window past the
        stale reset) lands on the live one.
        """
        reset = self.now - timedelta(hours=12)
        stale = reset + timedelta(hours=1)          # window that has rolled over
        live = self.now - timedelta(hours=1)        # the window we are in now
        self.turn(stale, tokens=999)
        self.turn(live, tokens=5)
        self._limits_with(reset)

        window = dashboard_data.claude_limits(self.conn)["windows"][0]
        # 5 in + 5 out. Without the correction — or with a walk that stops at
        # the first turn it finds — this reads 2 turns / 2008 tokens, billing a
        # closed window's work to the open one.
        self.assertEqual(window["recorded"], {"turns": 1, "tokens": 10})
        self.assertEqual(window["window_start"], _iso(live))


class TestItRefusesToGuess(WindowStartFixture):
    def test_no_turns_since_the_reset_leaves_the_projection_alone(self):
        """Nothing has been sent since the window expired, so there is no open
        window to describe. Inventing one from the grid would claim the user is
        inside a window they have not started."""
        reset = self.now - timedelta(hours=6)
        self.turn(self.now - timedelta(hours=30))
        w = self.window(reset)
        w["window_start"] = "PROJECTED"
        dashboard_data._correct_window_start(self.conn, w)
        self.assertEqual(w["window_start"], "PROJECTED")
        self.assertNotIn("window_start_source", w)

    def test_an_unparseable_reset_is_left_alone(self):
        w = {"kind": "session", "group": "", "resets_at": "not a timestamp",
             "window_start": "PROJECTED"}
        dashboard_data._correct_window_start(self.conn, w)
        self.assertEqual(w["window_start"], "PROJECTED")

    def test_another_source_s_turns_never_open_a_claude_window(self):
        """Codex usage does not consume a Claude window. If it did, a Codex-only
        stretch would silently reset the Claude panel's clock."""
        reset = self.now - timedelta(hours=9)
        self.turn(self.now - timedelta(hours=2), source="codex")
        w = self.window(reset)
        w["window_start"] = "PROJECTED"
        dashboard_data._correct_window_start(self.conn, w)
        self.assertEqual(w["window_start"], "PROJECTED")


if __name__ == "__main__":
    unittest.main()
