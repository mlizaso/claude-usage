"""A standalone quota page and threshold API, without the rest of the tool.

    python -m claude_usage.limits_server  # this alone, on 127.0.0.1:8081
    claude-usage dashboard               # the full app alone, on 8080

Neither needs the other. The full dashboard reads quota through `account.py`
exactly as it always has. This server's much smaller built-in front end only
wants "tell me when I hit N% of a limit" and runs against a backend that never
opens the usage database, scans a transcript or grows a chart.

The page and health check carry no private data; JSON routes require the token:

    GET  /, /index.html              quota-only page shell
    GET  /healthz                    liveness, no data
    GET  /api/limits                 every quota window, with its key + label
    GET  /api/limits/thresholds      the stored per-window thresholds
    PUT  /api/limits/thresholds      replace them
    PATCH /api/limits/thresholds     merge selected windows

It deliberately does not import `dashboard`, which would pull in the scanner,
database and payload stack. Both servers instead import the small
`loopback_http` guard, so their Host/Origin security rules have one owner while
the quota service remains independent.

The startup URL carries its token in a fragment, which is never sent in the HTTP
request. The page reads it once, clears the address bar and then uses only the
header above. Its assets are assembled once at import and every response has a
fresh CSP nonce.
"""

import http.server
import json
import os
import re
import secrets
import socket
import sys
from urllib.parse import urlparse

from . import account
from . import limits_core
from . import live_limits
from . import limits_web
from .loopback_http import (
    ADDRESS_IN_USE_ERRNOS,
    LoopbackHTTPServer,
    LoopbackRequestHandlerMixin,
    LOOPBACK_HOSTS,
    apply_threshold_write,
    normalized_host,
    read_bounded_json_body,
    request_host_is_acceptable,
    request_origin_is_acceptable,
)
from .safetext import terminal_safe

# Standalone API contract version, deliberately independent of the product
# release in scanner.VERSION so this small service need not import the scanner.
VERSION = "1.0"
DEFAULT_PORT = 8081

# Same shape the dashboard requires, so a token generated for one is valid for
# the other and a user running both is not juggling two secrets.
LOCAL_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
API_TOKEN_HEADER = "X-Claude-Usage-Token"
TOKEN_ENV = "CLAUDE_USAGE_API_TOKEN"

_configured = os.environ.get(TOKEN_ENV, "")
API_TOKEN = _configured if LOCAL_TOKEN_RE.fullmatch(_configured) else secrets.token_urlsafe(32)

# A settings PUT is small. Anything larger is not a threshold map, and reading
# it would be the whole denial-of-service.
MAX_REQUEST_BYTES = 64 * 1024
MAX_HTTP_CONNECTIONS = 32
HTTP_SOCKET_TIMEOUT_SECONDS = 15
HTTP_REQUEST_READ_BUDGET_SECONDS = 10


def page_csp(nonce):
    """A closed policy for the standalone one-document client."""
    return "; ".join((
        "default-src 'none'",
        "base-uri 'none'",
        "object-src 'none'",
        "form-action 'none'",
        "frame-src 'none'",
        "frame-ancestors 'none'",
        "img-src 'none'",
        "font-src 'none'",
        "media-src 'none'",
        "worker-src 'none'",
        "connect-src 'self'",
        f"style-src 'nonce-{nonce}'",
        f"script-src 'nonce-{nonce}'",
        "script-src-attr 'none'",
    ))


def validate_bind_host(host):
    """Refuse to bind anywhere but loopback.

    This server answers with quota figures and accepts settings writes, both
    without any user account behind them, so it must never be reachable off the
    machine. Refused rather than silently rewritten to localhost: a caller who
    asked for 0.0.0.0 wanted something this cannot safely give, and quietly
    doing something else is how a service ends up exposed while its operator
    believes otherwise.
    """
    candidate = normalized_host(host)
    if candidate not in LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing to bind a quota API to a non-loopback host: "
            f"{terminal_safe(str(host))}")
    # Resolve no name at bind time: a poisoned localhost mapping must not turn
    # an allowed spelling into a network-visible listener.
    return "127.0.0.1" if candidate == "localhost" else candidate


def request_is_local(headers, port):
    """Whether the Host header names this loopback service.

    Rejecting a foreign Host is what stops DNS rebinding: a hostile page can
    make a browser resolve its own name to 127.0.0.1, but it cannot change the
    Host header the browser then sends.
    """
    return request_host_is_acceptable(headers, port)


def origin_is_acceptable(headers):
    """Whether the Origin header permits this request.

    An ABSENT Origin is allowed and that is deliberate, not an oversight: a
    browser sends one on cross-origin requests, while `curl`, a native app and
    the small front end this server exists for do not send one at all. Refusing
    the absent case would break every non-browser caller while stopping nothing
    -- a hostile page cannot suppress its own Origin.
    """
    return request_origin_is_acceptable(headers)


def token_is_valid(headers):
    """Constant-time comparison of the bearer token.

    `compare_digest` refuses a non-ASCII `str`, so the header is encoded first
    -- a header carrying one would otherwise raise inside the guard rather than
    being rejected by it.
    """
    supplied_values = headers.get_all(API_TOKEN_HEADER, [])
    if len(supplied_values) != 1:
        return False
    supplied = supplied_values[0]
    if not supplied or not supplied.isascii():
        return False
    try:
        return bool(supplied) and secrets.compare_digest(
            supplied.encode("utf-8"), API_TOKEN.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def limits_payload(now=None):
    """Every quota window this machine reports, keyed and labelled.

    Sourced from `account.py`, which is the only reader of `~/.claude.json` and
    drops every identifying field before anything can leave it. Windows are
    described by `limits_core`, so this server and the full dashboard name the
    same window the same way and a threshold set in one governs the other.

    Include orphaned thresholds because a stale cache can omit a window whose
    alert is still configured."""
    source = "cache"
    try:
        config = account.read_config()
        projection = account.current_limits_from_config(
            config, env=os.environ, now=now)
        if (account.detect_auth_mode(config, os.environ) == "subscription"
                and live_limits.enabled()):
            config, source = live_limits.config_with_live_limits(config)
            projection = account.current_limits_from_config(
                config, env=os.environ, now=now)
    except Exception:
        # `account` promises never to raise; this is the belt for that braces.
        # A quota API that 500s because a config file is odd is worse than one
        # that says it has nothing to report.
        projection = {"available": False, "windows": []}
    stored = limits_core.read_thresholds()
    windows = limits_core.describe_windows(
        projection.get("windows") or [], "claude", stored)
    live = limits_core.live_threshold_keys(
        projection.get("windows") or [], "claude")
    return {
        "available": bool(projection.get("available")),
        "reason": projection.get("reason"),
        "plan_type": projection.get("plan_type"),
        "fetched_at_ms": projection.get("fetched_at_ms"),
        "age_seconds": projection.get("age_seconds"),
        "windows": windows,
        "orphaned": limits_core.orphaned_thresholds(
            stored, live, source="claude"),
        "source": "claude",
        # Which reading this is. A client that shows an age needs to know
        # whether that age is a cache's or a request's -- they mean opposite
        # things about whether the LIST of windows can be trusted.
        "reading": source,
        "version": VERSION,
    }


def _safe_limits_value(value):
    """Escape display strings while retaining opaque mapping identities."""
    if isinstance(value, str):
        return terminal_safe(value)
    if isinstance(value, list):
        return [_safe_limits_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _safe_limits_value(item) for key, item in value.items()}
    return value


def safe_limits_payload(payload):
    """Sanitize quota display values without rewriting threshold identities.

    Window ``key`` values and ``orphaned`` mapping keys are opaque identities
    sent back by clients in threshold writes. They must remain byte-for-byte
    stable even when a display field contains terminal or bidi controls.
    """
    if not isinstance(payload, dict):
        return payload
    safe = {}
    for field, value in payload.items():
        if field == "windows" and isinstance(value, list):
            windows = []
            for window in value:
                if not isinstance(window, dict):
                    windows.append(_safe_limits_value(window))
                    continue
                windows.append({
                    name: item if name == "key" else _safe_limits_value(item)
                    for name, item in window.items()
                })
            safe[field] = windows
        elif field == "orphaned" and isinstance(value, dict):
            safe[field] = {
                identity: _safe_limits_value(item)
                for identity, item in value.items()
            }
        else:
            safe[field] = _safe_limits_value(value)
    return safe


class LimitsHandler(LoopbackRequestHandlerMixin, http.server.BaseHTTPRequestHandler):
    server_version = "claude-usage-limits"
    sys_version = ""

    def log_message(self, *args):
        """Silent by default. The dashboard's own server does the same: a
        request log on a localhost service carries no diagnostic value and
        prints paths to whatever stream the caller happened to leave open."""

    def _send(self, code, payload):
        # Error paths that reject a body before reading it are finished with
        # request input too; do not let the watchdog cut their response.
        self._finish_request_read()
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Quota is per-machine state; no proxy or browser should keep it.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_page(self):
        """Send a token-free shell with one request-specific CSP nonce."""
        self._finish_request_read()
        nonce = secrets.token_urlsafe(18)
        body = limits_web.render_page(nonce)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", page_csp(nonce))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy",
            "camera=(), geolocation=(), microphone=(), payment=(), usb=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _guarded(self):
        """True when the request may be answered. Order matters only in that
        every check must run before any data is read."""
        if not origin_is_acceptable(self.headers):
            self._send(403, {"error": "forbidden"})
            return False
        if not token_is_valid(self.headers):
            self._send(403, {"error": "forbidden"})
            return False
        return True

    def _authorize_host(self):
        if request_is_local(self.headers, self.server.server_address[1]):
            return True
        self._send(421, {"error": "untrusted host"})
        return False

    def _parsed_request_target(self):
        try:
            return urlparse(self.path)
        except ValueError:
            return None

    def do_GET(self):
        if not self._authorize_host():
            return
        target = self._parsed_request_target()
        if target is None:
            self._send(400, {"error": "bad request target"})
            return
        path = target.path
        if path == "/healthz":
            # Deliberately unauthenticated and carrying no data: it exists so a
            # launcher can tell "up" from "not up" without holding a token.
            self._send(200, {"status": "ok", "version": VERSION})
            return
        if path in ("/", "/index.html"):
            # The shell contains neither quota nor token, so it is safe before
            # API authentication. Host validation still prevents rebinding.
            self._send_page()
            return
        if not self._guarded():
            return
        if path == "/api/limits":
            self._send(200, safe_limits_payload(limits_payload()))
        elif path == "/api/limits/thresholds":
            self._send(200, {"thresholds": limits_core.read_thresholds()})
        else:
            self._send(404, {"error": "not found"})

    def _threshold_write_body(self):
        """Read one bounded JSON body while the absolute watchdog stays armed."""
        return read_bounded_json_body(
            self,
            MAX_REQUEST_BYTES,
            lambda status, message: self._send(status, {"error": message}),
            {
                "transfer_encoding": "transfer encoding is not supported",
                "content_length": "content length required",
                "bad_request": "bad length",
                "empty_body": "empty body",
                "too_large": "too large",
                "incomplete_body": "incomplete body",
                "bad_json": "bad json",
            },
        )

    def _prepare_threshold_write(self):
        if not self._authorize_host():
            return False
        target = self._parsed_request_target()
        if target is None:
            self._send(400, {"error": "bad request target"})
            return False
        path = target.path
        if not self._guarded():
            return False
        if path != "/api/limits/thresholds":
            self._send(404, {"error": "not found"})
            return False
        return True

    def do_PUT(self):
        if not self._prepare_threshold_write():
            return
        apply_threshold_write(
            self,
            patch=False,
            normalize=limits_core.normalize_thresholds,
            discarded=limits_core.patch_discarded_anything,
            replace=limits_core.write_thresholds,
            update=limits_core.update_thresholds,
            send=self._send,
            messages={
                "put_object": "thresholds must be an object",
                "patch_object": "threshold updates must be an object",
                "invalid": "invalid threshold update",
                "save": "could not save thresholds",
            },
        )

    def do_PATCH(self):
        if not self._prepare_threshold_write():
            return
        apply_threshold_write(
            self,
            patch=True,
            normalize=limits_core.normalize_thresholds,
            discarded=limits_core.patch_discarded_anything,
            replace=limits_core.write_thresholds,
            update=limits_core.update_thresholds,
            send=self._send,
            messages={
                "put_object": "thresholds must be an object",
                "patch_object": "threshold updates must be an object",
                "invalid": "invalid threshold update",
                "save": "could not save thresholds",
            },
        )

    def do_POST(self):
        if not self._authorize_host():
            return
        target = self._parsed_request_target()
        if target is None:
            self._send(400, {"error": "bad request target"})
            return
        self._send(404, {"error": "not found"})


class LimitsHTTPServer(LoopbackHTTPServer):
    """Threaded loopback server with bounded and time-bounded request reads."""

    def __init__(self, server_address, handler_class):
        super().__init__(
            server_address,
            handler_class,
            max_connections=MAX_HTTP_CONNECTIONS,
            socket_timeout=lambda: HTTP_SOCKET_TIMEOUT_SECONDS,
            request_read_budget=lambda: HTTP_REQUEST_READ_BUDGET_SECONDS,
            overload_body=(
                b'{"error": "too many concurrent connections; retry shortly}'
            ),
        )


class LimitsHTTPServerV6(LimitsHTTPServer):
    address_family = socket.AF_INET6


def serve(host="127.0.0.1", port=DEFAULT_PORT, on_ready=None):
    """Bind and serve. `on_ready` runs AFTER the bind, never before.

    The ordering is the same lesson `dashboard.serve` records: work started
    before the bind is spent on a process that may be about to exit, and its
    output lands on top of the failure that explains why.
    """
    host = validate_bind_host(host)
    server_class = LimitsHTTPServerV6 if host == "::1" else LimitsHTTPServer
    server = server_class((host, int(port)), LimitsHandler)
    bound = server.server_address[1]
    try:
        if on_ready:
            on_ready(host, bound, API_TOKEN)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return server


def _parse_cli_port(value, name):
    """Parse one command-line/environment port before any bind is attempted."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number: {terminal_safe(str(value))}")
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535: {port}")
    return port


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    host = "127.0.0.1"
    port_value = os.environ.get("LIMITS_PORT", str(DEFAULT_PORT))
    port_name = "LIMITS_PORT"
    while argv:
        flag = argv.pop(0)
        if flag == "--host" and argv:
            host = argv.pop(0)
        elif flag == "--port" and argv:
            port_value = argv.pop(0)
            port_name = "--port"
        else:
            print(f"unknown argument: {terminal_safe(flag)}", file=sys.stderr)
            return 1

    try:
        port = _parse_cli_port(port_value, port_name)
    except ValueError as exc:
        print(terminal_safe(str(exc)), file=sys.stderr)
        return 1

    def announce(bound_host, bound_port, token):
        # The token goes to STDOUT because it is the answer; everything else is
        # stderr. A caller can take the first line and use it.
        authority = f"[{bound_host}]" if ":" in bound_host else bound_host
        print(f"http://{authority}:{bound_port}/#token={token}")
        print("Quota page + threshold API. Ctrl-C to stop.", file=sys.stderr)

    try:
        serve(host=host, port=port, on_ready=announce)
    except ValueError as exc:
        print(terminal_safe(str(exc)), file=sys.stderr)
        return 1
    except OSError as exc:
        if getattr(exc, "errno", None) in ADDRESS_IN_USE_ERRNOS:
            print(f"Port {port} is already in use.", file=sys.stderr)
            return 1
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
