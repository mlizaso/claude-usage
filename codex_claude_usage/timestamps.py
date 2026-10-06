"""Neutral ISO-8601 timestamp ordering for storage and reporting.

The raw timestamp spelling remains part of the stored data. These helpers only
decide chronological order: aware and timezone-less ISO forms are compared as
UTC, while malformed values use a deterministic lexical fallback.
"""

from datetime import datetime, timezone


def parse_instant(value):
    """Parse ISO-8601 text as an aware UTC datetime, or return ``None``.

    A missing offset means UTC. Conversion is part of validation: a year-one
    or year-9999 value can parse but overflow when its offset is applied.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def register_timestamp_order(conn):
    """Register the shared timestamp ordering function on ``conn``.

    SQLite functions belong to a connection, not to a database file.  Keep
    registration beside the implementation so every query boundary can install
    exactly the same function without duplicating the callback definition.
    """
    conn.create_function("timestamp_order", 1, timestamp_order)


def _timestamp_sort_key(value):
    """Return the total-order key used by both Python and SQLite storage.

    The rank prefixes intentionally mirror the historical comparator: a
    year-one instant that underflows during UTC conversion sorts below the
    malformed-text bucket, valid instants sort by their UTC instant, and a
    year-9999 instant that overflows sorts above it.  The raw suffix makes
    equal instants deterministic without changing the displayed spelling.
    """
    raw = value if isinstance(value, str) else ""
    if not raw:
        return (0, "", raw)
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            # Transcript clocks use UTC when they omit an explicit offset.
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        # A year-9999 value with a negative offset can parse but cannot be
        # converted to UTC because the instant lies beyond datetime.max. The
        # symmetric year-0001 underflow stays below valid instants. Other
        # malformed values use the ordinary deterministic lexical bucket.
        if candidate.startswith("9999-"):
            return (2, raw, raw)
        if candidate.startswith("0001-"):
            return (-1, raw, raw)
        return (0, raw, raw)
    return (1, parsed, raw)


def timestamp_order(value):
    """Return a SQLite-safe, lexically sortable key for a raw timestamp.

    Raw timestamp text is retained for display and local-calendar bucketing.
    This separate value is fixed-width UTC text for valid instants and keeps
    the comparator's deterministic raw-text fallback for malformed input. It
    is deliberately a string rather than epoch seconds so years outside the
    platform epoch, and the two datetime boundary cases above, remain ordered
    without loss of precision.
    """
    rank, instant_or_raw, raw = _timestamp_sort_key(value)
    if rank == 1:
        instant = instant_or_raw
        canonical = (
            f"{instant.year:04d}-{instant.month:02d}-{instant.day:02d}T"
            f"{instant.hour:02d}:{instant.minute:02d}:{instant.second:02d}."
            f"{instant.microsecond:06d}Z"
        )
        # Include the raw value to preserve timestamp_compare's deterministic
        # tie-break when two spellings denote the same instant.
        return f"2|{canonical}|{raw}"
    if rank == -1:
        return f"0|{raw}"
    if rank == 0:
        return f"1|{raw}"
    return f"3|{raw}"


def timestamp_compare(left, right):
    """Compare timestamp strings by instant, retaining raw values."""
    left_key, right_key = _timestamp_sort_key(left), _timestamp_sort_key(right)
    return (left_key > right_key) - (left_key < right_key)


def timestamp_min(left, right):
    """Return the raw value that sorts earliest under ``timestamp_compare``."""
    left = left if isinstance(left, str) else ""
    right = right if isinstance(right, str) else ""
    if not left:
        return right
    if not right:
        return left
    return left if timestamp_compare(left, right) <= 0 else right


def timestamp_max(left, right):
    """Return the raw value that sorts latest under ``timestamp_compare``."""
    left = left if isinstance(left, str) else ""
    right = right if isinstance(right, str) else ""
    if not left:
        return right
    if not right:
        return left
    return left if timestamp_compare(left, right) >= 0 else right
