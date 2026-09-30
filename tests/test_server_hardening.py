"""Hardening regressions for the HTTP surface and the Docker loopback proxy.

Three separate ways a local peer (or just a slow scan) could take the server
out of the conversation without it ever answering:

* a byte >0x7F in a credential header raised out of the pre-auth check, so the
  socket was closed with no response at all — the exact failure
  `tests/test_resource_lifecycle.py` names, on the one path those tests stub out;
* `/api/limits` opened the database with a bare `sqlite3.connect`, skipping the
  symlink/hard-link/regular-file guard every other route goes through — and
  creating the file when it did not exist;
* the proxy read 30 seconds of upstream silence as an idle connection, which is
  what a request whose response is still being computed looks like.

Two more that are about what the server refuses rather than what it drops:

* the Host, Origin and credential headers are each read with `get_all` and
  refused when they arrive more than once, and nothing pinned those counts —
  all three could be rewritten as a truthiness check with the suite green;
* the web/vendor resolver probed exactly two directories, so an install whose
  data prefix is not `sys.prefix` found none of its assets. Widening that
  search is precisely the change that must not also widen what the server will
  execute, so the digest pin on the chart runtime is asserted against the new
  candidates too.

And one about what the server *ships* rather than what it refuses or drops:

* `/api/limits` sent the plan strings raw, while `/api/data` embedded the same
  `account.current_limits` strings through `safe_dashboard_value`. Assembling
  its own payload inside `do_GET` is NOT what made it unusual — `/healthz` and
  `/api/sources` build a dict literal right there in `do_GET` too. What was
  unique is that its strings had been wrapped by NOBODY. The escape now happens
  once in `_send_json`, so no route can ship a raw terminal or bidi control by
  forgetting the wrap, and which call sites hand that method something other
  than a literal is pinned below rather than counted in a comment.
"""

import ast
import contextlib
import hashlib
import hmac
import http.client
import inspect
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import dashboard
import dashboard_data
import db
import limits_core
import proxy
import scanner
from dashboard import (
    API_TOKEN,
    API_TOKEN_HEADER,
    DashboardHTTPServer,
    DashboardHandler,
)
from safetext import terminal_safe
from scanner import get_db, init_db, insert_turns


# Every route that goes through _api_request_is_authorized.
AUTHENTICATED_ROUTES = (
    ("GET", "/api/data"),
    ("GET", "/api/sources"),
    ("GET", "/api/scan-status"),
    ("GET", "/api/limits"),
    ("PUT", "/api/limits/thresholds"),
    ("PATCH", "/api/limits/thresholds"),
    ("POST", "/api/rescan"),
)


def raw_request(port, method, path, extra_headers=b"", timeout=5):
    """Send bytes verbatim and return the whole reply (b'' if none arrives).

    urllib cannot express "a header with a 0x80-0xFF byte in it" without going
    through the same latin-1 encode http.server decodes with, and a dropped
    connection has to be observable as *zero bytes* rather than an exception
    class, so this speaks the protocol directly.
    """
    raw = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n".encode("ascii")
           + extra_headers
           + b"Content-Length: 0\r\nConnection: close\r\n\r\n")
    conn = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    chunks = []
    try:
        conn.sendall(raw)
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError:
        pass
    finally:
        conn.close()
    return b"".join(chunks)


def status_line(response):
    return response.split(b"\r\n", 1)[0]


class _LocalServer(unittest.TestCase):
    """A dashboard on a throwaway port, pointed at a throwaway database."""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmpdir.name)
        cls.projects = cls.tmp / "projects"
        cls.projects.mkdir()
        cls.db_path = cls.tmp / "usage.db"
        # Belt and braces: an auth regression must not be able to scan or
        # overwrite the developer's real transcripts and usage database.
        cls._patchers = [
            mock.patch.object(dashboard, "DB_PATH", cls.db_path),
            mock.patch.object(scanner, "DB_PATH", cls.db_path),
            mock.patch.object(scanner, "DEFAULT_PROJECTS_DIRS", [cls.projects]),
        ]
        for patcher in cls._patchers:
            patcher.start()
        cls.prepare()
        cls.server = DashboardHTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def prepare(cls):
        """Hook for subclasses that need a config or a seeded database."""

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.cleanup()
        for patcher in reversed(cls._patchers):
            patcher.stop()
        cls._tmpdir.cleanup()

    @classmethod
    def cleanup(cls):
        """Hook mirroring prepare()."""


class TestNonAsciiCredentialHeadersAreRefused(_LocalServer):
    """A header byte >0x7F must produce a refusal, not a reset connection.

    http.server decodes headers as latin-1, so one such byte reaches
    `secrets.compare_digest` as a non-ASCII `str` — which that function refuses
    to compare, raising TypeError. The exception escaped into
    socketserver.handle_error: no response, a closed socket, and a full
    traceback on the operator's terminal for every repeat.

    `_host_is_allowed` and `_origin_is_allowed` already guard with `isascii()`;
    the two token headers did not.
    """

    def test_a_non_ascii_token_is_refused_on_every_authenticated_route(self):
        for method, path in AUTHENTICATED_ROUTES:
            with self.subTest(route=path):
                response = raw_request(
                    self.port, method, path,
                    API_TOKEN_HEADER.encode() + b": \xff\r\n")
                self.assertTrue(response, f"{path} dropped the connection")
                self.assertIn(b" 403 ", status_line(response))
                self.assertIn(b'"Forbidden"', response)

    def test_a_non_ascii_token_beside_a_valid_one_is_still_refused(self):
        """One header, whose value is the real token with a 0xFF byte stuck on
        the end: this pins that the refusal happens before the comparison
        rather than after it. (Two *headers* are refused by the count check,
        which is pinned separately — by the class below. This docstring used to
        claim that coverage existed when it did not.)"""
        response = raw_request(
            self.port, "GET", "/api/data",
            API_TOKEN_HEADER.encode() + b": " + API_TOKEN.encode() + b"\xff\r\n")
        self.assertTrue(response, "/api/data dropped the connection")
        self.assertIn(b" 403 ", status_line(response))

    def test_a_non_ascii_health_challenge_is_refused_on_healthz(self):
        """A malformed challenge is answered rather than dropping the socket."""
        with mock.patch.object(dashboard, "HEALTH_PROOF_SECRET", "z" * 43):
            response = raw_request(
                self.port, "GET", "/healthz?challenge=%FF")
        self.assertTrue(response, "/healthz dropped the connection")
        self.assertIn(b" 404 ", status_line(response))

    def test_an_ascii_token_still_authenticates(self):
        """The guard must reject the byte, not the header."""
        response = raw_request(
            self.port, "GET", "/api/limits",
            API_TOKEN_HEADER.encode() + b": " + API_TOKEN.encode() + b"\r\n")
        self.assertIn(b" 200 ", status_line(response))


class TestHealthSecretProducesNonceScopedProofs(_LocalServer):
    """The readiness secret must never cross the HTTP boundary itself."""

    SECRET = "health-secret-abcdefghijklmnopqrstuvwxyz"
    FIRST = "challenge-abcdefghijklmnopqrstuvwxyz"
    SECOND = "challenge-ZYXWVUTSRQPONMLKJIHGFEDCBA"

    def healthz(self, challenge):
        return raw_request(
            self.port, "GET", f"/healthz?challenge={challenge}")

    @staticmethod
    def expected(secret, challenge):
        return hmac.new(
            secret.encode("ascii"),
            b"claude-usage-health\0" + challenge.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    def test_each_challenge_gets_its_own_proof_and_never_the_secret(self):
        with mock.patch.object(dashboard, "HEALTH_PROOF_SECRET", self.SECRET):
            first = self.healthz(self.FIRST)
            second = self.healthz(self.SECOND)

        self.assertIn(b" 200 ", status_line(first))
        self.assertIn(b" 200 ", status_line(second))
        self.assertNotIn(self.SECRET.encode(), first + second)
        first_proof = json.loads(first.split(b"\r\n\r\n", 1)[1])["instance"]
        second_proof = json.loads(second.split(b"\r\n\r\n", 1)[1])["instance"]
        self.assertEqual(first_proof, self.expected(self.SECRET, self.FIRST))
        self.assertEqual(second_proof, self.expected(self.SECRET, self.SECOND))
        self.assertNotEqual(first_proof, second_proof)


class TestExtensionRescanProofsAreOneShot(_LocalServer):
    """The extension must not put its reusable browser bearer on the wire."""

    SECRET = "health-secret-rescan-abcdefghijklmnopqrstuvwxyz"
    CHALLENGE_PREFIX = "rescan-challenge-abcdefghijklmnopqrstuvwxyz"

    def setUp(self):
        super().setUp()
        self.issued_at = time.time()
        self.challenge = f"{self.CHALLENGE_PREFIX}_{int(self.issued_at * 1000)}"
        with dashboard.RESCAN_PROOF_LOCK:
            dashboard.RESCAN_PROOF_USED.clear()

    def tearDown(self):
        with dashboard.RESCAN_PROOF_LOCK:
            dashboard.RESCAN_PROOF_USED.clear()
        super().tearDown()

    def expected(self, challenge=None):
        challenge = self.challenge if challenge is None else challenge
        return hmac.new(
            self.SECRET.encode("ascii"),
            b"claude-usage-rescan\0POST /api/rescan\0"
            + challenge.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    def proof_headers(self):
        return (
            dashboard.RESCAN_CHALLENGE_HEADER.encode("ascii") + b": "
            + self.challenge.encode("ascii") + b"\r\n"
            + dashboard.RESCAN_PROOF_HEADER.encode("ascii") + b": "
            + self.expected().encode("ascii") + b"\r\n"
        )

    def test_a_valid_extension_proof_authorizes_once_without_api_bearer(self):
        with mock.patch.object(dashboard, "HEALTH_PROOF_SECRET", self.SECRET), \
                mock.patch.object(scanner, "scan", return_value={"new": 0}):
            first = raw_request(self.port, "POST", "/api/rescan",
                                self.proof_headers())
            replay = raw_request(self.port, "POST", "/api/rescan",
                                 self.proof_headers())
        self.assertIn(b" 200 ", status_line(first))
        self.assertIn(b" 403 ", status_line(replay))
        self.assertNotIn(API_TOKEN.encode("ascii"), first)

    def test_a_wrong_domain_proof_is_not_a_rescan_authority(self):
        wrong = hmac.new(
            self.SECRET.encode("ascii"),
            b"claude-usage-health\0" + self.challenge.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        headers = (
            dashboard.RESCAN_CHALLENGE_HEADER.encode("ascii") + b": "
            + self.challenge.encode("ascii") + b"\r\n"
            + dashboard.RESCAN_PROOF_HEADER.encode("ascii") + b": "
            + wrong.encode("ascii") + b"\r\n"
        )
        with mock.patch.object(dashboard, "HEALTH_PROOF_SECRET", self.SECRET):
            response = raw_request(self.port, "POST", "/api/rescan", headers)
        self.assertIn(b" 403 ", status_line(response))

    def test_a_consumed_proof_stays_invalid_past_its_wall_lifetime(self):
        class Headers:
            def get_all(inner, name, default):
                if name == dashboard.RESCAN_CHALLENGE_HEADER:
                    return [self.challenge]
                if name == dashboard.RESCAN_PROOF_HEADER:
                    return [self.expected()]
                return default

        handler = type("Handler", (), {"headers": Headers()})()
        with mock.patch.object(dashboard, "HEALTH_PROOF_SECRET", self.SECRET), \
                mock.patch.object(dashboard.time, "time", side_effect=(
                    self.issued_at, self.issued_at + 1, self.issued_at + 61)):
            self.assertTrue(dashboard._rescan_proof_is_authorized(handler))
            self.assertFalse(dashboard._rescan_proof_is_authorized(handler))
            self.assertFalse(dashboard._rescan_proof_is_authorized(handler))

    def test_a_consumed_proof_survives_arbitrary_wall_clock_rollback(self):
        class Headers:
            def get_all(inner, name, default):
                if name == dashboard.RESCAN_CHALLENGE_HEADER:
                    return [self.challenge]
                if name == dashboard.RESCAN_PROOF_HEADER:
                    return [self.expected()]
                return default

        handler = type("Handler", (), {"headers": Headers()})()
        replay_age = (
            dashboard.RESCAN_PROOF_MAX_AGE_SECONDS
            - dashboard.RESCAN_PROOF_CLOCK_SKEW_SECONDS
        )
        with mock.patch.object(dashboard, "HEALTH_PROOF_SECRET", self.SECRET), \
                mock.patch.object(dashboard.time, "monotonic", side_effect=(
                    0, 24 * 60 * 60)), \
                mock.patch.object(dashboard.time, "time", side_effect=(
                    self.issued_at, self.issued_at + replay_age)):
            self.assertTrue(dashboard._rescan_proof_is_authorized(handler))
            self.assertFalse(dashboard._rescan_proof_is_authorized(handler))

    def test_a_full_replay_set_fails_closed_instead_of_evicting_a_live_entry(self):
        class Headers:
            def get_all(inner, name, default):
                if name == dashboard.RESCAN_CHALLENGE_HEADER:
                    return [self.challenge]
                if name == dashboard.RESCAN_PROOF_HEADER:
                    return [self.expected()]
                return default

        handler = type("Handler", (), {"headers": Headers()})()
        with dashboard.RESCAN_PROOF_LOCK:
            dashboard.RESCAN_PROOF_USED.add("already-used")
        with mock.patch.object(dashboard, "HEALTH_PROOF_SECRET", self.SECRET), \
                mock.patch.object(dashboard, "RESCAN_PROOF_MAX_ENTRIES", 1), \
                mock.patch.object(dashboard.time, "time", return_value=self.issued_at):
            self.assertFalse(dashboard._rescan_proof_is_authorized(handler))
        self.assertIn("already-used", dashboard.RESCAN_PROOF_USED)

    def test_future_clock_skew_is_consumed_for_its_full_wall_lifetime(self):
        future = self.issued_at + dashboard.RESCAN_PROOF_CLOCK_SKEW_SECONDS
        challenge = f"{self.CHALLENGE_PREFIX}_{int(future * 1000)}"
        proof = self.expected(challenge)

        class Headers:
            def get_all(inner, name, default):
                if name == dashboard.RESCAN_CHALLENGE_HEADER:
                    return [challenge]
                if name == dashboard.RESCAN_PROOF_HEADER:
                    return [proof]
                return default

        handler = type("Handler", (), {"headers": Headers()})()
        with mock.patch.object(dashboard, "HEALTH_PROOF_SECRET", self.SECRET), \
                mock.patch.object(dashboard.time, "time", side_effect=(
                    self.issued_at, self.issued_at + 61,
                    self.issued_at + 66)):
            self.assertTrue(dashboard._rescan_proof_is_authorized(handler))
            self.assertFalse(dashboard._rescan_proof_is_authorized(handler))
            self.assertFalse(dashboard._rescan_proof_is_authorized(handler))

    def test_an_exactly_expired_proof_is_never_accepted(self):
        class Headers:
            def get_all(inner, name, default):
                if name == dashboard.RESCAN_CHALLENGE_HEADER:
                    return [self.challenge]
                if name == dashboard.RESCAN_PROOF_HEADER:
                    return [self.expected()]
                return default

        handler = type("Handler", (), {"headers": Headers()})()
        boundary = self.issued_at + dashboard.RESCAN_PROOF_MAX_AGE_SECONDS
        with mock.patch.object(dashboard, "HEALTH_PROOF_SECRET", self.SECRET), \
                mock.patch.object(dashboard.time, "time",
                                  side_effect=(boundary, boundary)):
            self.assertFalse(dashboard._rescan_proof_is_authorized(handler))
            self.assertFalse(dashboard._rescan_proof_is_authorized(handler))


class TestARepeatedHeaderIsRefusedRatherThanResolved(_LocalServer):
    """A header that arrives twice must be refused, not read as its first value.

    `_host_is_allowed`, `_origin_is_allowed` and `_supplied_credential` each
    take `headers.get_all(...)` and refuse any count other than one. Nothing
    pinned that: rewriting all three as `if not values:` — the simplification a
    reader would call obviously equivalent — leaves the whole suite green while
    `/api/data` answers 200 to a request carrying the good Host followed by
    `Host: evil.example.com`.

    **The legitimate value has to come first, and that is the whole design of
    these three tests.** Put the hostile value first and the fallback to
    `values[0]` refuses the request on its own merits, so the test passes
    against the weakened check too and pins nothing — measured against a
    weakened build: hostile-first is 421/403/403 either way, legitimate-first is
    421/403/403 with the counts and 200/200/200 without them.

    These are well-formedness pins, not a live hole. `Host` and `Origin` are
    forbidden request-header names, so no browser can send a second one, and a
    caller who reaches the flipped cases already holds the API token and could
    have had the same 200 by simply dropping the duplicate. What they defend is
    the rule itself: an ambiguous request is refused rather than resolved.
    """

    def token(self, value=None):
        return (API_TOKEN_HEADER.encode() + b": "
                + (API_TOKEN if value is None else value).encode() + b"\r\n")

    def origin(self, value):
        return b"Origin: " + value.encode() + b"\r\n"

    def api_data(self, extra_headers):
        return status_line(
            raw_request(self.port, "GET", "/api/data", extra_headers))

    def test_a_second_host_header_is_refused(self):
        # raw_request already writes `Host: 127.0.0.1:<port>` as the first
        # line, so this is the legitimate host followed by the hostile one —
        # the order that flips to 200 once the count check is gone.
        self.assertIn(b" 421 ", self.api_data(
            b"Host: evil.example.com\r\n" + self.token()))

    def test_a_second_origin_header_is_refused(self):
        # The token is load-bearing: without it /api/data is 403 for the
        # missing credential whether or not the Origin count is checked, so the
        # test would pass against the weakened build and pin nothing.
        self.assertIn(b" 403 ", self.api_data(
            self.origin(f"http://127.0.0.1:{self.port}")
            + self.origin("http://evil.example.com")
            + self.token()))

    def test_a_second_api_token_header_is_refused(self):
        self.assertIn(b" 403 ", self.api_data(
            self.token() + self.token("nonsense")))

    def test_one_of_each_header_still_authenticates(self):
        """The control. Refusing every Host, Origin or token satisfies all
        three assertions above the lazy way; this is what stops it."""
        self.assertIn(b" 200 ", self.api_data(
            self.origin(f"http://127.0.0.1:{self.port}") + self.token()))


class TestThresholdPutCannotSilentlyEraseSettings(_LocalServer):
    def request(self, path, body=b"", headers=b"", method="PUT"):
        raw = (
            f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"{API_TOKEN_HEADER}: {API_TOKEN}\r\n".encode("ascii")
            + headers + b"Connection: close\r\n\r\n" + body
        )
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        chunks = []
        try:
            conn.sendall(raw)
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            conn.close()
        return b"".join(chunks)

    def redirected(self):
        target = self.tmp / f"{self._testMethodName}.json"
        return target, mock.patch.dict(
            os.environ, {limits_core.THRESHOLDS_ENV: str(target)})

    def test_missing_and_chunked_lengths_are_refused_without_a_write(self):
        target, redirect = self.redirected()
        with redirect:
            limits_core.write_thresholds({"claude:session": [30]})
            missing = self.request(
                "/api/limits/thresholds", b'{"claude:session":[90]}',
                b"Content-Type: application/json\r\n")
            chunked = self.request(
                "/api/limits/thresholds",
                b'1b\r\n{"claude:session":[90]}\r\n0\r\n\r\n',
                b"Content-Type: application/json\r\n"
                b"Transfer-Encoding: chunked\r\n")
            self.assertIn(b" 411 ", status_line(missing))
            self.assertIn(b" 501 ", status_line(chunked))
            self.assertEqual(limits_core.read_thresholds(),
                             {"claude:session": [30]})
            self.assertTrue(target.exists())

    def test_empty_and_non_object_json_are_400_without_a_write(self):
        target, redirect = self.redirected()
        with redirect:
            for body in (b"", b"null", b"[]", b'"text"', b"7"):
                with self.subTest(body=body):
                    limits_core.write_thresholds({"claude:session": [30]})
                    response = self.request(
                        "/api/limits/thresholds", body,
                        f"Content-Length: {len(body)}\r\n".encode("ascii"))
                    self.assertIn(b" 400 ", status_line(response))
                    self.assertEqual(limits_core.read_thresholds(),
                                     {"claude:session": [30]})
            self.assertTrue(target.exists())

    def test_patch_merges_one_window_without_replacing_the_map(self):
        _, redirect = self.redirected()
        body = b'{"thresholds":{"claude:weekly":[90]}}'
        with redirect:
            limits_core.write_thresholds({"claude:session": [30],
                                          "codex:weekly": [70]})
            response = self.request(
                "/api/limits/thresholds", body,
                f"Content-Length: {len(body)}\r\n".encode("ascii"),
                method="PATCH")
            stored = limits_core.read_thresholds()
        self.assertIn(b" 200 ", status_line(response))
        self.assertEqual(stored, {
            "claude:session": [30],
            "claude:weekly": [90],
            "codex:weekly": [70],
        })

    @unittest.skipUnless(os.name == "posix", "symlink semantics are POSIX")
    def test_a_refused_target_is_a_500_not_a_success_echo(self):
        target, redirect = self.redirected()
        victim = self.tmp / f"{self._testMethodName}-victim.json"
        victim.write_text("keep", encoding="utf-8")
        target.symlink_to(victim)
        body = b'{"claude:session":[30]}'
        with redirect:
            response = self.request(
                "/api/limits/thresholds", body,
                f"Content-Length: {len(body)}\r\n".encode("ascii"))
        self.assertIn(b" 500 ", status_line(response))
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")


class TestAssetsResolveInEveryInstalledLayout(unittest.TestCase):
    """`pip install --user` and `--prefix=X` put the module under one prefix and
    the data files under another, and the resolver probed exactly two places:
    beside `dashboard.py`, and under `sys.prefix`. The assets install correctly
    and are found nowhere, so `import dashboard` — which builds HTML_TEMPLATE at
    module scope — dies, blaming the packaging step for a web/ that shipped and
    is sitting three directories away.

    The offset from the module to the data root is not a constant: purelib is
    three directories below it under the posix schemes and two below it under
    the Windows ones, so a hardcoded walk repairs one platform and leaves the
    identical install crashing on the other. Hence the ancestor search, and
    hence a case here for each depth.

    This lives beside the hardening tests because widening an asset search is
    the change that must not also widen what the server will execute: a chart
    runtime reached through a new candidate is still checked against its pinned
    digest before it is served.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        # Resolved, because the resolver resolves: on macOS the temp root is a
        # symlink and the unresolved string never appears in what it returns.
        self.tmp = Path(self._tmpdir.name).resolve()
        self.chart = dashboard.find_chart_file()
        self.assertIsNotNone(self.chart, "the checkout's own chart.umd.js")

    def tearDown(self):
        self._tmpdir.cleanup()

    def build_layout(self, name, site_packages):
        """A data prefix laid out the way a pip install scheme lays one out."""
        root = self.tmp / name
        module_dir = root / site_packages
        module_dir.mkdir(parents=True)
        share = root / "share" / "claude-usage"
        (share / "web").mkdir(parents=True)
        (share / "web" / "index.html").write_text(
            "__APP_CSS__ __APP_JS__", encoding="utf-8")
        (share / "vendor").mkdir(parents=True)
        shutil.copyfile(self.chart, share / "vendor" / "chart.umd.js")
        return module_dir, share

    def resolve_from(self, module_dir):
        """What the two resolvers find for a module living at `module_dir`.

        `sys.prefix` is pointed at an empty directory so the interpreter this
        suite happens to run under cannot answer for the layout under test.
        """
        empty_prefix = self.tmp / "unrelated-prefix"
        empty_prefix.mkdir(exist_ok=True)
        with mock.patch.object(dashboard, "__file__",
                               str(module_dir / "dashboard.py")), \
                mock.patch.object(sys, "prefix", str(empty_prefix)):
            return dashboard.find_web_dir(), dashboard.find_chart_file()

    def test_a_posix_user_or_prefix_install_finds_its_assets(self):
        """Three directories up: posix_prefix, posix_user, osx_framework_user
        and every venv."""
        module_dir, share = self.build_layout("posix", "lib/python3.13/site-packages")
        web, chart = self.resolve_from(module_dir)
        self.assertEqual(web, share / "web")
        self.assertEqual(chart, share / "vendor" / "chart.umd.js")

    def test_a_windows_install_finds_its_assets(self):
        """Two directories up: nt and nt_user. A fix that counted hops would
        repair the case above and leave this one raising at import."""
        module_dir, share = self.build_layout("windows", "Lib/site-packages")
        web, chart = self.resolve_from(module_dir)
        self.assertEqual(web, share / "web")
        self.assertEqual(chart, share / "vendor" / "chart.umd.js")

    def test_assets_beside_the_module_still_outrank_an_ancestor(self):
        """The checkout, Homebrew's libexec, the Docker image and the .vsix all
        put web/ and vendor/ beside dashboard.py. That candidate stays first."""
        module_dir, share = self.build_layout("beside", "lib/python3.13/site-packages")
        beside = module_dir / "web"
        beside.mkdir()
        (beside / "index.html").write_text("__APP_CSS__ __APP_JS__", encoding="utf-8")
        web, _chart = self.resolve_from(module_dir)
        self.assertEqual(web, beside)
        self.assertNotEqual(web, share / "web")

    def test_a_tampered_chart_reached_through_a_new_candidate_is_refused(self):
        """The digest pin is why the resolver may search more places at all."""
        module_dir, share = self.build_layout("tampered", "lib/python3.13/site-packages")
        (share / "vendor" / "chart.umd.js").write_text(
            "alert('pwned')", encoding="utf-8")
        _web, chart = self.resolve_from(module_dir)
        self.assertIsNone(chart)

    def test_the_not_found_message_names_every_place_it_looked(self):
        """The old message asserted a cause it had not checked — "the packaging
        step did not ship web/" — about an install whose files were present and
        correct. It must say where it looked and stop there, and it must say
        *all* of where it looked: a hand-written list of two is how it came to
        describe a search it no longer performed."""
        root = self.tmp / "empty"
        module_dir = root / "lib" / "python3.13" / "site-packages"
        module_dir.mkdir(parents=True)
        empty_prefix = self.tmp / "unrelated-prefix"
        empty_prefix.mkdir(exist_ok=True)
        with mock.patch.object(dashboard, "__file__",
                               str(module_dir / "dashboard.py")), \
                mock.patch.object(sys, "prefix", str(empty_prefix)):
            with self.assertRaises(RuntimeError) as raised:
                dashboard.load_html_template()
        message = str(raised.exception)
        self.assertIn(str(module_dir / "web"), message)
        self.assertIn(str(empty_prefix / "share" / "claude-usage" / "web"), message)
        self.assertIn(str(root / "share" / "claude-usage" / "web"), message)
        self.assertNotIn("packaging step", message)


def write_expired_window_config(path, reset):
    """A ~/.claude.json whose five-hour window reset `reset` ago.

    Carries the identifying fields the real file does, so the privacy contract
    is asserted against a payload that actually had something to leak.
    """
    path.write_text(json.dumps({
        "oauthAccount": {
            "organizationType": "claude_max",
            "organizationRateLimitTier": "default_claude_max_20x",
            "emailAddress": "someone@example.com",
            "accountUuid": "11111111-2222-3333-4444-555555555555",
            "organizationUuid": "66666666-7777-8888-9999-000000000000",
            "organizationName": "Some Org",
            "displayName": "Some Person",
        },
        "userID": "abcdef0123456789",
        "projects": {"/Users/someone/code/private": {"lastCost": 1.0}},
        "cachedUsageUtilization": {
            "fetchedAtMs": int((reset.timestamp() - 600) * 1000),
            "utilization": {
                "limits": [{
                    "kind": "session",
                    "group": "session",
                    "percent": 100,
                    "severity": "critical",
                    "is_active": True,
                    "resets_at": reset.isoformat(),
                }],
            },
        },
    }), encoding="utf-8")


def seed_window_turns(db_path, reset):
    """Two turns inside the current window and one well before it."""
    def stamp(moment):
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = get_db(db_path)
    try:
        init_db(conn)
        insert_turns(conn, [
            {"session_id": "sess-window", "timestamp": stamp(reset + timedelta(minutes=5)),
             "model": "claude-opus-4-8", "input_tokens": 100, "output_tokens": 50,
             "cache_read_tokens": 10, "cache_creation_tokens": 5,
             "tool_name": None, "cwd": "/tmp", "message_id": "msg-in-1"},
            {"session_id": "sess-window", "timestamp": stamp(reset + timedelta(minutes=10)),
             "model": "claude-opus-4-8", "input_tokens": 100, "output_tokens": 50,
             "cache_read_tokens": 10, "cache_creation_tokens": 5,
             "tool_name": None, "cwd": "/tmp", "message_id": "msg-in-2"},
            {"session_id": "sess-window", "timestamp": stamp(reset - timedelta(hours=3)),
             "model": "claude-opus-4-8", "input_tokens": 999, "output_tokens": 999,
             "cache_read_tokens": 999, "cache_creation_tokens": 999,
             "tool_name": None, "cwd": "/tmp", "message_id": "msg-before"},
        ])
        conn.commit()
    finally:
        conn.close()
    return {"turns": 2, "tokens": 2 * (100 + 50 + 10 + 5)}


class TestLimitsGoesThroughTheDatabaseGuard(_LocalServer):
    """`/api/limits` must open the database the way every other route does.

    `get_dashboard_data` and `available_sources` both call
    `db.secure_db_permissions` first, whose own comment calls the symlink,
    hard-link and regular-file refusals "the actual trust boundary". This route
    called `sqlite3.connect(DB_PATH)` directly, which both *creates* the file
    (with the process umask rather than 0600) and follows a symlink that
    `/api/data` refuses outright.
    """

    @classmethod
    def prepare(cls):
        cls.reset = (datetime.now(timezone.utc) - timedelta(minutes=20)).replace(
            second=0, microsecond=0)
        cls.config = cls.tmp / "claude.json"
        write_expired_window_config(cls.config, cls.reset)
        cls.expected = seed_window_turns(cls.db_path, cls.reset)
        cls._env = mock.patch.dict(os.environ, {
            "CLAUDE_USAGE_CONFIG": str(cls.config),
            # Neutralise the developer's own ~/.claude/settings.json: an
            # api-key declaration there would make every window unavailable and
            # quietly vacuum these assertions.
            "HOME": str(cls.tmp),
            "USERPROFILE": str(cls.tmp),
        })
        cls._env.start()
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            os.environ.pop(name, None)

    @classmethod
    def cleanup(cls):
        cls._env.stop()

    def limits(self):
        response = raw_request(
            self.port, "GET", "/api/limits",
            API_TOKEN_HEADER.encode() + b": " + API_TOKEN.encode() + b"\r\n")
        self.assertIn(b" 200 ", status_line(response))
        return json.loads(response.split(b"\r\n\r\n", 1)[1])

    def test_the_fixture_really_does_enrich_the_window(self):
        """The control. Without this the two tests below could pass because
        nothing was ever enriched, rather than because the guard held."""
        window = self.limits()["windows"][0]
        self.assertTrue(window["expired"])
        self.assertIn("window_start", window)
        self.assertEqual(window["recorded"], self.expected)

    def test_database_enrichment_stays_inside_rebuild_admission(self):
        """The route must hold admission through its last database read."""
        state = {"held": False, "entered": 0}
        observations = []
        real_usage_since = dashboard_data.usage_since

        @contextlib.contextmanager
        def observed_admission(conn, path):
            self.assertFalse(state["held"])
            state["held"] = True
            state["entered"] += 1
            try:
                yield False
            finally:
                state["held"] = False

        def observed_usage_since(*args, **kwargs):
            observations.append(state["held"])
            return real_usage_since(*args, **kwargs)

        with mock.patch.object(dashboard, "database_admission",
                               observed_admission), \
                mock.patch.object(dashboard_data, "usage_since",
                                  observed_usage_since):
            payload = self.limits()

        self.assertEqual(state["entered"], 1)
        self.assertTrue(observations, "fixture never queried recorded usage")
        self.assertTrue(all(observations),
                        "a usage query escaped rebuild admission")
        self.assertEqual(payload["windows"][0]["recorded"], self.expected)

    def test_a_marked_database_is_not_read_before_recovery(self):
        """A durable marker produces the safe config-only fallback."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(f"PRAGMA user_version = {db.REBUILD_IN_PROGRESS}")
            conn.commit()
        finally:
            conn.close()

        try:
            with mock.patch.object(dashboard_data, "claude_limits",
                                   wraps=dashboard_data.claude_limits) as limits:
                payload = self.limits()
            self.assertEqual(limits.call_count, 0,
                             "marked database reached its usage queries")
            self.assertNotIn("recorded", payload["windows"][0])
        finally:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("PRAGMA user_version = 0")
                conn.commit()
            finally:
                conn.close()

    def test_it_does_not_create_the_database(self):
        """cmd_dashboard binds and serves before its background scan runs, and
        the plan panel polls this every 30 seconds — so on a fresh install this
        route reached a non-existent path first and made the file itself, with
        the process umask instead of 0600."""
        missing = self.tmp / "not-created-yet.db"
        with mock.patch.object(dashboard, "DB_PATH", missing):
            payload = self.limits()
        self.assertFalse(missing.exists(),
                         "/api/limits created the usage database")
        self.assertNotIn("recorded", payload["windows"][0])

    @unittest.skipUnless(os.name == "posix", "renaming an open SQLite file is POSIX-only")
    def test_a_missing_database_releases_dashboard_cache_state(self):
        """The config-only fallback must not retain the displaced database."""
        dashboard_data.reset_payload_cache()
        held = self.tmp / "held-usage.db"
        try:
            with mock.patch.object(
                    dashboard_data, "PAYLOAD_CACHE_MIN_BUILD_SECONDS", 0.0):
                dashboard_data.get_dashboard_data(self.db_path)
            self.assertTrue(dashboard_data._PAYLOAD_CACHE)
            self.assertIsNotNone(dashboard_data._VERSION_PROBE)

            os.replace(self.db_path, held)
            payload = self.limits()

            self.assertNotIn("recorded", payload["windows"][0])
            self.assertEqual(dashboard_data._PAYLOAD_CACHE, {})
            self.assertIsNone(dashboard_data._VERSION_PROBE)
        finally:
            dashboard_data.reset_payload_cache()
            if held.exists():
                os.replace(held, self.db_path)

    @unittest.skipUnless(os.name == "posix", "POSIX inode verification only")
    def test_it_does_not_read_a_database_replaced_after_validation(self):
        original = self.tmp / "limits-original.db"
        replacement = self.tmp / "limits-replacement.db"
        seed_window_turns(original, self.reset)
        seed_window_turns(replacement, self.reset)
        real_guard = db.secure_db_permissions
        swapped = False

        def swap_after_validation(*args, **kwargs):
            nonlocal swapped
            result = real_guard(*args, **kwargs)
            if not swapped and Path(args[0]) == original:
                os.replace(replacement, original)
                swapped = True
            return result

        with mock.patch.object(dashboard, "DB_PATH", original), \
                mock.patch.object(db, "secure_db_permissions",
                                  swap_after_validation):
            payload = self.limits()

        self.assertTrue(swapped, "the fixture never replaced the file")
        self.assertNotIn("recorded", payload["windows"][0])

    @unittest.skipUnless(os.name == "posix",
                         "creating a symlink needs privilege on Windows")
    def test_it_refuses_a_symlinked_database_like_every_other_route(self):
        link = self.tmp / "link.db"
        link.symlink_to(self.db_path)
        with mock.patch.object(dashboard, "DB_PATH", link):
            payload = self.limits()
        self.assertNotIn(
            "recorded", payload["windows"][0],
            "/api/limits read through a symlinked database path")


# Terminal escapes, an OSC-8 hyperlink and a bidi override, in the four
# account-cache strings the plan panel renders. `\x1b[2J\x1b[H` clears and homes
# a terminal, U+202E reverses the run that follows it, and OSC 8 makes the
# following text a hyperlink.
HOSTILE_PLAN_TYPE = "claude_max\x1b[2J\x1b[H\u202eDEZINWO"
HOSTILE_RATE_TIER = "tier\x1b[31m4"
HOSTILE_SEVERITY = "warning\x1b[5m"
HOSTILE_SCOPE = "Sonnet\x1b]8;;http://evil\x07"


def write_hostile_window_config(path, reset):
    """`write_expired_window_config`, with control characters in the strings.

    Same shape and the same expired five-hour window, so the enrichment path is
    identical; only the four strings the panel puts on screen differ.
    """
    path.write_text(json.dumps({
        "oauthAccount": {
            "organizationType": HOSTILE_PLAN_TYPE,
            "organizationRateLimitTier": HOSTILE_RATE_TIER,
        },
        "cachedUsageUtilization": {
            "fetchedAtMs": int((reset.timestamp() - 600) * 1000),
            "utilization": {
                "limits": [{
                    "kind": "session",
                    "group": "session",
                    "percent": 100,
                    "severity": HOSTILE_SEVERITY,
                    "is_active": True,
                    "resets_at": reset.isoformat(),
                    "scope": {"model": {"display_name": HOSTILE_SCOPE}},
                }],
            },
        },
    }), encoding="utf-8")


def control_characters(value):
    """Every terminal/bidi/surrogate code point in a decoded JSON value."""
    found = set()
    if isinstance(value, str):
        found.update(c for c in value
                     if unicodedata.category(c) in ("Cc", "Cf", "Cs"))
    elif isinstance(value, list):
        for item in value:
            found |= control_characters(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            found |= control_characters(key) | control_characters(item)
    return found


class TestLimitsEscapesWhatApiDataEscapes(_LocalServer):
    """`/api/limits` must not ship what `/api/data` escapes out of the same call.

    Both routes render the plan panel from `account.current_limits` — the first
    paint from the copy embedded in `/api/data`, every refresh 30 seconds later
    from this route — and only the embedded copy went through
    `safe_dashboard_value`. So the same install showed the escaped form on load
    and the raw form on the next poll, and `web/js/56-plan.js` put the raw
    `plan_type` in `tier.textContent` and the raw `rate_limit_tier` in
    `tier.title`, while `planWindowLabel`'s raw `kind`/`scope` reached
    `new Notification(...)` in `web/js/58-alerts.js` — a sink no `esc()` covers.

    These strings come from Anthropic's API via `~/.claude.json`, not from a
    transcript, so this is a falsified boundary rather than a live exploit. The
    assertions are therefore that the route ships the ESCAPED form: comparing
    the two routes for equality alone would also pass if a change made both raw.
    """

    @classmethod
    def prepare(cls):
        cls.reset = (datetime.now(timezone.utc) - timedelta(minutes=20)).replace(
            second=0, microsecond=0)
        cls.config = cls.tmp / "claude.json"
        write_hostile_window_config(cls.config, cls.reset)
        seed_window_turns(cls.db_path, cls.reset)
        cls._env = mock.patch.dict(os.environ, {
            "CLAUDE_USAGE_CONFIG": str(cls.config),
            "HOME": str(cls.tmp),
            "USERPROFILE": str(cls.tmp),
        })
        cls._env.start()
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            os.environ.pop(name, None)

    @classmethod
    def cleanup(cls):
        cls._env.stop()

    def get_json(self, path):
        response = raw_request(
            self.port, "GET", path,
            API_TOKEN_HEADER.encode() + b": " + API_TOKEN.encode() + b"\r\n")
        self.assertIn(b" 200 ", status_line(response))
        return json.loads(response.split(b"\r\n\r\n", 1)[1])

    def test_the_fixture_really_carries_control_characters(self):
        """The control. Every assertion below is vacuous if the hostile strings
        survive the config round trip unchanged, or if `terminal_safe` were to
        become the identity function."""
        raw = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(raw["oauthAccount"]["organizationType"],
                         HOSTILE_PLAN_TYPE)
        for hostile in (HOSTILE_PLAN_TYPE, HOSTILE_RATE_TIER,
                        HOSTILE_SEVERITY, HOSTILE_SCOPE):
            self.assertTrue(control_characters(hostile))
            self.assertNotEqual(terminal_safe(hostile), hostile)

    def test_the_route_ships_the_escaped_form(self):
        payload = self.get_json("/api/limits")
        window = payload["windows"][0]
        # The four fields that reach a rendering sink: plan_type ->
        # tier.textContent, rate_limit_tier -> tier.title, kind and scope ->
        # planWindowLabel -> the Notification body. (`severity` is allow-listed
        # to normal/warning/critical client-side, so it is swept below rather
        # than named as a vector.)
        self.assertEqual(payload["plan_type"], terminal_safe(HOSTILE_PLAN_TYPE))
        self.assertEqual(payload["rate_limit_tier"],
                         terminal_safe(HOSTILE_RATE_TIER))
        self.assertEqual(window["kind"], "session")
        self.assertEqual(window["scope"], terminal_safe(HOSTILE_SCOPE))
        self.assertEqual(control_characters(payload), set())

    def test_both_routes_ship_the_identical_escaped_strings(self):
        standalone = self.get_json("/api/limits")
        embedded = self.get_json("/api/data")["subscription_limits"]
        for field in ("plan_type", "rate_limit_tier"):
            self.assertEqual(standalone[field], embedded[field])
        self.assertEqual(standalone["windows"][0]["scope"],
                         embedded["windows"][0]["scope"])
        self.assertEqual(control_characters(embedded), set())
        self.assertEqual(control_characters(standalone), set())

    def test_the_fallback_taken_before_the_database_exists_is_escaped_too(self):
        """`do_GET` reaches `dashboard_data.claude_limits` only inside
        `if DB_PATH.exists():` and otherwise answers from
        `account.current_limits` — the only branch a fresh install takes, since
        cmd_dashboard binds and serves before its background scan creates the
        file, and this route is polled every 30 seconds meanwhile. Sanitising
        inside `claude_limits` would leave exactly this branch raw."""
        missing = self.tmp / "not-created-yet.db"
        with mock.patch.object(dashboard, "DB_PATH", missing):
            payload = self.get_json("/api/limits")
        self.assertNotIn("recorded", payload["windows"][0])
        self.assertEqual(payload["plan_type"], terminal_safe(HOSTILE_PLAN_TYPE))
        self.assertEqual(control_characters(payload), set())


def send_json_calls(module_path):
    """Every `self._send_json(...)` in `module_path`, as `ast.Call` nodes."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"), str(module_path))
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_send_json"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"]


def payload_source(call):
    """The payload expression of a `_send_json(status, payload)` call.

    It raises rather than indexing blindly, so a call written some other way
    names itself instead of surfacing as an `IndexError` out of `args[1]`.
    """
    if len(call.args) != 2 or call.keywords:
        raise AssertionError(
            f"_send_json at dashboard.py:{call.lineno} is not "
            f"(status, payload), so this census cannot read its payload")
    return ast.unparse(call.args[1])


def is_a_literal(source):
    try:
        ast.literal_eval(source)
    except (ValueError, SyntaxError, TypeError, MemoryError):
        return False
    return True


class TestEveryNonLiteralPayloadWasAlreadyCovered(unittest.TestCase):
    """`_send_json` escapes what it is handed; this pins WHAT it is handed.

    The comment above that method claims every payload reaching it that is not
    a literal error dict was already escaped somewhere upstream, and says which
    ones those are. A comment cannot notice a new one. This can: a route that
    starts handing `_send_json` a value assembled elsewhere shows up here as a
    payload expression the table below does not know, and has to be shown safe
    rather than assumed so — which is exactly the step `/api/limits` skipped.

    It replaces a hand-taken census that used to live in that comment. A count
    nobody executes goes stale silently and in the direction of looking
    authoritative — the payload-key count that stood beside it in the same
    comment already had.
    """

    # Payload expression -> why its strings cannot reach the browser raw.
    ALREADY_COVERED = {
        "health":
            "/healthz — VERSION, plus a SHA-256 HMAC rendered as lowercase "
            "hex when the extension's proof secret is configured",
        "{'service': 'claude-usage', 'proof': _liveness_proof(challenges[0])}":
            "/api/instance — the challenge is confined by LOCAL_TOKEN_RE and "
            "the only derived string is a SHA-256 HMAC rendered as lowercase "
            "hex; neither recovery secret is included",
        "data":
            "/api/data — dashboard_data._collect_dashboard_data returns "
            "through safe_dashboard_value",
        "{'sources': sources}":
            "/api/sources — dashboard_data.available_sources returns through "
            "safe_dashboard_value",
        "{'snapshot': read_snapshot(DB_PATH, key, VERSION)}":
            "/api/snapshot — saved successful payloads were sanitized by their "
            "assembler; the shared _send_json boundary also sanitizes strings "
            "read back from the bounded, owner-validated local file",
        "{**scan_status(), 'docker': docker_sources.status()}":
            "/api/scan-status — scan_status returns a fixed lifecycle name and "
            "an integer generation under the process-local activity lock; it "
            "carries no transcript-derived value. Docker status adds only a "
            "fixed state and integer counts, never container names or paths",
        "{'error': message, 'scan': status}":
            "scan-gated /api/data and /api/sources responses — message is one "
            "of three fixed strings in _send_scan_read_blocked and status is "
            "the fixed lifecycle name plus integer generation above",
        "payload":
            "/api/limits — the one that used to go out raw, and the reason "
            "the wrap moved into _send_json",
        "{**result, 'docker': docker_sources.status()}":
            "/api/rescan — scanner.scan returns counts; Docker status adds a "
            "fixed state and integer counts, no transcript or container text",
        "{'error': message}":
            "PUT/PATCH /api/limits/thresholds body errors — `message` is "
            "selected from the fixed per-front-end message map passed to the "
            "shared bounded body reader; it is never request-controlled",
        "{'thresholds': limits_core.read_thresholds()}":
            "GET /api/limits/thresholds — limits_core.normalize_thresholds "
            "rebuilds the mapping on the way out of the file: every key goes "
            "through _bounded_text and is length-capped, and every value is an "
            "int in 1..100. Nothing that was not rebuilt survives, so no raw "
            "string from the file can reach the browser",
        "self._database_error_body(exc)":
            "/api/data and /api/sources — the body for a database read that "
            "failed. Every value in it is a literal written in this file: the "
            "fixed `error` sentence and a `permanent` boolean. `exc` is read "
            "ONLY by db.a_retry_could_succeed, which returns a bool; neither "
            "SQLite's message nor the database path is put in the body, and "
            "that is deliberate — this dict is rendered into the document, "
            "and the precise diagnosis (which names a filesystem path) is "
            "printed to the terminal instead. tests/"
            "test_unreadable_database_notice.py asserts both absences",
    }

    def setUp(self):
        self.calls = send_json_calls(Path(dashboard.__file__))

    def test_the_walk_finds_the_call_sites_at_all(self):
        """Without this, every assertion below passes against an empty list."""
        self.assertGreater(len(self.calls), 10)

    def test_every_call_passes_a_status_and_exactly_one_payload(self):
        """`payload_source` reads `args[1]`, so a call written any other way
        cannot be read at all; this is the assertion that says so plainly
        rather than leaving it to that helper's own error."""
        for call in self.calls:
            with self.subTest(line=call.lineno):
                self.assertEqual(len(call.args), 2)
                self.assertEqual(call.keywords, [])

    def test_every_non_literal_payload_is_one_that_was_already_wrapped(self):
        non_literal = {payload_source(call) for call in self.calls
                       if not is_a_literal(payload_source(call))}
        self.assertEqual(non_literal, set(self.ALREADY_COVERED))

    def test_the_rest_are_literal_error_answers(self):
        """The other side of the split, so a broken `is_a_literal` that called
        everything a literal would fail here instead of passing above."""
        literals = [ast.literal_eval(payload_source(call))
                    for call in self.calls
                    if is_a_literal(payload_source(call))]
        self.assertGreater(len(literals), len(self.ALREADY_COVERED))
        for payload in literals:
            with self.subTest(payload=payload):
                self.assertEqual(list(payload), ["error"])
                self.assertIsInstance(payload["error"], str)


class TestConnectionLimits(unittest.TestCase):
    """The two bounds DashboardHTTPServer puts on what a local peer can hold.

    Neither had a test: `request.settimeout(...)` and the `_request_slots`
    acquire could both be deleted with the whole suite green.

    What they buy is bounded threads and file descriptors in THIS process, not
    availability — a peer that can open sockets can always open more, and with
    the cap gone the flood would simply be served. So the cap's contract is
    "refuse rather than queue", and the timeout's is "a peer that connects and
    then says nothing does not keep a worker forever".

    The server is built here rather than reusing `_LocalServer` because
    MAX_HTTP_CONNECTIONS is read once, in `__init__`, so it can only be lowered
    before construction — and because a leaked slot must not reach a sibling
    test. HTTP_SOCKET_TIMEOUT_SECONDS is the opposite: `get_request` reads it on
    every accept, so the timeout test patches it around the connection.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)
        projects = tmp / "projects"
        projects.mkdir()
        # Same belt and braces as _LocalServer: this class builds a real server
        # with the real handler, so nothing it does may reach the developer's
        # transcripts or usage database.
        self._patchers = [
            mock.patch.object(dashboard, "DB_PATH", tmp / "usage.db"),
            mock.patch.object(scanner, "DB_PATH", tmp / "usage.db"),
            mock.patch.object(scanner, "DEFAULT_PROJECTS_DIRS", [projects]),
            mock.patch.object(dashboard, "MAX_HTTP_CONNECTIONS", 2),
        ]
        for patcher in self._patchers:
            patcher.start()
        self.server = DashboardHTTPServer(("127.0.0.1", 0), DashboardHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmpdir.cleanup()

    def test_a_request_past_the_connection_cap_is_refused_not_queued(self):
        """Held slots are taken directly rather than by flooding the port: the
        cap is what is under test, and a flood would race the accept loop."""
        slots = self.server._request_slots
        held = 0
        try:
            while slots.acquire(blocking=False):
                held += 1
            self.assertEqual(held, 2,
                             "the semaphore was not sized by MAX_HTTP_CONNECTIONS")
            # This used to assert b"" -- a silent close. It now answers 503,
            # deliberately: a reset connection is indistinguishable from a dead
            # server, so saturation read as a crash and sent the reader looking
            # in the wrong place. What the test is really for is unchanged and
            # still asserted below: the request is REFUSED rather than queued
            # behind the held slots, and the slot comes back afterwards.
            refused = raw_request(self.port, "GET", "/healthz")
            self.assertIn(b" 503 ", status_line(refused),
                          "a request past the cap was served anyway")
            self.assertIn(b"Retry-After", refused)
            headers, body = refused.split(b"\r\n\r\n", 1)
            self.assertIn(b"Content-Type: application/json", headers)
            self.assertIn(f"Content-Length: {len(body)}".encode("ascii"), headers)
            self.assertEqual(json.loads(body), {
                "error": "Too many concurrent connections; retry shortly",
            })
            self.assertNotIn(b" 200 ", status_line(refused))
        finally:
            for _ in range(held):
                slots.release()
        self.assertIn(b" 200 ",
                      status_line(raw_request(self.port, "GET", "/healthz")),
                      "the slot was never returned")

    def test_a_late_request_does_not_erase_the_overload_response(self):
        slots = self.server._request_slots
        held = 0
        try:
            while slots.acquire(blocking=False):
                held += 1
            with socket.create_connection(("127.0.0.1", self.port), timeout=2) as client:
                # First prove that the refusal was sent without waiting for
                # request bytes. Then deliver the request during the close.
                response = client.recv(1)
                self.assertEqual(response, b"H")
                client.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n")
                while True:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    response += chunk
            self.assertIn(b"503 Service Unavailable", status_line(response))
            self.assertEqual(json.loads(response.split(b"\r\n\r\n", 1)[1]), {
                "error": "Too many concurrent connections; retry shortly",
            })
        finally:
            for _ in range(held):
                slots.release()

    def test_a_peer_that_connects_and_says_nothing_is_dropped(self):
        """0.3s of silence stands in for 15s of it, the same way the proxy's
        idle-timeout test stands in for thirty seconds."""
        with mock.patch.object(dashboard, "HTTP_SOCKET_TIMEOUT_SECONDS", 0.3):
            client = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            try:
                started = time.monotonic()
                self.assertEqual(client.recv(64), b"",
                                 "the stalled connection was never closed")
                self.assertLess(time.monotonic() - started, 4.0)
            finally:
                client.close()

    def test_the_read_watchdog_assumes_one_request_per_connection(self):
        """A landmine for the day someone sets `protocol_version = "HTTP/1.1"`.

        The watchdog is armed in `setup()`, which runs once per CONNECTION, and
        cancelled by the first `parse_request`. Under HTTP/1.0 that is the whole
        story, because `close_connection` stays True and there is never a second
        request on the socket. Enable keep-alive and the second request is read
        with no watchdog at all -- one valid request would buy an attacker the
        indefinite hold this class exists to prevent.

        An adversarial review raised exactly this. It is not a live hole; the
        point of the test is that it cannot BECOME one silently. If you are here
        because this failed, re-arm the watchdog per request before shipping the
        keep-alive.
        """
        self.assertEqual(
            dashboard.DashboardHandler.protocol_version, "HTTP/1.0",
            "keep-alive is on, but the read watchdog is still armed once per "
            "connection -- re-arm it per request in handle_one_request")

    def test_a_client_that_keeps_talking_cannot_hold_a_slot_forever(self):
        """A DRIP-FEEDING peer is dropped, which the timeout above cannot do.

        The test above passes on a peer that says nothing, because each recv
        expires. It says nothing about a peer that sends a byte just often
        enough: `settimeout` is per blocking call, so every arriving byte
        satisfies the current recv and the next one starts with a full
        allowance. Wrapping `rfile` to re-arm a deadline does not fix it either
        -- `readline` blocks inside its own recv loop, so a budget checked on
        entry is never consulted again while the drip continues. Both were
        measured against 32 held slots before the watchdog replaced them.

        The watchdog fires once and cannot be renewed by anything the client
        sends, which is exactly what this asserts.
        """
        with mock.patch.object(dashboard, "HTTP_REQUEST_READ_BUDGET_SECONDS", 0.5):
            client = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            try:
                client.sendall(b"GET /healthz HTTP/1.1\r\n")
                started = time.monotonic()
                dropped = False
                client.setblocking(False)
                # Drip a well-formed header line every 0.1s -- far inside the
                # per-recv timeout, so only an absolute deadline can end this.
                while time.monotonic() - started < 5.0:
                    try:
                        client.sendall(b"X-Pad: 1\r\n")
                    except OSError:
                        dropped = True
                        break
                    try:
                        if client.recv(1, socket.MSG_PEEK) == b"":
                            dropped = True
                            break
                    except BlockingIOError:
                        pass
                    except OSError:
                        dropped = True
                        break
                    time.sleep(0.1)
                self.assertTrue(
                    dropped,
                    "a drip-feeding client held its connection slot indefinitely")
                self.assertLess(time.monotonic() - started, 5.0)
            finally:
                client.close()

    def test_the_read_watchdog_also_covers_a_slow_put_body(self):
        """Fast headers must not cancel the absolute body-read deadline."""
        with mock.patch.object(dashboard, "HTTP_REQUEST_READ_BUDGET_SECONDS", 0.5):
            client = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            try:
                client.sendall((
                    "PUT /api/limits/thresholds HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{self.port}\r\n"
                    f"{API_TOKEN_HEADER}: {API_TOKEN}\r\n"
                    "Content-Type: application/json\r\n"
                    "Content-Length: 100\r\n\r\n{"
                ).encode("ascii"))
                started = time.monotonic()
                dropped = False
                client.setblocking(False)
                while time.monotonic() - started < 5:
                    time.sleep(0.1)
                    try:
                        client.sendall(b" ")
                        if client.recv(1, socket.MSG_PEEK) == b"":
                            dropped = True
                            break
                    except BlockingIOError:
                        continue
                    except OSError:
                        dropped = True
                        break
                self.assertTrue(dropped, "a drip-fed PUT body held its slot")
                self.assertLess(time.monotonic() - started, 5)
            finally:
                client.close()


class SlowUpstreamHandler(BaseHTTPRequestHandler):
    """An upstream that thinks before it answers, like a cold rescan."""

    delay = 1.0

    def do_GET(self):
        time.sleep(self.delay)
        body = b"slow-upstream"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


class TestProxyKeepsAnInFlightRequestAlive(unittest.TestCase):
    """The proxy is the only host-published port in the Docker deployment, so
    every request — including POST /api/rescan — crosses it.

    Thirty seconds of silence in *either* direction was read as an idle
    connection, but a request that has been sent and whose response is still
    being computed looks exactly like that. cli.py's own comment says a cold
    scan "can take well over a minute", so the Rescan button was guaranteed to
    lose its answer on first use.
    """

    def setUp(self):
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), SlowUpstreamHandler)
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        self.proxy = proxy.FixedTargetProxy(("127.0.0.1", 0),
                                            self.upstream.server_address)
        self.proxy_thread = threading.Thread(
            target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.proxy_thread.join(timeout=2)
        self.upstream_thread.join(timeout=2)

    def test_a_response_slower_than_the_idle_timeout_still_arrives(self):
        # 0.2s of "idle" against a 1.0s response: the same shape as 30s against
        # a two-minute scan, without spending two minutes to prove it.
        with mock.patch.object(proxy, "IDLE_TIMEOUT_SECONDS", 0.2):
            connection = http.client.HTTPConnection(*self.proxy.server_address,
                                                    timeout=10)
            try:
                connection.request("GET", "/api/rescan")
                response = connection.getresponse()
                body = response.read()
            finally:
                connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body, b"slow-upstream")

    def test_a_connection_with_nothing_outstanding_is_still_closed(self):
        """The timeout must still exist: a peer that connects and says nothing
        cannot be allowed to hold a slot open indefinitely."""
        with mock.patch.object(proxy, "IDLE_TIMEOUT_SECONDS", 0.2):
            client = socket.create_connection(self.proxy.server_address, timeout=5)
            try:
                started = time.monotonic()
                self.assertEqual(client.recv(64), b"",
                                 "the idle connection was not closed")
                self.assertLess(time.monotonic() - started, 4.0)
            finally:
                client.close()


class TestTheReadRoutesOpenTheDatabaseThroughTheGuard(unittest.TestCase):
    """The twin of `test_it_refuses_a_symlinked_database_like_every_other_route`.

    That test's name asserts against `/api/data` and `/api/sources` — the two
    routes `dashboard.py`'s comment calls the reference implementation, and the
    two AGENTS.md names as opening the database "through `secure_db_permissions`"
    — and it covers neither. Measured 2026-08-10: deleting both
    `secure_db_permissions(db_path)` calls from `dashboard_data.py` left the
    full suite at 1565 tests, OK, so the guard `db.py` calls "the actual trust
    boundary" could be dropped from the page's two busiest reads by a refactor
    with no signal at all, while `/api/limits` went on refusing — three routes
    silently disagreeing about the same path.

    The directory case leads deliberately. It exercises the not-a-regular-file
    limb, which needs no privilege, so it runs on the Windows leg where a
    symlink test can only skip; and it discriminates, because with the call
    deleted `sqlite3.connect` raises `OperationalError`, which is not a
    `RuntimeError`. The symlink case below is the limb the `/api/limits` test
    actually pins, kept for parity — it is the one that reads THROUGH rather
    than failing differently.

    Scope, stated rather than left to be assumed: these assert that the guard is
    *called*, not that all three of its refusals work. `secure_db_permissions`
    owns its own tests; deleting the call is what this catches.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = self.tmp / "usage.db"
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.close()

    def _routes(self):
        return (("get_dashboard_data", dashboard_data.get_dashboard_data),
                ("available_sources", dashboard_data.available_sources))

    def test_the_fixture_database_really_is_readable(self):
        """The control. Without it both tests below could pass because the
        fixture was broken rather than because the guard held."""
        self.assertEqual(dashboard_data.available_sources(self.db_path), [])
        self.assertIn("daily_by_model",
                      dashboard_data.get_dashboard_data(self.db_path))

    def test_they_refuse_a_database_path_that_is_not_a_regular_file(self):
        not_a_file = self.tmp / "as-a-directory" / "usage.db"
        not_a_file.mkdir(parents=True)
        for name, route in self._routes():
            with self.subTest(route=name):
                with self.assertRaises(db.UnsafeDatabasePathError):
                    route(not_a_file)

    @unittest.skipUnless(os.name == "posix", "POSIX inode verification only")
    def test_they_reject_a_database_replaced_after_path_validation(self):
        for name, route in self._routes():
            with self.subTest(route=name):
                dashboard_data.reset_payload_cache()
                original = self.tmp / f"{name}-original.db"
                replacement = self.tmp / f"{name}-replacement.db"
                for candidate in (original, replacement):
                    conn = sqlite3.connect(candidate)
                    conn.row_factory = sqlite3.Row
                    init_db(conn, candidate)
                    conn.close()

                real_guard = db.secure_db_permissions
                swapped = False

                def swap_after_validation(*args, **kwargs):
                    nonlocal swapped
                    result = real_guard(*args, **kwargs)
                    if not swapped and Path(args[0]) == original:
                        os.replace(replacement, original)
                        swapped = True
                    return result

                with mock.patch.object(db, "secure_db_permissions",
                                       swap_after_validation):
                    with self.assertRaisesRegex(
                            RuntimeError, "Database path changed during open"):
                        route(original)
                self.assertTrue(swapped, "the fixture never replaced the file")

    @unittest.skipUnless(os.name == "posix",
                         "creating a symlink needs privilege on Windows")
    def test_they_refuse_a_symlinked_database_the_way_api_limits_does(self):
        """The link must point at a REAL database. Both functions return early
        on `db_path.exists()`, which follows the link, so a dangling one never
        reaches the guard and the test would be green with or without it."""
        link = self.tmp / "link.db"
        link.symlink_to(self.db_path)
        for name, route in self._routes():
            with self.subTest(route=name):
                with self.assertRaises(RuntimeError) as caught:
                    route(link)
                self.assertIn("symbolic-link", str(caught.exception))


# Two instants that parse cleanly and then overflow `datetime` on the way to
# UTC: the conversion carries the first past `datetime.max` and the second
# before `datetime.min`. `turns.timestamp` is stored through `_bounded_text`,
# which bounds LENGTH and nothing else, so either can sit in the database.
OVERFLOWING_INSTANTS = ("9999-12-31T23:59:59.999-05:00",
                        "0001-01-01T00:00:00+05:00")
# The same magnitude in the form both assistants actually write. It does NOT
# overflow the parse — `Z` yields the `timezone.utc` singleton and `astimezone`
# short-circuits — which is exactly why it is the string that reaches the
# arithmetic below, and why it has to keep parsing after the fix.
FAR_FUTURE_UTC = "9999-12-31T23:59:59Z"


class TestOneBadTimestampCannotBlankTheWholePage(unittest.TestCase):
    """`_collect_dashboard_data` has no try/except, so anything that raises
    inside it answers `GET /api/data` with 500 "Failed to read the usage
    database" — the whole page, on every poll, for as long as the row stays in
    the database. `rollups.limit_incidents` carries a comment about that exact
    structural gap, having been the last function to fall through it.

    Two more doors were open on the same class, both reached from a raw
    transcript timestamp that nothing validates for format:

    * `_parse_utc` ran `astimezone` OUTSIDE its `try` and caught only
      `ValueError`, so it raised `OverflowError` while its docstring said
      "Never raises" — the identical shape `account._parse_instant` was
      hardened against, whose reasoning AGENTS.md records verbatim. It is
      reached from `MAX(turns.timestamp)` for source='codex', and `max()` over
      strings is lexicographic, so a year-9999 row outranks every real one.
    * `_correct_window_start` adds `first + step` to a `MIN(turns.timestamp)`
      it has parsed but not bounded. That is the same arithmetic
      `account.current_window_bounds` already guards, whose test says "the
      overflow is in `reset + step`, *before* the anti-spin loop". This one
      needs no exotic offset: `9999-12-31T23:59:59Z`, the form the transcripts
      actually carry, parses fine and detonates the addition.

    Synthetic hostile timestamps test the boundary even when ordinary client
    output is well formed. A poisoned stored row must not make every later
    query fail."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = self.tmp / "usage.db"

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        init_db(conn)
        return conn

    def _seed_codex(self, conn, timestamp):
        """One Codex turn plus the snapshot row the projection needs to get
        past its `no_codex_usage` gate — without it lines 140-145 are never
        reached and the test would pass on any tree."""
        conn.execute(
            "INSERT INTO turns (session_id, message_id, timestamp, model, source,"
            " input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens)"
            " VALUES ('codex-s', ?, ?, 'gpt-5.6-sol', 'codex', 10, 1, 0, 0)",
            ("codex:root:1000", timestamp))
        conn.execute(
            "INSERT INTO usage_limits_snapshots (kind, grp, scope, resets_key,"
            " percent, severity, is_active, resets_at, observed_at)"
            " VALUES ('codex', '10080m', '', '2026-08-16T10:23', 42, '', 1,"
            " '2026-08-16T10:23:00+00:00', '2026-08-05T10:00:01.000Z')")
        conn.commit()

    def test_the_parser_never_raises_as_its_docstring_promises(self):
        for value in OVERFLOWING_INSTANTS:
            with self.subTest(timestamp=value):
                self.assertIsNone(dashboard_data._parse_utc(value))

    def test_a_legitimate_far_future_instant_is_still_parsed(self):
        """The negative control. A later "just validate the year range" patch
        must not start discarding instants that convert perfectly well —
        `astimezone` is what fails, not `fromisoformat`."""
        parsed = dashboard_data._parse_utc(FAR_FUTURE_UTC)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.year, 9999)
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_the_parser_is_the_one_account_already_hardened(self):
        """One definition, not two hand-synced copies. The divergence between
        them WAS the defect: the same four statements, one hardened and one
        silently not."""
        import account
        for value in OVERFLOWING_INSTANTS + (FAR_FUTURE_UTC, "", "x", None, 7):
            with self.subTest(timestamp=value):
                self.assertEqual(dashboard_data._parse_utc(value),
                                 account._parse_instant(value))

    def test_a_codex_turn_stamped_past_the_end_of_time_still_serves_the_page(self):
        """End to end through the real payload assembler, not the parser alone.

        The year-9999 row is what `MAX(timestamp)` selects — lexicographically
        it beats every real 2026 one — so this is the row the projection ages
        from on every poll."""
        conn = self._connect()
        self._seed_codex(conn, OVERFLOWING_INSTANTS[0])
        payload = dashboard_data._collect_dashboard_data(conn)
        self.assertIn("daily_by_model", payload)
        self.assertTrue(payload["codex_limits"]["available"])
        self.assertIsNone(payload["codex_limits"]["age_seconds"],
                          "an unplaceable reading has no age, and says so")

    def test_a_claude_turn_stamped_past_the_end_of_time_still_serves_the_page(self):
        """The other door, and the one reachable from the format the
        transcripts actually use. `_correct_window_start` walks forward from
        the stale reset to the first turn at or after it; that turn is in year
        9999, and `first + step` leaves `datetime`'s range.

        `CLAUDE_USAGE_CONFIG` goes in `os.environ`, not only in the `env` this
        function takes: `account.config_path` reads the process environment
        while `env` reaches only `detect_auth_mode`. Passed as an argument alone
        this test read the DEVELOPER'S OWN `~/.claude.json` and asserted against
        whatever window it happened to hold — green in isolation and red in the
        suite twenty minutes later. `HOME` moves for the same reason: a real
        `~/.claude/settings.json` declaring an API key makes every window
        unavailable, which would vacuum the assertions.
        """
        conn = self._connect()
        reset = datetime.now(timezone.utc) - timedelta(minutes=30)
        conn.execute(
            "INSERT INTO turns (session_id, message_id, timestamp, model, source,"
            " input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens)"
            " VALUES ('claude-s', 'm-far', ?, 'claude-opus-5', 'claude', 5, 1, 0, 0)",
            (FAR_FUTURE_UTC,))
        conn.commit()
        config = self.tmp / "claude.json"
        write_expired_window_config(config, reset)
        env = {"CLAUDE_USAGE_CONFIG": str(config),
               "HOME": str(self.tmp), "USERPROFILE": str(self.tmp)}
        with mock.patch.dict(os.environ, env):
            for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
                os.environ.pop(name, None)
            payload = dashboard_data.claude_limits(conn, os.environ)
        window = payload["windows"][0]
        self.assertEqual(window["kind"], "session", "the fixture's own window")
        self.assertTrue(window["expired"])
        self.assertNotIn("window_start_source", window,
                         "an unplaceable turn must leave the projection alone")


class TestAnUnparseableRequestTargetIsAnswered(_LocalServer):
    """A malformed request target must get a response, not a dropped socket.

    `urlparse` raises on a malformed authority, and both `do_GET` and `do_POST`
    called it outside any `try`. The ValueError escaped into
    `socketserver.handle_error`, which closes the connection with **no reply**
    and prints a ~2.3 KB traceback — so an unauthenticated caller could spend
    the operator's terminal at will (290 KB/s measured), and under the VS Code
    extension that stderr is piped into the output channel and grows unbounded.

    The reply matters as much as the absence of the crash: zero bytes is
    indistinguishable from a dead server, which is the state `url_is_live`'s
    tri-state exists to avoid guessing about.
    """

    #: Targets whose authority `urlparse` cannot parse. Kept as data so a
    #: future CPython that rejects a new shape can be added in one place.
    UNPARSEABLE = (
        "http://[1:2:3:4:5:6:7:8:9]:x/",   # too many groups, non-numeric port
        "http://[::1",                     # unterminated literal
        "http://[]/",                      # empty literal
    )

    def test_every_unparseable_target_is_refused_with_400(self):
        for target in self.UNPARSEABLE:
            for method in ("GET", "POST"):
                with self.subTest(target=target, method=method):
                    response = raw_request(self.port, method, target)
                    self.assertTrue(response, "the connection was dropped instead of answered")
                    self.assertIn(b"400", status_line(response))

    def test_the_server_still_serves_afterwards(self):
        """The point of answering rather than crashing: the next caller is
        unaffected. A handler that dies mid-request can leave the connection
        slot or the socket in a state the next request pays for."""
        for target in self.UNPARSEABLE:
            raw_request(self.port, "GET", target)
        response = raw_request(self.port, "GET", "/healthz")
        self.assertIn(b"200", status_line(response),
                      "a malformed target poisoned the server for later callers")

    def test_a_well_formed_target_is_unaffected(self):
        """The control. Without it this class would still pass if the guard
        rejected everything, which is the cheapest way to break a server."""
        response = raw_request(self.port, "GET", "/healthz?range=all")
        self.assertIn(b"200", status_line(response))


class TestTheIconIsServedAsInertContent(_LocalServer):
    """`/icon.svg` is unauthenticated ACTIVE content and must be neutered.

    SVG is the one image format that executes. Navigated to at the top level --
    not loaded through the CSS mask the header uses -- an SVG carrying an inline
    <script> runs it in the dashboard's own origin, beside the token. The route
    takes no credential, so any page that can guess the port can send a reader
    there.

    Two facts made it safe in practice and neither was enforced: the shipped
    web/icon.svg is inert, and `find_icon_file` returns the nearest root first
    so a copy planted further away is never reached. `/assets/chart.umd.js`
    pins a digest; this route pins nothing. So the response carries a CSP that
    makes the content question moot instead.
    """

    def _headers(self, path):
        response = raw_request(self.port, "GET", path)
        self.assertTrue(response, f"{path} dropped the connection")
        return response.split(b"\r\n\r\n", 1)[0].decode("latin-1")

    def test_the_icon_response_forbids_script_and_sandboxes_itself(self):
        head = self._headers("/icon.svg")
        self.assertIn(" 200 ", head.splitlines()[0])
        csp = [line for line in head.splitlines()
               if line.lower().startswith("content-security-policy:")]
        self.assertTrue(csp, "/icon.svg carries no Content-Security-Policy")
        policy = csp[0].split(":", 1)[1].strip()
        self.assertIn("default-src 'none'", policy)
        self.assertIn("sandbox", policy)
        self.assertNotIn("script-src 'self'", policy)
        self.assertNotIn("unsafe-eval", policy)

    def test_it_is_still_served_as_an_svg_and_still_cacheable(self):
        """The lockdown must not cost the icon: it is a real 200 with bytes."""
        response = raw_request(self.port, "GET", "/icon.svg")
        head, _, body = response.partition(b"\r\n\r\n")
        self.assertIn(b"image/svg+xml", head)
        self.assertIn(b"private, max-age=86400", head)
        self.assertGreater(len(body), 0, "the icon body is empty")

    def test_the_page_itself_keeps_its_own_nonce_policy(self):
        """The icon's policy must not have leaked onto the document route."""
        head = self._headers("/")
        self.assertIn("nonce-", head)
        self.assertNotIn("sandbox", head)


class TestTheChartDigestGuardsWhatIsActuallySent(unittest.TestCase):
    """The pin has to cover the bytes on the wire, not a different read of them.

    `find_chart_file()` hashed `candidate.read_bytes()` and returned a PATH; the
    route then did a SECOND, independent `chart.read_bytes()` and served that.
    Nothing tied the two together, so a writer flipping `vendor/chart.umd.js`
    while the server ran got tampered JavaScript served with HTTP 200 straight
    past `CHART_JS_SHA256` — measured on this tree at 35 of 600 sequential
    requests under one flipping thread.

    Why it mattered more than the file permission suggests: the chart is the
    ONLY executable asset re-read per request. `HTML_TEMPLATE` and every
    `web/js/*.js` byte are frozen at import, so an attacker who gains write
    access AFTER the server starts has no other route into the page's origin —
    where `API_TOKEN` is readable from `location.hash`. The response is cached
    `private, max-age=86400`, so one won race persists for a day.

    The guard that existed asserted on the RESOLVER's return value and never on
    a served body, while its own docstring claimed the runtime "is still checked
    against its pinned digest before it is served". This tests the claim.
    """

    def test_the_verified_bytes_are_the_returned_bytes(self):
        """`chart_asset_bytes()` hashes and returns ONE read.

        Driven through a `read_bytes` that answers differently on each call —
        the file-flip, made deterministic. A function that re-reads hands back
        the second answer; one that returns what it hashed cannot.
        """
        genuine = dashboard.chart_asset_bytes()
        self.assertIsNotNone(genuine, "the checkout's own chart.umd.js")
        self.assertEqual(
            hashlib.sha256(genuine).hexdigest(), dashboard.CHART_JS_SHA256,
            "the bytes handed back are not the ones the pin names")

        reads = []
        original = Path.read_bytes

        def flipping(self_path):
            data = original(self_path)
            reads.append(data)
            # First read genuine, every later read tampered: exactly what a
            # writer flipping the file between the check and the send produces.
            return data if len(reads) == 1 else b"/*EVIL*/" + data

        with mock.patch.object(Path, "read_bytes", flipping):
            served = dashboard.chart_asset_bytes()
        self.assertIsNotNone(
            served, "the genuine first read must still satisfy the pin")
        self.assertNotIn(
            b"/*EVIL*/", served,
            "the bytes returned came from a LATER read than the one hashed")
        self.assertEqual(hashlib.sha256(served).hexdigest(),
                         dashboard.CHART_JS_SHA256)

    def test_a_tampered_first_read_is_refused(self):
        """Anti-vacuity: the pin still rejects, so the test above is not passing
        because the check was removed."""
        original = Path.read_bytes

        def tampered(self_path):
            return b"/*EVIL*/" + original(self_path)

        with mock.patch.object(Path, "read_bytes", tampered):
            self.assertIsNone(dashboard.chart_asset_bytes())

    def test_the_route_serves_what_was_verified(self):
        """Structural, because the defect was a second read at the CALL SITE
        rather than anything wrong inside the checker. The handler must not
        re-read the path it was given."""
        source = inspect.getsource(dashboard.DashboardHandler.do_GET)
        branch = source[source.index('"/assets/chart.umd.js"'):]
        branch = branch[:branch.index("elif path ==", 1)]
        self.assertIn("chart_asset_bytes()", branch,
                      "the route must take the verified buffer")
        self.assertNotIn("read_bytes()", branch,
                         "the route re-reads the file it was told about, so "
                         "what it sends is not what the digest covered")


if __name__ == "__main__":
    unittest.main()
