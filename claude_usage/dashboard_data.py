"""Reading the usage database into the JSON payload the dashboard renders.

The query layer, kept apart from the HTTP server that publishes it and the page
that draws it. Everything crossing this boundary is sanitised on the way out:
SQLite is dynamically typed and the values originate in transcripts, so numbers
are range-checked and every string passes through `terminal_safe` before it can
reach a browser.

Cost-bearing aggregates are grouped by (…, local day, model) so the client can
both range-filter and price per model. Aggregating a session's lifetime totals
against its single "primary" model is the defect this shape exists to prevent —
see `project_by_day_model`.

**This module does contain queries, and the boundary with rollups.py is not
"no SQL here".** The `.execute` calls below, AST-walked on 2026-08-15: seven
queries, held by `codex_limits_projection` (two), `codex_limit_history`,
`usage_since`, `_correct_window_start`, `available_sources` and
`_database_is_unscanned`; plus three `PRAGMA`s — a `busy_timeout` set on the
handed-in connection by each of the two read paths, `_payload_with_live_fields`
and `_collect_dashboard_data`, and the `data_version` probe in
`_database_version`, which runs on its own cached connection rather than on
`conn`. (`available_sources` opens its own and takes the same wait through
`connect_existing_db`'s timeout, so it needs no `PRAGMA` of its own.)

Walk that again rather than trusting the tally: this passage read "six
`conn.execute` calls and one `PRAGMA`" until that date and was short on both
halves — `_database_is_unscanned` had landed since it was written, and the
`data_version` probe was never in the count at all. AGENTS.md's module map
claimed there were none, in a sentence whose own neighbouring clause
contradicts it: a module that "owns the Codex quota projection" cannot be one
that never reads `usage_limits_snapshots`. The split that every function on both
sides actually obeys:

* **rollups.py owns the usage sections** — aggregates of what the scanner
  recorded. All ten take `(conn, source=None)`, because the page shows one
  assistant at a time and that scoping happens in SQL.
* **This module owns the connection, the quota surfaces, and the two endpoint
  bodies dashboard.py imports by name** (`claude_limits` for `/api/limits`,
  `available_sources` for `/api/sources`). A quota surface *cannot* take a
  `source`: `claude_limits` reads `~/.claude.json`, and the two Codex functions
  are `WHERE kind = 'codex'`. Each one **is** one assistant rather than being
  scoped to one, which is the discriminator, not a coincidence of naming.

`usage_since` and `_correct_window_start` are not sections at all — they are
`claude_limits`' own scalar aggregate and timestamp-ordered candidate scan —
and `codex_limits_projection`'s newest-turn scan is a one-value staleness stamp,
not a rollup. They use persisted neutral ordering keys because raw offset
spellings cannot be compared lexically.

Moving the quota surfaces into rollups.py would buy nothing the split exists to
buy. The ten inline blocks it cured were unreadable alone *and untestable
alone*; every function here is already named, already module-level and already
driven directly by a test. Mutate any of the six usage and quota queries and
tests/test_codex_transcripts.py, tests/test_usage_since_source_filter.py or
tests/test_window_start_from_transcripts.py goes red on its own (measured by
mutation, 2026-08-10). `_database_is_unscanned` is on none of those three, and
its own guard was found the same way: replacing its query with `SELECT 0` and
running the whole suite on 2026-08-15 reddened three tests in
tests/test_rebuilt_database_notice.py and nothing else. The `PRAGMA`s are the
unguarded part — observing one needs a concurrent writer. The `data_version`
probe is NOT: keying the payload cache is exactly why it is pinned. Swap it
for `PRAGMA user_version` (always 0, so the cache never invalidates) and
`tests/test_dashboard.py::TestThePayloadCacheCannotServeStaleData` reds three
tests — measured 2026-08-15, after an earlier version of this sentence widened
"unguarded" from the one busy_timeout it used to mean and asserted the
opposite. Moving the quota surfaces would
also drag `account` and `os.environ` into a module whose whole contract is
"hand me an open connection".

`tests/test_payload_surface.py::TestEveryPayloadSectionHasADeclaredOwner` is that
boundary made executable, so this paragraph cannot rot the way the sentence it
replaces did.
"""

import os
import sqlite3
import stat
import threading
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from . import account
from . import limits_core
from . import live_limits
from .db import (BUSY_TIMEOUT_MS, DB_PATH, connect_existing_db,
                 database_admission)
# Day buckets are the viewer's LOCAL calendar day. The client picks its range
# bounds from the local calendar (localISODate), so the day keys those bounds are
# compared against have to be local too; grouping by the raw UTC date prefix put a
# CEST user's 00:00-02:00 usage on the previous day's bar. The expressions live in
# localdays.py because cli.py needs exactly the same ones.
#
# Deliberately NOT applied to the hourly query: that data ships as UTC day+hour
# pairs the browser shifts behind a local/UTC toggle, and converting only its day
# key would desynchronise the pair across midnight.
from .localdays import local_day_expr as _local_day
from .localdays import local_minute_expr as _local_minute
from . import rollups
from .timestamps import register_timestamp_order, timestamp_compare, timestamp_order
from .timestamps import parse_instant as _parse_utc
from .safejson import (
    dashboard_number as _dashboard_number,
    dashboard_text as _dashboard_text,
    safe_dashboard_value as _safe_dashboard_value,
)


def _register_timestamp_order(conn):
    """Install the timestamp-ordering SQL function on a caller's connection.

    Dashboard-owned connections register it when they are opened, but the
    quota helpers are also public query functions and tests and integrations
    legitimately hand them ordinary ``sqlite3`` connections. SQLite functions
    are connection-local, so each helper that uses the compatibility fallback
    must make that dependency explicit at its boundary.
    """
    register_timestamp_order(conn)

# Cache database-derived sections while the database is unchanged. Use
# PRAGMA data_version on the same persistent connection: file mtime misses
# uncheckpointed WAL writes, and counters from different connections cannot
# be compared. Rebuild LIVE_PAYLOAD_FIELDS on every request.
PAYLOAD_CACHE_MAX_ENTRIES = 2

# A payload cheaper than this to build is never retained. The cache exists to
# remove an observable wait while avoiding the memory cost of retaining cheap
# payloads. Small fixture payloads stay uncached and hold no database file open.
PAYLOAD_CACHE_MIN_BUILD_SECONDS = 1.0

# The payload fields that are NOT a function of the stored bytes, and so cannot
# be served from a cache entry however fresh the database is:
#
# * `subscription_limits` reads ~/.claude.json — a cache Claude Code updates
#   independently of this database — and dates every window against the clock
#   (invariant 7).
# * `codex_limits` is a projection against `datetime.now(timezone.utc)`: both
#   `expired` and `age_seconds` change while the database does not.
# * `generated_at` is the assembler's own stamp, and a frozen one would make the
#   page claim a snapshot it did not take.
#
# `codex_limit_history` is deliberately NOT here: it is the stored series itself,
# with no `now` in it. Any new field belongs on one side or the other, and
# tests/test_payload_surface.py refuses a payload key that is on neither.
LIVE_PAYLOAD_FIELDS = ("subscription_limits", "codex_limits", "generated_at")

# Guards the cache dict AND the probe connection. The probe is opened in whichever
# server thread happens to miss first and read from another, so it is created with
# check_same_thread=False and every touch of it happens under this lock.
_CACHE_LOCK = threading.Lock()
# (database identity, source) -> (data_version, payload). OrderedDict as an LRU.
_PAYLOAD_CACHE = OrderedDict()
# ((path, st_dev, st_ino), connection), or None. At most one, ever.
_VERSION_PROBE = None
# Bumped every time a probe is opened. A reopened probe restarts SQLite's counter
# from its baseline — three fresh connections all read `2` — so without this a
# reading of 2 taken before a reopen would compare equal to a reading of 2 taken
# after one, across a window in which a commit could have landed. Carrying the
# generation in the version makes any reopen a mismatch, which is the whole point.
_PROBE_GENERATION = 0


def reset_payload_cache():
    """Drop every cached payload and close the probe connection.

    Public because a long-lived server is not the only caller: a test that
    rebuilds a database at a path it has already read, or that unlinks one, wants
    the mechanism back at its start state — and on Windows an open connection is
    what stops the unlink.
    """
    with _CACHE_LOCK:
        _close_version_probe()


def _close_version_probe():
    """Close the probe and clear the cache. The two cannot be separated.

    Every stored `data_version` came off the probe connection, and readings from
    a different connection are not comparable with it (see the block above). A
    cache surviving its probe would compare a stored 5 against a fresh
    connection's 2 and answer "unchanged" — which is precisely the stale read
    this whole key exists to prevent. Caller holds _CACHE_LOCK.
    """
    global _VERSION_PROBE
    _PAYLOAD_CACHE.clear()
    if _VERSION_PROBE is not None:
        try:
            _VERSION_PROBE[1].close()
        except sqlite3.Error:
            pass
        _VERSION_PROBE = None


def _release_version_probe_if_idle():
    """Hold the probe open only while there is something to validate.

    Nothing cached means nothing to compare, so there is no reason to keep a
    database file open between requests — and on the small databases that never
    clear PAYLOAD_CACHE_MIN_BUILD_SECONDS that is every request. Caller holds
    _CACHE_LOCK.
    """
    if not _PAYLOAD_CACHE:
        _close_version_probe()


def _cache_path_key(db_path):
    """Resolved spelling without assuming the filesystem folds case."""
    absolute = os.path.abspath(os.fsdecode(os.fspath(db_path)))
    return os.path.realpath(absolute)


def _cache_invalidation_path_keys(db_path):
    """Names that may identify cached state for one requested pathname.

    Cache identity resolves aliases while the database is valid. A later final
    symlink is deliberately refused, but resolving that new link for cleanup
    produces its target rather than the name whose former inode the probe still
    holds. Match both spellings without resetting an unrelated database.
    """
    absolute = os.path.abspath(os.fsdecode(os.fspath(db_path)))
    resolved_parent = os.path.join(
        os.path.realpath(os.path.dirname(absolute)),
        os.path.basename(absolute),
    )
    return {
        absolute,
        resolved_parent,
        os.path.realpath(absolute),
    }


def _case_variants(name):
    seen = {name}
    for index, char in enumerate(name):
        for replacement in (char.lower(), char.upper()):
            alternate = name[:index] + replacement + name[index + 1:]
            if alternate not in seen:
                seen.add(alternate)
                yield alternate


def _normalization_variant(name):
    composed = unicodedata.normalize("NFC", name)
    if composed != name:
        return composed
    decomposed = unicodedata.normalize("NFD", name)
    if decomposed != name:
        return decomposed
    return None


_COMPONENT_CASE_INSENSITIVE = 1
_COMPONENT_NORMALIZATION_INSENSITIVE = 2


def _cache_component_equivalence(path):
    """Path equivalences proven through this filesystem's directory entries.

    Each component is checked through its own parent's directory entry, because
    a mount point's spelling belongs to the parent filesystem rather than the
    database's device. Matching component by component avoids folding a
    case-sensitive child merely because one of its ancestors happens to be
    insensitive. Case and Unicode normalization are proven independently.
    Symlink aliases and hard-linked files are not evidence of filesystem name
    behavior.
    """
    parts = Path(path).parts
    equivalence = [0] * len(parts)
    candidate = path
    for index in range(len(parts) - 1, 0, -1):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        try:
            original_info = os.lstat(candidate)
        except OSError:
            candidate = parent
            continue
        name = os.path.basename(candidate)
        alternate_groups = (
            (_COMPONENT_CASE_INSENSITIVE, _case_variants(name)),
            (_COMPONENT_NORMALIZATION_INSENSITIVE,
             (_normalization_variant(name),)),
        )
        for capability, alternate_names in alternate_groups:
            for alternate_name in alternate_names:
                if alternate_name is None:
                    continue
                alternate = os.path.join(parent, alternate_name)
                try:
                    alternate_info = os.lstat(alternate)
                except OSError:
                    continue
                same_entry = (
                    not stat.S_ISLNK(original_info.st_mode)
                    and not stat.S_ISLNK(alternate_info.st_mode)
                    and (original_info.st_dev, original_info.st_ino)
                    == (alternate_info.st_dev, alternate_info.st_ino)
                )
                single_regular_link = (
                    not stat.S_ISREG(original_info.st_mode)
                    or (original_info.st_nlink == 1
                        and alternate_info.st_nlink == 1)
                )
                if same_entry and single_regular_link:
                    equivalence[index] |= capability
                    break
        candidate = parent
    return tuple(equivalence)


def _components_match(cached, supplied, equivalence):
    if cached == supplied:
        return True
    if equivalence & _COMPONENT_NORMALIZATION_INSENSITIVE:
        cached = unicodedata.normalize("NFC", cached)
        supplied = unicodedata.normalize("NFC", supplied)
        if cached == supplied:
            return True
    if not equivalence & _COMPONENT_CASE_INSENSITIVE:
        return False
    cached = cached.casefold()
    supplied = supplied.casefold()
    if equivalence & _COMPONENT_NORMALIZATION_INSENSITIVE:
        cached = unicodedata.normalize("NFC", cached)
        supplied = unicodedata.normalize("NFC", supplied)
    return cached == supplied


def _identity_matches_invalidation(identity, requested):
    if identity[0] in requested:
        return True
    equivalences = identity[5] if len(identity) > 5 else ()
    cached_parts = Path(identity[0]).parts
    if len(equivalences) != len(cached_parts):
        return False
    for candidate in requested:
        candidate_parts = Path(candidate).parts
        if len(candidate_parts) != len(cached_parts):
            continue
        if all(
            _components_match(cached, supplied, equivalences[index])
            for index, (cached, supplied) in enumerate(
                zip(cached_parts, candidate_parts))
        ):
            return True
    return False


def invalidate_payload_cache(db_path):
    """Retire cache state whose database path just failed validation/opening.

    A missing or rejected pathname can leave the probe attached to its former
    inode. That state cannot validate a later recovery at the same name and, on
    Windows, the open probe may itself prevent replacement. Do not reset an
    unrelated database's cache when a caller merely probes some other bad path.
    Caller does not hold ``_CACHE_LOCK``.
    """
    requested = _cache_invalidation_path_keys(db_path)
    with _CACHE_LOCK:
        cached_path_matches = any(
            _identity_matches_invalidation(key[0], requested)
            for key in _PAYLOAD_CACHE
        )
        if ((_VERSION_PROBE is not None
             and _VERSION_PROBE[0][0] in requested)
                or cached_path_matches):
            # All entries came from this one probe generation, so closing it
            # necessarily invalidates the complete cache.
            _close_version_probe()


def _database_identity(db_path):
    """What this cache means by "the same database file", or None.

    `data_version` alone cannot notice the file being REPLACED — a fresh
    `usage.db` at the same path leaves the probe reading the old inode. The
    identity carries the size and mtime too, which costs nothing in extra
    invalidation: a WAL commit moves neither (measured), and the operations that
    do move them — a checkpoint, the one-shot VACUUM — advance `data_version` in
    the same breath.
    """
    cache_path = _cache_path_key(db_path)
    try:
        info = os.stat(cache_path)
    except OSError:
        return None
    return (cache_path, info.st_dev, info.st_ino,
            info.st_size, info.st_mtime_ns,
            _cache_component_equivalence(cache_path))


def _database_version(identity):
    """`(probe generation, SQLite's commit counter)` for this file, or None.

    Returns None when it cannot be read, which is a "do not cache" answer rather
    than an error: the payload is still built and still returned, exactly as it
    was before this cache existed. Caller holds _CACHE_LOCK.
    """
    global _VERSION_PROBE, _PROBE_GENERATION
    if identity is None:
        return None
    file_id = identity[:3]
    if _VERSION_PROBE is not None and _VERSION_PROBE[0] != file_id:
        _close_version_probe()
    if _VERSION_PROBE is None:
        try:
            # The request already stat'ed the file. Require this independently
            # opened probe to match that device/inode before attaching its
            # data-version counter to the request's cache identity.
            conn = connect_existing_db(
                identity[0], check_same_thread=False,
                expected_file_identity=identity[1:3])
        except (OSError, RuntimeError, sqlite3.Error):
            return None
        if conn is None:
            return None
        _VERSION_PROBE = (file_id, conn)
        _PROBE_GENERATION += 1
    try:
        row = _VERSION_PROBE[1].execute("PRAGMA data_version").fetchone()
    except sqlite3.Error:
        _close_version_probe()
        return None
    return (_PROBE_GENERATION, row[0]) if row else None


def _cached_payload(identity, version, source):
    """The stored payload for this exact (file, commit counter, source), or None.

    `source` is part of the key because `?source=` scopes every usage rollup in
    SQL — serving a Codex reader Claude's payload would be the same defect as
    serving them yesterday's.
    """
    with _CACHE_LOCK:
        if identity is None or version is None:
            # No current probe reading can validate any retained payload.
            _PAYLOAD_CACHE.clear()
            return None
        # A commit changes ``version``; a checkpoint or replacement can also
        # change the file identity. Entries from either previous observation
        # are unreachable cache state, not a reason to retain the probe if the
        # rebuild below fails. Preserve only other source views that the same
        # current probe reading can still validate.
        for key, (cached_version, _payload) in tuple(_PAYLOAD_CACHE.items()):
            if key[0] != identity or cached_version != version:
                _PAYLOAD_CACHE.pop(key, None)
        entry = _PAYLOAD_CACHE.get((identity, source))
        if entry is None:
            return None
        _PAYLOAD_CACHE.move_to_end((identity, source))
        return entry[1]


def _store_payload(identity, before, after, source, payload, seconds):
    """Retain a payload only if it is provably a snapshot of one committed state.

    `before` and `after` bracket the build. They differ exactly when another
    connection committed while the eleven aggregations were running — a scan from
    `cli.py`, the background thread `cmd_dashboard` starts, or `/api/rescan`
    holding RESCAN_LOCK in another server thread. The read transaction keeps
    the sections consistent, but retaining its older snapshot as CURRENT after
    a concurrent commit would pin it until the next write. The live cache drops
    that build; the explicitly dated startup preview can still retain it.

    Error bodies are never stored — `{"error": ...}` is what the page's retry loop
    watches for, and a cached one would never change.
    """
    if identity is None or before is None or after is None or before != after:
        return
    if seconds < PAYLOAD_CACHE_MIN_BUILD_SECONDS:
        return
    if not isinstance(payload, dict) or "error" in payload:
        return
    with _CACHE_LOCK:
        # A probe closed, switched to another file, or reopened between the
        # final version read and publication cannot validate that old reading.
        # Merely checking that *some* probe is open labels an old generation's
        # payload with unrelated current state.
        if (_VERSION_PROBE is None
                or _VERSION_PROBE[0] != identity[:3]
                or _PROBE_GENERATION != after[0]):
            return
        _PAYLOAD_CACHE[(identity, source)] = (after, payload)
        _PAYLOAD_CACHE.move_to_end((identity, source))
        while len(_PAYLOAD_CACHE) > PAYLOAD_CACHE_MAX_ENTRIES:
            _PAYLOAD_CACHE.popitem(last=False)


def _payload_with_live_fields(cached, conn):
    """A cache hit, with the three not-from-the-database fields rebuilt.

    The caller holds database rebuild admission across both cache revalidation
    and these live queries. A rebuild normally moves the cache counter, but its
    durable marker can commit after an optimistic version read; the admission
    boundary prevents that ordering from serving the old payload mid-cleanup.

    The usage sections are handed back by reference: they were sanitised when they
    were built and re-sanitising all already-safe structures on every hit
    would give back most of what the cache saves. Treat the returned payload as
    read-only; mutating a section mutates the cache.
    """
    # Database admission and session reconciliation can hold the write lock
    # during a concurrent scan. Use the shared database timeout instead of
    # failing a normal page load prematurely.
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    payload = dict(cached)
    payload["subscription_limits"] = _safe_dashboard_value(
        claude_limits(conn, os.environ))
    payload["codex_limits"] = _safe_dashboard_value(codex_limits_projection(conn))
    payload["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return payload


def get_dashboard_data(db_path=None, source=None):
    """Build the complete /api/data payload, always releasing the connection.

    The server is long-lived — the VS Code extension keeps it up for the whole
    session — and this runs on every poll, so a query that raises (a locked
    database during a concurrent scan, a damaged page, a failed rebuild) used
    to leak its connection and file descriptor for the life of the process.

    A repeat call against an unchanged database is answered from the cache above,
    which costs the two quota queries and nothing else. Read that block before
    changing anything here — in particular before reaching for the file's mtime,
    which cannot see a write-ahead-logged commit at all.
    """
    # Resolved at call time, not frozen into the signature: a def-time
    # default captures the module global as it was at IMPORT, so patching
    # `DB_PATH` moved the name and not the default, and the call went to the
    # real ~/.claude/usage.db. See the comment on `db.get_db`.
    if db_path is None:
        db_path = DB_PATH
    # connect_existing_db FIRST, and the order is the whole point. Its
    # descriptor guard is the only thing that looks at the path itself -- a
    # symlink, a hard link, a non-regular file -- while `exists()` follows a
    # symlink and therefore answers False for a dangling one. Probing first turned a
    # refused path into "Database not found. Run: python cli.py scan": served
    # at HTTP 200 with no `permanent`, so the page retried it every three
    # seconds forever, while the command it named exited 1 without creating
    # anything, because `db.get_db` refuses the same path. Advice that cannot
    # be followed, on a loop that cannot end.
    #
    # The helper also carries the validated inode across SQLite's pathname
    # hand-off and uses mode=rw, so a removal cannot silently create a new
    # database. A genuinely missing file still returns the retryable body.
    #
    # This is the ordering `cli.require_db` already uses, for this reason.
    try:
        conn = connect_existing_db(db_path)
    except BaseException:
        invalidate_payload_cache(db_path)
        raise
    if conn is None:
        invalidate_payload_cache(db_path)
        # The page chooses an actionable spelling for its delivery surface:
        # console script, checkout launcher, or VS Code command palette.
        return {"error": "Database not found", "recovery": "scan"}

    try:
        _register_timestamp_order(conn)
        # Cache identity/version and every database-derived field belong to one
        # rebuild-admission observation. Otherwise a rebuild can commit its
        # durable marker after the version read and a hit serves old history;
        # a miss can likewise mix sections from opposite sides of the rebuild.
        with database_admission(conn, db_path):
            identity = _database_identity(db_path)
            with _CACHE_LOCK:
                version = _database_version(identity)
            cached = _cached_payload(identity, version, source)
            if cached is not None:
                return _payload_with_live_fields(cached, conn)

            started = time.perf_counter()
            # One SQLite snapshot for all sections, including when an external
            # CLI scan (outside the HTTP lifecycle counter) commits mid-build.
            conn.execute("BEGIN")
            try:
                payload = _collect_dashboard_data(conn, source)
            finally:
                conn.rollback()
            with _CACHE_LOCK:
                after = _database_version(identity)
            build_seconds = time.perf_counter() - started
        # Publication belongs after the context manager's final pathname
        # identity check. A non-cooperating same-user process can replace the
        # path while this connection keeps reading the old inode; caching that
        # payload before admission rejects the swap lets a later request see
        # data that this request was never allowed to serve.
        _store_payload(identity, version, after, source, payload, build_seconds)
    except BaseException:
        # Admission and query failures make any retained state for this path an
        # unsafe promise about a file we could not validate completely.
        invalidate_payload_cache(db_path)
        raise
    finally:
        try:
            conn.close()
        finally:
            # A failed build can open the data-version probe before there is a
            # payload for it to validate. Release that otherwise-orphaned
            # connection on every exit path, including exceptions and returns.
            with _CACHE_LOCK:
                _release_version_probe_if_idle()
    return payload

def codex_limits_projection(conn, now=None):
    """Codex's quota windows, in the SAME shape account.limits_projection returns.

    Deliberately identical so the plan panel renders either source with one
    renderer rather than two — the difference between the assistants is where the
    figures come from, not what they mean.

    Where they genuinely differ is honesty about age. Claude's figure is a cache
    Claude Code refreshes every few tens of minutes, so it can be stale in a way
    the user cannot see. Codex stamps the state on every API response, so the
    newest observation is exactly as old as the last scan — which is a fact this
    page already knows and can state precisely.

    Use the newest turn or quota observation to measure age. A snapshot
    records the first time a percentage was observed, so its timestamp alone
    stops advancing while that percentage stays constant. Observations without
    a stored turn still count."""
    _register_timestamp_order(conn)
    now = now or datetime.now(timezone.utc)
    rows = conn.execute("""
        SELECT grp, scope, resets_key, percent, severity, is_active, resets_at,
               COALESCE(NULLIF(resets_at_order, ''), timestamp_order(resets_at))
                   as resets_at_order,
               observed_at,
               COALESCE(NULLIF(observed_at_order, ''), timestamp_order(observed_at))
                   as observed_at_order
        FROM usage_limits_snapshots
        WHERE kind = 'codex' AND percent >= 0
    """).fetchall()
    if not rows:
        return {"available": False, "reason": "no_codex_usage"}
    rows.sort(key=lambda row: row["observed_at_order"])

    # One window per reset target, keeping its highest observed percentage.
    # used_percent is monotonic within a window, so MAX is not a smoothing choice
    # — it is the only reading immune to a stale replay landing out of order.
    windows = {}
    newest = ""
    newest_order = ""
    plan = ""
    for row in rows:
        key = _dashboard_text(row["resets_key"])
        observed = _dashboard_text(row["observed_at"])
        observed_order = row["observed_at_order"]
        if not newest or observed_order > newest_order:
            newest, newest_order = observed, observed_order
        plan = _dashboard_text(row["scope"]) or plan
        current = windows.get(key)
        percent = _dashboard_number(row["percent"])
        if current is None or percent >= current["percent"]:
            windows[key] = {
                "kind": _dashboard_text(row["grp"]) or "codex",
                "group": _dashboard_text(row["grp"]),
                "percent": percent,
                "severity": _codex_severity(row["severity"]),
                "resets_at": _dashboard_text(row["resets_at"]),
                "_resets_order": row["resets_at_order"],
                "scope": "",
                "is_active": True,
                "expired": False,
                "_observed": observed,
            }
    # Only the window that has not yet reset is current; earlier ones are history.
    live = []
    for window in windows.values():
        moment = _parse_utc(window["resets_at"])
        window["expired"] = bool(moment and moment <= now)
        window.pop("_observed", None)
        live.append(window)
    live.sort(key=lambda window: window["_resets_order"], reverse=True)
    # Keep the most recent window per group, so a six-day history does not render
    # as six stacked gauges.
    seen, kept = set(), []
    for window in live:
        if window["group"] in seen:
            continue
        seen.add(window["group"])
        window.pop("_resets_order", None)
        kept.append(window)

    # The last thing Codex actually recorded, which is what "as of N ago" on the
    # panel claims to be reporting. `newest` above is only the newest instant the
    # SERIES holds, and the series dates each level from when it was reached.
    for row in conn.execute(
            "SELECT timestamp, COALESCE(NULLIF(timestamp_order, ''), "
            "timestamp_order(timestamp)) as timestamp_order "
            "FROM turns WHERE source = 'codex'"):
        raw = _dashboard_text(row[0])
        if not newest or row[1] > newest_order:
            newest, newest_order = raw, row[1]

    age = None
    observed_at = _parse_utc(newest)
    if observed_at is not None:
        age = max(0, int((now - observed_at).total_seconds()))
    # Through the SAME describer the Claude side and the standalone limits
    # server use, so a Codex window carries a key, a label and its configured
    # thresholds in the identical shape. The source is part of the key, which is
    # what stops a Codex `weekly` and a Claude `weekly` -- two genuinely
    # different windows -- from sharing one threshold.
    stored = limits_core.read_thresholds()
    described = limits_core.describe_windows(kept, "codex", stored)
    return {
        "available": True,
        "plan_type": plan,
        "rate_limit_tier": "",
        "fetched_at_ms": 0,
        "age_seconds": age,
        "windows": described,
        "orphaned": limits_core.orphaned_thresholds(
            stored, limits_core.live_threshold_keys(kept, "codex"),
            source="codex"),
        "source": "codex",
    }


# The graded levels the plan panel understands. Claude's cache publishes one per
# window; Codex publishes none, so this is the vocabulary a Codex row is
# translated INTO, never out of.
PLAN_SEVERITY_LEVELS = ("normal", "warning", "critical")


def _codex_severity(recorded):
    """Codex's stored `severity` is not a graded level, so translate it.

    rate_limit_reached_type identifies which limit was reached. It does not
    grade the severity of a filling window and may remain null until a limit
    is hit.

    Two different things therefore have to come out of one column:

    * `''` stays `''` — NOT RECORDED, which is not the same claim as "normal",
      and is the distinction the page already keeps between `n/a` and `$0.00`.
      Deriving a level from the percentage is deliberately not done here: the
      renderer does it for both assistants at once (`planSeverity` in
      web/js/56-plan.js), and doing it here would put a derived value under a
      key `/api/data` and `/api/limits` otherwise ship as provenance.
    * anything else means a limit WAS reached, which is `critical` whatever
      percentage sits beside it. Passing the discriminator through is worse than
      passing the blank: the renderer allow-lists these three names, so
      `"primary"` fell back — painting the gauge green at the one moment it
      mattered most.

    The stored column is untouched; `usage_limits_snapshots` stays a record of
    what the transcript said.
    """
    reached = _dashboard_text(recorded)
    if reached in PLAN_SEVERITY_LEVELS:
        return reached
    return "critical" if reached else ""


def codex_limit_history(conn):
    """The quota series itself — the thing Claude Code cannot provide.

    Claude's equivalent table only ever holds what a mutable cache happened to
    contain when a scan ran. Codex's comes off an append-only transcript, so this
    is a genuine curve: a window filling, and a window resetting.
    """
    _register_timestamp_order(conn)
    rows = conn.execute(f"""
        SELECT grp, resets_key, percent, observed_at,
               COALESCE(NULLIF(observed_at_order, ''),
                        timestamp_order(observed_at)) as observed_at_order,
               {_local_day('observed_at')}    as day,
               {_local_minute('observed_at')} as observed_local
        FROM usage_limits_snapshots
        WHERE kind = 'codex' AND percent >= 0
    """).fetchall()
    rows.sort(key=lambda row: row["observed_at_order"])
    return [{
        "group":      _dashboard_text(r["grp"]),
        "resets_key": _dashboard_text(r["resets_key"]),
        "percent":    _dashboard_number(r["percent"]),
        "day":        _dashboard_text(r["day"]),
        "observed":   _dashboard_text(r["observed_local"]),
    } for r in rows]


def usage_since(conn, started_at, source="claude"):
    """Turns and tokens recorded here since `started_at`. Never raises."""
    if not started_at:
        return None
    try:
        _register_timestamp_order(conn)
        row = conn.execute(
            "SELECT COUNT(*) AS turns, "
            "COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens "
            "             + cache_creation_tokens), 0) AS tokens "
            "FROM turns "
            "WHERE COALESCE(NULLIF(source, ''), 'claude') = ? "
            "AND COALESCE(NULLIF(timestamp_order, ''), "
            "            timestamp_order(timestamp)) >= ?",
            (source, timestamp_order(started_at))).fetchone()
    except Exception:
        return None
    return {"turns": _dashboard_number(row["turns"]),
            "tokens": _dashboard_number(row["tokens"])}


def _limits_payload(env=None):
    """The quota projection, live if enabled, cached otherwise. Never raises."""
    try:
        config = account.read_config()
        cached = account.current_limits_from_config(config, env=env)
        # An API-key or otherwise non-subscription install must not make a live
        # quota request and must not resurrect a stale subscription cache.
        if account.detect_auth_mode(config, env) != "subscription":
            cached["reading"] = "cache"
            return cached
        if live_limits.enabled(env):
            config, source = live_limits.config_with_live_limits(
                config, env=env)
            projection = account.current_limits_from_config(config, env=env)
            projection["reading"] = source
            return projection
    except Exception:
        # A live path that throws must not take the panel with it; the cache is
        # right there and is what the tool used before this existed.
        pass
    payload = account.current_limits(env)
    payload["reading"] = "cache"
    return payload


def claude_limits(conn, env=None):
    """Claude's plan windows, with what this database recorded in each.

    The single definition both `/api/data` and `/api/limits` call — the panel is
    painted first from one and then refreshed from the other, and when only the
    endpoint enriched them the panel said "no usage recorded here yet" for
    thirty seconds while the database held a thousand turns.

    The enrichment matters after a reset. The cache goes on describing the
    window that has already rolled over until Claude Code refreshes, so there is
    no percentage for the window you are in — but "you have run N turns in it" is
    something we do know, and it beats a dead "window ended".
    """
    # Live when the user has asked for it AND supplied a token; the cache
    # otherwise, which is the behaviour this tool had before live_limits
    # existed. `current_limits` stays the cache-only path so nothing that
    # imports it starts making requests by surprise.
    payload = _limits_payload(env)
    payload["source"] = "claude"
    for window in payload.get("windows") or []:
        if window.get("window_start"):
            _correct_window_start(conn, window)
        started = window.get("window_start")
        if not started:
            continue
        recorded = usage_since(conn, started, "claude")
        if recorded:
            window["recorded"] = recorded
    # Identity, label and configured thresholds come from `limits_core`, the
    # same function the standalone limits server calls. The client must NOT
    # derive a window key of its own: two definitions of identity is how a
    # threshold set on one surface silently governs a different window on the
    # other, and it is the same failure mode the single `PRICING` table and the
    # single `local_day_expr` exist to prevent.
    stored = limits_core.read_thresholds()
    payload["windows"] = limits_core.describe_windows(
        payload.get("windows") or [], "claude", stored)
    # Configured windows the cache is not currently reporting. Kept and shown,
    # because a stale cache is the ordinary case and an alert the user set must
    # not become invisible just because Claude Code has not refreshed.
    payload["orphaned"] = limits_core.orphaned_thresholds(
        stored,
        limits_core.live_threshold_keys(payload.get("windows") or [], "claude"),
        source="claude")
    return payload


def _correct_window_start(conn, window):
    """Replace the projected window start with the one the transcripts show.

    A quota window starts with activity after the previous window expires.
    Rolling a cached reset forward by a fixed interval can therefore
    overestimate the new window start after an idle stretch.

    So after any idle period the projected start is early, by up to a whole
    window, and `usage_since` then counts turns from before the window began —
    inflating "recorded here" with work that was billed to a window that has
    already closed.

    The database is the only thing that knows when you next spoke, which is why
    this correction lives here, beside the connection, rather than in account.py.
    Walk forward: the window starts at the first turn at or after the stale
    reset, and if that window has itself already ended, at the first turn after
    that, and so on.

    Silent no-op when nothing can be established — no turns since the reset means
    you are not in a window at all, and the projection is left exactly as it was
    rather than replaced by a guess.
    """
    from datetime import timedelta

    _register_timestamp_order(conn)

    reset = _parse_utc(window.get("resets_at"))
    length = account._window_length_hours(window.get("kind"), window.get("group"))
    if reset is None or length is None:
        return
    now = datetime.now(timezone.utc)
    step = timedelta(hours=length)
    boundary = reset
    # SQLite compares TEXT lexically. Fetch the candidate timestamps once and
    # keep them in neutral instant order so both the lower bound and the first
    # value selected below agree for valid ISO-8601 values with offsets.
    turns = [(_dashboard_text(row[0]), row[1]) for row in conn.execute(
        "SELECT timestamp, COALESCE(NULLIF(timestamp_order, ''), "
        "timestamp_order(timestamp)) FROM turns "
        "WHERE COALESCE(NULLIF(source, ''), 'claude') = 'claude'")]
    turns.sort(key=lambda row: row[1])
    cursor = 0
    # A week of five-hour windows is 34; the bound only stops a pathological
    # clock from spinning, exactly as current_window_bounds' own loop does.
    for _ in range(64):
        boundary_text = boundary.isoformat()
        while (cursor < len(turns)
               and timestamp_compare(turns[cursor][0], boundary_text) < 0):
            cursor += 1
        first = _parse_utc(
            turns[cursor][0] if cursor < len(turns) else None)
        if first is None:
            return
        # `first + step` is its own way to fail, and the parse being hardened
        # does not cover it: `first` is a raw transcript timestamp bounded only
        # in length, so a year-9999 turn leaves `datetime`'s range on the way to
        # the window end — from `9999-12-31T23:59:59Z`, the plain UTC form both
        # assistants actually write, not from an exotic offset. This is the same
        # arithmetic `account.current_window_bounds` already guards, and its test
        # records why the guard has to be on the addition: "the overflow is in
        # `reset + step`, *before* the anti-spin loop". Uncaught it escaped
        # `claude_limits` into `_collect_dashboard_data`, which has none, and
        # answered `GET /api/data` with 500 for the whole page. Giving up is the
        # documented answer for an instant we cannot place.
        try:
            end = first + step
        except (OverflowError, OSError):
            return
        if end > now:
            window["window_start"] = first.isoformat()
            window["window_end"] = end.isoformat()
            window["window_start_source"] = "transcripts"
            return
        boundary = end


def available_sources(db_path=None):
    """Which assistants this database holds, and how many turns each has.

    Its own endpoint because it is the ONLY thing the page needs before it can
    ask which one you want, and it is one grouped count over `turns` against
    every rollup the full payload builds — a small fraction of the cost, by a
    margin that widens as history grows. Asking that question by building the
    whole dashboard meant the reader waited for both assistants' history to
    render behind a dialog before being allowed to say which one they wanted —
    and then half of it was thrown away by their answer.

    Deliberately no figures: the two this comment used to quote were taken on
    one machine on one afternoon and both rotted, and the ratio between them is
    no more constant — it collapses towards parity on a small database, where
    connection setup and admission dominate both calls. The shape is the
    durable claim.
    """
    # Resolved at call time, not frozen into the signature: a def-time
    # default captures the module global as it was at IMPORT, so patching
    # `DB_PATH` moved the name and not the default, and the call went to the
    # real ~/.claude/usage.db. See the comment on `db.get_db`.
    if db_path is None:
        db_path = DB_PATH
    # Same guarded, existing-only open and invalidation contract as
    # get_dashboard_data above. This lightweight endpoint is normally the
    # page's first request, so it must not leave the previous database's cache
    # or version probe alive when its pathname can no longer be admitted.
    try:
        conn = connect_existing_db(db_path)
    except BaseException:
        invalidate_payload_cache(db_path)
        raise
    if conn is None:
        invalidate_payload_cache(db_path)
        return []
    # Not sqlite3's 5 s default: admission can take the write lock, which
    # a concurrent scan's end-of-scan reconciliation holds for seconds on a real
    # corpus. This is the FIRST request the page makes and it has no retry, so a
    # timeout here costs the whole source list for that page load.
    try:
        conn.row_factory = sqlite3.Row
        # `db_path`, so a rebuild's stderr notice can name the file it threw
        # away. The yielded rebuild flag is still not acted on here, but the reason has
        # changed and the old one is worth recording as the mistake it was: it
        # said "a rebuilt database holds no turns, and this function's honest
        # answer for a database with no turns is already the empty list", which
        # conflates *just destroyed* with *nothing scanned yet* — the page reads
        # `[]` as the ordinary single-assistant install and says nothing. What
        # makes the omission safe now is that this is not where the fact is
        # carried: `/api/data` ships `unscanned` (see `_database_is_unscanned`),
        # which is a property of the FILE rather than of whichever call happened
        # to consume the mismatch, so it is still true on the request after this
        # one and in a process that did no rebuilding at all.
        with database_admission(conn, db_path):
            rows = conn.execute(
                "SELECT COALESCE(NULLIF(source, ''), 'claude') AS source, "
                "COUNT(*) AS turns FROM turns GROUP BY 1 ORDER BY turns DESC"
            ).fetchall()
    except BaseException:
        invalidate_payload_cache(db_path)
        raise
    finally:
        conn.close()
    return _safe_dashboard_value(
        [{"source": _dashboard_text(r["source"], "claude") or "claude",
          "turns": _dashboard_number(r["turns"])} for r in rows])


def _database_is_unscanned(conn):
    """Has this database ingested nothing at all?

    True when `processed_files` and `turns` are both empty, which is the state a
    freshly created database and a freshly REBUILT one share: admission drops
    every table when the schema in front of it is not one this build wrote, and
    what it leaves behind is indistinguishable, byte for byte, from a first
    install. The page renders it as a banner, because the two ways a reader can
    arrive at those zeros both deserve the same sentence — *nothing here has been
    read yet, so this is not your history* — and neither of them is an error.

    A property of the FILE, deliberately, rather than of the rebuild flag
    yielded by `database_admission`. That flag is true only for the caller that
    performed the drop, and the caller that performs it is usually not the one
    the reader is looking at: the page asks `/api/sources` first, so on the
    common upgrade path the rebuild is consumed by `available_sources` and
    `/api/data`'s admission then finds a schema that matches. Reproduced end to
    end before this existed — one
    authenticated GET returned every section `[]` with no `error` key, the stored
    turns were gone, and with auto-refresh off by default (`REFRESH_DEFAULT = 0`)
    nothing on the page ever asked again. Asking the database instead answers for
    a rebuild performed by a different process too, which is the shape an older
    build sharing ~/.claude/usage.db produces.

    NOT `{"error": ...}`: the page arms its three-second retry inside that branch,
    and retrying cannot clear this. NOT source-scoped either — `processed_files`
    has no `source` column, and "this file has read nothing" is one fact about the
    database rather than one per assistant.

    Both halves are required. `processed_files` empty is the durable half: it is
    written by every scan and cleared by nothing except a rebuild. `turns` empty
    is what makes the banner's claim — that every figure beside it is zero — true
    rather than merely likely, so a partially refilled database says nothing
    instead of contradicting the numbers next to it.
    """
    row = conn.execute(
        "SELECT NOT EXISTS(SELECT 1 FROM processed_files) "
        "AND NOT EXISTS(SELECT 1 FROM turns)").fetchone()
    return bool(row[0]) if row else False


def _collect_dashboard_data(conn, source=None):
    # The dashboard can read while a background scan commits. Use the shared
    # database timeout for admission and reconciliation locks, matching
    # db.get_db and cli.require_db.
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    # The production caller holds `database_admission` from schema validation
    # through the final query. cmd_dashboard binds and serves *before* its
    # background scan, so that context may rebuild an older schema first; it
    # must not release admission here and then assemble sections while another
    # process commits a new rebuild marker.
    #
    # Unlike `cli.require_db`, the return value is deliberately not acted on
    # here. The payload says it a different way — `unscanned` below — and that is
    # a correction rather than a restatement.
    #
    # What this comment used to argue was that the asymmetry rested on ONE
    # property: "every route to this function keeps a scan behind it", so a
    # rebuilt-empty payload was transient and self-correcting. It is not, in the
    # time dimension: a scan runs once, at `on_ready`, and a rebuild triggered by
    # a request an hour later has no scan behind it — or kills the one that is
    # running, since the drop lands under a scan already reading the tables. Both
    # were reproduced end to end against this code by the review that raised it
    # (2026-08-15): the request thread answered HTTP 200 with every section `[]`
    # and no `error` field, `Background scan failed: no such table: sessions`
    # went to a terminal nobody was reading, and `REFRESH_DEFAULT = 0` meant the
    # page never asked again.
    #
    # So the rule is now enforced by what goes out rather than by which caller
    # got the truthy return: `_database_is_unscanned` asks the file, and the page
    # renders a banner over the zeros. Read that docstring before changing this.
    # What this must NOT become is `{"error": ...}`, which the page renders as
    # "— retrying…" over a condition that retrying cannot clear.
    # ── Subscription plan limits ──────────────────────────────────────────────
    # Read live rather than out of the database: this panel answers "how much
    # headroom do I have right now", and the stored value is by definition the
    # state at the last scan. The database keeps the *history* (below); the two
    # answer different questions and neither substitutes for the other.
    #
    # API-token installs get available:false — they have no plan window to show,
    # only per-token billing — as does any setup where the config cannot be read
    # at all, which is what Docker sees (run-docker.sh mounts only
    # ~/.claude/projects, never ~/.claude.json).
    # One definition, shared with the /api/limits endpoint the panel polls on its
    # own short interval — this snapshot is only the value at page load.
    subscription_limits = claude_limits(conn, os.environ)

    # There was a second history series here — every kind of window, not only
    # Codex's — scanned and serialised on every poll under the comment "so
    # utilization can be plotted over time". No such plot was ever written: it
    # reached no renderer, no CSV export, no extension file and no test, and its
    # rows were a strict superset of `codex_limit_history` below, so the Codex
    # ones went down the wire twice. Removed rather than left as a feed a reader
    # could mistake for the intended one; the snapshots are still in the
    # database, which is where a chart would read them from anyway.

    # The source list was assembled here too, under the comment "Drives the
    # source picker". It stopped driving it when the picker moved to its own
    # endpoint: `available_sources` above runs the identical GROUP BY, and the
    # page asks /api/sources *before* deciding whether to fetch this payload at
    # all. Nothing read the copy — so it was a second scan of the whole `turns`
    # table, serialised into every response, for no reader. Call available_sources()
    # if you need the list; do not put it back here. Note that no name-based
    # check can protect this spot: `sources` is the key the page reads off the
    # *endpoint's* response, so tests/test_payload_surface.py accepted it for a
    # handful of comments and a URL until it learned to demand an access shape.

    return _safe_dashboard_value({
        "all_models":      rollups.all_models(conn, source),
        "daily_by_model":  rollups.daily_by_model(conn, source),
        "hourly_by_model": rollups.hourly_by_model(conn, source),
        "sessions_all":    rollups.sessions_all(conn, source),
        "project_by_day_model": rollups.project_by_day_model(conn, source),
        "effort_by_day_model": rollups.effort_by_day_model(conn, source),
        "stop_reason_by_day_model": rollups.stop_reason_by_day_model(conn, source),
        "subagent_by_type": rollups.subagent_by_type(conn, source),
        "top_dispatches":  rollups.top_dispatches(conn, source),
        "limit_incidents": rollups.limit_incidents(conn, source),
        "codex_limits":    codex_limits_projection(conn),
        # Served but not yet drawn — the one payload field with no consumer, and
        # deliberately so: it is the quota curve Claude's mutable cache cannot
        # produce (README, invariant 7), it is asserted by
        # tests/test_codex_transcripts.py, and the chart is the missing half.
        # tests/test_payload_surface.py names it as the single allowed exception,
        # so a second unread field cannot join it unnoticed.
        "codex_limit_history": codex_limit_history(conn),
        "subscription_limits": subscription_limits,
        # Not usage and not quota: one fact about the database file, which is why
        # it takes no `source`. See `_database_is_unscanned`.
        "unscanned":       _database_is_unscanned(conn),
        "generated_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
