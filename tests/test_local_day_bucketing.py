"""Tests that days are bucketed by the viewer's local calendar day.

Transcript timestamps are ISO-8601 UTC; the reports and charts are labelled with
local dates. Bucketing by the raw UTC date prefix therefore filed usage under the
wrong day for anyone not on UTC — in CEST every turn between 00:00 and 02:00
local appeared on the previous day's row, in both `cli.py today` and the
dashboard's daily chart.

These tests force a timezone with `TZ` + `time.tzset()`, because the whole defect
is invisible in UTC — CI runs there, where local and UTC days coincide and a
regression to UTC bucketing would pass every other test in the suite. `tzset` is
POSIX-only, so they skip on Windows; the rest of the suite still covers these
code paths there, just without the timezone discrimination.
"""

import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import cli
import dashboard
import localdays
import rollups
from dashboard import get_dashboard_data
from scanner import get_db, init_db, insert_turns, upsert_sessions

# UTC+14 and UTC-11: the extremes where a UTC day and a local day differ most.
TZ_AHEAD = "Pacific/Kiritimati"
TZ_BEHIND = "Pacific/Midway"

requires_tzset = unittest.skipUnless(
    hasattr(time, "tzset"), "TZ cannot be changed in-process on this platform")


def _turn(message_id, ts, model="claude-opus-4-8", inp=100, out=50):
    return {
        "session_id": "sess-1", "timestamp": ts, "model": model,
        "input_tokens": inp, "output_tokens": out,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "tool_name": None, "cwd": None, "message_id": message_id,
        "is_subagent": 0, "agent_id": None,
    }


def _seed_db(db_path, turns, session_last=None):
    """A database holding `turns` and the one session they belong to."""
    conn = get_db(db_path)
    init_db(conn)
    first = turns[0]["timestamp"]
    upsert_sessions(conn, [{
        "session_id": "sess-1", "project_name": "user/proj",
        "first_timestamp": first, "last_timestamp": session_last or first,
        "git_branch": "main", "model": "claude-opus-4-8",
        "total_input_tokens": 0, "total_output_tokens": 0,
        "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 0,
    }])
    insert_turns(conn, turns)
    conn.commit()
    conn.close()


class TestHourlyTimestampNormalization(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        init_db(self.conn)

    def test_equivalent_offset_spellings_share_the_utc_hour(self):
        insert_turns(self.conn, [
            _turn("positive", "2026-09-28T01:30:00+02:00", out=7),
            _turn("utc", "2026-09-27T23:30:00Z", out=11),
            _turn("negative", "2026-09-27T20:30:00-03:00", out=13),
        ])
        rows = rollups.hourly_by_model(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["day"], rows[0]["hour"]), ("2026-09-27", 23))
        self.assertEqual((rows[0]["turns"], rows[0]["output"]), (3, 31))
        self.assertEqual(rows[0]["local_day"],
                         rollups.daily_by_model(self.conn)[0]["day"])

    def test_an_unstamped_row_uses_the_same_utc_rule_and_source_scope(self):
        claude = _turn("claude", "2026-09-28T01:30:00+02:00", out=7)
        codex = {**_turn("codex", "2026-09-27T23:30:00Z", out=11),
                 "source": "codex"}
        insert_turns(self.conn, [claude, codex])
        self.conn.execute("UPDATE turns SET timestamp_order = ''")
        rows = rollups.hourly_by_model(self.conn, "claude")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["day"], rows[0]["hour"]), ("2026-09-27", 23))
        self.assertEqual((rows[0]["source"], rows[0]["output"]), ("claude", 7))
        self.assertEqual(sum(row["output"] for row in
                             rollups.hourly_by_model(self.conn)), 18)

    def test_an_impossible_calendar_date_keeps_its_raw_bucket(self):
        insert_turns(self.conn, [
            _turn("invalid", "2026-02-31T03:30:00+02:00", out=7),
            _turn("overflow", "9999-12-31T23:30:00-14:00", out=11),
        ])
        rows = rollups.hourly_by_model(self.conn)
        self.assertEqual([(row["day"], row["hour"], row["output"]) for row in rows],
                         [("2026-02-31", 3, 7), ("9999-12-31", 23, 11)])


class _ForcedTimezone(unittest.TestCase):
    """Base class that pins the process timezone for the duration of a test."""

    TZ = TZ_AHEAD

    def setUp(self):
        self._orig_tz = os.environ.get("TZ")
        os.environ["TZ"] = self.TZ
        time.tzset()
        self.addCleanup(self._restore_tz)
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"

    def _restore_tz(self):
        if self._orig_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._orig_tz
        time.tzset()

    def _seed(self, turns, session_last=None):
        _seed_db(self.db_path, turns, session_last)


@requires_tzset
class TestDashboardDailyBuckets(_ForcedTimezone):
    """At UTC+14, 12:00Z is already 02:00 the NEXT local day."""

    TZ = TZ_AHEAD

    def test_daily_bucket_uses_the_local_day(self):
        self._seed([_turn("m1", "2026-04-08T12:00:00Z")])
        rows = get_dashboard_data(db_path=self.db_path)["daily_by_model"]
        self.assertEqual([r["day"] for r in rows], ["2026-04-09"],
                         "12:00Z is 2026-04-09 02:00 in UTC+14; bucketing by the "
                         "raw UTC prefix would say 2026-04-08.")

    def test_two_turns_either_side_of_local_midnight_split_into_two_days(self):
        # 09:00Z -> 23:00 local on 04-08; 11:00Z -> 01:00 local on 04-09.
        # Both share a UTC day, so UTC bucketing would collapse them into one.
        self._seed([_turn("m1", "2026-04-08T09:00:00Z"),
                    _turn("m2", "2026-04-08T11:00:00Z")])
        rows = get_dashboard_data(db_path=self.db_path)["daily_by_model"]
        self.assertEqual(sorted(r["day"] for r in rows),
                         ["2026-04-08", "2026-04-09"])

    def test_session_row_day_matches_the_daily_chart(self):
        """The sessions table filters on last_date; it must agree with the bars."""
        self._seed([_turn("m1", "2026-04-08T12:00:00Z")],
                   session_last="2026-04-08T12:00:00Z")
        data = get_dashboard_data(db_path=self.db_path)
        self.assertEqual(data["sessions_all"][0]["last_date"], "2026-04-09")
        self.assertEqual(data["sessions_all"][0]["last"], "2026-04-09 02:00")
        self.assertEqual(data["daily_by_model"][0]["day"],
                         data["sessions_all"][0]["last_date"])

    def test_hourly_data_deliberately_stays_utc(self):
        """The hourly chart ships UTC day+hour pairs and shifts them in the
        browser behind a local/UTC toggle. Converting only its day key would
        desynchronise the pair across midnight, so it is intentionally left
        alone — this test records that as a decision, not an oversight."""
        self._seed([_turn("m1", "2026-04-08T12:00:00Z")])
        rows = get_dashboard_data(db_path=self.db_path)["hourly_by_model"]
        self.assertEqual(rows[0]["day"], "2026-04-08")
        self.assertEqual(rows[0]["hour"], 12)


@requires_tzset
class TestSubagentViewsUseLocalDay(_ForcedTimezone):
    """The subagent chart and the dispatch table are separate queries; they must
    land on the same local day as the main daily chart, or the same range filter
    would show a dispatch on one day and its tokens on another."""

    TZ = TZ_AHEAD

    def setUp(self):
        super().setUp()
        conn = get_db(self.db_path)
        init_db(conn)
        turn = _turn("m-sub", "2026-04-08T12:00:00Z")
        turn.update(is_subagent=1, agent_id="agent-1")
        insert_turns(conn, [turn])
        conn.execute(
            "INSERT INTO agents (agent_id, agent_type, dispatched_in_session, "
            "completed_at, status, total_tokens, total_duration_ms, tool_use_count) "
            "VALUES ('agent-1', 'Explore', 'sess-1', '2026-04-08T12:05:00Z', "
            "'completed', 150, 5000, 3)")
        conn.commit()
        conn.close()

    def test_subagent_daily_bucket_is_local(self):
        rows = get_dashboard_data(db_path=self.db_path)["subagent_by_type"]
        self.assertEqual([r["day"] for r in rows], ["2026-04-09"])
        self.assertEqual(rows[0]["agent_type"], "Explore")

    def test_dispatch_start_is_local(self):
        rows = get_dashboard_data(db_path=self.db_path)["top_dispatches"]
        self.assertEqual(rows[0]["start_date"], "2026-04-09")
        self.assertEqual(rows[0]["start"], "2026-04-09 02:00")
        # And the aggregate-inside-strftime form must not have produced NULL.
        self.assertNotEqual(rows[0]["start"], "")


@requires_tzset
class TestDashboardDailyBucketsBehindUTC(_ForcedTimezone):
    """At UTC-11, 02:00Z is still 15:00 the PREVIOUS local day."""

    TZ = TZ_BEHIND

    def test_daily_bucket_uses_the_local_day(self):
        self._seed([_turn("m1", "2026-04-08T02:00:00Z")])
        rows = get_dashboard_data(db_path=self.db_path)["daily_by_model"]
        self.assertEqual([r["day"] for r in rows], ["2026-04-07"])


@requires_tzset
class TestCliTodayUsesLocalDay(_ForcedTimezone):
    """`today` must count what the user did during their own calendar day."""

    TZ = TZ_AHEAD

    def setUp(self):
        super().setUp()
        self._orig_db = cli.DB_PATH
        cli.DB_PATH = self.db_path
        self.addCleanup(lambda: setattr(cli, "DB_PATH", self._orig_db))

    def _today(self):
        buf = StringIO()
        with redirect_stdout(buf):
            cli.cmd_today()
        return buf.getvalue()

    def _utc_for_local(self, local_hour):
        """UTC timestamp for `local_hour` today, computed in the forced zone."""
        from datetime import datetime, time as dtime, timezone
        from datetime import date as ddate
        naive = datetime.combine(ddate.today(), dtime(local_hour, 30))
        return (naive.astimezone().astimezone(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S.000Z"))

    def test_usage_just_after_local_midnight_counts_as_today(self):
        """The original report: at UTC+14 a turn at 00:30 local is 10:30Z the
        PREVIOUS UTC day, so UTC bucketing dropped it from `today` entirely."""
        self._seed([_turn("m1", self._utc_for_local(0), inp=1_000_000, out=0)])
        out = self._today()
        self.assertNotIn("No usage recorded today.", out)
        self.assertRegex(out, r"claude-opus-4-8\s+turns=1\s+in=1\.00M")
        self.assertIn("$5.0000", out)

    def test_usage_just_before_local_midnight_counts_as_today(self):
        self._seed([_turn("m1", self._utc_for_local(23), inp=1_000_000, out=0)])
        out = self._today()
        self.assertNotIn("No usage recorded today.", out)
        self.assertRegex(out, r"TOTAL\s+turns=1\s+in=1\.00M")

    def test_yesterdays_local_usage_is_not_counted_as_today(self):
        """The boundary has to hold in both directions."""
        from datetime import datetime, time as dtime, timedelta, timezone
        from datetime import date as ddate
        naive = datetime.combine(ddate.today() - timedelta(days=1), dtime(12, 0))
        ts = (naive.astimezone().astimezone(timezone.utc)
              .strftime("%Y-%m-%dT%H:%M:%S.000Z"))
        self._seed([_turn("m1", ts)])
        self.assertIn("No usage recorded today.", self._today())


@requires_tzset
class TestUtcWindowIsLossless(unittest.TestCase):
    """`cli.utc_window` is a pure optimisation and must stay one.

    The local-day expression is a function of the column, so filtering on it
    alone cannot use idx_turns_timestamp. The commands therefore bracket the
    query with a raw-timestamp window first. That window is only safe while it
    is a strict superset of the local day — if it ever clipped an edge, `today`
    would quietly under-report near midnight, which is exactly the class of bug
    the local-day change was made to fix.

    So compare the windowed count against the unwindowed one at every UTC hour,
    in zones that include 30- and 45-minute offsets, over a fixture that carries
    **non-`Z` offsets as well as `Z`**. That last part is not decoration: while
    the fixture held only `Z` timestamps this test passed against a one-day
    window that was NOT a superset, because the skew it has to survive is
    |timestamp offset| + |viewer offset| and `Z` contributes nothing to the
    first term. Reverting `utc_window` to one day reds this test now.
    """

    ZONES = ["UTC", TZ_AHEAD, TZ_BEHIND, "Asia/Kathmandu", "Australia/Lord_Howe",
             "America/Los_Angeles", "Europe/Madrid", "Pacific/Chatham"]

    def setUp(self):
        self._orig_tz = os.environ.get("TZ")
        self.addCleanup(self._restore_tz)
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(self.db_path)
        init_db(conn)
        turns, i = [], 0
        for day in ("2026-04-13", "2026-04-14", "2026-04-15", "2026-04-16", "2026-04-17"):
            for hour in range(24):
                for minute in (0, 30, 59):
                    i += 1
                    turns.append(_turn(f"m{i}", f"{day}T{hour:02d}:{minute:02d}:00.000Z",
                                       inp=1, out=0))
            # Non-`Z` OFFSETS, which is what this fixture lacked while the
            # window was one day wide and this test still passed. The skew
            # between a timestamp's own ISO prefix (what the window compares
            # lexicographically) and its local day key is
            # |timestamp offset| + |viewer offset|, so it only exceeds 24h when
            # BOTH are large -- unreachable with `Z` alone, and reachable with
            # ordinary data: `-12:00` and `+14:00` are both real zones, and
            # `2026-08-13T23:00:00-12:00` seen from Pacific/Kiritimati made
            # `cli today` report $1.00 where `cli stats` and `/api/data` both
            # reported $6.00.
            for offset in ("-12:00", "+14:00", "-09:30", "+05:45"):
                for hour in (0, 23):
                    i += 1
                    turns.append(_turn(f"o{i}", f"{day}T{hour:02d}:30:00{offset}",
                                       inp=1, out=0))
        insert_turns(conn, turns)
        conn.commit()
        conn.close()

    def _restore_tz(self):
        if self._orig_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._orig_tz
        time.tzset()

    def test_window_never_excludes_a_matching_row(self):
        conn = sqlite3.connect(self.db_path)
        self.addCleanup(conn.close)
        local_day = localdays.LOCAL_DAY
        for tz in self.ZONES:
            os.environ["TZ"] = tz
            time.tzset()
            for day in ("2026-04-14", "2026-04-15", "2026-04-16"):
                with self.subTest(tz=tz, day=day):
                    exact = conn.execute(
                        f"SELECT COUNT(*) FROM turns WHERE {local_day} = ?",
                        (day,)).fetchone()[0]
                    lo, hi = localdays.utc_window(day, day)
                    windowed = conn.execute(
                        "SELECT COUNT(*) FROM turns "
                        f"WHERE timestamp >= ? AND timestamp < ? AND {local_day} = ?",
                        (lo, hi, day)).fetchone()[0]
                    self.assertEqual(windowed, exact)
                    self.assertGreater(exact, 0, "fixture should cover this day")


@requires_tzset
class TestMalformedTimestampsSurviveBucketing(_ForcedTimezone):
    """date(..., 'localtime') returns NULL for unparseable input.

    The scanner stores whatever bounded text a transcript carried, so a corrupt
    or hostile timestamp is possible. Such a turn must keep landing in its raw
    prefix bucket rather than vanishing from every total — a silent drop would be
    worse than a wrong bucket, and NULL is what an unguarded date() would give.
    """

    TZ = TZ_AHEAD

    def test_unparseable_timestamp_is_not_dropped(self):
        conn = get_db(self.db_path)
        init_db(conn)
        insert_turns(conn, [
            _turn("m-good", "2026-04-08T12:00:00Z", inp=10, out=10),
            _turn("m-bad", "not-a-timestamp", inp=777, out=0),
        ])
        conn.commit()
        conn.close()

        data = get_dashboard_data(db_path=self.db_path)
        total_input = sum(r["input"] for r in data["daily_by_model"])
        self.assertEqual(total_input, 787,
                         "the malformed row must still be counted somewhere")
        self.assertIn("not-a-time", [r["day"] for r in data["daily_by_model"]])

        # And it must not have become a NULL day key in the JSON payload.
        self.assertNotIn(None, [r["day"] for r in data["daily_by_model"]])


class TestMalformedTimestampsStillRenderTheirLocalTime(unittest.TestCase):
    """The same property for `local_minute_expr`, the display half of the pair.

    Both expressions in localdays.py carry the same COALESCE for the same
    reason, but only the day one was pinned above. The minute one feeds the two
    columns a reader actually looks at — `sessions_all[].last`, a plain column,
    and `top_dispatches[].start`, an aggregate (`MIN(t.timestamp)`) wrapped by
    strftime. Unguarded, strftime answers NULL for both and the cell renders
    empty, so a corrupt timestamp reads as *missing* data beside a day column
    that still shows its raw prefix — a worse lie than a wrong bucket.

    Deliberately not under `@requires_tzset` like its twin: the fallback arm is
    string manipulation, so no timezone can change what it answers, and forcing
    one would only cost the assertion on Windows, where those classes skip.
    """

    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        dispatched = _turn("m-bad-sub", "not-a-timestamp")
        dispatched.update(is_subagent=1, agent_id="agent-1")
        _seed_db(self.db_path, [_turn("m-bad", "not-a-timestamp"), dispatched],
                 session_last="not-a-timestamp")
        self.data = get_dashboard_data(db_path=self.db_path)

    def test_a_sessions_last_active_keeps_its_raw_text(self):
        row = self.data["sessions_all"][0]
        self.assertEqual(row["last"], "not-a-timestamp",
                         "an unparseable last_timestamp must still print, not "
                         "blank the Last Active cell and its CSV column")
        # The day column beside it shows the raw prefix, as its own test above
        # requires. That contrast is the point: half a row of raw text and half
        # a row of nothing is what a missing fallback would render.
        self.assertEqual(row["last_date"], "not-a-time")

    def test_a_dispatch_start_keeps_its_raw_text(self):
        """The aggregate-inside-strftime form, which is the fragile one."""
        row = self.data["top_dispatches"][0]
        self.assertEqual(row["start"], "not-a-timestamp")
        self.assertEqual(row["start_date"], "not-a-time")


# The seven shapes a real transcript timestamp comes in, and the two extreme
# offsets that make the |timestamp offset| + |viewer offset| sum reach two days.
_REAL_TIMESTAMP_SHAPES = ("{d}T00:00:00.000Z", "{d}T23:59:59Z",
                          "{d}T12:00:00+14:00", "{d}T12:00:00-12:00",
                          "{d} 06:30:00", "{d}T09:15:00.123456Z", "{d}")

# ISO-SHAPED but calendar-impossible: SQLite's date() normalises each of these
# FORWARD across the month boundary rather than refusing it. `2026-02-31` is the
# worst, landing three days past its own prefix -- outside `utc_window`.
_CALENDAR_OVERFLOWS = ("2026-02-29", "2026-02-30", "2026-02-31", "2026-04-31",
                       "2026-06-31", "2026-09-31", "2026-11-31", "2025-02-29")


class TestTheDateGateIsARoundTripNotAShapeCheck(unittest.TestCase):
    """A day key must be derivable from the string, and shape does not prove it.

    `local_day_expr` may only hand a value to SQLite's date() when that value's
    own day key IS its first ten characters, because `utc_window` compares those
    ten characters lexicographically while the bucket is what date() answers. The
    round-14 gate tested the ISO SHAPE and its comment claimed exactly this
    guarantee -- but date() does not reject a calendar overflow, it NORMALISES
    one, so `2026-02-31T12:00:00Z` passed the shape check and keyed to
    `2026-03-03`, three days past its own prefix and outside a window widened by
    two. That is the precise class the gate was added to exclude.

    Timezone-independent on purpose, so it runs on Windows too: the property is
    that the key equals the string's own prefix, which no offset can change --
    every value here is rejected by the gate and never reaches `'localtime'`.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def _key(self, value):
        expr = localdays.local_day_expr("v")
        return self.conn.execute(
            f"SELECT {expr} FROM (SELECT ? AS v)", (value,)).fetchone()[0]

    def test_a_calendar_overflow_stays_in_its_own_raw_bucket(self):
        for day in _CALENDAR_OVERFLOWS:
            value = f"{day}T12:00:00Z"
            with self.subTest(value=value):
                self.assertEqual(
                    self._key(value), day,
                    "date() normalises this forward instead of refusing it, so "
                    "an ungated (or merely shape-gated) expression buckets it "
                    "past its own prefix, where utc_window cannot reach it")

    def test_the_shape_check_alone_would_not_have_caught_them(self):
        """Refuse to pass vacuously: prove each fixture defeats the old gate.

        Without this, a fixture of values SQLite happened to reject outright
        would satisfy the test above while proving nothing about the overflow
        class, which is exactly how the shape gate shipped believing it was one.
        """
        glob = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*"
        old = (f"COALESCE(date(CASE WHEN v GLOB '{glob}' THEN v END), "
               "substr(v, 1, 10))")
        for day in _CALENDAR_OVERFLOWS:
            value = f"{day}T12:00:00Z"
            with self.subTest(value=value):
                shaped = self.conn.execute(
                    f"SELECT {old} FROM (SELECT ? AS v)", (value,)).fetchone()[0]
                self.assertNotEqual(
                    shaped, day,
                    "this fixture value does not defeat the shape gate, so it "
                    "cannot demonstrate that the round trip is stronger")

    def test_the_re_interpreted_values_the_gate_was_added_for_still_bin(self):
        """The round trip must not be weaker than the shape check it replaced."""
        for value in ("now", "2460000.5", "1234567890", "start of day",
                      "not-a-timestamp", "2026-13-45T00:00:00Z", "9999-99-99"):
            with self.subTest(value=value):
                self.assertEqual(self._key(value), value[:10])

    def test_no_real_calendar_day_is_excluded(self):
        """The other half, and the one a tightening usually gets wrong.

        A gate that binned legitimate data would move every well-formed row out
        of its own local-day bucket and into a UTC-prefix one -- silently, and
        for everyone. Checked over a decade of real days rather than a handful,
        in the seven timestamp shapes the two parsers actually produce.
        """
        import datetime
        expr = localdays.local_day_expr("v")
        day, end = datetime.date(2020, 1, 1), datetime.date(2030, 12, 31)
        checked = 0
        while day <= end:
            iso = day.isoformat()
            for shape in _REAL_TIMESTAMP_SHAPES:
                value = shape.format(d=iso)
                gated, ungated = self.conn.execute(
                    f"SELECT {expr}, date(v, 'localtime') FROM (SELECT ? AS v)",
                    (value,)).fetchone()
                checked += 1
                self.assertEqual(
                    gated, ungated,
                    f"the gate refused the well-formed timestamp {value!r}")
            day += datetime.timedelta(days=1)
        self.assertGreater(checked, 25000, "the sweep collapsed to nothing")


@requires_tzset
class TestACalendarOverflowIsNotDroppedByTheWindow(_ForcedTimezone):
    """End to end: `cli today` and `/api/data` must not disagree about a row.

    This is the same failure mode as round 14's `-12:00`/`Pacific/Kiritimati`
    reproduction, reached through the other half of the pair. `reports.py`
    brackets its queries with `utc_window` and the payload does not, so any row
    whose day key sits further from its own ISO prefix than the widening is
    counted by `/api/data` and dropped by the CLI. Ungated, `2026-02-31T12:00:00Z`
    keys to `2026-03-03` while sorting below `2026-03-01` -- the window's low
    bound for that day -- so the CLI loses it.
    """

    TZ = "UTC"

    def _seed_pair(self):
        self._seed([
            _turn("m-good", "2026-03-03T12:00:00Z", inp=10, out=10),
            _turn("m-overflow", "2026-02-31T12:00:00Z", inp=777, out=0),
        ], session_last="2026-03-03T12:00:00Z")
        conn = sqlite3.connect(self.db_path)
        self.addCleanup(conn.close)
        return conn

    def test_the_overflow_row_keys_to_its_own_prefix(self):
        conn = self._seed_pair()
        self.assertEqual(
            conn.execute(f"SELECT {localdays.LOCAL_DAY} FROM turns "
                         "WHERE message_id = 'm-overflow'").fetchone()[0],
            "2026-02-31",
            "ungated, date() normalises this to 2026-03-03 -- three days past "
            "its own prefix, which utc_window (widened by two) cannot reach")

    def test_the_week_query_shape_does_not_clip_it(self):
        """`_cmd_week`'s exact WHERE, over real day bounds that span the row.

        The week report is the one that can legitimately ask for the overflow
        row: its `LOCAL_DAY BETWEEN ? AND ?` is a lexicographic range, and
        `2026-02-31` sorts inside `2026-02-25`..`2026-03-03`. So the window in
        front of it must not clip what that BETWEEN matches -- otherwise `cli
        week` prints a smaller total than `/api/data` for the same days.
        """
        conn = self._seed_pair()
        local_day = localdays.LOCAL_DAY
        start, end = "2026-02-25", "2026-03-03"
        lo, hi = localdays.utc_window(start, end)
        windowed = conn.execute(
            "SELECT COALESCE(SUM(input_tokens), 0) FROM turns WHERE timestamp "
            f">= ? AND timestamp < ? AND {local_day} BETWEEN ? AND ?",
            (lo, hi, start, end)).fetchone()[0]
        exact = conn.execute(
            "SELECT COALESCE(SUM(input_tokens), 0) FROM turns WHERE "
            f"{local_day} BETWEEN ? AND ?", (start, end)).fetchone()[0]
        self.assertEqual(windowed, exact, "utc_window clipped a row its own "
                         "LOCAL_DAY test matches")
        self.assertEqual(exact, 787, "the fixture must reach both rows")

    def test_the_today_query_can_never_ask_for_that_bucket(self):
        """Why a raw-prefix bucket is harmless rather than merely tolerable.

        `_cmd_today` tests `LOCAL_DAY = ?` with a day `date.today()` produced, so
        it is always a real calendar day and can never equal a gated-out row's
        key. The row is therefore invisible to `today` on both sides rather than
        counted by one -- which is the whole argument for binning these values
        instead of teaching the CLI to reproduce SQLite's re-interpretation.
        """
        conn = self._seed_pair()
        for day in ("2026-02-28", "2026-03-01", "2026-03-02", "2026-03-03"):
            lo, hi = localdays.utc_window(day, day)
            with self.subTest(day=day):
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM turns WHERE timestamp >= ? AND "
                        f"timestamp < ? AND {localdays.LOCAL_DAY} = ?",
                        (lo, hi, day)).fetchone()[0],
                    conn.execute(
                        f"SELECT COUNT(*) FROM turns WHERE "
                        f"{localdays.LOCAL_DAY} = ?", (day,)).fetchone()[0])

    def test_the_payload_still_counts_the_overflow_row(self):
        """A wrong bucket is survivable; a vanished row is not."""
        self._seed([
            _turn("m-good", "2026-03-03T12:00:00Z", inp=10, out=10),
            _turn("m-overflow", "2026-02-31T12:00:00Z", inp=777, out=0),
        ], session_last="2026-03-03T12:00:00Z")
        rows = get_dashboard_data(db_path=self.db_path)["daily_by_model"]
        self.assertEqual(sum(r["input"] for r in rows), 787)
        self.assertIn("2026-02-31", [r["day"] for r in rows])
        self.assertNotIn(None, [r["day"] for r in rows])


@requires_tzset
class TestTheHourlyRollupSplitsAnHourAcrossTwoLocalDays(_ForcedTimezone):
    """The SQL end of PAGE-HOURLY-SUBHOUR-ZONE.

    `hourly_by_model` is invariant 4's one deliberate exemption: it ships UTC
    day+hour PAIRS so the browser can re-bucket them behind its toggle. That is
    about the pair, and it used to mean the payload carried no local day at all
    — so the client derived membership from the bucket's UTC hour START, which
    is exact only where a local-day boundary falls on an hour boundary.

    At a sub-hour offset it never does. Kolkata is +05:30, so local midnight is
    18:30Z and the 18:00Z bucket holds turns on two local days. The client saw
    one bucket and one derived day; the stat tiles bucket per TURN with the same
    expression `daily_by_model` uses, so the two disagreed about which turns the
    range contained.

    Both assertions matter and they are different: the first is that the split
    happens at all, the second is that the key it splits on is the SAME key the
    tiles group by. A `local_day` computed by some other rule would satisfy the
    first and still leave the card and the tiles describing different turns.
    """

    TZ = "Asia/Kolkata"  # +05:30 year-round; no DST to confound the boundary

    def setUp(self):
        super().setUp()
        # 23:30 and 23:50 local on the 16th, then 00:10 local on the 17th.
        self._seed([_turn("m-before", "2026-08-16T18:00:00Z", out=10),
                    _turn("m-also-before", "2026-08-16T18:20:00Z", out=10),
                    _turn("m-after", "2026-08-16T18:40:00Z", out=10)],
                   session_last="2026-08-16T18:40:00Z")
        self.payload = get_dashboard_data(db_path=self.db_path)

    def test_one_utc_bucket_arrives_as_one_row_per_local_day(self):
        rows = self.payload["hourly_by_model"]
        self.assertEqual({(r["day"], r["hour"]) for r in rows},
                         {("2026-08-16", 18)},
                         "all three turns share one UTC hour, and the pair the "
                         "toggle re-buckets must stay that pair")
        self.assertEqual(
            {r["local_day"]: r["turns"] for r in rows},
            {"2026-08-16": 2, "2026-08-17": 1},
            "the bucket straddles local midnight, so it is two local days' "
            "worth of turns and has to arrive as two rows")

    def test_the_split_key_is_the_key_the_tiles_group_by(self):
        hourly = {r["local_day"] for r in self.payload["hourly_by_model"]}
        daily = {r["day"] for r in self.payload["daily_by_model"]}
        self.assertEqual(hourly, daily,
                         "the hourly card's membership key and the daily "
                         "rollup's day key must be the one local-day definition")

    def test_no_total_moves_because_of_the_split(self):
        """The split is a relabelling. Every figure the card sums must still
        agree with the rollup the tiles are built from."""
        hourly = self.payload["hourly_by_model"]
        daily = self.payload["daily_by_model"]
        self.assertEqual(sum(r["turns"] for r in hourly),
                         sum(r["turns"] for r in daily))
        self.assertEqual(sum(r["output"] for r in hourly),
                         sum(r["output"] for r in daily))


if __name__ == "__main__":
    unittest.main()
