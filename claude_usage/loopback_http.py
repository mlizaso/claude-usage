"""Shared loopback HTTP security mechanics for both local web servers.

This module deliberately imports neither server nor the database/scanner
branch. The full dashboard and the standalone quota service stay independently
runnable while sharing authority checks, request watchdogs, bounded body
parsing, threshold-write validation and connection-slot mechanics that must not
drift between them.
"""

import errno
import http.server
import json
import math
import re
import socket
import sys
import threading
import time
import urllib.request
from urllib.parse import urlparse


LOOPBACK_HOSTS = frozenset(("localhost", "127.0.0.1", "::1"))
# Windows may expose WSAEADDRINUSE directly while POSIX exposes EADDRINUSE.
# Keep this capability-probed set beside the shared loopback policy so both
# independent servers classify the same bind failure without importing either
# server or duplicating platform assumptions.
ADDRESS_IN_USE_ERRNOS = frozenset(
    code
    for code in (getattr(errno, "EADDRINUSE", None),
                 getattr(errno, "WSAEADDRINUSE", None))
    if code is not None
)

# These defaults are only fallbacks for a handler/server used outside the two
# product front ends.  The concrete servers pass their own (patchable) values
# into ``LoopbackHTTPServer`` below, so tests and operators retain the existing
# module-level knobs without copying the mechanics.
DEFAULT_REQUEST_READ_BUDGET_SECONDS = 10
DEFAULT_SOCKET_TIMEOUT_SECONDS = 15
DEFAULT_MAX_HTTP_CONNECTIONS = 32
DEFAULT_OVERLOAD_BODY = (
    b'{"error": "too many concurrent connections; retry shortly"}'
)


class _RefuseProbeRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_loopback_probe(request, timeout):
    """Contact the selected local peer without redirects or environment proxies."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _RefuseProbeRedirects(),
    )
    return opener.open(request, timeout=timeout)


def call_with_total_deadline(operation, timeout):
    """Return ``(completed, value)`` without waiting past a wall-clock budget.

    Socket timeouts bound inactivity, so an unidentified loopback peer can keep
    renewing them by dripping headers or body bytes. These probes are one-shot
    diagnostics, and they send no secret; running the blocking stdlib call in a
    daemon worker lets the caller preserve its total deadline without depending
    on private socket attributes inside ``urllib``.

    An operation exception is a completed inconclusive result (``None``). A
    timed-out worker is daemon-owned and cannot keep the process alive.
    """
    try:
        budget = float(timeout)
    except (TypeError, ValueError, OverflowError):
        return False, None
    if not math.isfinite(budget) or budget <= 0:
        return False, None

    done = threading.Event()
    result = [None]

    def run():
        try:
            result[0] = operation()
        except Exception:
            result[0] = None
        finally:
            done.set()

    threading.Thread(
        target=run,
        name="claude-usage-loopback-probe",
        daemon=True,
    ).start()
    if not done.wait(budget):
        return False, None
    return True, result[0]


class LoopbackRequestHandlerMixin:
    """Shared request-lifecycle protection for the local HTTP handlers.

    ``BaseHTTPRequestHandler.setup`` runs once per accepted connection.  Both
    services intentionally use HTTP/1.0, so one watchdog per connection is
    enough today; keeping this policy here makes a future protocol change an
    explicit review point instead of a second copy silently drifting.  A
    ``PUT`` or ``PATCH`` keeps the watchdog armed after headers are parsed until
    its declared body has been consumed.

    The mixin does not know anything about either service's response format.
    Concrete handlers retain their ``_send``/``_send_json`` methods and call the
    shared body reader below with their own error-message casing.
    """

    def setup(self):
        super().setup()
        budget = getattr(
            self.server, "request_read_budget",
            DEFAULT_REQUEST_READ_BUDGET_SECONDS,
        )
        if callable(budget):
            budget = budget()
        self._request_read_finished = False
        self._watchdog_lock = threading.Lock()
        self._read_watchdog = threading.Timer(
            budget, self._abort_unread_request)
        self._read_watchdog.daemon = True
        self._read_watchdog.start()

    def _abort_unread_request(self):
        lock = getattr(self, "_watchdog_lock", None)
        if lock is None:
            return
        with lock:
            if getattr(self, "_request_read_finished", True):
                return
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _finish_request_read(self):
        """Stop the absolute read budget once no more input is needed."""
        lock = getattr(self, "_watchdog_lock", None)
        watchdog = getattr(self, "_read_watchdog", None)
        if lock is None or watchdog is None:
            return
        with lock:
            if self._request_read_finished:
                return
            self._request_read_finished = True
            watchdog.cancel()

    def parse_request(self):
        parsed = super().parse_request()
        # A threshold write still has a declared body to consume. Cancelling
        # here would let a peer drip that body forever after fast headers.
        if not parsed or self.command not in {"PUT", "PATCH"}:
            self._finish_request_read()
        return parsed

    def finish(self):
        self._finish_request_read()
        super().finish()


def read_bounded_json_body(handler, max_bytes, send_error, messages):
    """Read one declared, bounded JSON body under the handler watchdog.

    ``send_error(status, message)`` is the tiny response hook supplied by each
    handler.  The parsing, duplicate-header checks, length limit and completion
    transition are one implementation for both front ends; only their historic
    error-message casing remains at the call site.
    """
    def reject(status, name):
        send_error(status, messages[name])
        return False, None

    if handler.headers.get_all("Transfer-Encoding", []):
        return reject(501, "transfer_encoding")
    lengths = handler.headers.get_all("Content-Length", [])
    if not lengths:
        return reject(411, "content_length")
    if len(lengths) != 1:
        return reject(400, "bad_request")
    try:
        length = int(lengths[0])
    except (TypeError, ValueError, OverflowError):
        return reject(400, "bad_request")
    if length <= 0:
        return reject(400, "empty_body")
    if length > max_bytes:
        return reject(413, "too_large")
    try:
        try:
            body = handler.rfile.read(length)
        finally:
            handler._finish_request_read()
        if len(body) != length:
            return reject(400, "incomplete_body")
        return True, json.loads(body)
    except (OSError, TypeError, ValueError, UnicodeError,
            RecursionError, MemoryError):
        return reject(400, "bad_json")


def apply_threshold_write(handler, *, patch, normalize, discarded,
                          replace, update, send, messages):
    """Apply one validated threshold PUT/PATCH with a response hook.

    Both services intentionally retain their own route/auth preparation and
    response serializer.  Once a request has passed those gates, however, the
    unwrap/shape/normalization/write sequence is one contract; keeping it here
    prevents a future fix to strict PATCH validation from landing on only one
    front end.
    """
    ok, raw = handler._threshold_write_body()
    if not ok:
        return
    if isinstance(raw, dict) and "thresholds" in raw:
        raw = raw["thresholds"]
    if patch:
        if not isinstance(raw, dict) or not raw:
            send(400, {"error": messages["patch_object"]})
            return
        cleaned = normalize(raw)
        if discarded(raw, cleaned):
            send(400, {"error": messages["invalid"]})
            return
        stored = update(cleaned)
    else:
        if not isinstance(raw, dict):
            send(400, {"error": messages["put_object"]})
            return
        stored = replace(raw)
    if stored is None:
        send(500, {"error": messages["save"]})
        return
    send(200, {"thresholds": stored})


class LoopbackHTTPServer(http.server.ThreadingHTTPServer):
    """Threaded server with shared connection, timeout and error mechanics."""

    daemon_threads = True

    def __init__(self, server_address, handler_class, *,
                 max_connections=DEFAULT_MAX_HTTP_CONNECTIONS,
                 socket_timeout=DEFAULT_SOCKET_TIMEOUT_SECONDS,
                 request_read_budget=DEFAULT_REQUEST_READ_BUDGET_SECONDS,
                 overload_body=DEFAULT_OVERLOAD_BODY):
        self._request_slots = threading.BoundedSemaphore(max_connections)
        self._socket_timeout_setting = socket_timeout
        self.request_read_budget = request_read_budget
        self._overload_body = bytes(overload_body)
        super().__init__(server_address, handler_class)

    def get_request(self):
        request, client_address = super().get_request()
        timeout = self._socket_timeout_setting
        if callable(timeout):
            timeout = timeout()
        request.settimeout(timeout)
        return request, client_address

    def handle_error(self, request, client_address):
        # A watchdog shutdown, a client navigating away, and a per-operation
        # socket timeout are expected connection endings, not application bugs.
        if isinstance(sys.exc_info()[1],
                      (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            body = self._overload_body
            response = (
                b"HTTP/1.0 503 Service Unavailable\r\n"
                b"Content-Type: application/json\r\n"
                b"Retry-After: 1\r\n"
                b"Connection: close\r\n"
                b"Content-Length: " + str(len(body)).encode("ascii")
                + b"\r\n\r\n" + body
            )
            # Send the refusal before reading, then briefly drain incoming
            # bytes after the write-side shutdown. Closing with unread bytes
            # can reset the connection and discard the 503 on Windows. Both
            # time and bytes are bounded: overload must not stall the accept
            # loop behind a silent or drip-feeding peer.
            deadline = time.monotonic() + 0.1
            try:
                request.settimeout(0.1)
                request.sendall(response)
                request.shutdown(socket.SHUT_WR)
                remaining = 65536
                while remaining:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    request.settimeout(left)
                    chunk = request.recv(min(remaining, 8192))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


LOOPBACK_AUTHORITY_RE = re.compile(
    r"(?:localhost\.?|127\.0\.0\.1|\[::1\])(?::([0-9]{1,5}))?\Z",
    re.IGNORECASE,
)


def normalized_host(host):
    """A hostname with brackets, trailing dot and case removed."""
    return (host or "").strip().strip("[]").rstrip(".").lower()


def request_host_is_acceptable(headers, port):
    """Whether exactly one Host names loopback and this service's port.

    A missing port remains accepted for HTTP/1.0 and native compatibility.
    When a port is supplied it must be the actual bound port; merely being a
    syntactically valid loopback port is not enough.
    """
    values = headers.get_all("Host", [])
    if len(values) != 1:
        return False
    host = values[0]
    if (not host or not host.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in host)):
        return False
    match = LOOPBACK_AUTHORITY_RE.fullmatch(host)
    if match is None:
        return False
    named_port = match.group(1)
    return named_port is None or int(named_port) == int(port)


def request_origin_is_acceptable(headers):
    """Whether an absent Origin or one exactly matching Host is acceptable."""
    origins = headers.get_all("Origin", [])
    if not origins:
        # Native clients do not send Origin; hostile browser pages cannot omit
        # their own, so absence is intentionally allowed.
        return True
    if len(origins) != 1:
        return False
    origin = origins[0]
    if (not origin.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in origin)):
        return False
    try:
        parsed = urlparse(origin)
        parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
        and parsed.netloc.lower() == headers.get("Host", "").lower()
    )
