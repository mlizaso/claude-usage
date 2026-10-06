"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from datetime import datetime

from .scanner import VERSION, invocation, terminal_safe
from .dashboard_data import get_dashboard_data, invalidate_payload_cache
from .dashboard_cache import read_snapshot, save_snapshot, snapshot_identity
from .assets import asset_roots as _shared_asset_roots
from .loopback_http import (
    ADDRESS_IN_USE_ERRNOS,
    LoopbackHTTPServer,
    LoopbackRequestHandlerMixin,
    LOOPBACK_HOSTS,
    apply_threshold_write,
    call_with_total_deadline,
    normalized_host as _normalized_host,
    open_loopback_probe,
    read_bounded_json_body,
    request_host_is_acceptable,
    request_origin_is_acceptable,
)
from .db import connect_existing_db, database_admission
from .safejson import safe_dashboard_value
from .safefile import open_regular_file_descriptor, read_bounded_regular_file

DB_PATH = Path(os.environ.get("CLAUDE_USAGE_DB", Path.home() / ".claude" / "usage.db"))

# Transcript roots the process was launched with, or None for "whatever the
# scanner defaults to". `cli.py dashboard --projects-dir X` sets this so the
# Rescan button scans X — it used to always fall back to
# scanner.DEFAULT_PROJECTS_DIRS, so a custom root was scanned once at startup
# and then silently abandoned on every press of the button. Kept as None rather
# than eagerly bound to scanner.DEFAULT_PROJECTS_DIRS so tests that patch that
# global still take effect (same contract as DB_PATH).
PROJECTS_DIRS = None

# Which surface is rendering the dashboard: "web" (standalone `cli.py dashboard`)
# or "vscode" (embedded in the extension's sidebar webview). serve() sets this
# from the --surface flag the extension passes. The footer reads it to decide
# what to show; external links are user-initiated and never fetched by the app.
SURFACE = "web"

LOCAL_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
_configured_api_token = os.environ.get("CLAUDE_USAGE_API_TOKEN", "")
API_TOKEN = (
    _configured_api_token
    if LOCAL_TOKEN_RE.fullmatch(_configured_api_token)
    else secrets.token_urlsafe(32)
)
API_TOKEN_HEADER = "X-Claude-Usage-Token"
RESCAN_PROOF_HEADER = "X-Claude-Usage-Rescan-Proof"
RESCAN_CHALLENGE_HEADER = "X-Claude-Usage-Rescan-Challenge"
# A separate, per-process secret used only to prove that a stale recovery URL
# still names the process that wrote it. It is never sent over HTTP: liveness
# uses a nonce/HMAC challenge, so a process that later reclaims the port cannot
# capture the API bearer or replay an earlier answer.
LIVENESS_TOKEN = secrets.token_urlsafe(32)
_health_token = os.environ.get("CLAUDE_USAGE_HEALTH_TOKEN", "")
HEALTH_PROOF_SECRET = (
    _health_token
    if LOCAL_TOKEN_RE.fullmatch(_health_token)
    else ""
)
VALID_SURFACES = frozenset(("web", "vscode"))
# The assistants whose usage this can show. A `?source=` outside this set
# is ignored rather than rejected, so an old link still renders everything.
KNOWN_SOURCES = frozenset(("claude", "codex"))
CONTAINER_BIND_ENV = "CLAUDE_USAGE_ALLOW_CONTAINER_BIND"
SUPPRESS_AUTH_URL_ENV = "CLAUDE_USAGE_SUPPRESS_AUTH_URL"
CHART_JS_SHA256 = "ecc3cd1eeb8c34d2178e3f59fd63ec5a3d84358c11730af0b9958dc886d7652a"
MAX_HTTP_CONNECTIONS = 32
HTTP_SOCKET_TIMEOUT_SECONDS = 15
# A TOTAL wall-clock budget for reading one request, which is a different thing
# from the per-operation timeout above and is why that one did not bound this.
# `socket.settimeout` applies to each blocking call separately, so a client that
# sends one byte every few seconds renews it forever while holding a connection
# slot -- and the slot is taken at accept time, before any Host, Origin or token
# check, so no credential is needed. 32 such sockets held the dashboard down
# indefinitely; measured, the 33rd connection was reset in 0.00s.
#
# **This bounds the hold; it does NOT close the denial-of-service class, and it
# would be wrong to read it as doing so.** An adversarial review demonstrated
# the obvious follow-up: reconnect faster than the budget and the semaphore
# stays saturated, denying a real user 3 probes in 4. What changes is that a
# wedge is now self-clearing rather than permanent, and that saturation answers
# 503 instead of resetting the socket -- previously indistinguishable from a
# dead server, which sent the reader looking in the wrong place.
#
# Closing the class properly is a different design (per-peer accounting cannot
# help here: every peer is 127.0.0.1). It is deliberately not attempted, because
# the attacker is a local process, and on a loopback-only service a local
# process that wants this dashboard gone can simply signal it. The boundary that
# does matter -- another local user READING the data -- is held by the token,
# the Host/Origin checks and the POSIX 0600 files, none of which this touches.
#
# The budget covers reads only. A response may legitimately take minutes
# (POST /api/rescan on a cold corpus) and is not on this clock.
HTTP_REQUEST_READ_BUDGET_SECONDS = 10
RESCAN_LOCK = threading.Lock()
# The scanner commits incrementally, so a successful data request can observe a
# valid but incomplete database while ingestion is still running.  Keep that
# lifecycle separate from RESCAN_LOCK: the startup scan deliberately does not
# hold that lock, and a manual scan may already be queued behind it.  A counter
# keeps the page in its provisional state until both have finished; a boolean
# would briefly report idle as soon as the first one returned.
_SCAN_ACTIVITY_LOCK = threading.Lock()
_SCAN_ACTIVITY_COUNT = 0
# Every transition gets a new generation.  A reader takes one before and after
# assembling a database response; equality proves that no tracked scan could
# have committed between its queries, even if that scan both started and
# finished inside the build window.  The final worker's outcome is retained so
# a crashed background scan cannot collapse into the same state as a genuinely
# completed, empty history.
_SCAN_GENERATION = 0
_SCAN_LAST_FAILED = False
# Challenges are consumed atomically when a valid extension proof arrives and
# retained for this server process's lifetime. Wall time can move backward by
# an arbitrary amount, so expiring a replay marker from another clock would
# eventually make the proof reusable. The fixed cap bounds hostile input; a
# full set fails closed until the server restarts.
RESCAN_PROOF_LOCK = threading.Lock()
RESCAN_PROOF_USED = set()
RESCAN_PROOF_MAX_AGE_SECONDS = 60.0
RESCAN_PROOF_CLOCK_SKEW_SECONDS = 5.0
RESCAN_PROOF_MAX_ENTRIES = 1024
# A per-window threshold map is small; see do_PUT.
MAX_THRESHOLD_BODY_BYTES = 64 * 1024


def _scan_activity_started():
    global _SCAN_ACTIVITY_COUNT, _SCAN_GENERATION
    with _SCAN_ACTIVITY_LOCK:
        _SCAN_ACTIVITY_COUNT += 1
        _SCAN_GENERATION += 1


def _scan_activity_finished(succeeded):
    global _SCAN_ACTIVITY_COUNT, _SCAN_GENERATION, _SCAN_LAST_FAILED
    with _SCAN_ACTIVITY_LOCK:
        if _SCAN_ACTIVITY_COUNT <= 0:
            raise RuntimeError("dashboard scan activity counter underflow")
        _SCAN_ACTIVITY_COUNT -= 1
        _SCAN_GENERATION += 1
        _SCAN_LAST_FAILED = not succeeded


def scan_status():
    """A stable, database-free snapshot of this process's scan lifecycle."""
    with _SCAN_ACTIVITY_LOCK:
        if _SCAN_ACTIVITY_COUNT > 0:
            state = "scanning"
        elif _SCAN_LAST_FAILED:
            state = "failed"
        else:
            state = "idle"
        return {"state": state, "generation": _SCAN_GENERATION}


def start_scan_thread(target, thread_factory=None):
    """Start one daemon scan thread and expose it before the first request.

    The activity is registered synchronously, before ``Thread.start``.  Without
    that ordering a fast browser can ask for scan status after ``serve`` binds
    but before the new thread runs its first instruction, accept zeroes as
    final, and recreate the original race.  ``finish`` is idempotent so a test
    thread factory that runs inline and propagates the target's exception does
    not decrement twice in the outer cleanup path.
    """
    if thread_factory is None:
        thread_factory = threading.Thread

    _scan_activity_started()
    finish_lock = threading.Lock()
    finished = False

    def finish(succeeded):
        nonlocal finished
        with finish_lock:
            if finished:
                return
            finished = True
        _scan_activity_finished(succeeded)

    def run():
        succeeded = False
        try:
            # Background wrappers return False after reporting a caught scan
            # error.  Existing thread targets conventionally return None, so
            # only an explicit False denotes failure.
            succeeded = target() is not False
        finally:
            finish(succeeded)

    try:
        thread = thread_factory(target=run, daemon=True)
        thread.start()
    except BaseException:
        finish(False)
        raise
    return thread


def url_authority(host):
    """`host` as it is spelled inside a URL — an IPv6 literal needs brackets.

    One definition because there are now three callers (the authenticated URL,
    the line `serve` prints, and the /healthz probe), and three copies of a
    conditional is how the two that already existed would have drifted.
    """
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def authenticated_dashboard_url(host, port, token=API_TOKEN,
                                liveness_token=None):
    """Return a URL whose fragment carries the local API bearer token.

    URL fragments are available to the dashboard JavaScript but are never sent
    in HTTP requests, server logs, or referrer headers. Keeping the token out of
    the unauthenticated HTML bootstrap page means another local OS account
    cannot recover it by simply requesting ``/``.
    """
    if not LOCAL_TOKEN_RE.fullmatch(token):
        raise ValueError("invalid dashboard API token")
    liveness = LIVENESS_TOKEN if liveness_token is None else liveness_token
    if not LOCAL_TOKEN_RE.fullmatch(liveness):
        raise ValueError("invalid dashboard liveness token")
    return (f"http://{url_authority(host)}:{port}/#token={token}"
            f"&liveness={liveness}")


# Where a running server leaves its authenticated URL so the same user can get
# back in. Beside the database as a POSIX 0600 file; a parent created here is
# 0700, while an existing parent is not described by that creation mode. It carries
# the API token, so it is exactly as sensitive as the usage database itself and
# no more widely readable.
#
# This exists because losing the URL was a dead end. The token is deliberately
# never in the page (another local account could otherwise recover it by
# requesting `/`), so a browser opened at the bare address had no way back and
# no way to say so — which is precisely what a reader hits after closing the
# terminal the launcher printed to.
URL_FILE = DB_PATH.parent / "dashboard-url"
MAX_URL_FILE_BYTES = 1024
URL_FILE_LOCK_WAIT_SECONDS = 5.0
# POSIX flock locks separate opens even within one process, and Windows byte
# locks have similarly unhelpful same-process semantics.  Serialize sibling
# threads first; the retained sidecar below is the cross-process transaction
# gate shared by URL writes and conditional removals.
_URL_FILE_TRANSACTION_LOCK = threading.RLock()


def _url_lock_path(target):
    return target.with_name(f".{target.name}.lock")


def _url_lock_descriptor_is_safe(handle, lock_path):
    """Whether ``handle`` and the stable sidecar path name one safe file."""
    descriptor_info = os.fstat(handle)
    path_info = os.lstat(lock_path)
    if (not stat.S_ISREG(descriptor_info.st_mode)
            or descriptor_info.st_nlink != 1
            or not stat.S_ISREG(path_info.st_mode)
            or path_info.st_nlink != 1
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)):
        return False
    if os.name == "posix" and (
            descriptor_info.st_uid != os.getuid()
            or path_info.st_uid != os.getuid()):
        return False
    return True


@contextmanager
def _url_file_lock(target):
    """Yield whether the recovery-file transaction lock was acquired.

    The URL file is atomically replaced, so its inode cannot be the lock: the
    next writer would open a different one.  A same-directory sidecar is kept
    permanently and locked with the platform's native advisory primitive.
    Removing that sidecar after release would recreate the inode-split race the
    lock exists to close.  An unsafe or unavailable lock fails closed.
    """
    target = Path(target)
    lock_path = None
    handle = None
    native_locked = False
    acquired = False
    with _URL_FILE_TRANSACTION_LOCK:
        try:
            lock_path = _url_lock_path(target)
            parent_existed = target.parent.exists()
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == "posix" and not parent_existed:
                try:
                    os.chmod(target.parent, 0o700)
                except OSError:
                    pass

            flags = os.O_RDWR | os.O_CREAT
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_NONBLOCK", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOINHERIT", 0)
            flags |= getattr(os, "O_BINARY", 0)
            handle = os.open(str(lock_path), flags, 0o600)
            if not _url_lock_descriptor_is_safe(handle, lock_path):
                raise OSError("unsafe dashboard URL lock file")
            if os.name == "posix":
                os.fchmod(handle, 0o600)
            elif os.name == "nt" and os.fstat(handle).st_size == 0:
                # msvcrt locks a byte range, so one durable byte must exist.
                os.write(handle, b"\0")
                os.fsync(handle)

            deadline = time.monotonic() + URL_FILE_LOCK_WAIT_SECONDS
            while True:
                try:
                    if os.name == "posix":
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    elif os.name == "nt":
                        import msvcrt
                        os.lseek(handle, 0, os.SEEK_SET)
                        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
                    else:
                        break
                    native_locked = True
                    if not _url_lock_descriptor_is_safe(handle, lock_path):
                        raise OSError("dashboard URL lock changed during acquisition")
                    acquired = True
                    break
                except OSError as exc:
                    retryable = {
                        errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EINTR,
                    }
                    if (exc.errno not in retryable
                            or time.monotonic() >= deadline):
                        break
                    time.sleep(0.01)
        except (ImportError, OSError, ValueError):
            acquired = False

        try:
            yield acquired
        finally:
            if handle is not None:
                if native_locked:
                    try:
                        if os.name == "posix":
                            import fcntl
                            fcntl.flock(handle, fcntl.LOCK_UN)
                        elif os.name == "nt":
                            import msvcrt
                            os.lseek(handle, 0, os.SEEK_SET)
                            msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
                    except (ImportError, OSError):
                        pass
                try:
                    os.close(handle)
                except OSError:
                    pass


def _url_target_is_safe(target):
    """Whether an existing recovery-file destination is safe to replace.

    The bearer is written to a same-directory temporary file and installed with
    ``os.replace`` below, so this is a refusal policy rather than the write
    boundary: replacing a path entry never follows a final symlink. The shared
    safe-file policy preserves the hard-link, special-file, symlink, identity,
    and POSIX-owner refusals for targets that already exist.
    """
    try:
        os.lstat(target)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    descriptor = open_regular_file_descriptor(
        target,
        follow_symlinks=False,
        single_link=True,
        owner_only=True,
    )
    if descriptor is None:
        try:
            os.lstat(target)
        except FileNotFoundError:
            # A disappearing destination is safe: final replacement creates a
            # new entry and cannot write through whatever removed it.
            return True
        except OSError:
            pass
        return False
    try:
        return True
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def write_url_file(host, port, path=None, token=API_TOKEN):
    """Record the authenticated URL for `cli.py url`. Best effort, never raises.

    This file carries the API token in the clear, so what it may be written
    through is the whole of its security. The complete URL is written to a
    same-directory temporary file, fsynced, and installed with `os.replace`, so
    the destination path is never opened for writing and a final symlink race
    cannot redirect the token.

    Four shapes are refused, and only the first of them used to be.

    * A SYMLINK, which would replace the path rather than the file it names.
    * A HARD LINK, which `O_NOFOLLOW` does not touch, because a hard link is not
      a symlink: `st_nlink != 1` is the only thing that sees it. Without this
      check an attacker who could pre-create `~/.claude/dashboard-url` as a
      second name for a file they can read would receive the token in the
      replacement's old role. That is the same refusal
      `db.secure_db_permissions` gives the database.
    * A NON-REGULAR file -- a FIFO, a device -- which `S_ISREG` sees. `O_NONBLOCK`
      is what keeps the FIFO case from hanging this call forever on the open
      itself, where no later check could ever run.
    * On POSIX, a file owned by another user. Ownership is checked both on the
      observed path and the descriptor, matching the database and read-side
      rule.

    A refusal returns None, like every other failure here -- losing the recovery
    link is a far smaller harm than handing the token to whoever pre-created the
    path.
    """
    target = Path(path) if path is not None else URL_FILE
    with _url_file_lock(target) as locked:
        if not locked:
            return None
        return _write_url_file_locked(host, port, target, token)


def _write_url_file_locked(host, port, target, token):
    """Install one recovery URL while ``_url_file_lock`` is held."""
    temp_path = None
    handle = None
    try:
        parent_existed = target.parent.exists()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix" and not parent_existed:
            try:
                os.chmod(target.parent, 0o700)
            except OSError:
                pass
        url = authenticated_dashboard_url(host, port, token)
        if not _url_target_is_safe(target):
            return None

        handle, raw_temp_path = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temp_path = Path(raw_temp_path)
        if os.name == "posix":
            os.fchmod(handle, 0o600)
        encoded = (url + "\n").encode("utf-8")
        offset = 0
        while offset < len(encoded):
            written = os.write(handle, encoded[offset:])
            if written <= 0:
                raise OSError("short dashboard URL write")
            offset += written
        os.fsync(handle)
        os.close(handle)
        handle = None

        # Refuse a destination that became unsafe while the temp file was being
        # written. A swap after this check is still safe: os.replace removes the
        # destination entry instead of following a final symlink.
        if not _url_target_is_safe(target):
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
        return target
    except (OSError, ValueError):
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


def read_url_file(path=None):
    """The URL a running server left, or None. Never raises.

    Refuses anything that is not a plain loopback dashboard URL, so a tampered
    file cannot turn `cli.py url` into a link to somewhere else.
    """
    target = Path(path) if path is not None else URL_FILE
    raw = read_bounded_regular_file(
        target,
        MAX_URL_FILE_BYTES,
        owner_only=True,
        follow_symlinks=False,
        single_link=True,
    )
    if raw is None:
        return None
    text = raw.decode("utf-8", "replace").strip()
    matched = re.fullmatch(
        r"http://(?:127\.0\.0\.1|localhost|\[::1\]):([0-9]{1,5})/"
        r"#token=([A-Za-z0-9_-]{32,128})"
        r"&liveness=([A-Za-z0-9_-]{32,128})",
        text)
    if not matched or not 1 <= int(matched.group(1)) <= 65535:
        return None
    return text


def url_is_live(url, timeout=1.5):
    """Does this URL still reach a working dashboard? Never raises.

    Three answers, not two — True (live), False (proven dead), None (could not
    tell) — because the caller *deletes the file* on anything but True, and that
    file is the only way back into a running dashboard. A request that did not
    complete is not evidence of a dead server: a process paused mid-request, a
    peer that accepts and never answers, a reset connection all look identical
    to one, and calling them dead spent the reader's only link for good.

    Existence of the file is not evidence that the server is up: a crash, a
    SIGTERM or a closed terminal never runs the cleanup, so the file outlives
    the process. Nor is a listening port evidence that the LINK works — a
    restarted server mints a new token, so a stale file can name a live port and
    still be useless.

    The API bearer is never sent to this unverified peer. The URL file also
    carries a per-process liveness secret; a fresh public nonce is sent to a
    proof endpoint, which HMACs the nonce together with the server's API-token
    digest. A matching proof establishes both process identity and that the
    recovered bearer is the one that process accepts. A later port owner sees
    neither secret and cannot replay a proof for a fresh nonce.
    """
    import urllib.error
    import urllib.request

    matched = re.fullmatch(
        r"(http://(?:127\.0\.0\.1|localhost|\[::1\]):([0-9]{1,5}))/"
        r"#token=([A-Za-z0-9_-]{32,128})"
        r"&liveness=([A-Za-z0-9_-]{32,128})",
        url or "",
        re.IGNORECASE,
    )
    if not matched:
        # Not a dashboard link at all — settled by inspection, no probe needed.
        return False
    if not 1 <= int(matched.group(2)) <= 65535:
        return False
    api_token = matched.group(3)
    liveness_token = matched.group(4)
    challenge = secrets.token_urlsafe(24)
    request = urllib.request.Request(
        matched.group(1) + "/api/instance?challenge=" + challenge,
    )
    # Compute the proof before handing work to the daemon. A peer that drips a
    # response beyond the deadline can retain this digest, but neither secret.
    expected = _liveness_proof(challenge, api_token, liveness_token)

    def probe():
        try:
            with open_loopback_probe(request, timeout=timeout) as response:
                if response.status != 200:
                    return None
                raw = response.read(4097)
                if len(raw) > 4096:
                    return False
                body = json.loads(raw.decode("utf-8"))
                if not isinstance(body, dict):
                    return False
                supplied = body.get("proof")
                return (isinstance(supplied, str)
                        and secrets.compare_digest(supplied, expected))
        except urllib.error.HTTPError as exc:
            # A 4xx proves that the port holder does not implement this
            # process's proof. A server-side failure may be transient.
            return False if 400 <= exc.code < 500 else None
        except Exception as exc:
            # A refused connection proves nothing is listening. A timeout,
            # reset, or other failure proves only that the probe did not finish.
            reason = getattr(exc, "reason", None)
            if (isinstance(exc, ConnectionRefusedError)
                    or isinstance(reason, ConnectionRefusedError)):
                return False
            return None

    completed, answer = call_with_total_deadline(probe, timeout)
    return answer if completed else None


def _liveness_proof(challenge, api_token=None, liveness_token=None):
    """HMAC proof for one nonce and one token-bearing recovery URL."""
    token = API_TOKEN if api_token is None else api_token
    liveness = LIVENESS_TOKEN if liveness_token is None else liveness_token
    token_digest = hashlib.sha256(token.encode("ascii")).digest()
    message = challenge.encode("ascii") + b"\0" + token_digest
    return hmac.new(liveness.encode("ascii"), message, hashlib.sha256).hexdigest()


def _health_proof(challenge, health_secret=None):
    """HMAC proof for the extension's one-shot readiness challenge."""
    secret = HEALTH_PROOF_SECRET if health_secret is None else health_secret
    message = b"claude-usage-health\0" + challenge.encode("ascii")
    return hmac.new(secret.encode("ascii"), message, hashlib.sha256).hexdigest()


def _rescan_proof(challenge, health_secret=None):
    """HMAC proof for one extension rescan request.

    This is deliberately a different domain from the liveness proof. The
    resulting value authorizes one scan, not a reusable API bearer, so a port
    occupant that wins the final check/connection race cannot capture the
    token used by the embedded browser.
    """
    secret = HEALTH_PROOF_SECRET if health_secret is None else health_secret
    message = b"claude-usage-rescan\0POST /api/rescan\0" + challenge.encode("ascii")
    return hmac.new(secret.encode("ascii"), message, hashlib.sha256).hexdigest()


def _rescan_challenge_lifetime(challenge, now=None):
    """Return this proof's remaining lifetime, or ``None`` if it is stale.

    The extension appends its wall-clock issue time in milliseconds to the
    random challenge. Binding that value into the HMAC makes ordinary expiry
    intrinsic. A small future allowance tolerates the two runtimes sampling the
    same host clock around an update, while a larger jump fails closed. Replay
    tracking is deliberately independent of this wall-clock lifetime.
    """
    try:
        _nonce, issued_text = challenge.rsplit("_", 1)
        if not issued_text or not issued_text.isdecimal():
            return None
        issued_ms = int(issued_text, 10)
        now_ms = int((time.time() if now is None else now) * 1000)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    max_age_ms = int(RESCAN_PROOF_MAX_AGE_SECONDS * 1000)
    skew_ms = int(RESCAN_PROOF_CLOCK_SKEW_SECONDS * 1000)
    age_ms = now_ms - issued_ms
    # The upper bound is exclusive: an exactly-expired proof has no authority.
    if age_ms < -skew_ms or age_ms >= max_age_ms:
        return None
    # A challenge accepted at the future-skew boundary remains valid for
    # ``max_age + skew`` seconds from receipt.
    return max(0.0, (max_age_ms - age_ms) / 1000.0)


def _rescan_proof_is_authorized(handler):
    """Validate and consume one extension rescan proof atomically."""
    if not HEALTH_PROOF_SECRET:
        return False
    challenge_values = handler.headers.get_all(RESCAN_CHALLENGE_HEADER, [])
    proof_values = handler.headers.get_all(RESCAN_PROOF_HEADER, [])
    if len(challenge_values) != 1 or len(proof_values) != 1:
        return False
    challenge = challenge_values[0]
    proof = proof_values[0]
    if (not challenge.isascii()
            or not LOCAL_TOKEN_RE.fullmatch(challenge)
            or not proof.isascii()
            or not re.fullmatch(r"[0-9a-f]{64}", proof)):
        return False
    if _rescan_challenge_lifetime(challenge) is None:
        return False
    expected = _rescan_proof(challenge)
    if not hmac.compare_digest(proof, expected):
        return False
    with RESCAN_PROOF_LOCK:
        if challenge in RESCAN_PROOF_USED:
            return False
        if len(RESCAN_PROOF_USED) >= RESCAN_PROOF_MAX_ENTRIES:
            # Evicting any consumed challenge could make it replayable after a
            # wall-clock rollback. A full set is exceptional, so reject the
            # new request until this server process restarts instead.
            return False
        RESCAN_PROOF_USED.add(challenge)
    return True


def remove_url_file(path=None, only_if=None):
    """Drop the URL when the server stops; it is no longer valid.

    `only_if` guards against a second dashboard on another port deleting the
    first one's link on its way out: both write the same file, so the last to
    start owns it, and without this the first server would become unreachable
    via `cli.py url` while still running perfectly well.
    """
    target = Path(path) if path is not None else URL_FILE
    try:
        target.lstat()
    except OSError:
        # There is no transaction to serialize yet. In particular, cleanup on
        # a fresh install must not create the config directory and lock file it
        # was called to remove. A writer racing after this check is newer state
        # and should survive this cleanup attempt.
        return
    with _url_file_lock(target) as locked:
        if not locked:
            return
        if only_if is not None and read_url_file(target) != only_if:
            return
        try:
            target.unlink()
        except OSError:
            pass


def _running_in_docker():
    return Path("/.dockerenv").is_file()


def validate_bind_host(host):
    """Refuse network-visible binds except the isolated Docker entry point."""
    normalized = _normalized_host(host)
    # Bind the numeric address even when the caller asks for `localhost`.
    # This keeps a poisoned hosts/DNS configuration from turning an allowed
    # hostname into a non-loopback listener.
    if normalized == "localhost":
        return "127.0.0.1"
    if normalized in LOOPBACK_HOSTS:
        return normalized
    if (normalized in ("0.0.0.0", "::")
            and os.environ.get(CONTAINER_BIND_ENV) == "1"
            and _running_in_docker()):
        return normalized
    raise ValueError(
        "Refusing non-loopback dashboard bind. Use localhost/127.0.0.1. "
        f"{CONTAINER_BIND_ENV}=1 is reserved for the loopback-published Docker setup."
    )


def _script_safe_json(value):
    """Serialize JSON for an inline script without allowing </script> breaks.

    `ensure_ascii=True` is the default and is stated here on purpose: it is the
    only reason the two line-separator escapes below are currently unreachable
    (an ASCII-only dump can carry no literal U+2028), and they are kept so that
    flipping this one keyword cannot quietly reintroduce the hazard. The `&`
    escape is not redundant at all — json.dumps never touches it.
    """
    return (json.dumps(value, ensure_ascii=True)
            .replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def _content_security_policy(nonce, surface):
    frame_ancestors = "vscode-webview:" if surface == "vscode" else "'none'"
    return "; ".join((
        "default-src 'self'",
        "base-uri 'none'",
        "object-src 'none'",
        "form-action 'none'",
        "frame-src 'none'",
        f"frame-ancestors {frame_ancestors}",
        "img-src 'self' data:",
        "font-src 'none'",
        "media-src 'none'",
        "worker-src 'none'",
        "connect-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        f"script-src 'self' 'nonce-{nonce}'",
        "script-src-attr 'none'",
    ))


def asset_roots():
    """Every directory a shipped asset tree can sit under, nearest first.

    Each root holds `web/` and `vendor/`. The checkout, Homebrew's libexec, the
    Docker image and the .vsix all put those beside the Python sources; a
    pip/uv install puts them under `<data prefix>/share/claude-usage`.

    `sys.prefix` is that data prefix for a venv — which is what `uv tool
    install` and `pipx` build — and for a plain `pip install`, but *not* for
    `pip install --user`, `--prefix=X` or `--root=X`, whose data root comes from
    the install scheme rather than from the running interpreter. Those installs
    ship every file correctly and used to find none of them, so the module's own
    ancestors are searched as well.
    """
    return _shared_asset_roots(__file__)


def find_web_dir():
    """Locate the web assets in both the source tree and an installed layout.

    Mirrors find_chart_file(): both walk asset_roots() in the same order, so a
    layout that can serve the page can also serve the chart runtime.
    """
    for root in asset_roots():
        candidate = root / "web"
        if (candidate / "index.html").is_file():
            return candidate
    return None


def load_html_template():
    """Assemble the dashboard document from web/index.html + app.css + app.js.

    The three files were one 2,067-line string literal inside this module. They
    are reassembled here rather than served as separate routes so the bytes on
    the wire, the single-document delivery and the CSP (one nonce'd inline
    script) stay exactly as they were — the split is a source-layout change, not
    a behavioural one. tests/test_web_assets.py asserts the reassembly still
    matches the shape the page depends on.
    """
    web = find_web_dir()
    if web is None:
        # Say where it looked, and nothing more. The message used to end "the
        # packaging step did not ship web/", which was a cause it had not
        # checked and which was wrong for every install this search was widened
        # to reach — the files were present, a few directories away. Built from
        # asset_roots() so a new candidate cannot leave it describing a search
        # the code no longer performs.
        searched = ", ".join(str(root / "web") for root in asset_roots())
        raise RuntimeError(
            "Dashboard web assets not found. Looked for web/index.html in: "
            f"{searched}."
        )
    shell = (web / "index.html").read_text(encoding="utf-8")
    return (shell
            .replace("__APP_CSS__", (web / "app.css").read_text(encoding="utf-8"))
            .replace("__APP_JS__", load_app_js(web)))


def app_js_parts(web_dir):
    """The application's JavaScript files, in load order.

    Ordered by filename, and the numeric prefixes are load-bearing rather than
    decorative: this is a classic script, so a top-level `const` must be defined
    before anything references it. Renaming a part re-orders the concatenation.
    """
    return sorted((web_dir / "js").glob("*.js"))


def load_app_js(web_dir):
    """Concatenate the JavaScript parts into the one script the page inlines.

    Splitting the source does not split the delivery: the page still ships as a
    single document with a single nonce'd inline script, so the CSP is unchanged
    and no extra requests are made.
    """
    parts = app_js_parts(web_dir)
    if not parts:
        raise RuntimeError(f"No dashboard JavaScript found in {web_dir / 'js'}.")
    return "".join(p.read_text(encoding="utf-8") for p in parts)


# Read once at import: the document is static, and keeping it a module-level
# constant preserves every existing reference (and the tests' ability to
# substitute it).
HTML_TEMPLATE = load_html_template()

# User-supplied rates, if any. Done at import so every price the server or the
# page quotes comes from the same table — see pricing.load_rate_overrides.
#
# The returned set is kept, not discarded: the page bills from its own copy of
# the table, so a rate that stopped here made `cli.py stats` and the Est. Cost
# tile disagree about the same turns — measured at $1.00 against $5.00, a 5x
# divergence, under a footer telling the reader to set the variable. It travels
# to the browser through APP_CONFIG below.
_APPLIED_RATES = set()
try:
    from . import pricing as _pricing
    _APPLIED_RATES = _pricing.load_rate_overrides()
except Exception:
    pass


def _rate_overrides_for_page():
    """The override table to hand the browser, or `{}` if there is none.

    Resolved server-side rather than shipping the user's file: "a missing field
    keeps the built-in value" is decided in `pricing.load_rate_overrides`, and a
    second implementation of that rule in JavaScript is exactly the drift these
    two copies of the price table already cost enough to avoid.

    Never raises. This runs inside the request handler, and a malformed override
    must degrade to the built-in rates — which is what the page shows anyway —
    rather than take the whole document down.
    """
    try:
        return _pricing.resolved_rates(_APPLIED_RATES)
    except Exception:
        return {}


def _rate_override_entries_for_page():
    """Return overrides in a script-literal-safe key/value transport.

    A JSON object embedded directly in JavaScript treats ``__proto__`` as a
    special object-literal property rather than an ordinary model name. Entry
    pairs preserve every model id as data; the browser accepts the historical
    object shape as well, so this changes only the server-to-page boundary.
    """
    return [[model, rates]
            for model, rates in _rate_overrides_for_page().items()]


def _commands_for_page(surface=None):
    """Action spellings that are actually reachable from this UI surface."""
    selected = surface or SURFACE
    if selected == "vscode":
        return {
            "scan": "Command Palette: Codex / Claude Usage: Rescan Transcripts",
            "diagnose": "Command Palette: Codex / Claude Usage: Show Logs",
            "reconnect": "Command Palette: Codex / Claude Usage: Restart Server",
        }
    command = invocation()
    return {
        "scan": f"{command} scan",
        "diagnose": f"{command} stats",
        "reconnect": f"{command} url --open",
    }


def find_icon_file():
    """Locate the page icon, in the source tree and in every installed layout.

    `web/icon.svg` first, walked through `asset_roots()` exactly as
    `find_web_dir` and `find_chart_file` walk it, so the icon travels with the
    page that asks for it. It used to be searched for in the *extension's*
    `resources/` alone, which only two of the five delivery surfaces carry:
    the checkout and the .vsix. Homebrew installs `libexec/{web,vendor}` and the
    modules, pip installs `share/claude-usage/{web,vendor}`, and the Docker
    image copies `web` and `vendor` — none of the three ships
    `vscode-extension/` at all, so this route 404'd on all three while the page
    around it rendered. `web/app.css` masks `header .header-icon` with it
    unconditionally, so what the reader saw was a blank 26px gap.

    The two extension-relative candidates stay, last: a .vsix built before
    `web/icon.svg` was bundled carries `resources/icon.svg` (put there by vsce
    from the extension directory, not by `scripts/copy-python.js`), and that is
    still the copy the sidebar webview loads.

    Widening the search does not widen what the server will execute, which is
    the objection `find_chart_file`'s digest pin answers. Every root added here
    is a root `find_web_dir` already accepts, and anyone who can plant a file
    at one of them can plant `index.html` and `js/*.js` there and own the whole
    document — the icon is strictly inside that blast radius, not beside it.

    Returns the first existing path, or ``None`` so the /icon.svg route can 404
    gracefully. Gracefully is the accurate word and *silently* is the honest
    one: `web/index.html` renders the icon as a `<span class="header-icon"
    role="img" aria-label="Codex / Claude Usage">` painted by a CSS mask, so a missing
    icon costs the glyph and keeps the accessible name. (This sentence used to
    promise "the header ``<img>`` then just renders empty alt text", describing
    markup the page has never contained — an `<img>` with an empty `alt` is a
    decorative image, which would have hidden the name too.)
    """
    here = Path(__file__).resolve().parent
    candidates = [root / "web" / "icon.svg" for root in asset_roots()]
    candidates += [
        here.parent.parent / "resources" / "icon.svg",
        here.parent / "vscode-extension" / "resources" / "icon.svg",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def chart_asset_bytes():
    """The pinned Chart.js asset's VERIFIED bytes, or None.

    **Returns the bytes it hashed, and that is the whole point.** The digest
    check used to hash one read of the file and hand back a Path, and the route
    then performed a SECOND, independent read and served that -- so the bytes
    on the wire were never the bytes that were verified. A writer who could
    flip `vendor/chart.umd.js` while the server ran got tampered JavaScript
    served with HTTP 200 straight past `CHART_JS_SHA256`: measured on this tree
    with one flipping thread against 600 sequential requests, 35 responses
    carried bytes whose SHA-256 is not the pinned one, and the rest 404'd or
    were genuine. The pin refused a PERSISTENT tamper and was defeated by a
    racing one.

    That mattered more than it looks, because the chart is the ONLY executable
    asset re-read per request: `HTML_TEMPLATE` and every `web/js/*.js` byte are
    frozen at import, so an attacker who gains write access after the server
    starts has no other way in. The page loads this URL under `script-src
    'self'` with no `integrity` attribute, and no CSP directive restricts
    top-level navigation, so code running here can read `API_TOKEN` out of
    `location.hash` and leave with it. The response is also cached
    `private, max-age=86400`, so one won race persists in the browser for a day.

    Reading once is also simply cheaper: the route used to read ~208 KB twice
    per request.
    """
    for root in asset_roots():
        candidate = root / "vendor" / "chart.umd.js"
        if candidate.is_file():
            data = candidate.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if not secrets.compare_digest(digest, CHART_JS_SHA256):
                return None
            return data
    return None


def find_chart_file():
    """The pinned asset's PATH, or None. Kept for callers that want a location.

    Not what the route uses -- serving from a path means a second read, which is
    exactly the gap `chart_asset_bytes` closes. Anything that puts bytes on the
    wire must go through that function instead.
    """
    for root in asset_roots():
        candidate = root / "vendor" / "chart.umd.js"
        if candidate.is_file():
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if not secrets.compare_digest(digest, CHART_JS_SHA256):
                return None
            return candidate
    return None


class DashboardHandler(LoopbackRequestHandlerMixin, BaseHTTPRequestHandler):
    server_version = "ClaudeUsage"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def _host_is_allowed(self):
        return request_host_is_acceptable(
            self.headers, self.server.server_address[1]
        )

    def _origin_is_allowed(self):
        return request_origin_is_acceptable(self.headers)

    def _supplied_credential(self, header):
        """The single value of `header`, or None if it cannot be compared.

        http.server decodes headers as latin-1, so one byte >0x7F arrives here
        as a non-ASCII `str` — and `secrets.compare_digest` refuses those,
        raising TypeError. That exception escaped the pre-auth check into
        socketserver.handle_error, which closes the socket with no response and
        prints a traceback: an unauthenticated caller could turn every
        authenticated route into a dropped connection and spam the operator's
        terminal at will. `_host_is_allowed` and `_origin_is_allowed` already
        apply exactly this guard; the credential headers were missed.
        """
        supplied_values = self.headers.get_all(header, [])
        if len(supplied_values) != 1:
            return None
        supplied = supplied_values[0]
        if not supplied or not supplied.isascii():
            return None
        return supplied

    def _api_request_is_authorized(self):
        supplied = self._supplied_credential(API_TOKEN_HEADER)
        return (supplied is not None
                and self._origin_is_allowed()
                and secrets.compare_digest(supplied, API_TOKEN))

    def _rescan_request_is_authorized(self):
        """Authorize browser rescans or one-shot extension rescans.

        The browser keeps using its bearer through the normal API gate. The
        extension's host-driven request uses the separate health secret and a
        consumed challenge instead, so a post-readiness port race cannot make
        a foreign listener receive a reusable API token.
        """
        if self._api_request_is_authorized():
            return True
        return self._origin_is_allowed() and _rescan_proof_is_authorized(self)

    def _send_bytes(self, status, body=b"", content_type=None,
                    cache_control="no-store", csp=None):
        # Error paths that reject a body before reading it are finished with
        # request input too; do not let the watchdog cut their response.
        self._finish_request_read()
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        if csp:
            self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status, value):
        # Escaped HERE rather than once per route, so a payload assembled in
        # this module cannot ship raw terminal/bidi controls by forgetting the
        # wrap. Values, not keys: safe_dashboard_value recurses into a dict's
        # values and leaves its keys alone, which is only safe while every key
        # is a literal field name in this repo's own code — every key in a real
        # /api/data body is, and a section keyed by transcript text would need a
        # wrap of its own. (No count here on purpose. The one that used to stand
        # in this sentence was a census of one machine's database on one day,
        # and it did not match the body it described.)
        #
        # /api/limits used to skip this. Assembling its own payload in the
        # handler is NOT what made it unusual: /healthz and /api/sources build a
        # dict literal right there in do_GET, which is more of that than
        # /api/limits does — it only assigns a helper's return. What was
        # unique is that its strings had been wrapped by NOBODY, where every
        # other non-literal this method is handed was already covered: /api/data
        # and /api/sources return through dashboard_data's safe_dashboard_value
        # (_collect_dashboard_data, available_sources), /api/rescan's result is
        # scan counts with no transcript text in it, and the two liveness
        # routes' derived strings are SHA-256 HMACs rendered as lowercase hex.
        # So the plan strings went out
        # raw while /api/data embedded the SAME account.current_limits strings
        # escaped — the panel showed the escaped form on first paint and the raw
        # form on the next poll. WHICH call sites hand this method something
        # other than a literal is asserted in tests/test_server_hardening.py
        # rather than counted here, because a census in a comment cannot notice
        # a route added after it was taken.
        #
        # Idempotent, so those existing wraps stay valid for the Python API
        # (`from dashboard import get_dashboard_data`) and this pass is a no-op
        # on them. NOT because the output is ASCII — it is not, and that is the
        # point: terminal_safe replaces only Cc/Cf/Cs, so `café`, CJK paths and
        # emoji survive byte for byte. It is idempotent because the escapes it
        # EMITS are drawn from `\`, `x`, `u` and the hex digits — categories Po,
        # Ll and Nd, none of them among the three it escapes — so a second pass
        # has nothing left to find. tests/test_safejson.py asserts that over the
        # whole code point space instead of quoting a total, because which code
        # points are Cc/Cf/Cs is a function of unicodedata.unidata_version: a
        # total written here would rot the next time CPython's UCD moves. The
        # wrap costs a small fraction of the /api/data build it follows, for a
        # body byte-identical to the one dashboard_data had already wrapped.
        self._send_bytes(
            status,
            json.dumps(safe_dashboard_value(value)).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _authorize_host(self):
        if self._host_is_allowed():
            return True
        self._send_json(421, {"error": "Untrusted Host header"})
        return False

    def _parsed_request_target(self):
        """`self.path` parsed once, or None when it cannot be parsed at all.

        `urlparse` RAISES on a malformed authority — `http://[::1`, `http://[]/`,
        or an IPv6 literal with too many groups — and both request methods used
        to call it outside any `try`. The ValueError escaped into
        `socketserver.handle_error`, which closes the socket with **no response**
        and prints a ~2.3 KB traceback, so an unauthenticated caller could spend
        the operator's terminal at will: measured at 290 KB/s of stderr, and
        under the VS Code extension that stderr is piped into the output channel,
        where it grows without bound.

        That is the same defect `_supplied_credential` already answers for the
        credential headers ("an unauthenticated caller could turn every
        authenticated route into a dropped connection and spam the operator's
        terminal at will") — the request target was simply left out of it.

        Parsed once and handed to both callers rather than re-parsed for the
        query string, so a target cannot be judged parseable in one line and
        raise on the next.
        """
        try:
            return urlparse(self.path)
        except ValueError:
            return None

    def _database_error_body(self, exc):
        """The 500 body for a database read that failed, and whether to retry.

        The string is unchanged and deliberately generic -- it is the one the
        page has always shown, and it names no path, no table and no SQLite
        text, because everything in this body is rendered into the document.

        What is new is `permanent`. The page arms a three-second retry inside
        this branch, which is right for the case it was written for (the server
        binds and serves before the first scan has created the database) and
        wrong for every other one: a foreign file, a damaged file, a refused
        path and a read-only mount all fail identically on every later request,
        so the reader watches "Failed to read the usage database - retrying..."
        forever while the terminal beside them already says exactly what is
        wrong and how to fix it. `cli.cmd_dashboard` has printed that sentence
        since 2026-08-16 -- "every data request will fail the same way until
        that is fixed" -- and the page it describes had no way to know it.

        Absent rather than false when a retry could work, so the flag reads as
        an assertion someone made rather than a default nobody chose.
        """
        from .db import a_retry_could_succeed
        body = {"error": "Failed to read the usage database"}
        if not a_retry_could_succeed(exc):
            body["permanent"] = True
        return body

    def _send_scan_read_blocked(self, status, changed=False):
        """Reject a database body that is provisional or scan-crossed.

        The public message is deliberately fixed and contains no exception,
        transcript, or filesystem text.  A changed generation can already be
        idle when the scan completed entirely inside a slow payload build; it
        still receives 409 so the client waits/checks and rebuilds the body.
        """
        if status["state"] == "failed":
            code = 503
            message = "Usage scan failed"
        elif changed and status["state"] == "idle":
            code = 409
            message = "Usage changed while data was loading"
        else:
            code = 409
            message = "Usage scan in progress"
        self._send_json(code, {"error": message, "scan": status})

    def _begin_scan_stable_read(self):
        """Return the idle generation a database response must retain."""
        status = scan_status()
        if status["state"] != "idle":
            self._send_scan_read_blocked(status)
            return None
        return status

    def _scan_read_stayed_stable(self, before):
        """Whether no tracked scan crossed a database response build."""
        after = scan_status()
        if after == before and after["state"] == "idle":
            return True
        self._send_scan_read_blocked(after, changed=True)
        return False

    def do_GET(self):
        if not self._authorize_host():
            return
        target = self._parsed_request_target()
        if target is None:
            self._send_json(400, {"error": "Bad request target"})
            return
        # self.path includes the query string, but every URL the UI emits has
        # one (e.g. "/?range=all"); compare the bare path so bookmarkable
        # URLs don't fall through to 404.
        path = target.path
        if path in ("/", "/index.html"):
            nonce = secrets.token_urlsafe(18)
            config = _script_safe_json({
                "version": VERSION,
                "surface": SURFACE,
                "commands": _commands_for_page(SURFACE),
                # Fully-resolved five-field rates for every model the user
                # overrode, which is what the page's applyRateOverrides
                # consumes. Empty when nothing was overridden, so the ordinary
                # page ships the same bytes it always did.
                "rate_overrides": _rate_override_entries_for_page(),
            })
            html = (HTML_TEMPLATE
                    .replace("__APP_CONFIG_JSON__", config)
                    .replace("__CSP_NONCE__", nonce))
            body = html.encode("utf-8")
            self._send_bytes(
                200,
                body,
                "text/html; charset=utf-8",
                csp=_content_security_policy(nonce, SURFACE),
            )

        elif path == "/api/instance":
            values = parse_qs(target.query, keep_blank_values=True)
            challenges = values.get("challenge", [])
            if (set(values) != {"challenge"} or len(challenges) != 1
                    or not LOCAL_TOKEN_RE.fullmatch(challenges[0])):
                self._send_json(400, {"error": "Invalid challenge"})
                return
            self._send_json(200, {
                "service": "claude-usage",
                "proof": _liveness_proof(challenges[0]),
            })

        elif path == "/healthz":
            health = {
                "service": "claude-usage",
                "status": "ok",
                "version": VERSION,
            }
            if HEALTH_PROOF_SECRET:
                values = parse_qs(target.query, keep_blank_values=True)
                challenges = values.get("challenge", [])
                if (set(values) != {"challenge"} or len(challenges) != 1
                        or not LOCAL_TOKEN_RE.fullmatch(challenges[0])):
                    self._send_json(404, {"error": "Not found"})
                    return
                health["instance"] = _health_proof(challenges[0])
            self._send_json(200, health)

        elif path == "/api/scan-status":
            if not self._api_request_is_authorized():
                self._send_json(403, {"error": "Forbidden"})
                return
            # Deliberately does not open SQLite.  It is polled while a cold scan
            # owns that file and answers only for work this server process
            # started, so a page can distinguish provisional zeroes from a
            # completed, genuinely empty history without adding database load.
            from . import docker_sources
            self._send_json(200, {**scan_status(), "docker": docker_sources.status()})

        elif path == "/api/snapshot":
            if not self._api_request_is_authorized():
                self._send_json(403, {"error": "Forbidden"})
                return
            values = parse_qs(target.query, keep_blank_values=True)
            requested = values.get("source", [])
            if not values:
                key = "sources"
            elif (set(values) == {"source"} and len(requested) == 1
                  and requested[0] in KNOWN_SOURCES):
                key = requested[0]
            else:
                self._send_json(400, {"error": "Invalid snapshot source"})
                return
            self._send_json(200, {
                "snapshot": read_snapshot(DB_PATH, key, VERSION),
            })

        elif path == "/api/data":
            if not self._api_request_is_authorized():
                self._send_json(403, {"error": "Forbidden"})
                return
            scan_before = self._begin_scan_stable_read()
            if scan_before is None:
                return
            # Pass DB_PATH explicitly so this handler and dashboard_data use
            # the same configured (or test-patched) path rather than each
            # resolving its own module global.
            # Without this the exception escapes into socketserver, which just
            # closes the socket: the page's fetch() and the VS Code webview see
            # a reset connection and cannot tell "the database is briefly
            # locked" from "the server is gone".
            # `?source=` scopes the payload to one assistant. Validated against a
            # fixed set rather than passed through: it reaches a SQL parameter,
            # and an unknown value must mean "everything" (the pre-existing
            # behaviour) rather than an empty dashboard.
            requested = parse_qs(target.query).get("source", [])
            source = requested[0] if len(requested) == 1 else None
            if source not in KNOWN_SOURCES:
                source = None
            snapshot_before = snapshot_identity(DB_PATH) if source else None
            try:
                data = get_dashboard_data(DB_PATH, source)
            except Exception as exc:
                if not self._scan_read_stayed_stable(scan_before):
                    return
                self._send_json(500, self._database_error_body(exc))
                return
            if not self._scan_read_stayed_stable(scan_before):
                return
            if source in KNOWN_SOURCES:
                save_snapshot(DB_PATH, source, data, VERSION,
                              expected_identity=snapshot_before)
            self._send_json(200, data)

        elif path == "/api/sources":
            # Answered before anything is rendered, so the page can ask which
            # assistant you want without first building both. It is one GROUP BY
            # over `turns` where a full payload runs the whole rollup set, so it
            # costs a small fraction of one. No seconds quoted: the figures that
            # used to stand here were taken on one machine on one day and no
            # longer reproduce on it.
            if not self._api_request_is_authorized():
                self._send_json(403, {"error": "Forbidden"})
                return
            scan_before = self._begin_scan_stable_read()
            if scan_before is None:
                return
            snapshot_before = snapshot_identity(DB_PATH)
            try:
                from .dashboard_data import available_sources
                sources = available_sources(DB_PATH)
            except Exception as exc:
                if not self._scan_read_stayed_stable(scan_before):
                    return
                self._send_json(500, self._database_error_body(exc))
                return
            if not self._scan_read_stayed_stable(scan_before):
                return
            save_snapshot(DB_PATH, "sources", {"sources": sources}, VERSION,
                          expected_identity=snapshot_before)
            self._send_json(200, {"sources": sources})

        elif path == "/api/limits/thresholds":
            # The per-window alert thresholds, shared with the standalone
            # `limits_server`. Served from `limits_core` rather than from a copy
            # here, so a threshold set on either surface governs both -- which is
            # the whole reason they live on disk instead of in localStorage.
            if not self._api_request_is_authorized():
                self._send_json(403, {"error": "Forbidden"})
                return
            from . import limits_core
            self._send_json(
                200, {"thresholds": limits_core.read_thresholds()})

        elif path == "/api/limits":
            # The plan panel answers "how much headroom do I have RIGHT NOW", so
            # it polls this on its own short interval rather than riding on
            # /api/data — which costs a full history read, and which the user can
            # (and by default does) turn off. Before this endpoint existed the
            # panel was frozen at whatever it showed on page load: a five-hour
            # window that had ended still read "Window ended" hours after the
            # cache had rolled over to a new, live window.
            if not self._api_request_is_authorized():
                self._send_json(403, {"error": "Forbidden"})
                return
            # The same definition /api/data embeds, so the panel's first paint
            # and its 30-second refresh cannot disagree about what this database
            # recorded in the current window.
            from . import account
            from .dashboard_data import claude_limits
            # The plan projection comes from the config; the database only says
            # what was recorded inside the current window. So a database that
            # cannot be opened SAFELY degrades to the un-enriched projection
            # rather than being opened anyway.
            #
            # It is opened the way /api/data and /api/sources open it: through
            # the guarded existing-only helper, then through path-supplied
            # database_admission retained around the derived reads. A bare
            # sqlite3.connect followed a symlink and created a missing file
            # with the process umask instead of POSIX 0600. That create was
            # reachable on every fresh install — cmd_dashboard binds and serves
            # before its background scan runs, and the plan panel polls this
            # route every 30 seconds.
            payload = None
            try:
                conn = connect_existing_db(DB_PATH)
                if conn is None:
                    invalidate_payload_cache(DB_PATH)
                else:
                    try:
                        conn.row_factory = sqlite3.Row
                        # Keep rebuild admission through the database-derived
                        # projection. Initializing and immediately releasing
                        # the lock would still allow a rebuild marker to commit
                        # between admission and these reads.
                        with database_admission(conn, DB_PATH):
                            payload = claude_limits(conn, os.environ)
                    finally:
                        conn.close()
            except Exception:
                invalidate_payload_cache(DB_PATH)
                payload = None
            if payload is None:
                payload = account.current_limits(os.environ)
            self._send_json(200, payload)

        elif path == "/assets/chart.umd.js":
            chart = chart_asset_bytes()
            if chart is None:
                self._send_json(404, {"error": "Chart asset not found"})
                return
            self._send_bytes(
                200,
                chart,
                "text/javascript; charset=utf-8",
                cache_control="private, max-age=86400",
            )

        elif path == "/icon.svg":
            icon = find_icon_file()
            if icon is None:
                self._send_json(404, {"error": "Icon not found"})
                return
            # SVG is ACTIVE CONTENT, unlike every other image format. Navigated
            # to at the top level — not loaded through the CSS mask the header
            # uses — an SVG carrying an inline <script> runs it in this
            # dashboard's own origin, next to the token. This route is
            # unauthenticated, so that is reachable by any page that can guess
            # the port. The shipped web/icon.svg is inert and `find_icon_file`
            # returns the nearest root first, but neither fact is enforced here
            # the way `find_chart_file` enforces its digest, so the response
            # carries a CSP that makes the question moot: no script, no plugins,
            # no subresources of any kind, and sandboxed into an opaque origin.
            self._send_bytes(
                200,
                icon.read_bytes(),
                "image/svg+xml",
                cache_control="private, max-age=86400",
                csp="default-src 'none'; style-src 'unsafe-inline'; sandbox",
            )

        else:
            self._send_json(404, {"error": "Not found"})

    def _prepare_threshold_write(self):
        if not self._authorize_host():
            return False
        target = self._parsed_request_target()
        if target is None:
            self._send_json(400, {"error": "Bad request target"})
            return False
        if target.path != "/api/limits/thresholds":
            self._send_json(404, {"error": "Not found"})
            return False
        if not self._api_request_is_authorized():
            self._send_json(403, {"error": "Forbidden"})
            return False
        return True

    def _threshold_write_body(self):
        """Read one bounded JSON body while the absolute watchdog stays armed."""
        return read_bounded_json_body(
            self,
            MAX_THRESHOLD_BODY_BYTES,
            lambda status, message: self._send_json(status, {"error": message}),
            {
                "transfer_encoding": "Transfer encoding is not supported",
                "content_length": "Content length required",
                "bad_request": "Bad request",
                "empty_body": "Empty body",
                "too_large": "Too large",
                "incomplete_body": "Incomplete body",
                "bad_json": "Bad JSON",
            },
        )

    def do_PUT(self):
        """Replace the shared alert-threshold map."""
        if not self._prepare_threshold_write():
            return
        from . import limits_core
        apply_threshold_write(
            self,
            patch=False,
            normalize=limits_core.normalize_thresholds,
            discarded=limits_core.patch_discarded_anything,
            replace=limits_core.write_thresholds,
            update=limits_core.update_thresholds,
            send=self._send_json,
            messages={
                "put_object": "Thresholds must be an object",
                "patch_object": "Threshold updates must be an object",
                "invalid": "Invalid threshold update",
                "save": "Could not save thresholds",
            },
        )

    def do_PATCH(self):
        """Merge selected windows without overwriting unrelated settings."""
        if not self._prepare_threshold_write():
            return
        from . import limits_core
        apply_threshold_write(
            self,
            patch=True,
            normalize=limits_core.normalize_thresholds,
            discarded=limits_core.patch_discarded_anything,
            replace=limits_core.write_thresholds,
            update=limits_core.update_thresholds,
            send=self._send_json,
            messages={
                "put_object": "Thresholds must be an object",
                "patch_object": "Threshold updates must be an object",
                "invalid": "Invalid threshold update",
                "save": "Could not save thresholds",
            },
        )

    def do_POST(self):
        if not self._authorize_host():
            return
        target = self._parsed_request_target()
        if target is None:
            self._send_json(400, {"error": "Bad request target"})
            return
        path = target.path
        if path == "/api/rescan":
            if not self._rescan_request_is_authorized():
                self._send_json(403, {"error": "Forbidden"})
                return
            if not RESCAN_LOCK.acquire(blocking=False):
                self._send_json(409, {"error": "A rescan is already running"})
                return
            _scan_activity_started()
            activity_finished = False
            # Incremental scan: ingest new/changed JSONL without touching
            # existing rows; scan() dedupes via the message_id index.
            #
            # **This endpoint CAN delete history, and `include_defaults=True`
            # is what bounds the damage.** The sentence here used to read "the
            # DB is append-only ... so we must never delete it here", which
            # stopped being true when the migrations were replaced by
            # declare-and-rebuild: `scan()` calls `db.init_db`, which drops
            # every table when the schema in front of it is not this build's,
            # and the scan then refills only the roots it was handed. A scan
            # that can delete must never cover fewer roots than the scan that
            # filled the database — which is what `cli.cmd_scan` covers, so
            # this passes what `cmd_scan` passes rather than a narrower set.
            #
            # Reproduced 2026-08-15 against the arguments this handler used to
            # compute: a dashboard started with `--projects-dir X`, whose
            # startup scan goes through `cmd_scan` and so legitimately holds
            # the defaults as well; an older install touches the file; one
            # press of Rescan. Six sessions down to one, HTTP 200 with a
            # success-shaped body, and no banner, because `processed_files` is
            # no longer empty and `dashboard_data._database_is_unscanned` reads
            # exactly that. The only signal was one stderr line nobody looking
            # at a browser can see.
            #
            # DB_PATH is passed explicitly so tests that patch the module
            # global are honored (scan's defaults are frozen at def time and
            # would otherwise target the real paths). PROJECTS_DIRS is passed
            # as-is, None included — serve() sets it from --projects-dir, and
            # `scanner.resolve_scan_roots` reads DEFAULT_PROJECTS_DIRS at call
            # time, so a patched one is still effective and no fallback is
            # needed here.
            try:
                try:
                    from . import scanner
                    db_path = DB_PATH
                    result = scanner.scan(
                        db_path=db_path,
                        projects_dirs=PROJECTS_DIRS,
                        include_defaults=True,
                        include_docker=True,
                        verbose=False,
                    )
                except Exception:
                    # Answer, rather than dropping the socket: the UI's Rescan
                    # button needs to distinguish a failed scan from a dead server.
                    invalidate_payload_cache(db_path)
                    _scan_activity_finished(False)
                    activity_finished = True
                    self._send_json(500, {"error": "Rescan failed"})
                    return
                # Publish the outcome before sending 200.  The browser reloads
                # immediately after the response; leaving the counter active
                # until after the body write would create a needless 409 race.
                _scan_activity_finished(True)
                activity_finished = True
                from . import docker_sources
                self._send_json(200, {**result, "docker": docker_sources.status()})
            finally:
                if not activity_finished:
                    _scan_activity_finished(False)
                RESCAN_LOCK.release()
        else:
            self._send_json(404, {"error": "Not found"})


class DashboardHTTPServer(LoopbackHTTPServer):
    """Threaded local server with bounded concurrency and stalled-I/O limits."""

    def __init__(self, server_address, handler_class):
        super().__init__(
            server_address,
            handler_class,
            max_connections=MAX_HTTP_CONNECTIONS,
            socket_timeout=lambda: HTTP_SOCKET_TIMEOUT_SECONDS,
            request_read_budget=lambda: HTTP_REQUEST_READ_BUDGET_SECONDS,
            overload_body=(
                b'{"error": "Too many concurrent connections; retry shortly"}'
            ),
        )


class DashboardHTTPServerV6(DashboardHTTPServer):
    address_family = socket.AF_INET6


class PortInUseError(RuntimeError):
    """The port the dashboard asked for is held by something else.

    Carries `lines` — the remedy, already worked out — instead of leaving the
    caller to reconstruct it, because which advice is correct depends on WHAT
    holds the port, and that is only answerable around the moment the bind
    failed. `str(exc)` is the first line alone, so even a caller that just
    prints the exception says something true.

    A class of its own rather than the ValueError `validate_dashboard_args`
    raises: a busy port is not a bad argument. Nothing the user typed is wrong
    and the identical command works once the other process is gone, so it earns
    a remedy rather than the one-line complaint every mistyped flag gets.

    It exists because the alternative was socketserver's bare `OSError: [Errno
    48] Address already in use` under nine frames of stdlib — which names
    neither the port, nor the dashboard already serving on it, nor the
    `cli.py url` that would have reopened that dashboard without stopping
    anything at all.
    """

    def __init__(self, host, port, lines):
        super().__init__(lines[0])
        self.host = host
        self.port = port
        self.lines = list(lines)


def _port_answers_as_this_app(host, port, timeout=1.5):
    """Is the thing holding this port one of our own dashboards? Never raises.

    True only on /healthz's own marker, so it cannot be fooled by an unrelated
    service that merely answers. False means something replied and was not us.

    None means the question was not settled, and the caller must never phrase
    that as "not this app" — a dashboard launched by the VS Code extension is
    exactly a None: it guards /healthz with an instance token and answers an
    unauthenticated probe with 404, which is indistinguishable here from any
    other program's 404. Advice given for None therefore has to be safe when
    the holder turns out to be ours after all.
    """
    import urllib.request

    request = urllib.request.Request(
        f"http://{url_authority(host)}:{port}/healthz")

    def probe():
        try:
            with open_loopback_probe(request, timeout=timeout) as response:
                if response.status != 200:
                    return None
                # Bounded read: this is an unidentified peer, and it may be
                # happy to send a gigabyte to a client asking what it is.
                body = json.loads(response.read(4096).decode("utf-8"))
        except Exception:
            # Includes the HTTPError for the extension's guarded 404.
            return None
        if not isinstance(body, dict):
            return None
        return (body.get("service") == "claude-usage"
                and body.get("status") == "ok")

    completed, answer = call_with_total_deadline(probe, timeout)
    return answer if completed else None


def _first_free_port(host, start, attempts=20):
    """The nearest free port at or above `start`, or None. Never raises.

    Probed with the same address family and SO_REUSEADDR the real server binds
    with, so a port this offers is one `serve()` can actually take — suggesting
    a port that then fails the same way would be worse than saying nothing,
    which is what returning None does.
    """
    family = socket.AF_INET6 if host == "::1" else socket.AF_INET
    for candidate in range(max(int(start), 1), min(int(start) + attempts, 65536)):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((host, candidate))
        except OSError:
            # In use, or a host this process cannot bind at all. The first is
            # worth stepping over; the second fails identically for every
            # candidate and simply yields None.
            continue
        return candidate
    return None


def _stop_commands(port, identified):
    """How to stop whatever holds `port`, as printable command lines.

    Two shapes on purpose, and the difference is a safety rule rather than a
    style choice. Once the holder has identified itself as one of our own
    dashboards there is nothing to inspect, so the one-liner does the whole
    job. When it has NOT — which includes every case `_port_answers_as_this_app`
    could not settle — that same one-liner would kill a stranger's process on
    the strength of a port number, so the discovery command is separate and the
    reader supplies the PID once they have looked at it.
    """
    if os.name == "posix":
        if identified:
            return [f"  kill $(lsof -ti tcp:{port} -sTCP:LISTEN)"]
        steps = [(f"lsof -nP -iTCP:{port} -sTCP:LISTEN", "what is holding it"),
                 ("kill <PID>", "once you know what it is")]
    else:
        steps = [(f"netstat -ano | findstr :{port}", "the PID is the last column"),
                 ("taskkill /PID <PID>", "" if identified else "once you know what it is")]
    # Padded from the widest command rather than by hand: the port is 1-5
    # digits, so hand-spaced comments line up for one port width and stagger
    # for the rest.
    width = max(len(command) for command, _ in steps)
    return [f"  {command.ljust(width)}  # {note}" if note else f"  {command}"
            for command, note in steps]


def port_in_use_lines(host, port):
    """Why the dashboard did not start and what to do about it, line by line.

    Three outcomes, because three different actions are right:

    * a dashboard is already running there AND its saved link still works —
      then nothing needs stopping at all, `cli.py url --open` gets you back in.
      This is the case the bare traceback hid most expensively: the remedy was
      one command, and it read as a crash.
    * a dashboard is already running there and no working link names it — its
      token is unrecoverable (it is deliberately never in the page), so the
      page it is serving cannot be reached by anyone and stopping it is the
      only way forward.
    * anything else — including a dashboard that could not be identified, see
      `_port_answers_as_this_app` — so the advice must not assume it is ours,
      and must not assume it is not.

    Every value interpolated here is this process's own: `port` is an int past
    `validate_dashboard_args`, `host` is past `validate_bind_host`, and the
    suggested port comes from a bind. Nothing read off the network reaches the
    text, which is why these lines are printed as-is — `terminal_safe` escapes
    Cc, and a newline is Cc, so routing a multi-line remedy through it would
    collapse the whole thing onto one line as `\\x0a`.
    """
    lines = [f"Port {port} is already in use, so the dashboard did not start."]
    saved = read_url_file()
    # read_url_file has already refused anything that is not a loopback
    # dashboard URL, so this matches whenever `saved` is not None.
    saved_port = re.search(r":([0-9]{1,5})/#token=", saved or "")
    if saved and saved_port and int(saved_port.group(1)) == port and url_is_live(saved) is True:
        identified = True
        lines += [
            "",
            "A claude-usage dashboard is already running on it, and its link still works:",
            "",
            f"  {invocation()} url --open     # reopen the one that is already running",
            "",
            "Stop it first only if you actually want a fresh one:",
        ]
    elif _port_answers_as_this_app(host, port):
        identified = True
        lines += [
            "",
            "It is another claude-usage dashboard, but no saved link names it, so its API",
            "token cannot be recovered and the page it serves is unreachable. Stop it, then",
            "run this command again:",
        ]
    else:
        identified = False
        lines += [
            "",
            "Whatever is listening there did not identify itself as this dashboard, so",
            "check what it is before stopping it:",
        ]
    lines += _stop_commands(port, identified)
    free = _first_free_port(host, port + 1)
    if free is not None:
        lines += [
            "",
            "Or leave it alone and use another port:",
            f"  {invocation()} dashboard --port {free}",
        ]
    return lines


def serve(host=None, port=None, surface=None, projects_dirs=None, on_ready=None):
    global SURFACE, PROJECTS_DIRS
    if projects_dirs:
        PROJECTS_DIRS = [Path(d) for d in projects_dirs]
    selected_surface = surface or SURFACE
    if selected_surface not in VALID_SURFACES:
        raise ValueError("surface must be one of: " + ", ".join(sorted(VALID_SURFACES)))
    SURFACE = selected_surface
    host = host or os.environ.get("HOST", "localhost")
    host = validate_bind_host(host)
    port = port or int(os.environ.get("PORT", "8080"))
    server_class = DashboardHTTPServerV6 if host == "::1" else DashboardHTTPServer
    try:
        server = server_class((host, port), DashboardHandler)
    except OSError as exc:
        # Only the one condition that has a remedy. Every other bind failure —
        # a privileged port, an address that is not ours — is left exactly as
        # it was, because inventing advice for it would be guessing.
        if exc.errno not in ADDRESS_IN_USE_ERRNOS:
            raise
        raise PortInUseError(host, port, port_in_use_lines(host, port)) from exc
    print(f"Dashboard listening at http://{url_authority(host)}:{port}")
    if os.environ.get(SUPPRESS_AUTH_URL_ENV) != "1":
        print(f"Open the authenticated dashboard: {authenticated_dashboard_url(host, port)}")
    print("Press Ctrl+C to stop.")
    # Leave the URL where the same user can find it again. The launcher prints
    # it once; a terminal that has scrolled away or been closed used to make the
    # dashboard unreachable without restarting it.
    write_url_file(host, port)
    try:
        # Whatever the launcher wants running alongside the server, started only
        # now that the port is genuinely ours. `cmd_dashboard` hangs its
        # background scan here: that scan prints for as long as it takes to walk
        # ~/.claude/projects, so starting it before the bind buried the failure
        # under its output — and spent a full cold scan on a process that was
        # about to exit.
        if on_ready is not None:
            on_ready()
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        # Only if it is still OUR link — another dashboard may have started
        # since and taken ownership of the file.
        remove_url_file(only_if=authenticated_dashboard_url(host, port))


def _background_scan():
    """Ingest in the background, exactly as `cli.cmd_dashboard` does.

    **This entry point used to serve without ever scanning, and that was not a
    missing nicety — it was the one path to `/api/data` with nothing behind
    it.** The dashboard request deliberately does not act on the rebuild flag
    yielded by `database_admission`; what makes that safe is the payload's
    `unscanned` flag, read off the FILE rather than off any caller's flag
    value, which the page renders a banner from. `serve()` defaults `on_ready`
    to None, so this entry point served with no ingestion at all.

    Reproduced before this existed, against a seeded database on a schema this
    build does not write: `python dashboard.py`, then one authenticated
    `GET /api/data` — the stored turn was dropped, the response came back with
    `daily_by_model: []`, `sessions_all: []`, `all_models: []` and **no `error`
    field at all**, and nothing ever scanned. The page renders that as a
    complete, correct dashboard of an empty history. It is the same defect
    `cli.require_db` exits 1 for, wearing a nicer interface.

    Returning `{"error": ...}` instead would be worse rather than better: the
    page treats that as transient and appends "— retrying…", and a rebuild is
    not a condition that clears by retrying.

    Independent of any of that, this entry point never ingested anything at all,
    so its dashboard was frozen at whatever the last `cli.py scan` had left.
    """
    from . import scanner
    print("Scanning in the background...")
    try:
        # **`RESCAN_LOCK` is deliberately NOT held here, and that was argued
        # rather than overlooked.** `POST /api/rescan` takes it with
        # `blocking=False` and answers `409 A rescan is already running` when it
        # cannot; because this scan leaves it free, a Rescan pressed during
        # startup instead gets the lock and parks inside `scanner.scan` on
        # `scanner._SCAN_LOCK` until this one finishes. Taking it here would
        # turn that wait into an immediate, truthful 409 — and make the reader
        # worse off, which is why it is not taken.
        #
        # Measured 2026-08-16 by driving the page's own `triggerRescan` under
        # node with `apiFetch` stubbed: on a 409 it sets the button to "Rescan
        # failed: A rescan is already running" and calls `loadData` ZERO times;
        # on the 200 this path produces it calls it once. So on a cold corpus —
        # the only time the window is wide — the 409 leaves the reader looking
        # at the empty page they pressed Rescan to fill, with nothing scheduled
        # to replace it (auto-refresh is off by default), while the wait ends in
        # the freshly scanned data. `_SCAN_LOCK` is what makes the wait safe:
        # the two scans never overlap, and the body that comes back reports the
        # second, genuinely empty pass.
        #
        # The endpoint's own contract is intact meanwhile: two concurrent
        # `/api/rescan` calls still get the 409, since the first one does hold
        # the lock. What the wait costs is one of `MAX_HTTP_CONNECTIONS` slots,
        # for a response the read watchdog already exempts by name.
        #
        # `include_defaults=True` for the reason `do_POST` gives at length: a
        # scan that reaches `db.init_db` can empty the database and then refill
        # only the roots it was handed, so it must never cover fewer of them
        # than `cli.cmd_scan` does. Unobservable from `python dashboard.py`
        # today — that path takes no arguments, so PROJECTS_DIRS is None and
        # the resolved roots are the defaults either way — and written this way
        # so that stops depending on which entry point happens to set it.
        scanner.scan(db_path=DB_PATH,
                     projects_dirs=PROJECTS_DIRS,
                     include_defaults=True,
                     include_docker=True,
                     verbose=False)
    except Exception as exc:
        # A daemon thread that dies takes the only ingestion path with it and
        # the dashboard goes on serving stale data with no visible signal.
        #
        # `terminal_safe` escapes Cc, and a newline is Cc -- so an exception
        # carrying a multi-line remedy came out as one `\x0a`-run of a line.
        # `db.ForeignDatabaseError` is exactly that, and it is reachable from
        # here: point `CLAUDE_USAGE_DB` at somebody else's SQLite file and this
        # is the thread that finds out. Its text is already escaped field by
        # field by `db`, so it is printed as-is; everything else still goes
        # through `terminal_safe`, which is what an arbitrary exception string
        # needs. Same rule, same reason, as `port_in_use_lines`.
        from .db import ForeignDatabaseError
        invalidate_payload_cache(DB_PATH)
        detail = str(exc) if isinstance(exc, ForeignDatabaseError) \
            else terminal_safe(exc)
        print(f"Background scan failed: {detail}")
        return False
    print("Background scan complete.")
    return True


if __name__ == "__main__":
    # `python dashboard.py` is the second way in, and it reached bind() through
    # no argument handling at all — so it, too, has to answer a busy port with
    # the remedy rather than a stack trace.
    #
    # **It parses no arguments, and now says so instead of ignoring them.**
    # `--port 9000` was accepted and silently discarded: the server listened on
    # `PORT` or 8080 and exited 0, so a reader who asked for one port and got
    # another had nothing to read. That is the same silent-drop-at-exit-0 class
    # `cli.validate_flags` exists to remove -- `today --sourcex codex` printed a
    # complete, correctly formatted report of the wrong thing -- and this entry
    # point was promoted to a real one when it was given a background scan, so
    # it no longer gets to be the exception.
    #
    # Rejected rather than parsed, deliberately: `cli.py dashboard` already
    # parses these, and a second parser is a second thing to keep equal.
    #
    # **The word is "Refused", and that is not a style preference.** This line
    # said "Ignoring:" while the code below it exited 1 without binding
    # anything, so the message stated the opposite of what it did -- and the
    # ordinary English reading of "ignoring" is the behaviour the PREVIOUS
    # release actually had, which is what makes it credible. A reader told the
    # argument was ignored browses to the default port and finds nothing
    # listening there. That is the same silent-drop class this guard exists to
    # remove, inverted: right exit code, wrong sentence. "Nothing was started"
    # is said outright rather than left to be inferred from the exit code,
    # because a human reads the words and only a wrapper reads the code.
    if sys.argv[1:]:
        print("python dashboard.py takes no arguments; it reads HOST and PORT "
              "from the environment. Nothing was started.\n"
              f"  Refused: {' '.join(terminal_safe(a) for a in sys.argv[1:])}\n"
              f"  For arguments, use:  {invocation()} dashboard "
              "--host H --port N", file=sys.stderr)
        sys.exit(1)
    #
    # `on_ready`, not before the bind, for the reason `cmd_dashboard` gives: a
    # cold walk of ~/.claude/projects prints for as long as it takes, and
    # starting it first buried a busy-port complaint under that output and spent
    # a full scan on a process about to exit.
    try:
        serve(on_ready=lambda: start_scan_thread(
            _background_scan, threading.Thread))
    except PortInUseError as exc:
        print("\n".join(exc.lines), file=sys.stderr)
        sys.exit(1)
