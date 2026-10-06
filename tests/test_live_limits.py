"""The optional live quota query, and the guarantee it does not break by default.

This is the only part of the tool that talks to the network, and the README's
promise that it makes **no automatic internet requests** has to stay true unless
the user asks otherwise. So the first thing asserted here is a negative: with
nothing configured, no request is attempted at all.

Nothing in this file makes a real request. Every test injects an opener.
"""

import io
import hashlib
import json
import os
import shlex
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import live_limits


class _Response(io.BytesIO):
    """The subset of an http response `fetch_utilization` touches."""

    def __init__(self, payload, status=200):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(payload, status=200, record=None):
    def open_url(request, timeout=None):
        if record is not None:
            record.append(request)
        return _Response(payload, status)
    return open_url


LIVE_ENV = {"CODEX_CLAUDE_USAGE_LIVE_LIMITS": "1", "CODEX_CLAUDE_USAGE_OAUTH_TOKEN": "tok"}
SAMPLE = {"utilization": {"limits": [
    {"kind": "weekly", "group": "weekly", "percent": 37, "severity": "normal",
     "resets_at": "2026-08-22T00:00:00+00:00", "scope": None, "is_active": True}]}}


class TestItIsOffUnlessAskedFor(unittest.TestCase):
    """The default must remain "this program makes no network requests"."""

    def test_nothing_configured_attempts_no_request(self):
        calls = []
        self.assertIsNone(live_limits.fetch_utilization(
            env={}, opener=_opener(SAMPLE, record=calls)))
        self.assertEqual(calls, [], "a request was made with nothing configured")

    def test_a_token_alone_is_not_consent(self):
        """Someone who exports a credential for another purpose has not asked
        this tool to start making requests on their behalf."""
        calls = []
        self.assertIsNone(live_limits.fetch_utilization(
            env={"CODEX_CLAUDE_USAGE_OAUTH_TOKEN": "tok"},
            opener=_opener(SAMPLE, record=calls)))
        self.assertEqual(calls, [])

    def test_the_opt_in_alone_does_nothing_either(self):
        calls = []
        self.assertIsNone(live_limits.fetch_utilization(
            env={"CODEX_CLAUDE_USAGE_LIVE_LIMITS": "1"},
            opener=_opener(SAMPLE, record=calls)))
        self.assertEqual(calls, [])

    def test_both_together_is_what_enables_it(self):
        calls = []
        got = live_limits.fetch_utilization(
            env=dict(LIVE_ENV), opener=_opener(SAMPLE, record=calls))
        self.assertIsNotNone(got)
        self.assertEqual(len(calls), 1)

    def test_api_key_mode_is_gated_before_any_live_request(self):
        import dashboard_data
        config = {
            "oauthAccount": {"organizationType": "claude_max"},
            "cachedUsageUtilization": {"utilization": {"limits": []}},
        }
        env = dict(LIVE_ENV, ANTHROPIC_API_KEY="billing-key")
        with mock.patch.object(dashboard_data.account, "read_config",
                               return_value=config), \
                mock.patch.object(live_limits, "config_with_live_limits") as fetch:
            payload = dashboard_data._limits_payload(env)
        fetch.assert_not_called()
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "api_key")

    def test_subscription_without_a_cache_can_still_request_live_limits(self):
        import dashboard_data
        config = {"oauthAccount": {"organizationType": "claude_max"}}
        live_config = {
            **config,
            "cachedUsageUtilization": {
                "fetchedAtMs": 1767268800000,
                "utilization": SAMPLE["utilization"],
            },
        }
        with mock.patch.object(dashboard_data.account, "read_config",
                               return_value=config), \
                mock.patch.object(
                    live_limits, "config_with_live_limits",
                    return_value=(live_config, "live")) as fetch:
            payload = dashboard_data._limits_payload(dict(LIVE_ENV))
        fetch.assert_called_once()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["reading"], "live")


class TestTheCredentialIsHandledCarefully(unittest.TestCase):
    @staticmethod
    def _python_command(source):
        args = [sys.executable, "-c", source]
        return (subprocess.list2cmdline(args) if os.name == "nt"
                else shlex.join(args))

    def test_the_token_command_keeps_first_line_precedence(self):
        output = "tok" + chr(10) + "warning"
        source = f"import sys; sys.stdout.write({output!r})"
        env = {live_limits.TOKEN_COMMAND_ENV: self._python_command(source)}
        self.assertEqual(live_limits._token_from_command(env), "tok")

    def test_a_normal_completion_retires_its_containment_exactly_once(self):
        source = "print('tok')"
        env = {
            live_limits.TOKEN_COMMAND_ENV: self._python_command(source),
        }
        original = live_limits._stop_token_process
        with mock.patch.object(
                live_limits, "_stop_token_process", wraps=original) as stop:
            self.assertEqual(live_limits._token_from_command(env), "tok")
        self.assertEqual(stop.call_count, 1)

    def test_the_injected_runner_is_a_bounded_popen_seam(self):
        seen = {}

        def runner(command, **kwargs):
            seen.update(kwargs)
            return subprocess.Popen(command, **kwargs)

        output = "tok" + chr(10)
        source = f"import sys; sys.stdout.write({output!r})"
        env = {live_limits.TOKEN_COMMAND_ENV: self._python_command(source)}
        self.assertEqual(live_limits._token_from_command(env, runner=runner), "tok")
        self.assertIs(seen["stdout"], subprocess.PIPE)
        self.assertIs(seen["stderr"], subprocess.DEVNULL)
        self.assertFalse(seen["text"])

    def test_an_injected_runner_without_a_stdout_pipe_is_stopped(self):
        process_holder = []

        def runner(command, **kwargs):
            kwargs["stdout"] = None
            process = subprocess.Popen(command, **kwargs)
            process_holder.append(process)
            return process

        source = "import time; time.sleep(10)"
        env = {live_limits.TOKEN_COMMAND_ENV: self._python_command(source)}
        began = time.monotonic()
        self.assertEqual(live_limits._token_from_command(env, runner=runner), "")
        self.assertLess(time.monotonic() - began, 1.0)
        self.assertIsNotNone(process_holder[0].poll())

    def test_an_overproducing_stdout_is_cut_before_the_command_deadline(self):
        command = self._python_command(
            'import sys; sys.stdout.write("x" * 20000); sys.stdout.flush()')
        env = {live_limits.TOKEN_COMMAND_ENV: command}
        began = time.monotonic()
        self.assertEqual(live_limits._token_from_command(env), "")
        self.assertLess(time.monotonic() - began, 1.0)

    def test_an_invalid_command_token_uses_the_static_fallback(self):
        source = "import sys; sys.stdout.write('bad token\\n')"
        env = {
            live_limits.TOKEN_COMMAND_ENV: self._python_command(source),
            live_limits.TOKEN_ENV: "static-token",
        }
        self.assertEqual(live_limits.resolve_token(env), "static-token")

    def test_a_valid_command_still_wins_over_the_static_fallback(self):
        source = "import sys; sys.stdout.write('fresh-token\\n')"
        env = {
            live_limits.TOKEN_COMMAND_ENV: self._python_command(source),
            live_limits.TOKEN_ENV: "static-token",
        }
        self.assertEqual(live_limits.resolve_token(env), "fresh-token")

    def test_an_oversized_command_token_uses_the_static_fallback(self):
        source = (
            "import sys; "
            f"sys.stdout.write('x' * {live_limits.MAX_TOKEN_CHARS + 1})"
        )
        env = {
            live_limits.TOKEN_COMMAND_ENV: self._python_command(source),
            live_limits.TOKEN_ENV: "static-token",
        }
        self.assertEqual(live_limits.resolve_token(env), "static-token")

    def test_empty_and_nonzero_commands_use_the_static_fallback(self):
        for source in (
            "pass",
            "import sys; raise SystemExit(7)",
        ):
            with self.subTest(source=source):
                env = {
                    live_limits.TOKEN_COMMAND_ENV: self._python_command(source),
                    live_limits.TOKEN_ENV: "static-token",
                }
                self.assertEqual(live_limits.resolve_token(env), "static-token")

    def test_a_valid_looking_token_from_a_failed_command_uses_the_fallback(self):
        source = "import sys; print('not-usable'); raise SystemExit(7)"
        env = {
            live_limits.TOKEN_COMMAND_ENV: self._python_command(source),
            live_limits.TOKEN_ENV: "static-token",
        }
        self.assertEqual(live_limits.resolve_token(env), "static-token")

    def test_a_timed_out_command_uses_the_static_fallback(self):
        source = "import time; time.sleep(10)"
        env = {
            live_limits.TOKEN_COMMAND_ENV: self._python_command(source),
            live_limits.TOKEN_ENV: "static-token",
        }
        with mock.patch.object(live_limits, "TOKEN_COMMAND_TIMEOUT", 0.05), \
                mock.patch.object(
                    live_limits, "TOKEN_COMMAND_TERMINATE_GRACE", 0.05):
            self.assertEqual(live_limits.resolve_token(env), "static-token")

    def test_an_invalid_static_fallback_is_not_returned_as_usable(self):
        self.assertEqual(live_limits.resolve_token({
            live_limits.TOKEN_ENV: "bad token",
        }), "")

    def test_an_invalid_command_fallback_reaches_the_authorization_header(self):
        observed = []

        def opener(request, timeout=None):
            observed.append(request.get_header("Authorization"))
            return _Response(SAMPLE)

        source = "import sys; sys.stdout.write('bad token\\n')"
        env = {
            live_limits.ENABLE_ENV: "1",
            live_limits.TOKEN_COMMAND_ENV: self._python_command(source),
            live_limits.TOKEN_ENV: "static-token",
        }
        self.assertIsNotNone(live_limits.fetch_utilization(env=env, opener=opener))
        self.assertEqual(observed, ["Bearer static-token"])

    @unittest.skipUnless(os.name == "posix", "process-group semantics are POSIX")
    def test_a_descendant_that_keeps_stdout_does_not_survive_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            quoted = shlex.quote(str(marker))
            command = (
                f"printf 'tok\\n'; (sleep 0.5; touch {quoted}) & exit 0"
            )
            env = {live_limits.TOKEN_COMMAND_ENV: command}
            with mock.patch.object(live_limits,
                                   "TOKEN_COMMAND_TERMINATE_GRACE", 0.05):
                # The shell exits after the first line, while its background
                # child retains stdout. The saved process group must be killed
                # even though the shell's own return code is already available.
                self.assertEqual(live_limits._token_from_command(env), "tok")
            time.sleep(0.7)
            self.assertFalse(marker.exists(), "a descendant outlived cleanup")

    @unittest.skipUnless(os.name == "posix", "process-group semantics are POSIX")
    def test_an_injected_popen_factory_retains_descendant_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            command = (
                f"printf 'tok\\n'; (sleep 0.5; touch {shlex.quote(str(marker))}) "
                "& exit 0"
            )

            def runner(launch_command, **kwargs):
                return subprocess.Popen(launch_command, **kwargs)

            env = {live_limits.TOKEN_COMMAND_ENV: command}
            with mock.patch.object(
                    live_limits, "TOKEN_COMMAND_TERMINATE_GRACE", 0.05):
                self.assertEqual(
                    live_limits._token_from_command(env, runner=runner), "tok")
            time.sleep(0.7)
            self.assertFalse(marker.exists(), "the injected seam lost the PGID")

    @unittest.skipUnless(os.name == "posix", "process-group semantics are POSIX")
    def test_a_successful_no_newline_command_cleans_redirected_descendants(self):
        """EOF can arrive while a child survives with stdout redirected."""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            command = (
                f"printf tok; (sleep 0.5; touch {shlex.quote(str(marker))}) "
                ">/dev/null 2>&1 & exit 0"
            )
            env = {live_limits.TOKEN_COMMAND_ENV: command}
            with mock.patch.object(
                    live_limits, "TOKEN_COMMAND_TERMINATE_GRACE", 0.05):
                self.assertEqual(live_limits._token_from_command(env), "tok")
            time.sleep(0.7)
            self.assertFalse(marker.exists(), "a redirected descendant survived EOF")

    @unittest.skipUnless(os.name == "nt", "Job Object semantics are Windows-only")
    def test_a_windows_descendant_does_not_survive_job_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            child = (
                "import pathlib,time; time.sleep(0.5); "
                f"pathlib.Path({str(marker)!r}).write_text('alive')"
            )
            parent = (
                "import subprocess,sys,time; time.sleep(0.1); "
                f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
                "print('tok', flush=True)"
            )
            env = {live_limits.TOKEN_COMMAND_ENV: self._python_command(parent)}
            with mock.patch.object(live_limits,
                                   "TOKEN_COMMAND_TERMINATE_GRACE", 0.05):
                self.assertEqual(live_limits._token_from_command(env), "tok")
            time.sleep(0.7)
            self.assertFalse(marker.exists(), "a descendant outlived its Job Object")

    def test_stderr_and_a_trailing_infinite_producer_cannot_hold_the_poll(self):
        output = "tok" + chr(10)
        command = self._python_command(
            "import sys\n"
            f"sys.stdout.write({output!r})\n"
            "sys.stdout.flush()\n"
            "while True:\n"
            "    sys.stderr.write('x' * 4096)\n")
        env = {live_limits.TOKEN_COMMAND_ENV: command}
        began = time.monotonic()
        self.assertEqual(live_limits._token_from_command(env), "")
        self.assertLess(time.monotonic() - began, 1.0)

    def test_the_token_goes_in_the_authorization_header_and_nowhere_else(self):
        observed = []
        calls = []

        def opener(request, timeout=None):
            observed.append(request.get_header("Authorization"))
            calls.append(request)
            return _Response(SAMPLE)

        live_limits.fetch_utilization(env=dict(LIVE_ENV), opener=opener)
        self.assertEqual(observed, ["Bearer tok"])
        request = calls[0]
        self.assertIsNone(request.get_header("Authorization"),
                          "the completed caller scrubbed its Request")
        self.assertNotIn("tok", request.full_url,
                         "the credential is in the URL, where it reaches logs")

    def test_an_http_endpoint_is_refused_rather_than_upgraded(self):
        """Sending the credential in clear text is worse than not answering, and
        silently upgrading the scheme would hide that somebody asked for it."""
        calls = []
        env = dict(LIVE_ENV, CODEX_CLAUDE_USAGE_LIMITS_URL="http://example.invalid/u")
        self.assertIsNone(live_limits.fetch_utilization(
            env=env, opener=_opener(SAMPLE, record=calls)))
        self.assertEqual(calls, [], "a credential was sent over plain http")

    def test_the_default_endpoint_is_https_and_anthropic(self):
        url = live_limits.endpoint({})
        self.assertTrue(url.startswith("https://api.anthropic.com/"))

    def test_the_real_transport_refuses_to_redirect_the_bearer_token(self):
        """The default urllib redirect handler copies Authorization to the
        redirected request, including when the destination changes origin.

        A quota endpoint redirect therefore has to fail closed. Injected test
        openers are deliberately unaffected; this pins the transport used in
        production when no opener is supplied.
        """
        transport = mock.Mock()
        transport.open.return_value = _Response(SAMPLE)
        with mock.patch.object(
                live_limits.urllib.request, "build_opener",
                return_value=transport) as build_opener, \
                mock.patch.object(
                    live_limits.urllib.request, "urlopen",
                    side_effect=AssertionError(
                        "the redirect-following default transport was used")):
            got = live_limits.fetch_utilization(env=dict(LIVE_ENV))

        self.assertIsNotNone(got)
        transport.open.assert_called_once()
        handlers = [
            value for value in build_opener.call_args.args
            if isinstance(value, live_limits.urllib.request.HTTPRedirectHandler)
        ]
        self.assertEqual(len(handlers), 1)
        original = live_limits.urllib.request.Request(
            live_limits.DEFAULT_ENDPOINT,
            headers={"Authorization": "Bearer tok"},
        )
        self.assertIsNone(
            handlers[0].redirect_request(
                original, None, 302, "Found", {},
                "https://attacker.invalid/credential",
            ),
            "the bearer credential can be forwarded to another origin",
        )

    def test_a_completed_fetch_does_not_retain_the_plaintext_credential(self):
        env = dict(
            LIVE_ENV,
            CODEX_CLAUDE_USAGE_OAUTH_TOKEN="completed-fetch-secret",
        )
        self.assertIsNotNone(live_limits.fetch_utilization(
            env=env, opener=_opener(SAMPLE)))
        task = live_limits._FETCH_IN_FLIGHT
        self.assertNotIn("completed-fetch-secret", repr(task.key))
        self.assertIsNone(
            task.operation,
            "the completed worker retained its request and Authorization header",
        )

    def test_a_surrogate_escaped_environment_token_falls_back_without_raising(self):
        """POSIX can surface undecodable environment bytes as lone surrogates."""
        env = dict(LIVE_ENV, CODEX_CLAUDE_USAGE_OAUTH_TOKEN="bad\udcfftoken")
        self.assertIsNone(
            live_limits.fetch_utilization(env=env, opener=_opener(SAMPLE))
        )

    def test_invalid_header_shaped_tokens_are_refused_before_the_opener(self):
        def should_not_open(*args, **kwargs):
            raise AssertionError("an invalid bearer reached the transport")

        for token in ("line\nbreak", "space in token",
                      "x" * (live_limits.MAX_TOKEN_CHARS + 1)):
            with self.subTest(token_length=len(token)):
                env = dict(LIVE_ENV, CODEX_CLAUDE_USAGE_OAUTH_TOKEN=token)
                self.assertIsNone(live_limits.fetch_utilization(
                    env=env, opener=should_not_open
                ))


class TestAFailureFallsBackRatherThanBreaking(unittest.TestCase):
    """Every failure mode must degrade to "use the cache", which is what the
    tool did before this module existed."""

    def test_a_network_error_answers_none(self):
        import urllib.error

        def boom(request, timeout=None):
            raise urllib.error.URLError("offline")

        self.assertIsNone(live_limits.fetch_utilization(
            env=dict(LIVE_ENV), opener=boom))

    def test_a_non_200_answers_none(self):
        self.assertIsNone(live_limits.fetch_utilization(
            env=dict(LIVE_ENV), opener=_opener(SAMPLE, status=401)))

    def test_malformed_json_answers_none(self):
        self.assertIsNone(live_limits.fetch_utilization(
            env=dict(LIVE_ENV), opener=_opener(b"{not json")))

    def test_an_unrecognised_shape_answers_none_not_a_half_block(self):
        """An endpoint read out of a bundle is not a contract. A changed
        response must degrade to the cache, never render as nonsense."""
        for payload in ({}, {"something": "else"}, [], "text", 5):
            with self.subTest(payload=payload):
                self.assertIsNone(live_limits.fetch_utilization(
                    env=dict(LIVE_ENV), opener=_opener(payload)))

    def test_an_oversized_body_is_refused(self):
        huge = b'{"utilization":{"limits":[]},"pad":"' + b"x" * (
            live_limits.MAX_RESPONSE_BYTES + 10) + b'"}'
        self.assertIsNone(live_limits.fetch_utilization(
            env=dict(LIVE_ENV), opener=_opener(huge)))

    def test_a_dripping_upstream_cannot_hold_request_slots_indefinitely(self):
        """The deadline covers the whole fetch, not one socket inactivity gap.

        One wedged daemon operation is retained instead of spawning a new stuck
        thread for every HTTP request. Cached limits remain the fallback.
        """
        started = threading.Event()
        release = threading.Event()
        closed = threading.Event()

        class SlowResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                closed.set()
                return False

            def read(self, amount):
                started.set()
                release.wait(5)
                return json.dumps(SAMPLE).encode("utf-8")

        calls = []

        def slow_open(request, timeout=None):
            calls.append(request)
            return SlowResponse()

        began = time.monotonic()
        try:
            self.assertIsNone(live_limits.fetch_utilization(
                env=dict(LIVE_ENV), timeout=0.1, opener=slow_open))
            elapsed = time.monotonic() - began
            self.assertTrue(started.wait(1), "the fixture never reached the read")
            self.assertLess(elapsed, 1.0, "the total fetch deadline was ignored")

            second_calls = []
            self.assertIsNone(live_limits.fetch_utilization(
                env=dict(LIVE_ENV), timeout=0.1,
                opener=_opener(SAMPLE, record=second_calls)))
            self.assertEqual(
                second_calls, [],
                "a second network worker was started while the first was stuck",
            )
        finally:
            release.set()
        self.assertTrue(closed.wait(1), "the released response was not closed")
        self.assertEqual(len(calls), 1)

    def test_a_timed_out_worker_loses_the_bearer_while_still_blocked(self):
        started = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        calls = []
        secret = "blocked-worker-secret"

        class BlockedResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                closed.set()
                return False

            def read(self, amount):
                started.set()
                release.wait(5)
                return json.dumps(SAMPLE).encode("utf-8")

        def blocked_open(request, timeout=None):
            calls.append(request)
            return BlockedResponse()

        try:
            self.assertIsNone(live_limits.fetch_utilization(
                env=dict(LIVE_ENV, CODEX_CLAUDE_USAGE_OAUTH_TOKEN=secret),
                timeout=0.1,
                opener=blocked_open,
            ))
            self.assertTrue(started.wait(1), "the worker never reached the blocked read")
            self.assertEqual(len(calls), 1)
            self.assertIsNone(
                calls[0].get_header("Authorization"),
                "a timed-out caller left its bearer on the blocked Request",
            )

            task = live_limits._FETCH_IN_FLIGHT
            self.assertIsNotNone(task)
            self.assertFalse(task.done.is_set(), "the worker was not still blocked")
            self.assertIsNotNone(task.operation,
                                 "the blocked worker lost its operation too early")
            self.assertEqual(
                task.key[1], hashlib.sha256(secret.encode("ascii")).digest()
            )
            self.assertNotIn(secret, repr(task.key))
        finally:
            release.set()
        self.assertTrue(closed.wait(1), "the released response was not closed")

    def test_the_config_merge_says_which_reading_it_returned(self):
        config = {"cachedUsageUtilization": {"utilization": {"limits": []}}}
        merged, source = live_limits.config_with_live_limits(
            config, env=dict(LIVE_ENV), opener=_opener(SAMPLE))
        self.assertEqual(source, "live")
        self.assertEqual(
            merged["cachedUsageUtilization"]["utilization"]["limits"][0]["kind"],
            "weekly")
        # The caller's config is NEVER mutated: it is the parsed ~/.claude.json,
        # a file this tool only reads.
        self.assertEqual(config["cachedUsageUtilization"]["utilization"]["limits"], [])

    def test_a_failed_fetch_returns_the_original_config_untouched(self):
        import urllib.error

        def boom(request, timeout=None):
            raise urllib.error.URLError("offline")

        config = {"cachedUsageUtilization": {"marker": True}}
        merged, source = live_limits.config_with_live_limits(
            config, env=dict(LIVE_ENV), opener=boom)
        self.assertEqual(source, "cache")
        self.assertIs(merged, config)


class TestTheLiveReadingIsStampedNow(unittest.TestCase):
    def test_the_age_is_the_requests_age_not_the_servers(self):
        """`account.limits_projection` computes the age from `fetchedAtMs`.
        Inheriting a server-side timestamp would report a reading taken this
        second as hours old — and the panel warns about stale readings."""
        import time
        stale = dict(SAMPLE, fetchedAtMs=1)
        got = live_limits.fetch_utilization(
            env=dict(LIVE_ENV), opener=_opener(stale))
        self.assertGreater(got["fetchedAtMs"], (time.time() - 60) * 1000)

    def test_a_live_reading_projects_like_a_cached_one(self):
        """The whole design: the live block is the SAME shape, so everything
        downstream is unchanged and unaware."""
        import account
        import limits_core
        merged, _ = live_limits.config_with_live_limits(
            {}, env=dict(LIVE_ENV), opener=_opener(SAMPLE))
        projection = account.limits_projection(merged)
        windows = limits_core.describe_windows(projection["windows"], "claude", {})
        self.assertEqual([w["key"] for w in windows], ["claude:weekly"])
        self.assertEqual(windows[0]["label"], "Weekly (all models)")
        self.assertEqual(windows[0]["percent"], 37)


if __name__ == "__main__":
    unittest.main()
