"""Local-calendar-day SQL helpers, shared by the CLI and the dashboard.

Transcript timestamps are ISO-8601 UTC but every report is labelled with a local
date, so both halves of the product have to agree on where a day starts. They
used to hold separate copies of the same expression that AGENTS.md required be
"kept identical" by hand; this module is that agreement, in one place.

Bucketing by the raw `substr(timestamp, 1, 10)` UTC prefix filed a CEST user's
00:00-02:00 usage under the previous day. The COALESCE fallback is load-bearing:
date() returns NULL for text it cannot parse, and the scanner stores whatever a
transcript carried rather than a validated datetime, so without it a malformed
timestamp would drop out of every aggregate instead of staying in its raw bucket.

NULL is not the only way date() answers wrongly, and the other way is quieter:
handed a shape-valid but calendar-impossible date it NORMALISES rather than
refusing, so `2026-02-31` becomes `2026-03-03`. `local_day_expr` therefore gates
date() on a ROUND TRIP of the value's own ten-character prefix rather than on its
shape -- the same rule, and for the same reason, that `dayToLocalDate` applies to
a day key on the page.
"""

from datetime import date, timedelta


def local_day_expr(column="timestamp"):
    """SQL for `column`'s local calendar day, as YYYY-MM-DD.

    **The gate on date() is a ROUND TRIP, not a shape check**, and that
    distinction is the whole of it. SQLite's date() accepts far more than
    ISO-8601 -- `now`, a bare Julian day number, `start of day` -- whose calendar
    day is not derivable from the string's leading characters at all; and it
    *normalises* a calendar overflow rather than rejecting it, so the perfectly
    ISO-shaped `2026-02-31` comes back as `2026-03-03`. The scanner stores
    whatever bounded text a transcript carried, so both kinds reach the column.
    Either one makes this expression disagree with the raw prefix by more than
    the widening in `utc_window` below, and a row whose day key no lexicographic
    window can contain is a row `cli today/week` silently drops while
    `/api/data`, which uses no window, still reports it.

    So the value only reaches date() when its own first ten characters survive a
    round trip through date() unchanged, which is true exactly when they are a
    real calendar day. Anything else stays in the raw-prefix bucket the module
    docstring has always promised malformed input.

    **That bucket is not unreachable, and two sentences here used to say it
    was.** `_cmd_today` cannot ask for it -- `LOCAL_DAY = ?` against a string
    `date.today()` produced can never equal `2026-02-31` -- but `_cmd_week`'s
    `LOCAL_DAY BETWEEN ? AND ?` is a LEXICOGRAPHIC range, and `2026-02-31` sorts
    inside `2026-02-25`..`2026-03-03`, which is why
    `tests/test_local_day_bucketing.py::test_the_week_query_shape_does_not_clip_it`
    exists at all. So a gated-out row is counted by the week report's TOTAL
    while its "By Day:" loop, which iterates seven real calendar dates, prints
    no row for it. Measured 2026-08-15 on the two-row fixture that test uses:
    the BETWEEN matches both rows, and one of the two day keys it returns is a
    date no By Day line can carry. No total anywhere is wrong; what the reader
    loses is the ability to see which row a week TOTAL came from.

    This replaced a GLOB shape check, `[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*`,
    whose comment claimed exactly the round-trip guarantee while admitting every
    calendar overflow. Measured on SQLite 3.49.1: of the 412 calendar-impossible
    but ISO-shaped dates between 1990 and 2050, the GLOB gate put **all 412** in
    a bucket that is not their own prefix, and the round trip puts **none** of
    them there.

    It costs no legitimate timestamp, which is the half worth checking rather
    than assuming: over every real calendar day from 1990-01-01 to 2050-12-31 in
    seven time shapes -- `Z`, a bare date, a space separator, fractional seconds,
    and the `+14:00` / `-12:00` / `+05:45` extremes -- all 155,960 values key
    identically under both gates. The round trip is also strictly the stronger
    of the two: date() only ever returns `YYYY-MM-DD`, so a value that survives
    it necessarily matched that GLOB, and keeping both would be a second rule
    that can only ever agree with the first.

    No `file:line` and no keyword list: `substr(column, 1, 10)` is the same ten
    characters the COALESCE below falls back to, by construction, so the gate and
    the fallback cannot drift apart.
    """
    prefix = f"substr({column}, 1, 10)"
    return (f"COALESCE(date(CASE WHEN date({prefix}) = {prefix} "
            f"THEN {column} END, 'localtime'), {prefix})")


def local_minute_expr(column):
    """SQL for `column` as local 'YYYY-MM-DD HH:MM', for display.

    **Gated on the same round trip as `local_day_expr`, and that is the whole
    reason this docstring exists.** The two are siblings over one column -- the
    day a row is filtered by and the wall clock printed beside it -- and
    `rollups.sessions_all` says so outright: "both are local so a session lands
    on the same day the charts put its turns on".

    Gating only the day key broke that. For exactly the value class the gate was
    added for, the two disagreed: `2026-02-31T12:00:00Z` bucketed to
    `2026-02-31` (raw prefix, gated) while the sessions table and its CSV
    printed `2026-03-03 12:00` (SQLite's normalisation, ungated), so two
    sessions three days apart read identically in the table and sat in different
    day buckets. A stored literal `now` was worse: it printed the moment the
    payload was built, recomputed on every poll, so a session that never
    happened read as active right now. At the commit before the gate landed both
    fields agreed; this was a regression the gate introduced and did not finish.

    Cost rollups use local_day_expr; this display normalization must not
    change their accounting."""
    prefix = f"substr({column}, 1, 10)"
    return (f"COALESCE(strftime('%Y-%m-%d %H:%M', "
            f"CASE WHEN date({prefix}) = {prefix} THEN {column} END, "
            f"'localtime'), "
            f"replace(substr({column}, 1, 16), 'T', ' '))")


LOCAL_DAY = local_day_expr("timestamp")


def utc_window(first_local_day, last_local_day):
    """Raw-timestamp bounds that safely bracket a range of local days.

    LOCAL_DAY is a function of the column, so a query filtered only by it cannot
    use idx_turns_timestamp and has to scan every turn. Widening the requested
    range gives a window that contains every matching row and *can* be served by
    the index; the exact LOCAL_DAY test then runs on just the rows inside it.

    Restricting the candidate rows avoids applying local-calendar conversion
    to the entire history. Its benefit depends on the selected date range
    and data distribution; no particular wall-clock speedup is assumed.

    **TWO days on each side, not one.** The one-day version was justified by
    "local midnight is never more than a day from UTC midnight", which is false:
    the skew between a timestamp's own ISO prefix and its local day key is
    |timestamp offset| + |viewer offset|, and both halves reach 14 hours in real
    zones, so 2 days is achievable with entirely well-formed input. Measured:
    with the timestamp `2026-08-13T23:00:00-12:00` viewed from
    Pacific/Kiritimati (+14), the row keys to today and fell OUTSIDE the old
    window, so `cli today` reported $1.00 where `cli stats` and `/api/data` both
    reported $6.00 -- the CLI silently dropping money, and disagreeing with
    itself. Both offsets in that reproduction are in real-world use.

    The gate on date() above is the other half of the repair and neither half
    suffices alone: the gate keeps `now`, Julian numbers and calendar overflows
    like `2026-02-31` in their raw bucket, while only the wider window fixes the
    well-formed ISO case. What that raw bucket does and does not hide from a
    date-keyed query is written out under `local_day_expr` -- `today` cannot ask
    for it, `week`'s lexicographic BETWEEN can.

    Two days is exactly enough and not more than enough, which is only true
    because of that gate. Under it every value reaching date() has a real
    calendar day as its own prefix, so the skew is bounded by
    |timestamp offset| + |viewer offset| -- measured at a worst case of exactly
    2 days over `Z`/`+14:00`/`-12:00`/`+05:45`/`-09:30` timestamps read from
    seven zones including Pacific/Kiritimati and Etc/GMT+12. Remove the gate and
    that bound does not hold, and the arithmetic is worth writing out because it
    is the whole argument: ungated, `2026-02-31T12:00:00Z` keys to `2026-03-03`,
    whose window is `['2026-03-01', '2026-03-06')` -- and the timestamp string
    sorts BELOW `2026-03-01`, so the row matches its own day key and is excluded
    by the window in front of it. `/api/data` builds no window and still counts
    it; the CLI alone loses it.

    Returned as `[lo, hi)` half-open date strings, compared lexicographically
    against the ISO-8601 timestamps the scanner stores.

    **This function RAISES on a day key that is not a real calendar day**, and
    that is a documented limit rather than an oversight. `date.fromisoformat`
    refuses `2026-02-31`, which is exactly the bucket key the gate above hands a
    calendar-overflow row. No caller reaches it: all three sites in `reports.py`
    pass a string `date.today()` produced, never a key read back out of the
    database. Do not wrap this in a `try` to make it "safe" -- a day key arriving
    here from a query result is a programming error worth the traceback, and
    swallowing it would substitute a silently wrong window for a loud stop.
    """
    lo = (date.fromisoformat(first_local_day) - timedelta(days=2)).isoformat()
    hi = (date.fromisoformat(last_local_day) + timedelta(days=3)).isoformat()
    return lo, hi
