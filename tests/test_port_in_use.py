"""Tests for the busy-port path of `cli.py dashboard`.

A port already in use used to surface as socketserver's own `OSError: [Errno 48]
Address already in use` under nine frames of stdlib, printed underneath however
many lines the background scan had already emitted. It named none of the three
things a reader needs: the port, the dashboard that is usually what holds it,
and `cli.py url`, which reopens that dashboard without stopping anything at all.

The branch that matters most is the one that recommends stopping something. A
message that hands over a blind `kill` for a port it could not identify is worse
than no message, so `test_an_unidentified_holder_is_never_blind_killed` is the
load-bearing case here rather than any of the wording ones.
"""

import contextlib
import errno
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import cli
import dashboard


@contextlib.contextmanager
def occupied_port(host="127.0.0.1"):
    """Hold a real ephemeral port for the duration of the block.

    Bound to port 0 and then listened on, so the port handed out is one the OS
    has genuinely allocated — asking for a fixed number would race any other
    test, and any other program on the developer's machine.
    """
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind((host, 0))
    holder.listen(5)
    try:
        yield holder.getsockname()[1]
    finally:
        holder.close()


@contextlib.contextmanager
def temporary_url_file():
    """Point URL_FILE somewhere disposable.

    Every test here reaches `port_in_use_lines`, which reads that file to decide
    which of the three branches to take. Left alone it reads the developer's own
    ~/.claude/dashboard-url and probes whatever it names, so a developer with a
    dashboard running would take a different branch than CI does.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        with mock.patch.object(dashboard, "URL_FILE", Path(tmpdir) / "dashboard-url"):
            yield Path(tmpdir) / "dashboard-url"


def refuse_to_bind(*args, **kwargs):
    """Stand in for a taken port, deterministically and on every platform.

    A real listening socket cannot do this job everywhere: `server_bind` sets
    SO_REUSEADDR whenever the platform has it, and on Windows that permits
    binding a port another socket is already listening on, so the real bind
    would succeed there and the case would evaporate. The one real-socket test
    below is what ties this errno back to the one the OS actually raises.
    """
    raise OSError(errno.EADDRINUSE, "Address already in use")


class HealthzStub(BaseHTTPRequestHandler):
    """Answers /healthz with whatever `body` the test asked for."""

    status = 200
    body = {"service": "codex-claude-usage", "status": "ok", "version": "test"}

    def do_GET(self):
        payload = json.dumps(self.body).encode("utf-8")
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def healthz_serving(status=200, body=None):
    handler = type("Stub", (HealthzStub,), {
        "status": status,
        "body": HealthzStub.body if body is None else body,
    })
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestTheFailureIsTypedRatherThanBare(unittest.TestCase):
    @unittest.skipIf(os.name == "nt",
                     "SO_REUSEADDR lets Windows bind a port another socket is "
                     "listening on, so there is no failure to observe")
    def test_a_real_busy_port_raises_the_errno_this_code_catches(self):
        """The only test that ties ADDRESS_IN_USE_ERRNOS to the OS.

        Everything else patches the bind, which would keep passing if the set
        named an errno the platform never actually raises.
        """
        with occupied_port() as port, temporary_url_file(), \
             mock.patch.object(dashboard, "_port_answers_as_this_app", return_value=False):
            with self.assertRaises(dashboard.PortInUseError) as caught:
                dashboard.serve(host="127.0.0.1", port=port)
        self.assertEqual(caught.exception.port, port)
        self.assertEqual(caught.exception.host, "127.0.0.1")

    def test_the_first_line_alone_is_what_str_gives(self):
        """A caller that only prints the exception still says something true —
        the remedy is several lines, and `str(exc)` must not be one of the
        indented commands out of context."""
        with temporary_url_file(), \
             mock.patch.object(dashboard, "DashboardHTTPServer", refuse_to_bind), \
             mock.patch.object(dashboard, "_port_answers_as_this_app", return_value=False):
            with self.assertRaises(dashboard.PortInUseError) as caught:
                dashboard.serve(host="127.0.0.1", port=8123)
        self.assertEqual(str(caught.exception), caught.exception.lines[0])
        self.assertIn("8123", str(caught.exception))
        self.assertIn("already in use", str(caught.exception))

    def test_any_other_bind_failure_is_left_exactly_as_it_was(self):
        """Only the one condition with a remedy is translated. A privileged
        port or an address that is not ours gets no invented advice."""
        def refuse_with_permission_denied(*args, **kwargs):
            raise OSError(errno.EACCES, "Permission denied")

        with temporary_url_file(), \
             mock.patch.object(dashboard, "DashboardHTTPServer", refuse_with_permission_denied):
            with self.assertRaises(OSError) as caught:
                dashboard.serve(host="127.0.0.1", port=80)
        self.assertNotIsInstance(caught.exception, dashboard.PortInUseError)
        self.assertEqual(caught.exception.errno, errno.EACCES)


class TestWhatTheMessageRecommends(unittest.TestCase):
    def lines_for(self, port=8123, saved=None, live=None, identified=False):
        with temporary_url_file(), \
             mock.patch.object(dashboard, "read_url_file", return_value=saved), \
             mock.patch.object(dashboard, "url_is_live", return_value=live), \
             mock.patch.object(dashboard, "_port_answers_as_this_app",
                               return_value=identified):
            return dashboard.port_in_use_lines("127.0.0.1", port)

    def test_a_dashboard_whose_link_still_works_is_reopened_not_killed(self):
        """The case the traceback hid most expensively: nothing needs stopping,
        one command gets you back in."""
        saved = f"http://127.0.0.1:8123/#token={'a' * 43}"
        text = "\n".join(self.lines_for(saved=saved, live=True))
        self.assertIn("cli.py url --open", text)
        self.assertIn("already running", text)

    def test_a_link_naming_another_port_is_not_mistaken_for_this_one(self):
        """The saved link belongs to a dashboard on 9999. Recommending it here
        would send the reader to a page that has nothing to do with the port
        they could not bind."""
        saved = f"http://127.0.0.1:9999/#token={'a' * 43}"
        text = "\n".join(self.lines_for(port=8123, saved=saved, live=True))
        self.assertNotIn("cli.py url --open", text)

    def test_a_saved_link_that_no_longer_works_is_not_offered(self):
        """`url_is_live` answers None when it could not tell, and only True is
        proof. Offering `cli.py url` on an unproven link sends the reader to a
        command that will refuse them."""
        saved = f"http://127.0.0.1:8123/#token={'a' * 43}"
        for verdict in (None, False):
            with self.subTest(live=verdict):
                text = "\n".join(self.lines_for(saved=saved, live=verdict))
                self.assertNotIn("cli.py url --open", text)

    def test_an_identified_dashboard_with_no_link_says_why_it_must_be_stopped(self):
        text = "\n".join(self.lines_for(identified=True))
        self.assertIn("codex-claude-usage dashboard", text)
        self.assertIn("unreachable", text)

    def test_an_unidentified_holder_is_never_blind_killed(self):
        """The safety rule this module exists to pin.

        `_port_answers_as_this_app` returns None for a dashboard the VS Code
        extension launched (it guards /healthz with an instance token and
        answers an unauthenticated probe with 404) and for every unrelated
        program alike. Handing over a one-liner that kills whatever holds the
        port would, on that answer, kill something the reader never looked at.
        """
        text = "\n".join(self.lines_for(identified=None))
        self.assertNotIn("kill $(", text)
        self.assertIn("<PID>", text, "no way to find out what it is either")
        self.assertIn("check what it is", text)

    def test_the_direct_kill_is_offered_only_once_it_is_ours(self):
        identified = "\n".join(dashboard._stop_commands(8123, True))
        unidentified = "\n".join(dashboard._stop_commands(8123, False))
        if os.name == "posix":
            self.assertIn("kill $(", identified)
            self.assertNotIn("kill $(", unidentified)
        self.assertIn("8123", identified)
        self.assertIn("8123", unidentified)

    def test_the_suggested_port_is_one_that_can_actually_be_bound(self):
        """A suggestion that fails the same way would be worse than silence."""
        with occupied_port() as port:
            suggested = dashboard._first_free_port("127.0.0.1", port + 1)
            self.assertIsNotNone(suggested)
            self.assertNotEqual(suggested, port)
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", suggested))

    def test_ipv6_suggestions_are_probed_with_an_ipv6_socket(self):
        probe = mock.MagicMock()
        probe.__enter__.return_value = probe
        probe.__exit__.return_value = False
        with mock.patch.object(dashboard.socket, "socket", return_value=probe) as make:
            suggested = dashboard._first_free_port("::1", 8124, attempts=1)

        self.assertEqual(suggested, 8124)
        make.assert_called_once_with(socket.AF_INET6, socket.SOCK_STREAM)
        probe.bind.assert_called_once_with(("::1", 8124))

    def test_no_port_is_suggested_when_none_could_be_found(self):
        """Rather than naming one that will fail: `_first_free_port` returns
        None for a host it cannot bind at all, and the line has to disappear."""
        with temporary_url_file(), \
             mock.patch.object(dashboard, "read_url_file", return_value=None), \
             mock.patch.object(dashboard, "_port_answers_as_this_app", return_value=False), \
             mock.patch.object(dashboard, "_first_free_port", return_value=None):
            text = "\n".join(dashboard.port_in_use_lines("127.0.0.1", 8123))
        self.assertNotIn("--port", text)


class TestIdentifyingTheHolder(unittest.TestCase):
    def test_neither_probe_follows_a_redirect_from_a_reclaimed_port(self):
        redirected = []

        class RedirectingPeer(HealthzStub):
            def do_GET(self):
                if self.path == "/redirected":
                    redirected.append(self.path)
                    super().do_GET()
                else:
                    self.send_response(302)
                    self.send_header(
                        "Location",
                        f"http://localhost:{self.server.server_port}/redirected",
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), RedirectingPeer)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_port
            url = dashboard.authenticated_dashboard_url("127.0.0.1", port)
            for probe in (
                    lambda: dashboard.url_is_live(url),
                    lambda: dashboard._port_answers_as_this_app("127.0.0.1", port)):
                with self.subTest(probe=probe):
                    self.assertIsNone(probe())
                    self.assertEqual(redirected, [], "the probe followed Location")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_our_own_marker_is_the_only_true(self):
        with healthz_serving() as port:
            self.assertIs(dashboard._port_answers_as_this_app("127.0.0.1", port), True)

    def test_another_program_answering_json_is_false(self):
        with healthz_serving(body={"service": "something-else"}) as port:
            self.assertIs(dashboard._port_answers_as_this_app("127.0.0.1", port), False)

    def test_a_404_is_unsettled_rather_than_foreign(self):
        """A dashboard launched by the VS Code extension answers exactly this
        to an unauthenticated probe, and so does any unrelated program. Calling
        it False would let the message claim the holder is not ours when it may
        well be — which is what licenses the blind kill."""
        with healthz_serving(status=404, body={"error": "Not found"}) as port:
            self.assertIsNone(dashboard._port_answers_as_this_app("127.0.0.1", port))

    def test_nothing_listening_is_unsettled_too(self):
        with occupied_port() as free_after_close:
            pass
        self.assertIsNone(
            dashboard._port_answers_as_this_app("127.0.0.1", free_after_close, timeout=0.5))

    def test_a_health_body_cannot_renew_the_total_probe_deadline(self):
        started = threading.Event()
        release = threading.Event()
        closed = threading.Event()

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                closed.set()
                return False

            def read(self, _amount=-1):
                started.set()
                release.wait(5)
                return b'{}'

        began = time.monotonic()
        try:
            with mock.patch.object(dashboard, "open_loopback_probe", return_value=Response()):
                self.assertIsNone(dashboard._port_answers_as_this_app(
                    "127.0.0.1", 8123, timeout=0.05))
            self.assertTrue(started.wait(1), "the fixture never reached the body")
            self.assertLess(time.monotonic() - began, 1)
        finally:
            release.set()
        self.assertTrue(closed.wait(1), "the released response was not closed")


class TestTheCommandLineTranslatesIt(unittest.TestCase):
    def run_main(self, argv):
        """Drive `cli.main()` at a port that refuses to bind.

        Patch PROJECTS_DIRS and SURFACE because serve sets them before
        attempting to bind. A failing bind must not leave real transcript
        roots active for a later rescan test. Restore both globals so test
        order cannot change the fixture."""
        buffer = io.StringIO()
        with temporary_url_file(), \
             mock.patch.object(cli, "cmd_scan") as scan, \
             mock.patch.object(dashboard, "DashboardHTTPServer", refuse_to_bind), \
             mock.patch.object(dashboard, "_port_answers_as_this_app", return_value=False), \
             mock.patch.object(dashboard, "PROJECTS_DIRS", dashboard.PROJECTS_DIRS), \
             mock.patch.object(dashboard, "SURFACE", dashboard.SURFACE), \
             mock.patch.object(cli.sys, "argv", ["cli.py"] + argv), \
             mock.patch("threading.Thread") as thread, \
             redirect_stdout(buffer), redirect_stderr(buffer):
            os.environ.pop("HOST", None)
            os.environ.pop("PORT", None)
            try:
                cli.main()
                code = 0
            except SystemExit as exit_code:
                code = exit_code.code or 0
        return code, buffer.getvalue(), scan, thread

    def test_it_prints_the_remedy_and_exits_1(self):
        code, output, _, _ = self.run_main(["dashboard", "--port", "8123", "--no-browser"])
        self.assertEqual(code, 1)
        self.assertIn("8123", output)
        self.assertIn("already in use", output)
        self.assertNotIn("Traceback", output)
        self.assertGreater(len(output.strip().splitlines()), 1,
                           "a busy port gets a remedy, not the one-liner a typo gets")

    def test_the_remedy_survives_as_separate_lines(self):
        """`terminal_safe` escapes Cc and a newline is Cc, so routing this
        through it — as every other translated error is — would fold the whole
        thing onto one line as `\\x0a`."""
        _, output, _, _ = self.run_main(["dashboard", "--port", "8123", "--no-browser"])
        self.assertNotIn("\\x0a", output)

    def test_nothing_scans_when_the_port_could_not_be_taken(self):
        """The ordering half of the fix. The scan thread used to start before
        the bind, so a cold walk of ~/.claude/projects ran — and printed over
        the failure — on a process that was about to exit."""
        _, _, scan, thread = self.run_main(["dashboard", "--port", "8123", "--no-browser"])
        scan.assert_not_called()
        thread.assert_not_called()


class TestTheSecondWayInAlsoScans(unittest.TestCase):
    """`python dashboard.py` must ingest, not just serve.

    This is the same `on_ready` contract as the class above, on the other entry
    point, and it is here because getting it wrong is not a missing nicety —
    it was **the one route to `/api/data` with no scan behind it**. Payload
    correctness no longer relies on that timing: `_collect_dashboard_data`
    carries the file-level `unscanned` state after admission. The second entry
    point still needs a scan so that state can become real usage without a
    separate operator action.

    Reproduced before the fix, against a seeded database on a schema this build
    does not write: `python dashboard.py`, then one authenticated
    `GET /api/data` — the stored turn was dropped by the rebuild, the response
    came back with `daily_by_model: []`, `sessions_all: []`, `all_models: []`
    and **no `error` field at all**, and nothing ever scanned. A page renders
    that as a complete, correct dashboard of an empty history. After the fix the
    same sequence answers with real rows.

    Independently of the rebuild, that entry point never ingested anything, so
    its dashboard was frozen at whatever the last `cli.py scan` had left.
    """

    def _main_block(self):
        """The `if __name__ == "__main__":` body of dashboard.py, parsed.

        Read structurally rather than executed: importing a module does not run
        its `__main__` guard, and running `dashboard.py` as a subprocess to
        observe the difference would bind a real port and walk the developer's
        real `~/.claude/projects`.
        """
        import ast
        source = (Path(__file__).resolve().parent.parent / "codex_claude_usage" /
                  "dashboard.py").read_text(encoding="utf-8")
        for node in ast.parse(source).body:
            if (isinstance(node, ast.If)
                    and isinstance(node.test, ast.Compare)
                    and isinstance(node.test.left, ast.Name)
                    and node.test.left.id == "__name__"):
                return node
        self.fail("dashboard.py has no `if __name__ == '__main__':` block")

    def _run_main_block(self, threading_module):
        """Execute that block with `serve` and `threading` substituted.

        Compiled from the same AST `_main_block` returns and run in a copy of
        `dashboard`'s own globals, so the lambda resolves `_background_scan` and
        `threading` out of the namespace handed in here. Nothing binds a port
        and nothing scans: `serve` only records what it was called with.

        `sys` is substituted too, with an argv of exactly one element. The
        block refuses a non-empty `sys.argv[1:]` -- `python dashboard.py` parses
        nothing, so accepting `--port 9000` and discarding it was a silent drop
        at exit 0 -- and without this the block would read the TEST RUNNER's
        argv and exit before reaching `serve`.

        Returns the recorded keyword arguments.
        """
        import ast
        import types
        import dashboard
        recorded = {}
        namespace = dict(vars(dashboard))
        namespace["serve"] = lambda *args, **kwargs: recorded.update(kwargs)
        namespace["threading"] = threading_module
        stub_sys = types.SimpleNamespace(
            argv=["dashboard.py"], exit=sys.exit, stderr=sys.stderr)
        namespace["sys"] = stub_sys
        module = ast.Module(body=self._main_block().body, type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, "<dashboard.py __main__>", "exec"), namespace)
        return recorded

    def test_the_entry_point_hands_serve_an_on_ready(self):
        import ast
        calls = [n for n in ast.walk(self._main_block())
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "serve"]
        self.assertEqual(len(calls), 1, "expected exactly one serve() call")
        self.assertIn("on_ready", [kw.arg for kw in calls[0].keywords],
                      "`python dashboard.py` serves without ever scanning: "
                      "serve() defaults on_ready to None, so /api/data is "
                      "answered off whatever the last scan happened to leave — "
                      "including nothing at all, after a schema rebuild")

    def test_the_on_ready_actually_starts_a_scan_thread(self):
        """The assertion above is a name check and cannot tell the wiring from
        `on_ready=lambda: None`, or from a `threading.Thread(...)` whose
        `.start()` has been dropped — a one-token slip that builds the thread
        object and discards it. Both leave this entry point serving without
        ever ingesting, which is the whole defect the class exists for, so run
        the callback and look at what it does.
        """
        import dashboard
        threading_stub = mock.MagicMock()
        with mock.patch.object(dashboard, "_background_scan") as background_scan:
            recorded = self._run_main_block(threading_stub)
            self.assertIn("on_ready", recorded)
            recorded["on_ready"]()
            threading_stub.Thread.assert_called_once_with(
                target=mock.ANY, daemon=True)
            threading_stub.Thread.return_value.start.assert_called_once_with()

            # The factory sees a lifecycle wrapper rather than the scan itself.
            # Run it explicitly because this MagicMock thread does not: that
            # proves the wiring still reaches ingestion and balances the shared
            # activity counter for every later test in this process.
            tracked_target = threading_stub.Thread.call_args.kwargs["target"]
            tracked_target()
            background_scan.assert_called_once_with()
        self.assertEqual(dashboard.scan_status()["state"], "idle")

    def test_the_background_scan_targets_the_configured_database(self):
        """Not the module default. `DB_PATH` is read at call time so a patched
        one — which is how every test and `CODEX_CLAUDE_USAGE_DB` reach it — is what
        gets scanned."""
        import dashboard
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "usage.db"
            roots = [Path(tmp) / "projects"]
            with mock.patch.object(dashboard, "DB_PATH", db_path), \
                    mock.patch.object(dashboard, "PROJECTS_DIRS", roots), \
                    mock.patch("scanner.scan") as scan, \
                    redirect_stdout(io.StringIO()):
                dashboard._background_scan()
        scan.assert_called_once()
        self.assertEqual(scan.call_args.kwargs["db_path"], db_path)
        self.assertEqual(scan.call_args.kwargs["projects_dirs"], roots)

    def test_a_failing_scan_does_not_take_the_server_down_silently(self):
        """It runs on a daemon thread, so an escaping exception would end
        ingestion for the life of the process with nothing printed."""
        import dashboard
        buf = io.StringIO()
        with mock.patch("scanner.scan", side_effect=RuntimeError("disk gone")), \
                mock.patch.object(dashboard, "invalidate_payload_cache") as invalidate, \
                redirect_stdout(buf):
            dashboard._background_scan()
        self.assertIn("Background scan failed", buf.getvalue())
        self.assertIn("disk gone", buf.getvalue())
        invalidate.assert_called_once_with(dashboard.DB_PATH)



class TestTheSecondWayInRejectsArgumentsItCannotHonour(unittest.TestCase):
    """`python dashboard.py` parses nothing, so it must not accept anything.

    It took `--port 9000` and discarded it — the server listened on `PORT` or
    8080 and exited 0, so a reader who asked for one port and got another had
    nothing at all to read. Reproduced: `PORT=A python dashboard.py --port B`
    left B refused and A listening, with both streams empty.

    That is the silent-drop-at-exit-0 class `cli.validate_flags` exists to
    remove, and this entry point stopped being exempt from it when it was
    promoted to a real one by being given a background scan.

    Structural rather than a subprocess: running it for real binds a port and
    walks the developer's transcripts, and what is asserted here is that the
    guard exists and precedes `serve`.
    """

    def _main_block(self):
        import ast
        source = (Path(__file__).resolve().parent.parent / "codex_claude_usage" /
                  "dashboard.py").read_text(encoding="utf-8")
        for node in ast.parse(source).body:
            if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                    and isinstance(node.test.left, ast.Name)
                    and node.test.left.id == "__name__"):
                return node
        self.fail("dashboard.py has no `if __name__ == '__main__':` block")

    def test_it_refuses_argv_before_it_serves(self):
        import ast
        block = self._main_block()
        guard = exits = serves = None
        for i, node in enumerate(block.body):
            if isinstance(node, ast.If) and "argv" in ast.dump(node.test):
                guard = i
                exits = any(isinstance(n, ast.Call) and getattr(n.func, "attr", "")
                            == "exit" for n in ast.walk(node))
            if any(isinstance(n, ast.Name) and n.id == "serve"
                   for n in ast.walk(node)):
                serves = i if serves is None else serves
        self.assertIsNotNone(guard, "nothing checks sys.argv[1:]")
        self.assertTrue(exits, "the guard does not exit")
        self.assertLess(guard, serves, "the check must precede the bind")

    def test_the_message_points_at_the_entry_point_that_does_parse(self):
        source = (Path(__file__).resolve().parent.parent / "codex_claude_usage" /
                  "dashboard.py").read_text(encoding="utf-8")
        tail = source[source.index('if __name__ == "__main__":'):]
        self.assertIn("cli.py dashboard", tail)
        self.assertIn("file=sys.stderr", tail)


if __name__ == "__main__":
    unittest.main()
