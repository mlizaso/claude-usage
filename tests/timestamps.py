"""Fixture helpers for building transcript timestamps that land on a local day.

Claude Code writes ISO-8601 **UTC** timestamps, while `today` / `week` and the
dashboard bucket by the viewer's **local** calendar day. So a fixture cannot
paste a local date in front of a fixed ``"T10:00:00Z"`` and assume the row lands
on the day it names: at UTC+14 that instant is already the next local day, and
at UTC-11 it is still the previous one. Fixtures written that way pass or fail
depending on where the suite runs — CI runs in UTC and never notices.

Build the instant instead: pick the local wall-clock time you mean, then convert
it to the UTC string a real transcript would contain.
"""

from datetime import date, datetime, time, timedelta, timezone


def utc_ts_on_local_day(days_ago=0, hour=12, minute=0):
    """UTC transcript timestamp for `hour:minute` local time, `days_ago` back.

    Midday is the default because it is the furthest point from either midnight,
    so the row stays on the intended local day in every real timezone.
    """
    local_naive = datetime.combine(local_day_date(days_ago), time(hour, minute))
    # .astimezone() on a naive datetime reads it as local time on that date,
    # which is what applies the right offset across a DST boundary.
    return (local_naive.astimezone()
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.000Z"))


def local_day_date(days_ago=0):
    """The local calendar date `days_ago` before today, as a ``date``."""
    return date.today() - timedelta(days=days_ago)


def local_day(days_ago=0):
    """The local calendar date `days_ago` before today, as ``YYYY-MM-DD``.

    This is exactly what the CLI commands compute with ``date.today()``, so
    assertions built from it match the labels those commands print.
    """
    return local_day_date(days_ago).isoformat()


def local_day_of(timestamp):
    """Local calendar day containing an ISO-8601 UTC timestamp.

    Fixture expectations should be derived from the exact instant written to
    disk when setup may run long after the test module was imported.  Computing
    an expected day with a separate ``date.today()`` call makes the two disagree
    if a full suite crosses local midnight between those operations.
    """
    return (datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            .astimezone().date().isoformat())
