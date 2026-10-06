"""Quota windows and their alert thresholds, independent of any UI.

This module is the shared half of the limits backend: it knows how to name a
quota window, where a threshold for it is stored, and which thresholds a given
percentage has crossed. It opens no socket and touches no usage database, so
both the full dashboard and a standalone limits server can import it without
either depending on the other.

**Nothing here enumerates the limit kinds, and that is the point.** Claude Code
ships a self-describing `cachedUsageUtilization.utilization.limits[]` array --
each entry carrying its own `kind`, `group`, `percent` and optional `scope` --
and Codex ships its own windows the same way. A limit type that did not exist
when this was written (a weekly all-models window, a per-surface one, whatever
comes next) gets an identity, a threshold and an alert with no change here.
Anything that special-cases `five_hour` is a bug in the making.

Thresholds live on disk rather than in a browser so that two different front
ends see one set of settings, and so that clearing site data does not silently
disarm somebody's alerts.
"""

import errno
import hashlib
import json
import os
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from itertools import islice
from pathlib import Path

if os.name == "posix":
    import fcntl
elif os.name == "nt":
    import msvcrt

from .safetext import _bounded_text
from .safefile import read_bounded_regular_file

# The same 0-100 space the windows report in. A threshold outside it can never
# fire (or fires always), so it is refused rather than stored.
MIN_THRESHOLD = 1
MAX_THRESHOLD = 100
# Per window. Generous for a human, and a bound so a malformed or hostile file
# cannot turn one window into an unbounded allocation.
MAX_THRESHOLDS_PER_WINDOW = 16
# Windows a threshold file may describe. Deliberately larger than any plan is
# likely to publish, since the whole design is that new kinds appear on their
# own -- but bounded, for the same reason as above.
MAX_TRACKED_WINDOWS = 64
# What a window key may look like once assembled. Keys are built from
# transcript- and account-derived text, so they are bounded and sanitised
# before they are ever used as a dictionary key or sent to a browser.
MAX_KEY_LENGTH = 120
# Components are already bounded by ``safetext`` before they reach this module,
# but they may be much longer than one UI/storage key.  Keep a reserved marker
# for digest-shortened keys; it is escaped in ordinary components so a short
# key can never collide with the shortened form.
KEY_SHORTEN_MARKER = "~"
KEY_DIGEST_HEX_LENGTH = hashlib.sha256().digest_size * 2
# A raw marker cannot be forged by an upstream component because ``~`` is
# escaped there. It keeps the optional discriminator position distinct from an
# ordinary scope-only key without changing the readable spelling of the common
# source/kind/scope form.
KEY_DISCRIMINATOR_MARKER = "~d="
# The settings endpoint accepts at most this much, and the reader applies the
# same limit before parsing. A hand-edited, synced or otherwise external file
# must not turn one localhost request into an unbounded allocation.
MAX_THRESHOLD_FILE_BYTES = 64 * 1024
THRESHOLD_LOCK_WAIT_SECONDS = 5
# A missing key means "use the shipped default". An explicitly stored empty
# list means "never notify me for this window", so normalization preserves
# empty lists rather than collapsing those two states.
DEFAULT_THRESHOLDS = (80,)

# Where the shared thresholds live. Beside the usage database and the
# dashboard-url file, under the same POSIX 0600-file protection (with a 0700
# parent when this writer creates one): it is a settings
# file rather than a secret, but it is written by a localhost service and there
# is no reason for any other account to be able to rewrite somebody's alerts.
THRESHOLDS_ENV = "CODEX_CLAUDE_USAGE_THRESHOLDS"

# A single process may serve both front ends, and each HTTP server is threaded.
# This lock avoids taking the more expensive file lock concurrently from sibling
# threads; `_threshold_file_lock` is the actual cross-process transaction gate.
_THRESHOLD_WRITE_LOCK = threading.RLock()


def thresholds_path():
    """Where the shared threshold file lives, honouring the usual overrides.

    `CODEX_CLAUDE_USAGE_THRESHOLDS` first so a test -- or a second install -- can
    point somewhere else, then `CLAUDE_CONFIG_DIR`, then the default beside the
    database. Resolved on every call rather than cached at import, because the
    tests patch the environment and a module-level constant would freeze the
    first answer for the whole process.
    """
    override = os.environ.get(THRESHOLDS_ENV)
    if override:
        return Path(override)
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "limit-thresholds.json"
    return Path.home() / ".claude" / "limit-thresholds.json"


def _threshold_lock_file_info(handle, path):
    """Validated metadata, or ``None`` if the path names another file."""
    try:
        info = os.fstat(handle)
        path_info = os.lstat(path)
    except OSError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or not stat.S_ISREG(path_info.st_mode)
            or path_info.st_nlink != 1
            or (path_info.st_dev, path_info.st_ino)
            != (info.st_dev, info.st_ino)):
        return None
    return info


def _threshold_lock_directory_info(handle, path):
    """Validated directory metadata, or ``None`` after a path replacement."""
    try:
        info = os.fstat(handle)
        path_info = os.lstat(path)
    except OSError:
        return None
    if (not stat.S_ISDIR(info.st_mode)
            or not stat.S_ISDIR(path_info.st_mode)
            or (path_info.st_dev, path_info.st_ino)
            != (info.st_dev, info.st_ino)):
        return None
    return info


@contextmanager
def _threshold_file_lock(path=None):
    """Yield whether the cross-process threshold transaction lock was acquired.

    The JSON file itself is atomically replaced, so locking that inode would be
    ineffective: the next writer opens a different inode. Windows holds a
    stable owner-only sidecar with its advisory byte lock. POSIX locks both that
    sidecar (for compatibility with older codex-claude-usage processes) and the
    containing directory (so replacing the sidecar cannot split current
    writers into two lock domains). Native locks are released by the OS if a
    process exits, avoiding stale lock files.
    """
    target = Path(path) if path is not None else thresholds_path()
    lock_path = target.with_name(f".{target.name}.lock")
    handle = None
    directory_handle = None
    lock_handle = None
    acquired = False
    try:
        parent_existed = target.parent.exists()
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix" and not parent_existed:
            try:
                os.chmod(target.parent, 0o700)
            except OSError:
                pass
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_BINARY", 0)
        handle = os.open(str(lock_path), flags, 0o600)
        info = _threshold_lock_file_info(handle, lock_path)
        if info is None:
            raise OSError("unsafe threshold lock file")
        if os.name == "posix":
            os.fchmod(handle, 0o600)
        if info.st_size == 0:
            os.write(handle, b"\0")
            os.fsync(handle)
        lock_handle = handle
        if os.name == "posix":
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_flags |= getattr(os, "O_CLOEXEC", 0)
            directory_handle = os.open(str(target.parent), directory_flags)
            if _threshold_lock_directory_info(
                    directory_handle, target.parent) is None:
                raise OSError("unsafe threshold lock directory")
            lock_handle = directory_handle
        deadline = time.monotonic() + THRESHOLD_LOCK_WAIT_SECONDS
        while True:
            try:
                if os.name == "posix":
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    try:
                        fcntl.flock(
                            directory_handle,
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                    except OSError:
                        # Do not hold the previous-release lock while waiting
                        # to retry the current two-lock protocol.
                        fcntl.flock(handle, fcntl.LOCK_UN)
                        raise
                elif os.name == "nt":
                    os.lseek(lock_handle, 0, os.SEEK_SET)
                    msvcrt.locking(lock_handle, msvcrt.LK_NBLCK, 1)
                else:
                    # The project supports POSIX and Windows. Keep a safe local
                    # fallback for an unknown port instead of failing every save.
                    pass
                unsafe_file = (
                    os.name in ("posix", "nt")
                    and _threshold_lock_file_info(handle, lock_path) is None)
                unsafe_directory = (
                    os.name == "posix"
                    and _threshold_lock_directory_info(
                        directory_handle, target.parent) is None)
                if unsafe_file or unsafe_directory:
                    raise OSError(
                        "threshold lock changed during acquisition")
                acquired = True
                break
            except OSError as exc:
                if (exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
                        or time.monotonic() >= deadline):
                    break
                time.sleep(0.01)
    except OSError:
        acquired = False
    try:
        yield acquired
    finally:
        if handle is not None:
            if acquired:
                try:
                    if os.name == "posix":
                        fcntl.flock(directory_handle, fcntl.LOCK_UN)
                        fcntl.flock(handle, fcntl.LOCK_UN)
                    elif os.name == "nt":
                        os.lseek(lock_handle, 0, os.SEEK_SET)
                        msvcrt.locking(lock_handle, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            if directory_handle is not None:
                try:
                    os.close(directory_handle)
                except OSError:
                    pass
            try:
                os.close(handle)
            except OSError:
                pass


def _window_key_parts(window, source):
    """Return fixed-position source/kind/discriminator/scope components."""
    if not isinstance(window, dict):
        return ()
    kind = _bounded_text(window.get("kind") or "").strip()
    if not kind:
        kind = _bounded_text(window.get("group") or "").strip()
    if not kind:
        return ()
    scope = _bounded_text(window.get("scope") or "").strip()
    discriminator = _bounded_text(
        window.get("scope_discriminator") or "").strip()
    source = _bounded_text(source or "claude").strip() or "claude"
    return source, kind, discriminator, scope


def _legacy_window_key(window, source="claude"):
    """Reproduce the pre-escaped key for conservative compatibility reads."""
    if not isinstance(window, dict):
        return ""
    kind = _bounded_text(window.get("kind") or "", MAX_KEY_LENGTH).strip()
    if not kind:
        kind = _bounded_text(window.get("group") or "", MAX_KEY_LENGTH).strip()
    if not kind:
        return ""
    scope = _bounded_text(window.get("scope") or "", MAX_KEY_LENGTH).strip()
    discriminator = _bounded_text(
        window.get("scope_discriminator") or "", MAX_KEY_LENGTH).strip()
    source = _bounded_text(source or "claude", 32).strip() or "claude"
    parts = ([source, kind]
             + ([discriminator] if discriminator else [])
             + ([scope] if scope else []))
    return ":".join(p.replace(":", "_") for p in parts)[:MAX_KEY_LENGTH].strip()


def legacy_window_key(window, source="claude"):
    """The old key spelling, exposed for compatibility tests and projection."""
    return _legacy_window_key(window, source)


def _escape_key_component(component):
    """Escape separators, the shortening marker and the escape byte itself."""
    return (component.replace("%", "%25")
            .replace(":", "%3A")
            .replace(KEY_SHORTEN_MARKER, "%7E"))


def _shorten_window_key(key):
    """Keep a full-key digest when the display/storage bound is exceeded."""
    if len(key) <= MAX_KEY_LENGTH:
        return key
    prefix_length = MAX_KEY_LENGTH - 1 - KEY_DIGEST_HEX_LENGTH
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return key[:prefix_length] + KEY_SHORTEN_MARKER + digest


def window_key(window, source="claude"):
    """A stable identity for one quota window, or "" if it has none.

    Built from what the window says about ITSELF -- `kind`, and the scope's
    model name when it is scoped -- rather than from a list of known limits, so
    a kind nobody has seen before still gets a key and can still carry a
    threshold.

    The source is part of the key because both assistants publish a `weekly`
    window and they are not the same window. Without it, a Codex threshold
    would silently govern a Claude limit.

    `group` is the fallback when `kind` is missing rather than a component of
    the key: several kinds share one group (`session`, `weekly_scoped` and a
    plain `weekly` all report a group of `session` or `weekly`), so keying on
    it would collapse windows a user wants to set different thresholds on --
    which is the entire feature.

    Components escape `%`, `:`, and the shortening marker before joining. The
    optional discriminator has a reserved structural marker, so a
    discriminator-only identity cannot collide with a scope-only identity. A
    long joined identity keeps a prefix plus a SHA-256 digest, so two windows
    that share a long prefix do not collapse merely because the storage key is
    bounded. Reads still recognize an old spelling when its mapping is
    unambiguous; :func:`live_threshold_keys` keeps that alias out of orphaned
    projections without putting compatibility metadata on the wire.
    """
    components = _window_key_parts(window, source)
    if not components:
        return ""
    # A discriminator is emitted only when two upstream windows would
    # otherwise share the same human-readable scope. Put it before that label
    # so the bounded key cannot truncate away the part that makes it unique.
    # `%` and `~` are escaped as well as `:`: otherwise a literal escape or
    # shortening marker in upstream text could forge another component's key.
    source_component, kind, discriminator, scope = components
    parts = [_escape_key_component(source_component),
             _escape_key_component(kind)]
    if discriminator:
        parts.append(KEY_DISCRIMINATOR_MARKER
                     + _escape_key_component(discriminator))
    if scope:
        parts.append(_escape_key_component(scope))
    escaped = ":".join(parts)
    return _shorten_window_key(escaped).strip()


def window_label(window):
    """What to call this window in a UI, without enumerating the kinds.

    A display string only; `window_key` is the identity. Kept here so the two
    front ends describe the same window the same way.
    """
    if not isinstance(window, dict):
        return ""
    kind = _bounded_text(window.get("kind") or "", MAX_KEY_LENGTH).strip()
    group = _bounded_text(window.get("group") or "", MAX_KEY_LENGTH).strip()
    scope = _bounded_text(window.get("scope") or "", MAX_KEY_LENGTH).strip()
    base = {
        "session": "Session (5-hour)",
        "five_hour": "Session (5-hour)",
        "weekly": "Weekly (all models)",
        # What the live endpoint actually calls it. Confirmed against a real
        # response: `kind=weekly_all, group=weekly, percent=46`. The cache had
        # never carried this window at all, which is the whole reason the live
        # query exists.
        "weekly_all": "Weekly (all models)",
        "weekly_scoped": "Weekly",
        "seven_day": "Weekly (all models)",
    }.get(kind)
    for duration in (kind, group):
        if base is not None:
            break
        if len(duration) > 1 and duration.endswith("m") and duration[:-1].isdigit():
            minutes = int(duration[:-1])
            if minutes == 10080:
                base = "Weekly"
            elif minutes == 1440:
                base = "Daily"
            elif minutes == 300:
                base = "Session (5-hour)"
            elif minutes > 0 and minutes % 1440 == 0:
                base = f"{minutes // 1440}-day"
            elif minutes > 0 and minutes % 60 == 0:
                base = f"{minutes // 60}-hour"
            elif minutes > 0:
                base = f"{minutes}-minute"
    if base is None:
        # An unknown kind is TITLED, not dropped: the point of this module is
        # that a limit nobody anticipated still reaches the user.
        base = (kind or group or "Limit").replace("_", " ").title()
    return f"{base} — {scope}" if scope else base


def _clean_threshold_list(raw):
    """A sorted, de-duplicated list of usable percentages from anything."""
    if not isinstance(raw, list):
        return []
    seen = set()
    for item in raw[:MAX_THRESHOLDS_PER_WINDOW * 4]:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            continue
        # `int()` after the range test, so a float outside the range cannot
        # arrive here as something that overflows on conversion.
        if not (MIN_THRESHOLD <= item <= MAX_THRESHOLD):
            continue
        seen.add(int(item))
    return sorted(seen)[:MAX_THRESHOLDS_PER_WINDOW]


def patch_discarded_anything(submitted, cleaned):
    """Whether normalizing a PATCH dropped a key OR an individual value.

    One definition, because both servers ask it and both used to ask a weaker
    question of their own: `len(cleaned) != len(submitted) or set(cleaned) !=
    set(submitted)`, which is KEY-level only. `normalize_thresholds` drops
    invalid *elements* and keeps the key, so `{"claude:session": [999]}`
    normalized to `{"claude:session": []}`, satisfied both halves of that test,
    and was answered 200 -- while `[]` is the documented way to DISABLE alerts
    for a window. A mistyped percentage silently switched off the notification
    it was meant to set, and the response said it had worked.

    `LIMITS-BACKEND.md` already promised the opposite in as many words: "if any
    supplied update is invalid, the entire request is rejected with 400 and the
    file is left unchanged, so the server never reports success for an edit it
    skipped." This is the code catching up to its own published contract, not a
    new rule -- PUT stays compatibility-tolerant, as the same paragraph says.

    Membership rather than length, so the normalizer's legitimate tidying is
    not mistaken for a discard: it de-duplicates (`[50, 50]` -> `[50]`), sorts
    (`[90, 50]` -> `[50, 90]`) and coerces a whole float (`[50.0]` -> `[50]`).
    Every one of those still has each submitted value represented in the
    output. `0`, `101`, `True` and `"abc"` do not, and are refused. An empty
    list discards nothing and stays legal, which is what keeps "disable this
    window" reachable.
    """
    if set(cleaned) != set(submitted):
        return True
    for key, values in submitted.items():
        if not isinstance(values, list):
            return True
        kept = cleaned.get(key, [])
        for value in values:
            if value not in kept:
                return True
    return False


def normalize_thresholds(raw):
    """`{window key: [percent, ...]}` from anything at all. Never raises.

    Every value is validated on the way IN rather than trusted from the file,
    for the reason `account.py` validates its cache: this file is on disk, a
    second install or a text editor can write it, and a bad value must degrade
    to "no threshold" rather than to a traceback in a server thread.
    """
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in islice(raw.items(), MAX_TRACKED_WINDOWS * 4):
        if not isinstance(key, str):
            continue
        clean_key = _bounded_text(key, MAX_KEY_LENGTH).strip()
        if not clean_key:
            continue
        if not isinstance(value, list):
            continue
        chosen = _clean_threshold_list(value)
        # Empty is meaningful: it disables the default for this one window.
        out[clean_key] = chosen
        if len(out) >= MAX_TRACKED_WINDOWS:
            break
    return out


def read_thresholds(path=None):
    """The stored thresholds, or `{}`. Never raises.

    A missing file is the ordinary first-run state and is not an error. An
    unreadable or malformed one degrades to "no thresholds configured", which
    is the same thing the user would see before ever setting one -- the
    alternative, refusing to serve quota at all because a settings file is
    damaged, would be a worse answer to a smaller problem.
    """
    target = Path(path) if path is not None else thresholds_path()
    try:
        data = read_bounded_regular_file(
            target, MAX_THRESHOLD_FILE_BYTES,
            follow_symlinks=False, single_link=True,
        )
        if data is None:
            return {}
        return normalize_thresholds(json.loads(data.decode("utf-8")))
    except (OSError, ValueError, UnicodeError, RecursionError, MemoryError):
        return {}


def _write_thresholds_unlocked(mapping, path=None):
    """Store `mapping` atomically, or return ``None``. Never raises.

    A temporary file (owner-only on POSIX) is written and fsynced in the same directory,
    then moved into place with `os.replace`. Readers therefore see the old
    complete document or the new complete document, never the truncate/write
    gap; two processes can race without interleaving their bytes. Existing
    symlinks, hard links and non-regular targets retain the refusal policy of
    the old descriptor-based writer.

    Failure is observable. Returning the requested mapping after a refused or
    failed write made both HTTP servers answer 200 for a setting that was never
    armed, including in the read-only Docker image.
    """
    cleaned = normalize_thresholds(mapping)
    payload = json.dumps(cleaned, indent=2, sort_keys=True) + "\n"
    target = Path(path) if path is not None else thresholds_path()
    temp_path = None
    handle = None

    def target_is_safe():
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return stat.S_ISREG(info.st_mode) and info.st_nlink == 1

    try:
        parent_existed = target.parent.exists()
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix" and not parent_existed:
            try:
                os.chmod(target.parent, 0o700)
            except OSError:
                pass
        if not target_is_safe():
            return None
        handle, raw_temp_path = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temp_path = Path(raw_temp_path)
        if os.name == "posix":
            os.fchmod(handle, 0o600)
        encoded = payload.encode("utf-8")
        offset = 0
        while offset < len(encoded):
            written = os.write(handle, encoded[offset:])
            if written <= 0:
                raise OSError("short threshold-file write")
            offset += written
        os.fsync(handle)
        os.close(handle)
        handle = None
        # Refuse a target that was swapped into an unsafe shape while the temp
        # file was being written. A later swap is still safe: replace removes
        # the directory entry rather than following it.
        if not target_is_safe():
            return None
        os.replace(temp_path, target)
        temp_path = None
        if os.name == "posix" and hasattr(os, "O_DIRECTORY"):
            directory = None
            try:
                directory = os.open(
                    str(target.parent), os.O_RDONLY | os.O_DIRECTORY
                    | getattr(os, "O_CLOEXEC", 0))
                os.fsync(directory)
            except OSError:
                pass
            finally:
                if directory is not None:
                    os.close(directory)
    except OSError:
        return None
    finally:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
    return cleaned


def write_thresholds(mapping, path=None):
    """Replace the complete threshold map atomically, or return ``None``.

    This is the compatibility operation used by existing clients. New
    per-window edits should use :func:`update_thresholds`, whose read and write
    share this lock and therefore cannot lose an unrelated concurrent edit in
    the same server process.
    """
    with _THRESHOLD_WRITE_LOCK:
        with _threshold_file_lock(path) as locked:
            if not locked:
                return None
            return _write_thresholds_unlocked(mapping, path)


def update_thresholds(updates, path=None):
    """Merge one or more validated window updates and return the full map.

    The read-modify-write transaction is serialized with whole-map writes.
    Returning ``None`` means either the update shape or the eventual write was
    refused. An explicit empty list remains a valid update because it disables
    the default for that window.
    """
    if not isinstance(updates, dict) or not updates:
        return None
    cleaned = normalize_thresholds(updates)
    # PATCH is stricter than a tolerant file read: silently dropping a key
    # would let the HTTP endpoint claim success for an edit it did not apply.
    if len(cleaned) != len(updates) or set(cleaned) != set(updates):
        return None
    with _THRESHOLD_WRITE_LOCK:
        with _threshold_file_lock(path) as locked:
            if not locked:
                return None
            current = read_thresholds(path)
            current.update(cleaned)
            return _write_thresholds_unlocked(current, path)


def thresholds_for(key, stored, legacy_keys=()):
    """The thresholds configured for one key, with unambiguous legacy fallback.

    Older files used ``:`` -> ``_`` and whole-key truncation.  A caller that has
    the source window can provide its old spelling in ``legacy_keys``; a direct
    new-key entry always wins, and more than one legacy candidate is refused so
    two newly distinct windows can never silently share one old setting. The
    fallback is deliberately not persisted: a stale quota cache can omit a
    second colliding window, so one projection cannot prove global uniqueness.
    """
    if not isinstance(stored, dict):
        return list(DEFAULT_THRESHOLDS)
    if key in stored:
        return _clean_threshold_list(stored.get(key))
    candidates = {
        candidate for candidate in legacy_keys
        if isinstance(candidate, str) and candidate and candidate != key
        and candidate in stored
    }
    if len(candidates) != 1:
        return list(DEFAULT_THRESHOLDS)
    return _clean_threshold_list(stored[next(iter(candidates))])


def _window_key_compatibility(windows, source):
    """Return new keys and the legacy aliases that are safe to keep live."""
    windows = [window for window in (windows or [])
               if isinstance(window, dict)]
    pairs = []
    new_keys = set()
    for window in windows:
        key = window_key(window, source)
        if not key:
            continue
        legacy = legacy_window_key(window, source)
        pairs.append((key, legacy))
        new_keys.add(key)
    legacy_targets = {}
    for key, legacy in pairs:
        if legacy and legacy != key:
            legacy_targets.setdefault(legacy, set()).add(key)
    live = set(new_keys)
    for key, legacy in pairs:
        if (legacy and legacy != key and legacy not in new_keys
                and legacy_targets.get(legacy) == {key}):
            live.add(legacy)
    return new_keys, legacy_targets, live


def live_threshold_keys(windows, source="claude"):
    """Keys and unambiguous legacy aliases for currently reported windows.

    The returned set is an internal projection helper for orphan selection; it
    is never serialized. A legacy alias is included only when exactly one live
    escaped key owns it. New keys are always included, so an old key that is
    also a new identity keeps its exact-key precedence without pretending an
    ambiguous colon/underscore collision was migrated. This is projection-only
    because the reported window set may be incomplete.
    """
    return _window_key_compatibility(windows, source)[2]


def crossed(percent, thresholds):
    """Which thresholds `percent` has reached, lowest first.

    Reaching 91% returns every threshold at or below it, and the CALLER decides
    which of those it has already announced -- that separation is deliberate.
    Returning only "the highest" would mean a window that jumps past two
    thresholds between two polls announces one of them, and returning "the
    newest" would need this function to remember, which is what makes it
    untestable in isolation.
    """
    if percent is None or isinstance(percent, bool):
        return []
    if not isinstance(percent, (int, float)):
        return []
    return [t for t in _clean_threshold_list(thresholds) if percent >= t]


def describe_windows(windows, source="claude", stored=None):
    """`windows` annotated with their key, label and configured thresholds.

    The one place a front end needs to call to render per-window alert
    settings: it does not have to know how a key is built, and therefore cannot
    build one differently from the server that stores it.
    """
    stored = stored if isinstance(stored, dict) else {}
    windows = [window for window in (windows or [])
               if isinstance(window, dict)]
    new_keys, legacy_targets, _ = _window_key_compatibility(windows, source)
    described = []
    for window in windows:
        key = window_key(window, source)
        legacy = legacy_window_key(window, source)
        fallback = ()
        # A legacy spelling is usable only when exactly one live new key owns
        # it and it is not also the new identity of a different window.
        if (legacy and legacy != key
                and legacy not in new_keys
                and legacy_targets.get(legacy) == {key}):
            fallback = (legacy,)
        described.append({
            **window,
            "key": key,
            "label": window_label(window),
            # A keyless future shape still reaches the gauge. It cannot carry a
            # durable per-window setting until it has an identity, so only the
            # threshold control is omitted.
            "thresholds": (thresholds_for(key, stored, fallback)
                            if key else []),
        })
    return described


def orphaned_thresholds(stored, live_keys, source=None):
    """Configured windows that the cache is not currently reporting.

    Callers with live window objects should pass
    :func:`live_threshold_keys`, not only the new ``key`` fields, so an
    unambiguous pre-escape alias is not shown twice as both a live threshold
    and an orphan. Ambiguous legacy entries remain orphaned.

    Keep configured thresholds when a window disappears from a stale cache.
    Surface them as orphaned settings so users can still inspect or remove the
    alert."""
    live = set(live_keys or ())
    prefix = f"{source}:" if source else ""
    return {
        key: value
        for key, value in (stored or {}).items()
        if (key not in live and value
            and (not prefix or key.startswith(prefix)))
    }
