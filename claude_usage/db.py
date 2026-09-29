"""SQLite storage for the usage database: connection and schema.

Separated from scanner.py because this is not scanning. `cli.py` and
`dashboard.py` both need to open the database before they read it, and both
used to reach into a module named for transcript parsing to do so.

THERE ARE NO MIGRATIONS. The schema is declared once, inline, in `SCHEMA_SQL`,
and a stored database that does not match it is dropped and recreated rather
than converted — see `init_db`. That is safe because this file is a derived
cache: `scan()` rebuilds it from ~/.claude/projects and ~/.codex/sessions.

The security posture lives here too: the database holds a record of every
project worked on, so on POSIX it is created 0600 inside a 0700 parent, while
symlinked, hard-linked and non-regular paths are refused on every platform.
"""

import hashlib
import math
import os
import sqlite3
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .timestamps import register_timestamp_order

DB_PATH = Path(os.environ.get("CLAUDE_USAGE_DB", Path.home() / ".claude" / "usage.db"))

# SQLite reserves this header field for exactly this purpose: identifying the
# application that owns a database file.  ``CUSG`` is deliberately a file
# identity, not a schema-version or migration marker; schema compatibility is
# still derived exclusively from ``SCHEMA_SQL`` below.
APPLICATION_ID = 0x43555347

# A rebuild is a cache replacement, not a migration. Keep this durable bit set
# until the committed replacement has been checkpointed and all old WAL frames
# are gone. If the process dies before that cleanup, the next opener retries the
# cleanup while holding the rebuild lock and refuses to expose a marked file.
REBUILD_IN_PROGRESS = 0x43555352


class UnsafeDatabasePathError(RuntimeError):
    """The database or its lock cannot be safely opened at the chosen path."""


def normalize_source(value):
    """Return the canonical source key used by persistence joins.

    Databases written before source separation have blank/NULL values.  They
    are Claude rows, not a third source, so every storage path normalizes them
    to ``claude``.  Known non-empty source names are lower-cased for stable
    identity; callers only emit ``claude`` and ``codex`` today.
    """
    if not isinstance(value, str):
        return "claude"
    value = value.strip().lower()
    return value or "claude"


def normalize_stored_sources(conn):
    """Canonicalize pre-source Claude rows without duplicating identities.

    Released databases can contain blank source values, and hand-upgraded
    databases can contain both the legacy and canonical spelling.  Normalize
    at the database boundary so scanners, reports, and dashboard readers all
    see the same ``(source, id)`` identity.  When both spellings exist, retain
    the canonical metadata row.  For message-id turns, retain one row before
    updating so the conditional unique index cannot make normalization fail.
    """
    legacy_turn = conn.execute(
        "SELECT 1 FROM turns WHERE source IS NULL OR source = '' LIMIT 1"
    ).fetchone()
    if legacy_turn:
        conn.execute("""
            DELETE FROM turns AS legacy
            WHERE (legacy.source IS NULL OR legacy.source = '')
              AND legacy.message_id IS NOT NULL
              AND legacy.message_id != ''
              AND EXISTS (
                  SELECT 1
                  FROM turns AS keeper
                  WHERE keeper.message_id = legacy.message_id
                    AND (
                        keeper.source = 'claude'
                        OR ((keeper.source IS NULL OR keeper.source = '')
                            AND keeper.id < legacy.id)
                    )
              )
        """)
        conn.execute(
            "UPDATE turns SET source = 'claude' "
            "WHERE source IS NULL OR source = ''"
        )

    for table, identity in (("sessions", "session_id"),
                            ("agents", "agent_id")):
        legacy_row = conn.execute(
            f"SELECT 1 FROM {table} "
            "WHERE source IS NULL OR source = '' LIMIT 1"
        ).fetchone()
        if not legacy_row:
            continue
        conn.execute(f"""
            DELETE FROM {table} AS legacy
            WHERE (legacy.source IS NULL OR legacy.source = '')
              AND EXISTS (
                  SELECT 1 FROM {table} AS canonical
                  WHERE canonical.source = 'claude'
                    AND canonical.{identity} = legacy.{identity}
              )
        """)
        conn.execute(
            f"UPDATE {table} SET source = 'claude' "
            "WHERE source IS NULL OR source = ''"
        )

# processed_files keys are hashed, never plaintext paths: the table would
# otherwise retain usernames and client directory names for every transcript
# ever scanned, and the scanner only needs stable equality.
PROCESSED_FILE_KEY_PREFIX = "sha256:"


def _processed_file_key(filepath):
    """Return a non-reversible identifier for incremental scan bookkeeping."""
    if isinstance(filepath, bytes):
        encoded = filepath
    elif isinstance(filepath, str):
        encoded = os.fsencode(filepath)
    else:
        encoded = repr(filepath).encode("utf-8", "replace")
    digest = hashlib.sha256(encoded).hexdigest()
    return PROCESSED_FILE_KEY_PREFIX + digest

def _is_processed_file_key(value):
    if not isinstance(value, str) or not value.startswith(PROCESSED_FILE_KEY_PREFIX):
        return False
    digest = value[len(PROCESSED_FILE_KEY_PREFIX):]
    return len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)

def _processed_mtime(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None

def _processed_line_count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value

def _processed_size(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value

def _processed_identity(value):
    """Return a canonical nonnegative decimal device/inode component.

    ``os.stat_result`` can expose unsigned 64-bit file identifiers on
    platforms where SQLite's INTEGER binding cannot represent them.  Store
    the canonical decimal spelling as TEXT instead; accepting integers here
    keeps cursors written before that representation change readable.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value < 0:
            return None
        value = str(value)
    if (not isinstance(value, str) or not value
            or any(char < "0" or char > "9" for char in value)):
        return None
    return value.lstrip("0") or "0"

def _processed_prefix_hash(value):
    if not isinstance(value, str) or len(value) != 64:
        return None
    return value if all(char in "0123456789abcdef" for char in value) else None

# SQLite's connection defaults assume one process at a time. This product
# creates the opposite by design and `upsert_sessions` already says so: one
# dashboard process per VS Code window, each with a background scan thread,
# `/api/rescan`, and a terminal `cli.py scan` beside them, all on the same
# ~/.claude/usage.db. Five seconds — Python's default — is shorter than a
# single `/api/data` rollup on a real database, so the losing writer did not
# wait, it raised: a single second process holding an ordinary read
# transaction kills a concurrent `scan()` at its per-file `conn.commit()`
# after 5.2s. Thirty seconds is a ceiling on how long a writer waits, not a
# target; under WAL it is only ever paid against another *writer*.
BUSY_TIMEOUT_MS = 30000


class _GuardedConnection(sqlite3.Connection):
    """SQLite connection carrying the descriptor identity it was opened for."""


def get_db(db_path=None):
    # Resolve DB_PATH at call time. Capturing it in a default argument would
    # bypass later configuration or test patches and could write to the wrong
    # database.
    if db_path is None:
        db_path = DB_PATH
    path, _created, validated_identity = secure_db_permissions(
        db_path, create=True, return_identity=True)
    conn = sqlite3.connect(
        path, timeout=BUSY_TIMEOUT_MS / 1000, factory=_GuardedConnection)
    try:
        conn.row_factory = sqlite3.Row
        register_timestamp_order(conn)
        # sqlite3 accepts a pathname rather than an already-open descriptor.
        # Verify that the pathname SQLite opened is still the inode checked by
        # the descriptor-backed guard before any ownership claim or write.
        _verify_database_path_identity(path, validated_identity)
        conn._claude_usage_file_identity = validated_identity
        # Establish ownership before changing persistent connection settings.
        # In particular, WAL is a property of the file, so enabling it before
        # this check mutates an unrelated database even though ``init_db`` later
        # refuses to rebuild it.
        _establish_database_identity(conn, path)
        # A released legacy database was unmarked during the guarded path open,
        # so it could not be chmodded without mutating a possible foreign file.
        # Adoption has now persisted CUSG; the descriptor-level header check in
        # this second call can tighten only the file that proves it is ours.
        secure_db_permissions(path)
        # A killed rebuild leaves a durable in-progress marker. Recover before
        # enabling WAL on the marked database. Recovery is in place and holds
        # the same lock as the rebuild, so a pinned reader makes this call fail
        # closed instead of returning a connection that could write into a
        # database whose old pages have not been reclaimed yet.
        if _rebuild_in_progress(conn):
            conn.close()
            _recover_interrupted_rebuild(path)
            path, _created, validated_identity = secure_db_permissions(
                path, return_identity=True)
            conn = sqlite3.connect(
                path, timeout=BUSY_TIMEOUT_MS / 1000,
                factory=_GuardedConnection)
            conn.row_factory = sqlite3.Row
            register_timestamp_order(conn)
            _verify_database_path_identity(path, validated_identity)
            conn._claude_usage_file_identity = validated_identity
            _establish_database_identity(conn, path)
            secure_db_permissions(path)
        _enable_wal(conn)
        return conn
    except BaseException:
        conn.close()
        raise

def _enable_wal(conn):
    """Ask for write-ahead logging, best effort; return the mode now in force.

    The busy timeout above and this are not interchangeable, and each covers a
    failure the other does not — both measured with two real processes on one
    file, and both pinned by `tests/test_migrations.py`:

    - the timeout is what survives writer-vs-writer, a second scanner process
      holding the write lock. WAL does not remove that serialization, it only
      shortens it, and with WAL alone the scan still dies at 5.4s.
    - WAL is what survives reader-vs-writer. Under the default rollback journal
      a reader and the writer are mutually exclusive, so a page building
      `/api/data` blocks the scan for as long as its slowest statement and a
      larger timeout only widens the window it must wait out. With WAL the same
      scan commits immediately.

    Best effort in three distinct ways, because journal mode is a persistent
    property of the FILE: getting it wrong is not one bad run, it is every run
    after it.

    - The PRAGMA takes a lock to convert, so it can raise `database is locked`
      under exactly the contention it exists to remove. Once the file is
      already WAL it is a lock-free no-op, so only the one-time conversion is
      exposed — but `get_db` must not start failing there.
    - A filesystem that cannot do WAL does not raise, it returns the unchanged
      mode. Nothing may assert `wal`; callers get whatever is actually in force.
    - A conversion can succeed and only then fail to map the `-shm` wal-index
      (reported on some network and container-bind filesystems), leaving a
      database in a mode nothing there can read. So the mode is proved with a
      real read before it is trusted, and walked back to the rollback journal
      if that read fails. Verified working over a Docker Desktop bind mount,
      which is where `scripts/run-docker.sh` puts the database.
    """
    try:
        row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
    except sqlite3.DatabaseError:
        return None
    mode = (row[0] if row else "") or ""
    if mode.lower() != "wal":
        return mode
    try:
        # Cheap, but a real read transaction — which is what needs the
        # wal-index the conversion above may not have been able to create.
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError:
        try:
            row = conn.execute("PRAGMA journal_mode = DELETE").fetchone()
        except sqlite3.DatabaseError:
            return None
        return (row[0] if row else "") or ""
    return mode

def _checkpoint_wal(conn):
    """Copy the write-ahead log into the database file and empty it.

    Under WAL a committed rewrite lands in `usage.db-wal` first. SQLite does
    copy it back on its own — `PRAGMA wal_autocheckpoint`, 1000 log pages (WAL
    frames) by default, runs a PASSIVE checkpoint at COMMIT with every
    connection still open — but that is weak in two separate ways:

    - the threshold is on the LOG, not on the database, and a rewrite that
      never grows the log past it is not copied back at all: it sits in the
      sidecar until the LAST connection closes — for `cli.py dashboard` and the
      extension's server, the whole session.
    - a rewrite that does cross it is copied back with every connection still
      open, but a PASSIVE checkpoint copies without truncating, so the frames
      are left in `usage.db-wal` either way.

    Rebuild cleanup uses TRUNCATE and not FULL or PASSIVE: those land the pages
    but leave the log's own copy of them in a file sitting next to the database.

    **What pins it, and what does not.** The sentence here used to name
    `TestTheRebuildDoesNotLeaveThePlaintextInTheFile.test_the_dropped_labels_are_not_recoverable_from_the_file`
    and claim it checked `usage.db-wal` "precisely because this call is the only
    thing that empties it". Measured by mutation 2026-08-16, with `pass` in
    place of this call, that test stayed green -- and so did the whole suite AS IT
    THEN STOOD, which is the qualification this sentence carried for a day
    without: the sibling below did not exist yet, and on this tree the same
    mutation reds exactly one test. The reason the named test cannot see it is
    that it opens one connection and closes it, and closing the LAST connection
    to a write-ahead-logged database checkpoints it regardless. The claim was wrong
    in the file it named as well — what survives without this call is the MAIN
    database, which keeps the pre-VACUUM pages while the vacuumed result sits
    unread in the sidecar.

    Its sibling
    `test_a_second_open_connection_does_not_leave_the_plaintext_behind` is the
    test that does pin it, and one extra open connection across the rebuild is
    the whole difference — the state `cli.py dashboard`, the extension's server
    and every `dashboard_data` request are in by design. On the same mutated
    build it reds, with `usage.db` coming back at 135,168 bytes carrying the
    plaintext where the shipped build leaves 4,096 clean ones.

    A non-WAL database answers `(0, -1, -1)` and is already clean. A pinned
    reader answers with a non-zero busy flag; callers keep the rebuild marker
    and fail closed so a later open can retry after the reader releases.
    """
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.DatabaseError:
        return False
    # SQLite returns (busy, log_frames, checkpointed_frames).  Non-WAL modes
    # answer (0, -1, -1), which is already a clean state for this purpose.
    return bool(row is None or int(row[0]) == 0)


def _fd_application_id(fd):
    """Read SQLite's big-endian application id from this exact descriptor.

    The field occupies bytes 68..71 of the 100-byte database header. Reading
    the descriptor, rather than reopening or statting the path, is what lets
    permission tightening remain metadata-free for a foreign file: only the
    inode whose own durable header says ``CUSG`` is chmodded. A short or
    unreadable header is simply unmarked.
    """
    try:
        os.lseek(fd, 68, os.SEEK_SET)
        value = os.read(fd, 4)
    except OSError:
        return 0
    return int.from_bytes(value, "big") if len(value) == 4 else 0

def _database_parent_is_safe(path):
    """Reject parents that let another UID replace a checked database.

    SQLite's stdlib binding accepts a pathname, not an already-open descriptor.
    A private directory and a system-owned non-writable directory are safe for
    that hand-off. Shared sticky directories such as ``/tmp`` are safe as
    ancestors of a private leaf, but not as the database's immediate parent:
    SQLite may create predictable ``-wal``, ``-shm`` or ``-journal`` sidecars
    there, and a foreign pre-created sidecar would remain outside the final
    inode's owner check. A shared writable directory without sticky semantics
    is refused at every level.
    """
    if os.name != "posix":
        return
    try:
        uid = os.getuid()
    except AttributeError:
        return
    # Inspect the lexical chain before resolving it. A user-owned directory
    # symlink could otherwise be swapped after ``resolve`` and before SQLite's
    # pathname-based open, redirecting the main file and predictable sidecars.
    # Root-owned compatibility links such as macOS ``/var -> /private/var`` are
    # stable across this user boundary and remain supported.
    lexical = Path(os.path.abspath(path.parent))
    current = Path(lexical.anchor)
    for component in lexical.parts[1:]:
        current /= component
        try:
            entry = current.lstat()
        except OSError as exc:
            raise UnsafeDatabasePathError(
                f"Refusing unsafe database directory: {current}") from exc
        if stat.S_ISLNK(entry.st_mode):
            if entry.st_uid != 0:
                raise UnsafeDatabasePathError(
                    f"Refusing symbolic-link database directory: {current}")
            continue
        if not stat.S_ISDIR(entry.st_mode):
            raise UnsafeDatabasePathError(
                f"Database parent is not a directory: {current}")
        if entry.st_uid not in (uid, 0):
            raise UnsafeDatabasePathError(
                f"Refusing foreign-owned database directory: {current}")
        writable_by_other = bool(
            entry.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
        if writable_by_other and not (entry.st_mode & stat.S_ISVTX):
            raise UnsafeDatabasePathError(
                f"Refusing database in a shared writable directory: {current}")

    try:
        resolved = path.parent.resolve(strict=True)
    except OSError as exc:
        raise UnsafeDatabasePathError(
            f"Refusing unsafe database directory: {path.parent}") from exc

    current = resolved
    immediate = True
    while True:
        try:
            info = current.stat()
        except OSError as exc:
            raise UnsafeDatabasePathError(
                f"Refusing unsafe database directory: {current}") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafeDatabasePathError(
                f"Database parent is not a directory: {current}")
        if info.st_uid not in (uid, 0):
            raise UnsafeDatabasePathError(
                f"Refusing foreign-owned database directory: {current}")
        writable_by_other = bool(info.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
        sticky = bool(info.st_mode & stat.S_ISVTX)
        if writable_by_other and (immediate or not sticky):
            raise UnsafeDatabasePathError(
                f"Refusing database in a shared writable directory: {current}")
        if current.parent == current:
            break
        current = current.parent
        immediate = False


def _database_file_identity(info):
    """Return the identity needed across SQLite's pathname hand-off."""
    return (
        getattr(info, "st_dev", None),
        getattr(info, "st_ino", None),
        getattr(info, "st_uid", None),
        getattr(info, "st_nlink", None),
    )


def _verify_database_path_identity(path, expected, action="open"):
    """Ensure SQLite's pathname still names the descriptor we validated."""
    if expected is None:
        return
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise UnsafeDatabasePathError(
            f"Database path changed during {action}: {path}") from exc
    if (not stat.S_ISREG(current.st_mode)
            or _database_file_identity(current) != expected):
        raise UnsafeDatabasePathError(f"Database path changed during {action}: {path}")


def secure_db_permissions(db_path, create=False, *, return_created=False,
                           return_identity=False):
    """Create/tighten the usage database with owner-only POSIX permissions.

    ``return_created`` is retained for compatibility with older callers. Its
    second value reports only whether this call won the atomic ``O_EXCL``
    create. It is deliberately *not* an ownership decision: the descriptor is
    closed before SQLite opens the path, so even an atomic create must be
    revalidated through the connected database under SQLite's write lock.
    """
    path = Path(db_path)
    try:
        if path.is_symlink():
            raise UnsafeDatabasePathError(f"Refusing symbolic-link database path: {path}")
        if create:
            parent_existed = path.parent.exists()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == "posix" and not parent_existed:
                os.chmod(path.parent, 0o700)
        # Missing files remain ordinary presence probes for read-only callers.
        if create or path.exists():
            _database_parent_is_safe(path)
    except OSError as exc:
        raise UnsafeDatabasePathError(str(exc)) from exc

    created = False
    flags = os.O_RDWR if create else os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    try:
        if create:
            try:
                fd = os.open(str(path), flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                fd = os.open(str(path), flags, 0o600)
        else:
            fd = os.open(str(path), flags, 0o600)
    except FileNotFoundError as exc:
        if create:
            raise UnsafeDatabasePathError(str(exc)) from exc
        if return_identity:
            return path, False, None
        return (path, False) if return_created else path
    except OSError as exc:
        raise UnsafeDatabasePathError(f"Refusing unsafe database path: {path}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeDatabasePathError(f"Database path is not a regular file: {path}")
        if info.st_nlink != 1:
            raise UnsafeDatabasePathError(f"Refusing hard-linked database path: {path}")
        identity = _database_file_identity(info)
        _verify_database_path_identity(path, identity)
        if os.name == "posix":
            if not created and info.st_uid != os.getuid():
                raise UnsafeDatabasePathError(
                    f"Refusing foreign-owned database path: {path}")
            # A refusal must not mutate somebody else's file metadata. A file
            # this call atomically created is ours; an existing one is tightened
            # only when THIS descriptor's SQLite header already carries our
            # durable application id. An exact legacy schema is still unmarked
            # here and is tightened by the second call after locked adoption.
            if created or _fd_application_id(fd) == APPLICATION_ID:
                # Tightening is hygiene, not a guarantee the rest of the code
                # relies on — the checks above are the actual trust boundary.
                # A current-user-owned database that is already owner-only may
                # be on a read-only/immutable mount and needs no chmod. If it
                # is group/other-readable or writable and cannot be tightened,
                # fail closed rather than exposing usage or allowing sidecar
                # tampering.
                try:
                    os.fchmod(fd, 0o600)
                except OSError as exc:
                    if os.fstat(fd).st_mode & 0o077:
                        raise UnsafeDatabasePathError(
                            f"Refusing non-private database path: {path}") from exc
                if os.fstat(fd).st_mode & 0o077:
                    raise UnsafeDatabasePathError(
                        f"Refusing non-private database path: {path}")
    except OSError as exc:
        raise UnsafeDatabasePathError(str(exc)) from exc
    finally:
        os.close(fd)
    if return_identity:
        return path, created, identity
    return (path, created) if return_created else path


def _sqlite_existing_file_uri(path):
    """Return an existing-only SQLite URI without a UNC authority.

    ``Path.as_uri()`` spells the UNC path ``//server/share`` as
    ``file://server/share``. SQLite rejects non-empty URI authorities by
    default, even though the ordinary Windows pathname is supported. Four
    slashes preserve the leading UNC ``//`` inside an authority-free path.
    """
    uri = path.as_uri()
    if uri.startswith("file://") and not uri.startswith("file:///"):
        uri = "file:////" + uri[len("file://"):]
    return uri + "?mode=rw"


def connect_existing_db(db_path, *, check_same_thread=True,
                        expected_file_identity=None):
    """Open an existing guarded database without a create race.

    SQLite normally creates a missing pathname. Between the descriptor-backed
    safety check and ``sqlite3.connect`` that would let a removed file become a
    fresh database, or let a replacement inode bypass the descriptor we
    validated. URI ``mode=rw`` forbids creation; the post-connect identity
    check is the same hand-off guard used by ``get_db``.

    ``expected_file_identity`` is an optional ``(st_dev, st_ino)`` pair for a
    caller that already tied other state to one file, such as the dashboard's
    persistent data-version probe.
    """
    path, _created, validated_identity = secure_db_permissions(
        db_path, return_identity=True)
    if validated_identity is None:
        return None
    if (expected_file_identity is not None
            and tuple(validated_identity[:2]) != tuple(expected_file_identity)):
        raise UnsafeDatabasePathError(f"Database path changed during open: {path}")

    uri = _sqlite_existing_file_uri(path.absolute())
    conn = sqlite3.connect(
        uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000,
        check_same_thread=check_same_thread, factory=_GuardedConnection)
    try:
        _verify_database_path_identity(path, validated_identity)
        conn._claude_usage_file_identity = validated_identity
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def _rebuild_lock(db_path):
    """Serialize rebuild and interrupted-rebuild recovery across processes.

    SQLite's WAL reader/writer split means a reader can coexist with the
    marker transaction.  A separate advisory lock keeps a second opener from
    interpreting that marker as stale while the original process is still
    finishing its cleanup.  The lock file is deliberately retained in the
    database directory; removing it after release would reopen a create/race
    window for the next pair of processes.
    """
    path = Path(db_path)
    _database_parent_is_safe(path)
    lock_path = _rebuild_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        info = lock_path.lstat()
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise sqlite3.OperationalError(
            f"unable to inspect rebuild lock: {lock_path}") from exc
    if (info is not None and
            (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
             or info.st_nlink != 1)):
        raise UnsafeDatabasePathError(f"Refusing unsafe rebuild lock: {lock_path}")
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    try:
        fd = os.open(str(lock_path), flags, 0o600)
    except OSError as exc:
        raise sqlite3.OperationalError(
            f"unable to open rebuild lock: {lock_path}") from exc
    try:
        _validate_rebuild_lock_fd(fd, lock_path)
        if os.name == "posix":
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            import msvcrt
            # `locking` operates from the current offset and requires a byte
            # to exist.  A one-byte lock file is harmless and stays private.
            os.lseek(fd, 0, os.SEEK_SET)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        yield
    finally:
        try:
            if os.name == "posix":
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)


def _rebuild_in_progress(conn):
    """Whether this owned database has an uncompleted privacy rebuild."""
    row = conn.execute("PRAGMA user_version").fetchone()
    if not row:
        raise sqlite3.DatabaseError(
            "database rebuild marker could not be read")
    try:
        version = int(row[0])
    except (TypeError, ValueError) as exc:
        raise sqlite3.DatabaseError(
            "database rebuild marker is invalid") from exc
    return version == REBUILD_IN_PROGRESS


def _rebuild_lock_path(db_path):
    return Path(str(db_path) + ".rebuild.lock")


def _validate_rebuild_lock_fd(fd, path):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) != 1:
        raise UnsafeDatabasePathError(f"Refusing unsafe rebuild lock: {path}")
    if os.name == "posix" and info.st_uid != os.getuid():
        raise UnsafeDatabasePathError(f"Refusing foreign-owned rebuild lock: {path}")
    try:
        current = path.lstat()
    except OSError as exc:
        raise UnsafeDatabasePathError(f"Unable to revalidate rebuild lock: {path}") from exc
    if (stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino)
            != (info.st_dev, info.st_ino)):
        raise UnsafeDatabasePathError(f"Rebuild lock path changed during open: {path}")
    if os.name == "posix":
        try:
            os.fchmod(fd, 0o600)
        except OSError as exc:
            if os.fstat(fd).st_mode & 0o077:
                raise UnsafeDatabasePathError(
                    f"Refusing non-private rebuild lock: {path}") from exc
        if os.fstat(fd).st_mode & 0o077:
            raise UnsafeDatabasePathError(f"Refusing non-private rebuild lock: {path}")


def _cleanup_rebuild(conn):
    """Reclaim old pages and clear the marker only after WAL is clean.

    The operation is deliberately in place. A pathname replacement would
    leave connections opened before the replacement able to create/write
    sidecars for the old inode, so recovery instead keeps the marker durable
    until SQLite has copied the rebuilt image into the existing main file.
    """
    try:
        conn.execute("VACUUM")
    except sqlite3.DatabaseError as exc:
        raise sqlite3.OperationalError(
            "database rebuild cleanup failed; reopen the database") from exc
    if not _checkpoint_wal(conn):
        raise sqlite3.OperationalError(
            "database rebuild cleanup is blocked; reopen the database")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def _recover_interrupted_rebuild(db_path):
    """Finish an interrupted rebuild without exposing a marked database.

    Recovery shares the rebuild lock and uses the same in-place VACUUM and
    TRUNCATE checkpoint protocol. A WAL reader may keep the old main pages
    pinned; in that case checkpointing reports busy, the marker stays set, and
    this function raises instead of returning a usable connection. Once the
    reader releases, a later opener can finish the cleanup safely.
    """
    path = Path(db_path)
    with _rebuild_lock(path):
        return _recover_interrupted_rebuild_unlocked(path)


def _recover_interrupted_rebuild_unlocked(db_path):
    """Recovery implementation for a caller holding ``_rebuild_lock``."""
    path = Path(db_path)
    guard = connect_existing_db(path)
    if guard is None:
        raise FileNotFoundError(path)
    guard.row_factory = sqlite3.Row
    try:
        guard.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if not _rebuild_in_progress(guard):
            return False
        application_id = _application_id(guard)
        if application_id != APPLICATION_ID:
            raise ForeignDatabaseError(_foreign_database_message(
                stored_tables(guard), path, application_id))
        _cleanup_rebuild(guard)
    finally:
        guard.close()
    return True


def _secure_delete_literal(previous):
    """The PRAGMA argument that puts `secure_delete` back to `previous`.

    NOT `int(previous)`, which is lossy for the one value that matters.
    `PRAGMA secure_delete` READS a tri-state -- 0 (off), 1 (on), 2 (FAST) --
    but on the way IN SQLite parses the right-hand side as a boolean unless it
    is the literal keyword `FAST`, so `= 2` lands on 1. Measured on both SQLite
    builds present on the machine this was written on (3.49.1 and 3.51.0),
    identically: `= FAST` -> 2, `= 2` -> 1, `= 1` -> 1, `= 0` -> 0, `= 3` -> 1.

    So both of `init_db`'s save/restore envelopes used to hand a FAST
    connection back at full secure_delete: the restore clause was present, it
    ran, and it silently upgraded the setting it exists to preserve. That is
    not a corner. SQLite 3.51.0 -- shipped as this host's `/usr/bin/python3`
    stdlib -- opens every connection at 2, and `init_db`'s caller goes on to
    scan and serve on that same connection, paying the full freed-page
    overwrite on every later DELETE for the life of the process.

    An unrecognised value is passed straight through as an integer, which is no
    worse than the clause it replaces: `PRAGMA secure_delete` is documented to
    answer only 0/1/2, and anything else collapses to 1 on the way in anyway.
    `int()` is deliberately left able to raise, so each call site keeps exactly
    the `except` tuple it had before this helper existed.
    """
    return "FAST" if int(previous) == 2 else str(int(previous))

# The complete current schema, declared inline. There are no migrations: a
# database whose shape does not match this exactly is REBUILT (see
# `init_db`), because it is a derived cache of ~/.claude/projects and
# ~/.codex/sessions and the next scan refills it.
#
# Every statement is `IF NOT EXISTS`, so running this against a database that
# already matches is a no-op. Production file initialization supplied with its
# path is serialized by `database_admission`; the atomic schema transaction
# remains necessary for pathless callers and processes that predate admission.
SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id      TEXT NOT NULL,
        project_name    TEXT,
        first_timestamp TEXT,
        last_timestamp  TEXT,
        first_timestamp_order TEXT NOT NULL DEFAULT '',
        last_timestamp_order  TEXT NOT NULL DEFAULT '',
        git_branch      TEXT,
        total_input_tokens      INTEGER DEFAULT 0,
        total_output_tokens     INTEGER DEFAULT 0,
        total_cache_read        INTEGER DEFAULT 0,
        total_cache_creation    INTEGER DEFAULT 0,
        model           TEXT,
        turn_count      INTEGER DEFAULT 0,
        topic           TEXT,
        total_cache_creation_1h INTEGER DEFAULT 0,
        source          TEXT NOT NULL DEFAULT 'claude',
        PRIMARY KEY (source, session_id)
    );

    -- One row per assistant API response. `cwd` is present and always NULL:
    -- both parsers store None, and the column is kept so that a database
    -- written by an older build is not called foreign for holding it.
    CREATE TABLE IF NOT EXISTS turns (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id              TEXT,
        timestamp               TEXT,
        timestamp_order         TEXT NOT NULL DEFAULT '',
        model                   TEXT,
        input_tokens            INTEGER DEFAULT 0,
        output_tokens           INTEGER DEFAULT 0,
        cache_read_tokens       INTEGER DEFAULT 0,
        cache_creation_tokens   INTEGER DEFAULT 0,
        tool_name               TEXT,
        cwd                     TEXT,
        message_id              TEXT,
        is_subagent             INTEGER DEFAULT 0,
        agent_id                TEXT,
        -- Only the 1-hour slice of cache_creation_tokens is stored; the
        -- 5-minute part is the remainder, so the two can never drift out of
        -- agreement with the total every existing query already sums.
        cache_creation_1h_tokens INTEGER DEFAULT 0,
        source                  TEXT DEFAULT 'claude',
        -- Codex reports reasoning tokens as a SUBSET of output_tokens. Stored
        -- for display, never summed into a cost.
        reasoning_output_tokens INTEGER DEFAULT 0,
        -- '' means "not recorded", never a real level / reason / branch. See
        -- AGENTS.md: a reader must treat it as unknown rather than folding it
        -- into a named bucket.
        reasoning_effort        TEXT DEFAULT '',
        stop_reason             TEXT DEFAULT '',
        git_branch              TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS processed_files (
        path        TEXT PRIMARY KEY,
        mtime       REAL,
        lines       INTEGER,
        size        INTEGER,
        prefix_hash TEXT,
        -- The path may be atomically replaced while preserving mtime and size.
        -- These two values make the warm skip sensitive to that replacement.
        -- TEXT avoids sqlite3's signed-64-bit INTEGER binding limit: some
        -- platforms expose unsigned 64-bit device/inode values.
        st_dev      TEXT,
        st_ino      TEXT
    );

    CREATE TABLE IF NOT EXISTS agents (
        source                TEXT NOT NULL DEFAULT 'claude',
        agent_id              TEXT NOT NULL,
        agent_type            TEXT,
        dispatched_in_session TEXT,
        completed_at          TEXT,
        status                TEXT,
        total_tokens          INTEGER,
        total_duration_ms     INTEGER,
        tool_use_count        INTEGER,
        PRIMARY KEY (source, agent_id)
    );

    -- Rate-limit notices Claude Code recorded when a limit was hit. The
    -- allowance itself is never written to the transcripts, so this table
    -- answers "when was I throttled, and when did it reset" — not "how much
    -- headroom is left", which cannot be derived from local data.
    CREATE TABLE IF NOT EXISTS limit_events (
        event_uuid  TEXT PRIMARY KEY,
        kind        TEXT,
        session_id  TEXT,
        timestamp   TEXT,
        timestamp_order TEXT NOT NULL DEFAULT '',
        status      INTEGER,
        message     TEXT,
        reset_hint  TEXT,
        reset_zone  TEXT
    );

    -- Point-in-time observations of the plan-limit utilization Claude Code
    -- caches in ~/.claude.json. That cache is overwritten in place, so
    -- without this table the only possible view is "right now" — there is
    -- no way to see when a window reset or how fast it filled.
    --
    -- The key is (window identity, percentage), so re-reading an unchanged
    -- cache on every scan is a no-op and each new level costs exactly one
    -- row. What is stored is therefore "when did this window first reach
    -- this level", which is the step function a chart wants. `percent`
    -- defaults to -1 rather than being nullable because SQLite lets NULLs
    -- coexist in a rowid table's PRIMARY KEY and NULL never conflicts,
    -- which would silently defeat the dedupe.
    CREATE TABLE IF NOT EXISTS usage_limits_snapshots (
        kind          TEXT    NOT NULL,
        grp           TEXT    NOT NULL DEFAULT '',
        scope         TEXT    NOT NULL DEFAULT '',
        resets_key    TEXT    NOT NULL DEFAULT '',
        percent       INTEGER NOT NULL DEFAULT -1,
        severity      TEXT    NOT NULL DEFAULT '',
        is_active     INTEGER NOT NULL DEFAULT 0,
        resets_at     TEXT    NOT NULL DEFAULT '',
        resets_at_order TEXT  NOT NULL DEFAULT '',
        fetched_at_ms INTEGER NOT NULL DEFAULT 0,
        observed_at   TEXT    NOT NULL DEFAULT '',
        observed_at_order TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (kind, grp, scope, resets_key, percent)
    );

    CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(source, session_id);
    CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
    CREATE INDEX IF NOT EXISTS idx_turns_timestamp_order
        ON turns(timestamp_order);
    CREATE INDEX IF NOT EXISTS idx_sessions_first ON sessions(first_timestamp);
    CREATE INDEX IF NOT EXISTS idx_sessions_first_order
        ON sessions(first_timestamp_order);
    CREATE INDEX IF NOT EXISTS idx_sessions_last_order
        ON sessions(last_timestamp_order);
    CREATE INDEX IF NOT EXISTS idx_agents_type ON agents(source, agent_type);
    CREATE INDEX IF NOT EXISTS idx_turns_subagent ON turns(is_subagent);
    CREATE INDEX IF NOT EXISTS idx_turns_agent_id ON turns(source, agent_id);
    CREATE INDEX IF NOT EXISTS idx_limit_events_ts ON limit_events(timestamp);
    CREATE INDEX IF NOT EXISTS idx_limit_events_ts_order
        ON limit_events(timestamp_order);
    CREATE INDEX IF NOT EXISTS idx_usage_limits_observed
        ON usage_limits_snapshots(observed_at);
    CREATE INDEX IF NOT EXISTS idx_usage_limits_observed_order
        ON usage_limits_snapshots(observed_at_order);
    CREATE INDEX IF NOT EXISTS idx_turns_source ON turns(source);

    -- Conditional unique index: only dedup non-empty message ids, and only
    -- within their producer. Claude and Codex identifiers are independent
    -- namespaces even when their text happens to match.
    CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_message_id
        ON turns(source, message_id)
        WHERE message_id IS NOT NULL AND message_id != '';
"""


def _schema_statements():
    """Split the declared SQL with SQLite's own statement recognizer.

    `Connection.executescript` commits an already-open transaction before it
    starts.  Rebuilds need the drops and creates in one transaction, so use the
    same parser SQLite exposes to execute each complete statement explicitly.
    Comments and quoted semicolons are handled by `complete_statement`, unlike
    a text split on ``;``.
    """
    statements = []
    pending = ""
    for line in SCHEMA_SQL.splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            statements.append(pending)
            pending = ""
    if pending.strip():
        raise sqlite3.ProgrammingError("incomplete schema statement")
    return tuple(statements)


SCHEMA_STATEMENTS = _schema_statements()


def _complete_schema_atomically(conn):
    """Finish a source-owned partial schema without publishing another prefix.

    Partial schemas can only be residue from a process running an older build:
    pristine databases in this build publish identity and all schema statements
    together. Recheck after taking SQLite's write lock because the process that
    created the prefix may have completed while this caller waited.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if schema_mismatches(conn):
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise

# The column set `init_db` demands of a stored database, derived from SCHEMA_SQL
# at import rather than written out a second time — a hand-kept copy is how the
# check and the schema it checks drift apart. Tables only: an index is recreated
# by the `IF NOT EXISTS` script above whether or not it was there, so a missing
# one is repaired rather than being grounds to throw the data away.
def _expected_columns():
    columns = {}
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.executescript(SCHEMA_SQL)
        names = [r[0] for r in scratch.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'")]
        for name in names:
            columns[name] = frozenset(
                r[1] for r in scratch.execute(f'PRAGMA table_info("{name}")'))
    finally:
        scratch.close()
    return columns

EXPECTED_COLUMNS = _expected_columns()


def _expected_primary_keys():
    """Derive composite primary-key order from the declared schema."""
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.executescript(SCHEMA_SQL)
        result = {}
        for table in EXPECTED_COLUMNS:
            result[table] = tuple(
                row[1] for row in sorted(
                    (row for row in scratch.execute(
                        f'PRAGMA table_info("{table}")') if row[5]),
                    key=lambda row: row[5]))
        return result
    finally:
        scratch.close()


EXPECTED_PRIMARY_KEYS = _expected_primary_keys()


def _application_id(conn):
    """Return SQLite's durable application identity for the main database."""
    return int(conn.execute("PRAGMA application_id").fetchone()[0])


def _database_is_pristine(conn):
    """Whether a locked, unmarked, table-less database has no prior schema.

    This is asked only while ``BEGIN IMMEDIATE`` holds SQLite's write lock.
    Looking at file size before that lock is a TOCTOU: another application can
    create and drop its schema after the observation, leaving a table-less file
    that this tool would otherwise claim.  A fresh connection has schema
    version zero, no free pages and at most the single page SQLite materializes
    when the write transaction begins.  A create/drop cycle increments the
    schema version; even VACUUM leaves that history visible.
    """
    return (
        int(conn.execute("PRAGMA schema_version").fetchone()[0]) == 0
        and int(conn.execute("PRAGMA user_version").fetchone()[0]) == 0
        and int(conn.execute("PRAGMA freelist_count").fetchone()[0]) == 0
        and int(conn.execute("PRAGMA page_count").fetchone()[0]) <= 1
    )


def stored_tables(conn):
    """Every non-internal table, view and trigger actually in `conn`."""
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('table', 'view', 'trigger') AND name NOT LIKE 'sqlite_%'"
        " ORDER BY name")]


# TWO INSTALLED VERSIONS SHARING ONE usage.db NEVER CONVERGE, and this file
# does not fix it. Each build finds the other's schema foreign and rebuilds;
# the older one re-declares what it declares, and the next open of this one
# throws the database away again. On every open, forever.
#
# Measured 2026-08-15 by running each released tag's own `init_db` over a
# database this build had just created, then asking the function below about
# the result: 4 of the 21 tags come back non-empty -- v1.5.4, v1.5.5, v1.6.0
# and v1.6.1, every one of them for the single reason that `schema_meta` is not
# part of this schema, the marker table those builds declare and this one does
# not. The other 17 add nothing this build objects to, so they cost ONE rebuild
# and then coexist. Alternating v1.6.1's `init_db` with this one over a single
# file: `turns` and `processed_files` go 1 -> 0 with the notice printed on
# every cycle, three cycles of three, never settling -- and the Claude quota
# row seeded before the first cycle never comes back, because nothing on disk
# can regenerate it.
#
# The application id added after those measurements establishes ownership, not
# schema compatibility, so it deliberately does not make two schemas coexist.
# The remedy remains operational: do not run two versions against one database,
# and give each its own CLAUDE_USAGE_DB if you must. That works for the CLI,
# Homebrew and Docker
# and NOT for the VS Code extension, whose `sanitizedChildEnvironment` strips
# that variable on purpose, so its server always uses ~/.claude/usage.db --
# there the answer is to run one version of the extension.
#
# Dropping only the unknown extra tables would break the loop with no marker at
# all, and was deliberately not taken: an extra table is evidence the file was
# written by a build whose queries are not these, and deleting another build's
# table to keep our own is the interpretation this design exists to refuse.
def schema_mismatches(conn):
    """Why this database is not the one this build writes; [] if it is.

    A *set* comparison per table, in both directions — a missing column and an
    extra one are equally disqualifying, because either means the file was
    written by a build whose queries are not these. It deliberately says
    nothing about ordinary column TYPE or ORDER: SQLite's declared types are
    advisory and the order only decides what `SELECT *` yields, which nothing
    here relies on. The two processed-file identity columns are the exception:
    their TEXT type is a data-integrity requirement because SQLite otherwise
    coerces an unsigned file identifier past its signed INTEGER limit to REAL.
    """
    reasons = []
    present = set(stored_tables(conn))
    for table in sorted(EXPECTED_COLUMNS):
        if table not in present:
            reasons.append(f"table `{table}` is missing")
            continue
        table_info = list(conn.execute(f'PRAGMA table_info("{table}")'))
        actual = {r[1] for r in table_info}
        for column in sorted(EXPECTED_COLUMNS[table] - actual):
            reasons.append(f"`{table}.{column}` is missing")
        for column in sorted(actual - EXPECTED_COLUMNS[table]):
            reasons.append(f"`{table}.{column}` is not part of this schema")
        if table == "processed_files":
            declared_types = {r[1]: (r[2] or "").upper()
                              for r in table_info}
            for column in ("st_dev", "st_ino"):
                if column in actual and declared_types[column] != "TEXT":
                    reasons.append(
                        f"`{table}.{column}` has type "
                        f"{declared_types[column] or '<none>'}; expected TEXT")
        expected_key = EXPECTED_PRIMARY_KEYS.get(table, ())
        actual_key = tuple(
            row[1] for row in sorted(
                (row for row in table_info if row[5]), key=lambda row: row[5]))
        if actual_key != expected_key:
            reasons.append(
                f"`{table}` has primary key ({', '.join(actual_key) or '<none>'}); "
                f"expected ({', '.join(expected_key) or '<none>'})")
    for table in sorted(present - set(EXPECTED_COLUMNS)):
        reasons.append(f"`{table}` is not part of this schema")
    return reasons


def _announce_rebuild(reasons, db_path):
    """Say, on stderr, why the next scan is about to be slow.

    A rebuild rereads every available transcript. Explain this before starting
    so the user can distinguish a rebuild from a warm incremental scan."""
    from .safetext import terminal_safe as _safe

    shown = reasons[:5]
    more = len(reasons) - len(shown)
    lines = [
        "claude-usage: this usage database was written by a different version.",
    ]
    if db_path:
        lines.append(f"  file: {_safe(str(db_path))}")
    lines.extend(f"  - {_safe(reason)}" for reason in shown)
    if more > 0:
        lines.append(f"  - ... and {more} more")
    # **Both losses, not one.** This said "no usage history is lost except for
    # sessions whose transcript has since been deleted" -- an exception list
    # that reads as exhaustive and named only half of what a rebuild costs.
    # `rebuild_database`'s own docstring already named both, so the code knew
    # and only the user did not, at the one moment they could act on it.
    #
    # The second loss is the one that cannot be undone by scanning: Claude's
    # rows in `usage_limits_snapshots` are sampled from `~/.claude.json`, a
    # cache Claude Code overwrites in place, so a refilling scan re-records
    # only TODAY's reading under a new `observed_at` and every earlier window
    # and level is gone for good. Codex's half of that table is on the
    # transcripts and does come back, which is why the sentence names Claude.
    lines.append(
        "  Rebuilding it from scratch. The database is a cache of your\n"
        "  transcripts, so the next scan re-reads every one of them and will\n"
        "  take much longer than usual; later scans are back to normal speed.\n"
        "  Two things do NOT come back, because no file on disk holds them:\n"
        "    - sessions whose transcript has since been deleted;\n"
        "    - your Claude plan-usage history, which was sampled from a cache\n"
        "      Claude Code overwrites in place. Codex's is on the transcripts\n"
        "      and does return.")
    print("\n".join(lines), file=sys.stderr, flush=True)


def a_retry_could_succeed(exc):
    """Whether asking again could ever answer, for a database refusal.

    **This is NOT `cli._is_a_lock`, and the two deliberately disagree.** They
    read like the same predicate and answer different questions:

    - `cli._is_a_lock` asks *is the data intact RIGHT NOW* -- it chooses whether
      a stopped background scan may tell the reader the dashboard is "still
      serving whatever the database already held".
    - this asks *will a later request succeed* -- it chooses whether the PAGE
      re-arms its three-second retry.

    A lock answers yes to both. A rebuild window (`no such table: turns`, raised
    while another process sits between the DROP and the CREATE) answers **no**
    to the first and **yes** to the second: the tables really are gone, so the
    reassurance would be false, and the rebuild really does finish, so retrying
    really does work. Folding them into one predicate makes one of those two
    sentences a lie whichever way it is folded. That is why there are two, and
    why each docstring names the other.

    Everything else is permanent for the life of the process: a foreign file, a
    file SQLite cannot read as a database at all, a refused path, a read-only
    mount. `attempt to write a readonly database` and `unable to open database
    file` are `OperationalError`s too, which is why the class alone cannot be
    the test -- matching SQLite's own words is.
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return "locked" in text or text.startswith("no such table")


class ForeignDatabaseError(RuntimeError):
    """The file at the database path was not written by this product at all.

    Distinct from a version skew, and the distinction is the whole point: a
    version skew is ours to throw away, and this is not.
    """


# Released databases before the application marker have no durable identity.
# Automatic upgrades therefore need structural evidence, but a fragment is not
# evidence: an unrelated ``turns(session_id, message_id, input_tokens, ...)``
# database used to satisfy the old three-column subset and was then destroyed by
# the rebuild. These are the four COMPLETE at-rest schemas emitted by all 21
# released tags from v1.0.0 through v1.6.1, measured from each tag's CREATE and
# ALTER statements. Object names and every column must match exactly.
_LEGACY_BASE_SCHEMA = {
    "sessions": (
        "session_id", "project_name", "first_timestamp", "last_timestamp",
        "git_branch", "total_input_tokens", "total_output_tokens",
        "total_cache_read", "total_cache_creation", "model", "turn_count",
    ),
    "turns": (
        "id", "session_id", "timestamp", "model", "input_tokens",
        "output_tokens", "cache_read_tokens", "cache_creation_tokens",
        "tool_name", "cwd", "message_id",
    ),
    "processed_files": ("path", "mtime", "lines"),
}
_LEGACY_WITH_AGENTS_SCHEMA = {
    **_LEGACY_BASE_SCHEMA,
    "turns": (*_LEGACY_BASE_SCHEMA["turns"], "is_subagent", "agent_id"),
    "agents": (
        "agent_id", "agent_type", "dispatched_in_session", "completed_at",
        "status", "total_tokens", "total_duration_ms", "tool_use_count",
    ),
}
_LEGACY_WITH_TOPICS_SCHEMA = {
    **_LEGACY_WITH_AGENTS_SCHEMA,
    "sessions": (*_LEGACY_BASE_SCHEMA["sessions"], "topic"),
    "schema_meta": ("key", "value"),
}
_LEGACY_WITH_LIMITS_SCHEMA = {
    **_LEGACY_WITH_TOPICS_SCHEMA,
    "sessions": (
        *_LEGACY_WITH_TOPICS_SCHEMA["sessions"], "total_cache_creation_1h",
    ),
    "turns": (
        *_LEGACY_WITH_AGENTS_SCHEMA["turns"], "cache_creation_1h_tokens",
    ),
    "limit_events": (
        "event_uuid", "kind", "session_id", "timestamp", "status",
        "message", "reset_hint", "reset_zone",
    ),
    "usage_limits_snapshots": (
        "kind", "grp", "scope", "resets_key", "percent", "severity",
        "is_active", "resets_at", "fetched_at_ms", "observed_at",
    ),
}


def _freeze_schema(schema):
    return frozenset(
        (name, frozenset(columns)) for name, columns in schema.items()
    )


LEGACY_SCHEMA_FINGERPRINTS = frozenset(map(_freeze_schema, (
    _LEGACY_BASE_SCHEMA,
    _LEGACY_WITH_AGENTS_SCHEMA,
    _LEGACY_WITH_TOPICS_SCHEMA,
    _LEGACY_WITH_LIMITS_SCHEMA,
)))


def _quoted_identifier(name):
    return '"' + str(name).replace('"', '""') + '"'


def _database_schema_fingerprint(conn, present):
    """Exact table/column fingerprint for one stable ``present`` observation."""
    kinds = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table', 'view', 'trigger') "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    names = set(present)
    if set(kinds) != names or any(kinds[name] != "table" for name in names):
        return None
    try:
        return frozenset(
            (name, frozenset(
                row[1] for row in conn.execute(
                    f"PRAGMA table_info({_quoted_identifier(name)})"
                )
            ))
            for name in names
        )
    except sqlite3.DatabaseError:
        return None


def looks_like_our_database(conn, present):
    """Whether an unmarked file exactly matches a shipped usage schema.

    The caller supplies the object-name observation so a check never silently
    switches from one database state to another. Destructive callers invoke
    this while holding ``BEGIN IMMEDIATE``; an unlocked answer is only an early
    diagnostic and is never used as authority to claim or rebuild a file.
    """
    fingerprint = _database_schema_fingerprint(conn, present)
    return (
        fingerprint in LEGACY_SCHEMA_FINGERPRINTS
        or fingerprint == _freeze_schema(EXPECTED_COLUMNS)
    )


def _establish_database_identity(conn, db_path=None):
    """Claim a pristine or exactly recognised unmarked database under lock.

    Every application-id-zero path passes through the write lock, including
    ``get_db``. This closes both ownership races: a descriptor/file-size result
    cannot survive until a later SQLite open, and a legacy fingerprint cannot
    change between recognition and the durable claim. Schema creation remains
    `init_db`'s job; it publishes all missing tables in a later atomic
    transaction while preserving `get_db`'s connection-only contract.
    """
    application_id = _application_id(conn)
    if application_id == APPLICATION_ID:
        return stored_tables(conn)
    present = stored_tables(conn)
    if application_id != 0:
        raise ForeignDatabaseError(_foreign_database_message(
            present, db_path, application_id))

    conn.execute("BEGIN IMMEDIATE")
    try:
        application_id = _application_id(conn)
        present = stored_tables(conn)
        if application_id == APPLICATION_ID:
            conn.rollback()
            return present
        if application_id != 0:
            raise ForeignDatabaseError(_foreign_database_message(
                present, db_path, application_id))
        if present:
            recognised = looks_like_our_database(conn, present)
        else:
            recognised = _database_is_pristine(conn)
        if not recognised:
            raise ForeignDatabaseError(_foreign_database_message(
                present, db_path, application_id))
        conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    if _application_id(conn) != APPLICATION_ID:
        raise sqlite3.DatabaseError("application identity was not persisted")
    return present


def _foreign_database_message(present, db_path, application_id=None):
    """The refusal, in one place because two callers now raise it.

    Identity establishment and rebuilding both refuse from inside their own
    write-locked rechecks; the already-marked fast path can also refuse before
    taking a lock. A second hand-written copy is exactly how those messages
    would drift apart.

    The path and the table names are both attacker-influenced -- the path comes
    from `CLAUDE_USAGE_DB`, the names from whatever file it points at -- so each
    is escaped HERE, individually. Escaping the assembled message instead would
    fold its own newlines to `\\x0a` and print the whole remedy on one line,
    which is the trap `dashboard.port_in_use_lines` already documents.
    """
    from .safetext import terminal_safe as _safe
    if present:
        identity = (
            "  It holds objects that do not establish claude-usage ownership: "
            f"{', '.join(_safe(n) for n in list(present)[:5])}.")
    elif application_id not in (None, 0):
        identity = (
            "  Its SQLite application id belongs to another application: "
            f"{application_id}.")
    else:
        identity = (
            "  It is a non-empty, table-less SQLite file with no "
            "claude-usage identity.")
    return "\n".join([
        "Refusing to rebuild a database this tool did not write:",
        f"  file: {_safe(str(db_path)) if db_path else '<unknown path>'}",
        identity,
        "  That is not a usage database from an older version -- it is",
        "  something else, and this tool does not throw away files it",
        "  did not write.",
        "  Point CLAUDE_USAGE_DB somewhere else, or move that file aside.",
    ])


def is_a_half_built_database(conn):
    """Recognise an older process's complete-but-partial empty schema.

    Builds that published `executescript(SCHEMA_SQL)` without a transaction
    exposed each table as it was created. A second opener could therefore see a
    strict prefix, call a database nobody else had written foreign, and drop it.
    This build publishes pristine identity and schema atomically, but it still
    needs to recognise a prefix left by an older process sharing the file.

    So this is the empty-file exemption in `init_db`, one step wider, and the
    step is exactly what a rebuild is for. A rebuild's whole justification is
    that the rows it drops come back from the transcripts; where there are no
    rows there is nothing to justify, and `CREATE TABLE IF NOT EXISTS` finishes
    the job the other opener started. Three questions, all structural rather than
    read off the reason strings:

    - every stored table is one of ours (an extra one is a foreign build, and
      that is the case the rebuild exists for),
    - every stored table carries exactly this build's columns (a column skew is
      a different build's `turns`, not an unfinished one), and
    - not one of them holds a row.

    **It answers True for a genuinely old but never-scanned database too**, when
    that database happens to differ from this one only by tables it lacks, and
    that is deliberate rather than overlooked: creating what is missing leaves
    the schema this build writes and loses nothing, because there was nothing to
    lose. What it does not do is widen to a populated database — the caller
    rebuilds that, exactly as before.

    Asked under the write lock, so the answer describes the file rather than a
    state that has since moved on. `init_db` then completes the prefix in one
    transaction. The all-six case cannot reach this function because
    `rebuild_database` returns first when `schema_mismatches` is empty.
    """
    present = set(stored_tables(conn))
    if not present or not present <= set(EXPECTED_COLUMNS):
        return False
    for table in sorted(present):
        actual = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        if actual != EXPECTED_COLUMNS[table]:
            return False
    for table in sorted(present):
        if conn.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is not None:
            return False
    return True


def rebuild_database(conn, reasons, db_path=None):
    """Run a rebuild while holding the cross-process recovery lock."""
    if db_path is None:
        return _rebuild_database_unlocked(conn, reasons, db_path)
    with _rebuild_lock(db_path):
        return _rebuild_database_unlocked(conn, reasons, db_path)


def _rebuild_database_unlocked(conn, reasons, db_path=None):
    """Drop everything and recreate the cache in one marked transaction.

    NOT a migration and deliberately not one: nothing is preserved, converted or
    interpreted. The owner's decision, and it is safe because `turns` and
    `sessions` are derived — `scan()` rebuilds them from ~/.claude/projects and
    ~/.codex/sessions. What it does cost is named rather than hidden: a session
    whose transcript has since been pruned does not come back, and neither does
    a Claude quota observation in `usage_limits_snapshots`, which is sampled
    from a mutable cache rather than from an append-only file (invariant 7).
    Codex's half of that table IS on the transcripts and does come back.

    A durable rebuild marker remains until the new schema has been checkpointed.
    If this process dies after the commit, the next opener retries that cleanup
    in place before returning a connection. Re-checks still happen under the
    write lock before dropping anything, so a stale caller cannot drop a schema
    another opener already rebuilt.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        claimed = False
        present = stored_tables(conn)
        application_id = _application_id(conn)
        if application_id not in (0, APPLICATION_ID):
            conn.rollback()
            raise ForeignDatabaseError(_foreign_database_message(
                present, db_path, application_id))
        if not present:
            # A rebuild owned by this build keeps its application id while its
            # tables are absent.  Without that durable fact, a non-empty empty
            # schema is indistinguishable from another application's DDL
            # window and the only safe answer is refusal.
            if application_id == APPLICATION_ID:
                conn.rollback()
                return []
            conn.rollback()
            raise ForeignDatabaseError(_foreign_database_message(
                present, db_path, application_id))
        if application_id == 0:
            # Released databases predate the marker. Their complete at-rest
            # schema fingerprints are the compatibility bridge; claim them
            # under the same write lock before any destructive statement runs.
            if not looks_like_our_database(conn, present):
                conn.rollback()
                raise ForeignDatabaseError(_foreign_database_message(
                    present, db_path, application_id))
            conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            application_id = APPLICATION_ID
            claimed = True
        if not schema_mismatches(conn):
            # Another opener may have rebuilt the file while this caller waited
            # for the write lock. Do not announce or repeat that rebuild. The
            # only write on this path is adopting a released legacy schema.
            if claimed:
                conn.commit()
            else:
                conn.rollback()
            return []
        # An older process sharing this file can still be observed between its
        # individual CREATE statements. Under this write lock, an owned prefix
        # with no rows is unfinished publication, not evidence that populated
        # data should be discarded. This build's pristine creation and rebuilds
        # both publish every statement in one transaction.
        if is_a_half_built_database(conn):
            conn.rollback()
            return []
        # Announced only now, with the lock held and something actually here to
        # drop. It used to fire before `dropped` was computed, so a race could
        # print the whole notice and then drop nothing -- and `init_db` derives
        # its return from the drop, so `cli.require_db` was told on stderr that
        # the database had been rebuilt from scratch and then printed a report
        # and exited 0. Measured over 120 processes on one foreign database, 8
        # announced without dropping.
        # Ownership is re-asked HERE, at the moment of destruction, and this is
        # the only place it can be asked of a file that cannot move. Every
        # earlier answer came from an unlocked reading, and three rounds running
        # produced a defect in exactly that gap: the gate answered for a file
        # that had changed between the caller's reading and its own, and each
        # narrowing left a state open (a foreign file that ADDS a table; one
        # that shrinks to a name of ours; one that goes momentarily EMPTY).
        # Under `BEGIN IMMEDIATE` there is no gap to be wrong about.
        #
        # It costs nothing on any path of ours: the three branches above have
        # already excluded a matching schema, a table-less file and a half-built
        # one, so our own stale database still carries the application id (or a
        # released legacy signature) and matches on the first comparison.
        _announce_rebuild(reasons, db_path)
        # Persist the recovery marker before touching any old table. It is
        # deliberately part of the same transaction as the drops: an error
        # before commit rolls back both the marker and the old schema, while a
        # process killed after commit leaves an unmistakable in-place recovery
        # state for the next opener.
        conn.execute(f"PRAGMA user_version = {REBUILD_IN_PROGRESS}")
        # Zero pages freed by this rebuild and retain the VACUUM below to reclaim
        # pages orphaned by earlier writes. Both matter for transcript-derived data.
        previous_secure_delete = None
        try:
            previous_secure_delete = conn.execute(
                "PRAGMA secure_delete").fetchone()[0]
            conn.execute("PRAGMA secure_delete = ON")
        except sqlite3.DatabaseError:
            previous_secure_delete = None
        dropped = stored_tables(conn)
        try:
            for name in dropped:
                # Quoted, because a foreign schema may hold any identifier at
                # all. `DROP TABLE` also drops that table's indexes; a stray
                # index belonging to nothing cannot exist once its table is
                # gone.
                kind = conn.execute(
                    "SELECT type FROM sqlite_master WHERE name = ?",
                    (name,)).fetchone()
                if kind is None:
                    continue
                conn.execute(
                    f"DROP {kind[0].upper()} IF EXISTS "
                    f"{_quoted_identifier(name)}"
                )
            # `executescript` would commit the pending DROP transaction before
            # running the CREATE statements.  Execute the pre-parsed schema
            # statements individually so no committed empty/schema-prefix
            # window exists.
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
        finally:
            if previous_secure_delete is not None:
                try:
                    conn.execute("PRAGMA secure_delete = %s"
                                 % _secure_delete_literal(previous_secure_delete))
                except sqlite3.DatabaseError:
                    pass
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    # VACUUM cannot run inside the DDL transaction. The schema is complete here,
    # and the marker remains until `_cleanup_rebuild` has both reclaimed the
    # old pages and truncated the WAL. A pinned reader leaves the marker set;
    # callers receive an error and must reopen after that reader releases.
    _cleanup_rebuild(conn)
    return dropped


def init_db(conn, db_path=None):
    """Bring `conn` to the current schema. No migrations — match, or rebuild.

    Three outcomes, and the first two are the ordinary ones:

    - a pristine file or in-memory database: claim it, then publish every schema
      object together in a second SQLite write transaction.
    - a database that already matches: every statement is a no-op.
    - anything else — a missing column, an extra column, a missing table, an
      unknown table: `rebuild_database` drops the lot and the script recreates
      it, loudly. A database is a cache; the next scan refills it.

    A genuinely pristine file is NOT a mismatch. Its schema metadata is
    re-checked while ``BEGIN IMMEDIATE`` holds SQLite's write lock, then it is
    claimed with ``PRAGMA application_id`` before WAL or schema publication.
    A non-pristine, table-less file with no application id is refused: it may
    be another application's ordinary DDL window, and contains no positive
    evidence that licenses this process to claim it.

    Neither is a HALF-built schema left by an older process sharing the file.
    That case is decided under the write lock by `is_a_half_built_database`,
    which `rebuild_database` consults, and completed atomically rather than
    discarded. This build never publishes such a prefix during pristine setup.

    A database SHARED WITH ANOTHER INSTALLED VERSION is rebuilt on every open
    rather than once, and never converges: this build drops the table that one
    declares and this one does not, that one declares it again, and neither
    ever wins. The application id identifies the owning product, not a schema
    version, so it cannot make incompatible schemas coexist. The comment above
    `schema_mismatches` has which released versions loop, what each cycle costs,
    and the operational remedy.

    **Returns True if it rebuilt**, and a direct one-shot READ caller must act
    on that. `cli.require_db` calls this before querying; dashboard reads instead
    enter `database_admission`, retain the lock through response assembly, and
    surface the file-level ``unscanned`` state. Without the flag, a `cli.py stats` against a
    database written by
    an older version destroyed it and then printed `Total turns: 0`,
    `Est. total cost: $0.0000` and exit 0 -- a confidently wrong report, with no
    rescan and nothing telling the user one was needed. The notice on stderr
    promised "the next scan re-reads every transcript"; on that path there was
    no next scan. A reviewer reproduced it end to end before this return value
    existed.
    """
    with database_admission(conn, db_path) as rebuilt:
        return rebuilt


@contextmanager
def database_admission(conn, db_path=None):
    """Initialize and keep rebuild admission through a caller's read.

    ``init_db`` is sufficient when the caller only needs schema admission: it
    enters this context and returns immediately. A caller that derives a
    response from the connection can keep the context open around those reads,
    preventing another process from committing ``REBUILD_IN_PROGRESS`` between
    initialization and response assembly.

    Enter with no active transaction. Waiting for the advisory lock while this
    connection already owns SQLite's write lock can deadlock against a rebuilder
    that acquired those locks in the opposite order. The context does not
    manage transactions started by its body; current long-lived callers perform
    bounded read-only queries and close the connection after leaving it.
    """
    if conn.in_transaction:
        raise sqlite3.ProgrammingError(
            "database admission requires a connection outside a transaction")
    # Register the shared ordering function for direct sqlite3 callers too.
    # Production connections come through get_db(), but tests and embedders
    # commonly create an in-memory connection themselves before init_db().
    register_timestamp_order(conn)

    if db_path is None:
        yield _init_db_admitted(conn, db_path)
        return

    # Connections opened by this module retain the descriptor identity that
    # survived SQLite's pathname hand-off. Recheck it here: another same-user
    # process can replace the pathname after ``connect_existing_db`` returns but
    # before admission, otherwise coupling reads from the old inode to locks and
    # cache identities for the replacement.
    expected_identity = getattr(
        conn, "_claude_usage_file_identity", None)
    path = Path(db_path)
    # Validate before creating/opening the adjacent lock file. The validation
    # is repeated after locked ownership establishment below, as it was before
    # admission became serialized.
    secure_db_permissions(db_path)
    if expected_identity is not None:
        _verify_database_path_identity(
            path, expected_identity, action="admission")
    # The marker check and every later schema decision are one admission unit.
    # Otherwise a connection opened before another process's rebuild can read
    # "unmarked", let that process commit REBUILD_IN_PROGRESS, and then return
    # successfully against the marked schema while old pages remain pinned.
    with _rebuild_lock(db_path):
        if expected_identity is not None:
            _verify_database_path_identity(
                path, expected_identity, action="admission")
        rebuilt = _init_db_admitted(conn, db_path)
        yield rebuilt
        # A successful caller must not publish rows derived from an inode that
        # stopped being the named database during its admitted operation. The
        # lock serializes every cooperating rebuild; this final hand-off also
        # catches a non-cooperating replacement before a dashboard response or
        # scan proceeds from the stale connection.
        if expected_identity is not None:
            _verify_database_path_identity(
                path, expected_identity, action="admission")


def _init_db_admitted(conn, db_path=None):
    """Initialize after path-supplied file admission has been serialized.

    A non-``None`` path means the caller holds ``_rebuild_lock``. A pathless
    caller may use either an in-memory or file-backed SQLite connection, so the
    inner transaction remains its only cross-process schema protection.
    """

    # A marker is only durable during an interrupted rebuild. Connections
    # opened through `get_db` recover before reaching here; direct sqlite3
    # callers cannot safely continue on a connection that observed the marker,
    # so recover the pathname and ask them to reopen rather than allowing that
    # connection to query or write while cleanup is incomplete.
    if _rebuild_in_progress(conn):
        if db_path is None:
            raise sqlite3.OperationalError(
                "database rebuild was interrupted; reopen it with its path")
        _recover_interrupted_rebuild_unlocked(db_path)
        raise sqlite3.OperationalError(
            "database rebuild was interrupted; reopen the database")

    rebuilt = False
    # One admission-locked observation on the ordinary, already-owned path. An
    # unmarked file is re-read under SQLite's write lock by
    # `_establish_database_identity`; the first observation never licenses a
    # claim or a destructive rebuild.
    present = stored_tables(conn)
    application_id = _application_id(conn)
    if application_id not in (0, APPLICATION_ID):
        raise ForeignDatabaseError(_foreign_database_message(
            present, db_path, application_id))
    if application_id == 0:
        present = _establish_database_identity(conn, db_path)
        application_id = APPLICATION_ID
    if db_path is not None:
        # Direct sqlite3 callers (CLI/dashboard reads) do not pass through
        # get_db's post-adoption permission step. Now that identity is proved,
        # let the exact-descriptor header guard tighten an owned file.
        secure_db_permissions(db_path)
    if present:
        # **Ownership before mismatch.** A file with tables in it that are none
        # of ours is not a stale usage.db, it is somebody else's data, and the
        # rebuild's whole justification -- "safe because `scan()` rebuilds it
        # from the transcripts" -- is a statement about OUR tables and simply
        # false about anyone else's. Refuse instead, naming the path, and let
        # the caller decide. See `looks_like_our_database`.
        reasons = schema_mismatches(conn)
        if reasons:
            # The rebuild's OWN answer, not the observation above. The outer
            # `schema_mismatches` is not an SQLite write-locked ownership
            # decision: `_rebuild_database_unlocked` re-checks after
            # `BEGIN IMMEDIATE` and returns an empty result if the schema is
            # already current. Deriving the flag from the stale `reasons`
            # instead would report a rebuild this opener did not perform -- and
            # `cli.require_db` acts on that distinction.
            rebuilt = bool(_rebuild_database_unlocked(
                conn, reasons, db_path))
    # A pristine database has been claimed but still has no tables. Publish all
    # of them under one write lock; use the same path for a partial set left by
    # an older process. Recheck after the lock is acquired, and never make
    # ordinary matching-schema readers take a write lock merely to execute
    # no-op CREATE statements. The unwrapped script on the common path only
    # restores a missing index, which schema matching deliberately treats as
    # repairable rather than rebuild-worthy.
    if schema_mismatches(conn):
        _complete_schema_atomically(conn)
    else:
        conn.executescript(SCHEMA_SQL)
    normalize_stored_sources(conn)
    conn.commit()
    return rebuilt
