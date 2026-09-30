"""The standalone quota backend: window identity, thresholds, and its server.

Three things are being pinned, and they are the three that make the feature
work rather than merely exist.

**Identity is derived, never enumerated.** A limit kind nobody has seen must get
a key, a label and a threshold with no code change — that is the whole request
this was built for, and the reason a `five_hour` special case is a defect.

Thresholds are stored per window on disk and shared between front ends. A
stale cache can omit a window without deleting its configured threshold.

**The small server is genuinely small.** If it imports the dashboard it has not
achieved anything, so that is asserted rather than assumed.
"""

import errno
import http.client
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import limits_core
import limits_server
import dashboard
from claude_usage import loopback_http
from safetext import terminal_safe


class _Thresholds(unittest.TestCase):
    """Every test gets its own threshold file: these write to disk, and the
    default location is the developer's real `~/.claude`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._env = mock.patch.dict(
            os.environ, {limits_core.THRESHOLDS_ENV: str(self.tmp / "t.json")})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_the_real_home_is_not_what_we_are_writing(self):
        """Asserted rather than assumed. Nothing else in this file would fail
        if the redirection were dropped — it would just start rewriting the
        developer's own alert settings."""
        self.assertEqual(limits_core.thresholds_path().parent, self.tmp)


class TestAWindowNamesItself(unittest.TestCase):
    """Keys come from the window, so an unknown kind is still addressable."""

    def test_the_two_windows_a_real_cache_reports(self):
        session = {"kind": "session", "group": "session", "scope": ""}
        fable = {"kind": "weekly_scoped", "group": "weekly", "scope": "Fable"}
        self.assertEqual(limits_core.window_key(session), "claude:session")
        self.assertEqual(limits_core.window_key(fable),
                         "claude:weekly_scoped:Fable")

    def test_a_kind_that_does_not_exist_yet_still_gets_a_key_and_a_label(self):
        """The request in one test: a weekly all-models limit appeared in
        Claude Code that this tool had never seen. It must need no code."""
        weekly = {"kind": "weekly", "group": "weekly", "scope": ""}
        self.assertEqual(limits_core.window_key(weekly), "claude:weekly")
        self.assertEqual(limits_core.window_label(weekly), "Weekly (all models)")

        invented = {"kind": "monthly_burst", "group": "monthly", "scope": "Sonnet"}
        self.assertEqual(limits_core.window_key(invented),
                         "claude:monthly_burst:Sonnet")
        self.assertEqual(limits_core.window_label(invented),
                         "Monthly Burst — Sonnet")

    def test_the_two_assistants_cannot_collide(self):
        """Both publish a `weekly`, and they are not the same window. Without
        the source in the key, a Codex threshold would govern a Claude limit."""
        weekly = {"kind": "weekly"}
        self.assertNotEqual(limits_core.window_key(weekly, "claude"),
                            limits_core.window_key(weekly, "codex"))

    def test_a_scope_containing_the_separator_cannot_forge_a_key(self):
        forged = {"kind": "weekly", "scope": "x:y"}
        plain = {"kind": "weekly", "scope": "x"}
        self.assertNotEqual(limits_core.window_key(forged),
                            limits_core.window_key(plain) + ":y")

    def test_colon_and_underscore_components_have_distinct_keys(self):
        colon = {"kind": "weekly", "scope": "a:b"}
        underscore = {"kind": "weekly", "scope": "a_b"}
        self.assertNotEqual(limits_core.window_key(colon),
                            limits_core.window_key(underscore))
        self.assertIn("%3A", limits_core.window_key(colon))

    def test_discriminator_and_scope_positions_cannot_collide(self):
        discriminator = {"kind": "weekly", "scope_discriminator": "same"}
        scope = {"kind": "weekly", "scope": "same"}
        forged_marker = {"kind": "weekly", "scope": "~d=same"}
        keys = {
            limits_core.window_key(discriminator),
            limits_core.window_key(scope),
            limits_core.window_key(forged_marker),
        }
        self.assertEqual(len(keys), 3)
        self.assertIn(":~d=same", limits_core.window_key(discriminator))
        self.assertIn(":%7Ed=same", limits_core.window_key(forged_marker))

    def test_digest_shortening_keeps_long_common_prefixes_distinct(self):
        first = {"kind": "k" * 115, "scope": "first"}
        second = {"kind": "k" * 115, "scope": "second"}
        old_first = limits_core.legacy_window_key(first)
        old_second = limits_core.legacy_window_key(second)
        self.assertEqual(old_first, old_second,
                         "fixture must collide under the legacy truncation")
        new_first = limits_core.window_key(first)
        new_second = limits_core.window_key(second)
        self.assertLessEqual(len(new_first), limits_core.MAX_KEY_LENGTH)
        self.assertLessEqual(len(new_second), limits_core.MAX_KEY_LENGTH)
        self.assertNotEqual(new_first, new_second)

    def test_an_unambiguous_legacy_key_is_read_compatibly(self):
        window = {"kind": "weekly", "scope": "a:b"}
        legacy = limits_core.legacy_window_key(window)
        new = limits_core.window_key(window)
        stored = {legacy: [30]}
        described = limits_core.describe_windows([window], stored=stored)
        self.assertEqual(described[0]["thresholds"], [30])
        live = limits_core.live_threshold_keys([window])
        self.assertEqual(live, {new, legacy})
        self.assertEqual(limits_core.orphaned_thresholds(
            stored, live, source="claude"), {})

    def test_ordinary_legacy_keys_remain_the_exact_current_identity(self):
        for window in (
            {"kind": "session"},
            {"kind": "weekly_scoped", "scope": "Fable"},
            {"kind": "10080m", "scope": ""},
        ):
            with self.subTest(window=window):
                self.assertEqual(
                    limits_core.window_key(window),
                    limits_core.legacy_window_key(window),
                )

    def test_legacy_collision_does_not_merge_two_distinct_windows(self):
        colon = {"kind": "weekly", "scope": "a:b"}
        underscore = {"kind": "weekly", "scope": "a_b"}
        legacy = limits_core.legacy_window_key(colon)
        stored = {legacy: [30]}
        described = limits_core.describe_windows([colon, underscore], stored=stored)
        self.assertEqual(described[0]["thresholds"], [80])
        # The exact new key of the underscore window is the ambiguous legacy
        # spelling; it must not inherit the colon window's setting by accident.
        self.assertEqual(described[1]["thresholds"], [30])
        long_first = {"kind": "k" * 115, "scope": "first"}
        long_second = {"kind": "k" * 115, "scope": "second"}
        shared_legacy = limits_core.legacy_window_key(long_first)
        live = limits_core.live_threshold_keys([long_first, long_second])
        self.assertNotIn(shared_legacy, live,
                         "a colliding legacy alias must remain orphaned")
        self.assertEqual(limits_core.orphaned_thresholds(
            {shared_legacy: [30]}, live, source="claude"),
                         {shared_legacy: [30]})

    def test_numeric_duration_kinds_are_named_in_words(self):
        weekly = {"kind": "10080m", "group": "10080m", "scope": ""}
        self.assertEqual(limits_core.window_label(weekly), "Weekly")

    def test_truncating_a_key_cannot_leave_storage_and_lookup_disagreeing(self):
        window = {"kind": "k" * 64, "scope": "s" * 47 + " " + "tail"}
        key = limits_core.window_key(window)
        self.assertEqual(key, key.strip())
        stored = limits_core.normalize_thresholds({key: [30]})
        self.assertEqual(limits_core.thresholds_for(key, stored), [30])

    def test_a_window_with_no_kind_at_all_is_refused_not_guessed(self):
        self.assertEqual(limits_core.window_key({}), "")
        self.assertEqual(limits_core.window_key({"scope": "Fable"}), "")
        self.assertEqual(limits_core.window_key("not a dict"), "")

    def test_nothing_here_enumerates_the_known_kinds(self):
        """Structural. `window_key` must not grow a list of kinds — the moment
        it does, a new limit stops being addressable, which is the defect this
        module exists to prevent. `window_label` MAY name kinds, because it
        only chooses nicer words and falls through to a titled default."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(limits_core.window_key).strip())
        function = tree.body[0]
        # The DOCSTRING is stripped before the check, and deliberately: it
        # names several kinds in order to explain why `group` is not part of
        # the key, and that explanation is worth keeping. The claim being
        # asserted is about executable behaviour, so it is asserted about the
        # executable half.
        body = function.body[1:] if (
            isinstance(function.body[0], ast.Expr)
            and isinstance(function.body[0].value, ast.Constant)
            and isinstance(function.body[0].value.value, str)) else function.body
        literals = [n.value for n in ast.walk(ast.Module(body=body, type_ignores=[]))
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        for kind in ("five_hour", "seven_day", "weekly_scoped", "session"):
            self.assertNotIn(
                kind, literals,
                f"window_key special-cases {kind!r} in code; a kind it does "
                f"not name would then be unaddressable")


class TestTheTwoHttpFrontEndsShareTheirSecurityMechanics(unittest.TestCase):
    """Both servers route lifecycle/body/write mechanics through one owner."""

    def test_handlers_and_servers_use_the_shared_implementations(self):
        self.assertTrue(issubclass(
            dashboard.DashboardHandler,
            loopback_http.LoopbackRequestHandlerMixin))
        self.assertTrue(issubclass(
            limits_server.LimitsHandler,
            loopback_http.LoopbackRequestHandlerMixin))
        self.assertTrue(issubclass(
            dashboard.DashboardHTTPServer,
            loopback_http.LoopbackHTTPServer))
        self.assertTrue(issubclass(
            limits_server.LimitsHTTPServer,
            loopback_http.LoopbackHTTPServer))
        self.assertIs(dashboard.read_bounded_json_body,
                      loopback_http.read_bounded_json_body)
        self.assertIs(limits_server.read_bounded_json_body,
                      loopback_http.read_bounded_json_body)
        self.assertIs(dashboard.apply_threshold_write,
                      loopback_http.apply_threshold_write)
        self.assertIs(limits_server.apply_threshold_write,
                      loopback_http.apply_threshold_write)

    def test_put_and_patch_each_traverse_the_shared_write_flow(self):
        for module, handler_class, sender in (
                (dashboard, dashboard.DashboardHandler, "_send_json"),
                (limits_server, limits_server.LimitsHandler, "_send")):
            for method in ("do_PUT", "do_PATCH"):
                with self.subTest(module=module.__name__, method=method):
                    handler = object.__new__(handler_class)
                    setattr(handler, "_prepare_threshold_write", lambda: True)
                    setattr(handler, "_threshold_write_body",
                            lambda: (True, {"thresholds": {"k": [30]}}))
                    sent = mock.Mock()
                    setattr(handler, sender, sent)
                    with mock.patch.object(module, "apply_threshold_write") as shared:
                        getattr(handler_class, method)(handler)
                    shared.assert_called_once()


class TestThresholdsAreValidatedOnTheWayIn(_Thresholds):
    def test_only_usable_percentages_survive(self):
        got = limits_core.normalize_thresholds(
            {"k": [0, 1, 50, 100, 101, -5, True, "80", None, 2.7]})
        self.assertEqual(got, {"k": [1, 2, 50, 100]})

    def test_a_file_written_by_hand_cannot_crash_a_server_thread(self):
        for junk in ("[]", "null", '"text"', "{", '{"k": "not a list"}', ""):
            with self.subTest(content=junk):
                limits_core.thresholds_path().write_text(junk, encoding="utf-8")
                self.assertEqual(limits_core.read_thresholds(), {})

    def test_a_missing_file_is_the_ordinary_first_run(self):
        self.assertFalse(limits_core.thresholds_path().exists())
        self.assertEqual(limits_core.read_thresholds(), {})

    def test_the_number_of_tracked_windows_is_bounded(self):
        huge = {f"claude:k{i}": [50] for i in range(limits_core.MAX_TRACKED_WINDOWS * 3)}
        self.assertLessEqual(len(limits_core.normalize_thresholds(huge)),
                             limits_core.MAX_TRACKED_WINDOWS)

    def test_a_missing_key_uses_the_default_and_empty_disables_it(self):
        self.assertEqual(limits_core.thresholds_for("claude:session", {}), [80])
        limits_core.write_thresholds({"claude:session": []})
        stored = limits_core.read_thresholds()
        self.assertEqual(stored, {"claude:session": []})
        self.assertEqual(limits_core.thresholds_for("claude:session", stored), [])

    def test_an_oversized_file_is_refused_before_json_parsing(self):
        """The payload must PARSE TO SOMETHING, or the test cannot fail.

        This wrote `b"{" + 65536 spaces + b"}"` and asserted `== {}` -- which
        is what `json.loads` returns with or without a size bound, so it passed
        against the naive pre-fix reader too. Removing the whole bound (fstat
        gate, read cap and post-read length check) left the full suite green.
        The payload below is oversized AND holds real keys, so an unbounded
        reader returns them and the assertion fails.
        """
        keys = {f"claude:w{n}": [50] for n in range(4000)}
        blob = json.dumps(keys).encode("utf-8")
        self.assertGreater(len(blob), limits_core.MAX_THRESHOLD_FILE_BYTES,
                           "fixture is not actually oversized")
        limits_core.thresholds_path().write_bytes(blob)
        self.assertEqual(limits_core.read_thresholds(), {})

    @unittest.skipUnless(os.name == "posix", "FIFO semantics are POSIX")
    def test_a_fifo_cannot_block_the_reader(self):
        os.mkfifo(limits_core.thresholds_path())
        started = time.monotonic()
        self.assertEqual(limits_core.read_thresholds(), {})
        self.assertLess(time.monotonic() - started, 1)

    @unittest.skipUnless(os.name == "posix" and Path("/dev/zero").exists(),
                         "device and symlink semantics are POSIX")
    def test_a_symlink_to_a_device_is_refused_without_reading_it(self):
        limits_core.thresholds_path().symlink_to("/dev/zero")
        self.assertEqual(limits_core.read_thresholds(), {})


class TestThresholdsRoundTripAndAreOwnerOnly(_Thresholds):
    def test_written_then_read_is_what_was_stored(self):
        limits_core.write_thresholds({"claude:session": [30, 80],
                                      "claude:weekly": [4]})
        self.assertEqual(limits_core.read_thresholds(),
                         {"claude:session": [30, 80], "claude:weekly": [4]})

    def test_a_patch_preserves_unrelated_windows(self):
        limits_core.write_thresholds({"claude:session": [30],
                                      "codex:weekly": [70]})
        stored = limits_core.update_thresholds({"claude:weekly": [90]})
        self.assertEqual(stored, {
            "claude:session": [30],
            "claude:weekly": [90],
            "codex:weekly": [70],
        })
        self.assertEqual(limits_core.read_thresholds(), stored)

    def test_concurrent_patches_share_the_read_modify_write_lock(self):
        """Both readers must not observe the same stale map.

        The first read pauses after taking its snapshot. Without the lock around
        the complete transaction, the second thread reads that same snapshot,
        writes its key, and the first then overwrites it. With the lock, the
        second reader cannot enter until the first update is durable.
        """
        first_read = threading.Event()
        release_first = threading.Event()
        second_read = threading.Event()
        real_read = limits_core.read_thresholds
        reads = 0
        reads_lock = threading.Lock()

        def gated_read(path=None):
            nonlocal reads
            snapshot = real_read(path)
            with reads_lock:
                reads += 1
                number = reads
            if number == 1:
                first_read.set()
                release_first.wait(5)
            else:
                second_read.set()
            return snapshot

        results = []
        with mock.patch.object(limits_core, "read_thresholds", gated_read):
            first = threading.Thread(
                target=lambda: results.append(
                    limits_core.update_thresholds({"claude:session": [30]})))
            second = threading.Thread(
                target=lambda: results.append(
                    limits_core.update_thresholds({"claude:weekly": [90]})))
            first.start()
            self.assertTrue(first_read.wait(5), "the first patch never read")
            second.start()
            try:
                self.assertFalse(
                    second_read.wait(0.1),
                    "a second patch read while the first transaction was open")
            finally:
                release_first.set()
            first.join(5)
            second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(limits_core.read_thresholds(), {
            "claude:session": [30], "claude:weekly": [90]})
        self.assertEqual(len(results), 2)

    @unittest.skipUnless(os.name == "posix", "POSIX inode locking only")
    def test_a_replaced_lock_sidecar_cannot_split_the_transaction_lock(self):
        target = limits_core.thresholds_path()
        lock_path = target.with_name(f".{target.name}.lock")
        first_waiting = threading.Event()
        release_first = threading.Event()
        call_lock = threading.Lock()
        exclusive_calls = 0
        real_flock = limits_core.fcntl.flock

        def pause_first_exclusive(handle, operation):
            nonlocal exclusive_calls
            if operation & limits_core.fcntl.LOCK_EX:
                with call_lock:
                    exclusive_calls += 1
                    first = exclusive_calls == 1
                if first:
                    first_waiting.set()
                    release_first.wait(5)
            return real_flock(handle, operation)

        results = {}

        def acquire(name):
            with limits_core._threshold_file_lock(target) as locked:
                results[name] = locked

        with mock.patch.object(limits_core.fcntl, "flock",
                               pause_first_exclusive):
            first = threading.Thread(target=acquire, args=("first",))
            first.start()
            self.assertTrue(first_waiting.wait(5),
                            "the first opener never reached its native lock")
            lock_path.unlink()

            second = threading.Thread(target=acquire, args=("second",))
            second.start()
            second.join(5)
            self.assertFalse(second.is_alive())
            release_first.set()
            first.join(5)

        self.assertFalse(first.is_alive())
        self.assertTrue(results["second"])
        self.assertFalse(results["first"],
                         "the stale descriptor acquired a second lock domain")

    @unittest.skipUnless(os.name == "posix", "POSIX inode locking only")
    def test_the_directory_lock_remains_compatible_with_sidecar_only_writers(self):
        """A rolling upgrade must serialize with the previous lock protocol."""
        target = limits_core.thresholds_path()
        self.assertEqual(
            limits_core.write_thresholds({"claude:session": [30]}),
            {"claude:session": [30]},
        )
        lock_path = target.with_name(f".{target.name}.lock")
        handle = os.open(lock_path, os.O_RDWR)
        limits_core.fcntl.flock(handle, limits_core.fcntl.LOCK_EX)
        finished = threading.Event()
        result = {}

        def update():
            result["value"] = limits_core.update_thresholds(
                {"claude:weekly": [90]})
            finished.set()

        writer = threading.Thread(target=update)
        writer.start()
        try:
            self.assertFalse(
                finished.wait(0.1),
                "the new writer ignored a previous-release sidecar lock",
            )
            # Model the old process's read-modify-write while it still owns the
            # sidecar. The waiting new process must read this state afterwards.
            self.assertEqual(
                limits_core._write_thresholds_unlocked(
                    {"claude:session": [40]}, target),
                {"claude:session": [40]},
            )
        finally:
            limits_core.fcntl.flock(handle, limits_core.fcntl.LOCK_UN)
            os.close(handle)
        writer.join(5)

        self.assertFalse(writer.is_alive())
        self.assertEqual(result["value"], {
            "claude:session": [40], "claude:weekly": [90]})
        self.assertEqual(limits_core.read_thresholds(target), result["value"])

    @unittest.skipUnless(os.name == "posix", "POSIX inode locking only")
    def test_replacement_after_validation_still_has_one_lock_domain(self):
        target = limits_core.thresholds_path()
        lock_path = target.with_name(f".{target.name}.lock")
        first_validated = threading.Event()
        release_first = threading.Event()
        second_done = threading.Event()
        real_file_info = limits_core._threshold_lock_file_info
        call_lock = threading.Lock()
        calls = 0

        def pause_after_first_post_check(handle, path):
            nonlocal calls
            result = real_file_info(handle, path)
            with call_lock:
                calls += 1
                pause = calls == 2
            if pause:
                first_validated.set()
                release_first.wait(5)
            return result

        results = {}

        def acquire(name, done=None):
            with limits_core._threshold_file_lock(target) as locked:
                results[name] = locked
            if done is not None:
                done.set()

        with mock.patch.object(limits_core, "_threshold_lock_file_info",
                               pause_after_first_post_check):
            first = threading.Thread(target=acquire, args=("first",))
            first.start()
            self.assertTrue(first_validated.wait(5))
            lock_path.unlink()

            second = threading.Thread(
                target=acquire, args=("second", second_done))
            second.start()
            self.assertFalse(
                second_done.wait(0.1),
                "replacing the sidecar created a concurrent lock domain",
            )
            release_first.set()
            first.join(5)
            second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(results["first"])
        self.assertTrue(results["second"])

    def test_a_patch_waits_for_another_process_transaction(self):
        path = limits_core.thresholds_path()
        limits_core.write_thresholds({"claude:session": [30]})
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "import limits_core\n"
            "from pathlib import Path\n"
            "with limits_core._threshold_file_lock(Path(%r)) as locked:\n"
            " print('locked' if locked else 'failed', flush=True)\n"
            " sys.stdin.readline()\n" % (str(REPO_ROOT), str(path))
        )
        child = subprocess.Popen(
            [sys.executable, "-c", code], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8")

        def cleanup_child():
            if child.poll() is None:
                child.kill()
            child.wait()
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()

        self.addCleanup(cleanup_child)
        self.assertEqual(child.stdout.readline().strip(), "locked",
                         child.stderr.read() if child.poll() is not None else "")

        done = threading.Event()
        result = []

        def patch():
            result.append(limits_core.update_thresholds(
                {"claude:weekly": [90]}))
            done.set()

        worker = threading.Thread(target=patch)
        worker.start()
        try:
            self.assertFalse(done.wait(0.1),
                             "the patch ignored another process's lock")
            child.stdin.write("release\n")
            child.stdin.flush()
            self.assertEqual(child.wait(5), 0, child.stderr.read())
            self.assertTrue(done.wait(5), "the patch did not resume")
        finally:
            if child.poll() is None:
                child.kill()
            worker.join(5)
        self.assertEqual(result, [{"claude:session": [30],
                                   "claude:weekly": [90]}])

    def test_an_invalid_patch_is_refused_without_changing_the_file(self):
        old = {"claude:session": [30]}
        limits_core.write_thresholds(old)
        self.assertIsNone(limits_core.update_thresholds({" ": [90]}))
        self.assertIsNone(limits_core.update_thresholds({}))
        self.assertEqual(limits_core.read_thresholds(), old)

    @unittest.skipUnless(os.name == "posix", "POSIX modes only")
    def test_the_file_is_owner_only(self):
        limits_core.write_thresholds({"claude:session": [30]})
        target = limits_core.thresholds_path()
        lock = target.with_name(f".{target.name}.lock")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == "posix", "symlinks need no elevation here")
    def test_a_symlinked_target_is_refused_rather_than_followed(self):
        victim = self.tmp / "victim.json"
        victim.write_text("keep me", encoding="utf-8")
        link = self.tmp / "t.json"
        link.symlink_to(victim)
        result = limits_core.write_thresholds({"claude:session": [30]})
        self.assertIsNone(result)
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep me",
                         "the write followed a symlink out of its own file")

    @unittest.skipUnless(os.name == "posix", "hard links")
    def test_a_hard_linked_target_is_refused(self):
        """`O_NOFOLLOW` does not see a hard link — `st_nlink != 1` is the only
        thing that does. Same refusal `write_url_file` gives the token file."""
        victim = self.tmp / "victim.json"
        victim.write_text("keep me", encoding="utf-8")
        os.link(victim, self.tmp / "t.json")
        result = limits_core.write_thresholds({"claude:session": [30]})
        self.assertIsNone(result)
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep me")

    def test_a_hard_linked_target_is_refused_on_the_READ_path_too(self):
        """The write path was tested; the read path was not, and they are
        separate guards.

        `write_thresholds` refuses through `_is_safe_target`; `read_thresholds`
        refuses through its own `st_nlink != 1` conjuncts. Measured before this
        test existed: neutralising BOTH read-path conjuncts made
        `read_thresholds` return the victim's contents through a hard link, and
        the FULL suite still reported `Ran 2110 tests ... OK`. Nothing in the
        repository was sensitive to it -- `grep -rn 'os\\.link(' tests/` found
        five call sites and not one of them hard-linked a thresholds file for
        READING.

        A control asserts the refusal is not vacuous: the identical content at
        a single-link path reads back.
        """
        victim = self.tmp / "victim.json"
        victim.write_text('{"claude:session": [30]}', encoding="utf-8")
        os.link(victim, limits_core.thresholds_path())
        self.assertEqual(os.stat(victim).st_nlink, 2, "fixture is not a hard link")
        self.assertEqual(limits_core.read_thresholds(), {})
        # Control: the same bytes at a single-link path DO read back, so the
        # empty result above is the refusal and not an unreadable fixture.
        limits_core.thresholds_path().unlink()
        limits_core.thresholds_path().write_text('{"claude:session": [30]}',
                                                 encoding="utf-8")
        self.assertEqual(limits_core.read_thresholds(), {"claude:session": [30]})

    def test_a_reader_sees_the_old_file_until_atomic_replace(self):
        old = {"claude:session": [30]}
        new = {"claude:weekly": [80]}
        limits_core.write_thresholds(old)
        real_replace = os.replace
        replaced = []

        def inspect_then_replace(source, target):
            self.assertEqual(limits_core.read_thresholds(), old)
            replaced.append((source, target))
            real_replace(source, target)

        with mock.patch.object(limits_core.os, "replace", inspect_then_replace):
            self.assertEqual(limits_core.write_thresholds(new), new)
        self.assertEqual(len(replaced), 1)
        self.assertEqual(limits_core.read_thresholds(), new)

    @unittest.skipUnless(os.name == "posix", "POSIX modes only")
    def test_a_preexisting_parent_keeps_its_mode(self):
        os.chmod(self.tmp, 0o755)
        limits_core.write_thresholds({"claude:session": [30]})
        self.assertEqual(self.tmp.stat().st_mode & 0o777, 0o755)


class TestAThresholdOutlivesItsWindow(_Thresholds):
    """The situation the feature was requested from: a limit the user can see
    in Claude Code is absent from a stale cache."""

    def test_a_configured_window_that_is_not_reported_is_kept_and_surfaced(self):
        stored = {"claude:session": [30], "claude:weekly": [4]}
        live = {"claude:session"}
        orphaned = limits_core.orphaned_thresholds(stored, live)
        self.assertEqual(orphaned, {"claude:weekly": [4]},
                         "a threshold was dropped because its window was "
                         "missing from a stale cache")

    def test_it_attaches_again_when_the_window_returns(self):
        limits_core.write_thresholds({"claude:weekly": [4]})
        returned = [{"kind": "weekly", "group": "weekly", "scope": "",
                     "percent": 6}]
        described = limits_core.describe_windows(
            returned, "claude", limits_core.read_thresholds())
        self.assertEqual(described[0]["thresholds"], [4])
        self.assertEqual(limits_core.crossed(6, described[0]["thresholds"]), [4])

    def test_the_other_assistants_keys_are_not_false_orphans(self):
        stored = {"claude:weekly": [4], "codex:10080m": [90]}
        self.assertEqual(
            limits_core.orphaned_thresholds(
                stored, {"claude:session"}, source="claude"),
            {"claude:weekly": [4]},
        )

    def test_a_keyless_future_window_still_reaches_the_gauge(self):
        described = limits_core.describe_windows(
            [{"kind": "", "group": "", "percent": 91}], "claude", {})
        self.assertEqual(len(described), 1)
        self.assertEqual(described[0]["key"], "")
        self.assertEqual(described[0]["label"], "Limit")
        self.assertEqual(described[0]["thresholds"], [])


class TestCrossingIsReportedInFull(unittest.TestCase):
    def test_every_threshold_at_or_below_the_percentage(self):
        self.assertEqual(limits_core.crossed(91, [30, 80, 95]), [30, 80])
        self.assertEqual(limits_core.crossed(30, [30]), [30])
        self.assertEqual(limits_core.crossed(29, [30]), [])

    def test_a_missing_percentage_crosses_nothing(self):
        for value in (None, True, "80", float("nan")):
            with self.subTest(percent=value):
                self.assertEqual(limits_core.crossed(value, [30]), [])


class TestTheSmallServerIsActuallySmall(unittest.TestCase):
    def test_it_does_not_import_the_dashboard_or_the_database(self):
        """The point of a separate backend is that it can run without the rest
        of the tool. Importing `dashboard` would pull in `scanner`, `db`, both
        parsers and the payload assembler, and the claim would be a fiction.

        Run in a CHILD, because this test process has already imported half the
        repository and `sys.modules` here proves nothing.
        """
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "import limits_server\n"
            "heavy = {'scanner','db','dashboard','dashboard_data',"
            "'transcripts','codex_transcripts','rollups','cli','reports',"
            "'claude_usage.scanner','claude_usage.db',"
            "'claude_usage.dashboard','claude_usage.dashboard_data',"
            "'claude_usage.transcripts','claude_usage.codex_transcripts',"
            "'claude_usage.rollups','claude_usage.cli','claude_usage.reports'}\n"
            "print(','.join(sorted(heavy & set(sys.modules))))\n" % str(REPO_ROOT)
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, encoding="utf-8", timeout=120,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertEqual(proc.stdout.strip(), "",
                         "limits_server drags in the full application")

    def test_the_printed_page_url_delivers_the_token_as_a_fragment(self):
        def fake_serve(host, port, on_ready):
            on_ready("127.0.0.1", 8123, "x" * 43)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(limits_server, "serve", side_effect=fake_serve), \
                redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(limits_server.main([]), 0)
        self.assertEqual(
            out.getvalue().strip(),
            "http://127.0.0.1:8123/#token=" + "x" * 43)
        self.assertNotIn("token=", err.getvalue())

    def test_invalid_limit_port_environment_is_one_line_error(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"LIMITS_PORT": "not-a-port"}), \
                redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(limits_server.main([]), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("LIMITS_PORT must be a whole number", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_out_of_range_limit_port_is_rejected_before_serve(self):
        for value in ("0", "65536", "-1"):
            with self.subTest(value=value):
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(limits_server, "serve") as serve, \
                        redirect_stdout(out), redirect_stderr(err):
                    self.assertEqual(limits_server.main(["--port", value]), 1)
                serve.assert_not_called()
                self.assertIn("--port must be between 1 and 65535", err.getvalue())
                self.assertNotIn("Traceback", err.getvalue())

    def test_valid_cli_port_overrides_an_invalid_environment_value(self):
        with mock.patch.dict(os.environ, {"LIMITS_PORT": "not-a-port"}), \
                mock.patch.object(limits_server, "serve") as serve:
            self.assertEqual(limits_server.main(["--port", "8123"]), 0)
        serve.assert_called_once_with(
            host="127.0.0.1", port=8123, on_ready=mock.ANY)

    def test_library_serve_keeps_ephemeral_port_zero(self):
        selected = []

        class FakeServer:
            def __init__(self, address, handler):
                selected.append((address, handler))
                self.server_address = (address[0], 49152)

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                pass

        with mock.patch.object(limits_server, "LimitsHTTPServer", FakeServer):
            limits_server.serve(port=0)
        self.assertEqual(selected, [(("127.0.0.1", 0), limits_server.LimitsHandler)])

    def test_a_ready_callback_failure_closes_the_bound_server(self):
        closed = []

        class FakeServer:
            server_address = ("127.0.0.1", 49152)

            def __init__(self, address, handler):
                pass

            def serve_forever(self):
                self.fail("serving started after the ready callback failed")

            def server_close(self):
                closed.append(True)

        def fail_ready(*_args):
            raise RuntimeError("ready callback failed")

        with mock.patch.object(limits_server, "LimitsHTTPServer", FakeServer):
            with self.assertRaisesRegex(RuntimeError, "ready callback failed"):
                limits_server.serve(port=0, on_ready=fail_ready)
        self.assertEqual(closed, [True])

    def test_busy_port_uses_the_capability_probed_errno_set(self):
        windows_errno = 10048
        with mock.patch.object(limits_server, "ADDRESS_IN_USE_ERRNOS",
                               frozenset({windows_errno})), \
                mock.patch.object(
                    limits_server, "serve",
                    side_effect=OSError(windows_errno, "Address already in use")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(limits_server.main(["--port", "8123"]), 1)
        self.assertIn("Port 8123 is already in use.", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_unrelated_bind_errors_are_still_reraised(self):
        with mock.patch.object(
                limits_server, "serve",
                side_effect=OSError(errno.EACCES, "Permission denied")):
            with self.assertRaises(OSError) as raised:
                limits_server.main(["--port", "8123"])
        self.assertEqual(raised.exception.errno, errno.EACCES)

    def test_api_key_installs_do_not_publish_stale_subscription_windows(self):
        config = {
            "oauthAccount": {"organizationType": "claude_max"},
            "cachedUsageUtilization": {"utilization": {"limits": [{
                "kind": "session", "group": "session", "percent": 42,
                "resets_at": "2099-01-01T00:00:00Z", "is_active": True,
            }]}, "fetchedAtMs": 1},
        }
        env = {"ANTHROPIC_API_KEY": "audit-key"}
        with mock.patch.object(limits_server.account, "read_config",
                               return_value=config), \
                mock.patch.object(limits_server.limits_core, "read_thresholds",
                                  return_value={}), \
                mock.patch.dict(os.environ, env, clear=True):
            payload = limits_server.limits_payload()
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "api_key")
        self.assertEqual(payload["windows"], [])

    def test_live_limits_do_not_require_an_existing_cache(self):
        config = {"oauthAccount": {"organizationType": "claude_max"}}
        live_config = {
            **config,
            "cachedUsageUtilization": {
                "fetchedAtMs": 1767268800000,
                "utilization": {"limits": [{
                    "kind": "weekly", "group": "weekly", "percent": 37,
                    "resets_at": "2099-01-01T00:00:00Z", "is_active": True,
                }]},
            },
        }
        with mock.patch.object(limits_server.account, "read_config",
                               return_value=config), \
                mock.patch.object(limits_server.live_limits, "enabled",
                                  return_value=True), \
                mock.patch.object(
                    limits_server.live_limits, "config_with_live_limits",
                    return_value=(live_config, "live")) as fetch, \
                mock.patch.object(limits_server.limits_core, "read_thresholds",
                                  return_value={}):
            payload = limits_server.limits_payload()
        fetch.assert_called_once()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["reading"], "live")
        self.assertEqual([w["key"] for w in payload["windows"]],
                         ["claude:weekly"])


class TestTheSmallServerBoundsConnections(unittest.TestCase):
    def test_saturation_is_a_503_and_partial_requests_are_reclaimed(self):
        with mock.patch.object(limits_server, "MAX_HTTP_CONNECTIONS", 2):
            server = limits_server.LimitsHTTPServer(
                ("127.0.0.1", 0), limits_server.LimitsHandler)
        budget = mock.patch.object(
            limits_server, "HTTP_REQUEST_READ_BUDGET_SECONDS", 0.2)
        budget.start()
        self.addCleanup(budget.stop)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        held = []
        try:
            for _ in range(2):
                conn = socket.create_connection((host, port), timeout=2)
                conn.sendall(b"GET /api/limits HTTP/1.1\r\n")
                held.append(conn)
            extra = socket.create_connection((host, port), timeout=2)
            extra.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            self.assertIn(b"503 Service Unavailable", extra.recv(512))
            extra.close()
            deadline = time.monotonic() + 3
            while True:
                conn = http.client.HTTPConnection(host, port, timeout=2)
                conn.request("GET", "/healthz")
                status = conn.getresponse().status
                conn.close()
                if status != 503 or time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            self.assertEqual(status, 200, "expired partial requests did not release their slots")
        finally:
            for conn in held:
                conn.close()

    def test_disconnect_errors_do_not_print_tracebacks(self):
        server = limits_server.LimitsHTTPServer(
            ("127.0.0.1", 0), limits_server.LimitsHandler)
        self.addCleanup(server.server_close)
        stream = io.StringIO()
        with redirect_stderr(stream):
            try:
                raise BrokenPipeError("client left")
            except BrokenPipeError:
                server.handle_error(None, None)
        self.assertEqual(stream.getvalue(), "")

    def test_the_absolute_budget_covers_a_slow_put_body(self):
        with mock.patch.object(limits_server,
                               "HTTP_REQUEST_READ_BUDGET_SECONDS", 0.4):
            server = limits_server.LimitsHTTPServer(
                ("127.0.0.1", 0), limits_server.LimitsHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 5)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            host, port = server.server_address
            client = socket.create_connection((host, port), timeout=3)
            self.addCleanup(client.close)
            client.sendall((
                "PUT /api/limits/thresholds HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"{limits_server.API_TOKEN_HEADER}: {limits_server.API_TOKEN}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 100\r\n\r\n{"
            ).encode("ascii"))
            started = time.monotonic()
            dropped = False
            client.setblocking(False)
            while time.monotonic() - started < 3:
                time.sleep(0.05)
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
            self.assertLess(time.monotonic() - started, 3)


class TestStandalonePayloadSafety(unittest.TestCase):
    def test_display_strings_are_safe_without_changing_opaque_identities(self):
        key = "claude:session:\u202eX"
        orphan = "claude:missing\x1b[2J"
        scope = "Sonnet\x1b]8;;http://evil\x07"
        payload = {
            "available": True,
            "reason": "ok\u202e",
            "plan_type": "claude_max\x1b[2J",
            "windows": [{
                "kind": "session",
                "group": "session",
                "scope": scope,
                "key": key,
                "label": "Session — " + scope,
                "thresholds": [80],
            }],
            "orphaned": {orphan: [50]},
            "source": "claude",
            "reading": "cache",
            "version": "1.0",
        }

        safe = limits_server.safe_limits_payload(payload)

        self.assertEqual(safe["windows"][0]["key"], key)
        self.assertEqual(safe["orphaned"], {orphan: [50]})
        self.assertEqual(safe["plan_type"], terminal_safe(payload["plan_type"]))
        self.assertEqual(safe["windows"][0]["scope"], terminal_safe(scope))
        self.assertEqual(safe["windows"][0]["label"],
                         terminal_safe("Session — " + scope))
        self.assertEqual(payload["windows"][0]["scope"], scope)

    def test_limits_route_applies_the_safe_boundary(self):
        payload = {
            "plan_type": "claude_max\x1b[2J",
            "windows": [],
            "orphaned": {},
        }
        handler = object.__new__(limits_server.LimitsHandler)
        handler._authorize_host = lambda: True
        handler._parsed_request_target = lambda: mock.Mock(path="/api/limits")
        handler._guarded = lambda: True
        handler._send = mock.Mock()
        with mock.patch.object(limits_server, "limits_payload", return_value=payload):
            limits_server.LimitsHandler.do_GET(handler)
        handler._send.assert_called_once_with(
            200, limits_server.safe_limits_payload(payload))


class TestTheServerRefusesWhatItShould(_Thresholds):
    """Behavioural parity with `dashboard.py`'s guard.

    The two servers cannot share code — importing the dashboard would defeat
    the independence this one exists for — so the rules are asserted against
    behaviour instead, and a rule fixed in one and forgotten here fails.
    """

    def setUp(self):
        super().setUp()
        ready = threading.Event()
        self.info = {}

        def on_ready(host, port, token):
            self.info.update(host=host, port=port, token=token)
            ready.set()

        self.thread = threading.Thread(
            target=limits_server.serve,
            kwargs=dict(port=0, on_ready=on_ready), daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(10), "the server never bound")

    def call(self, method, path, body=None, token="valid", origin=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.info["port"], timeout=10)
        headers = {}
        if token == "valid":
            headers[limits_server.API_TOKEN_HEADER] = self.info["token"]
        elif token:
            headers[limits_server.API_TOKEN_HEADER] = token
        if origin:
            headers["Origin"] = origin
        if host:
            headers["Host"] = host
        payload = json.dumps(body) if body is not None else None
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            return response.status, json.loads(raw)
        except ValueError:
            return response.status, raw

    def page_response(self, path="/", host=None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.info["port"], timeout=10)
        headers = {"Host": host} if host else {}
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        result = response.status, dict(response.getheaders()), raw
        conn.close()
        return result

    def raw_status(self, request_line, headers=(), body=b""):
        sock = socket.create_connection(("127.0.0.1", self.info["port"]), timeout=3)
        try:
            raw = (request_line + "\r\n" + "\r\n".join(headers)
                   + "\r\n\r\n").encode("ascii") + body
            sock.sendall(raw)
            first = b""
            while b"\r\n" not in first:
                chunk = sock.recv(512)
                if not chunk:
                    break
                first += chunk
            return int(first.split(b" ", 2)[1])
        finally:
            sock.close()

    def test_healthz_is_open_and_carries_no_data(self):
        status, body = self.call("GET", "/healthz", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"status", "version"})

    def test_the_quota_page_is_open_but_contains_neither_data_nor_token(self):
        status, headers, raw = self.page_response()
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        text_body = raw.decode("utf-8")
        self.assertIn("Claude Usage Limits", text_body)
        self.assertNotIn(self.info["token"], text_body)
        self.assertNotIn("__CSP_NONCE__", text_body)
        self.assertNotIn("__LIMITS_JS__", text_body)
        self.assertIn('method: "PATCH"', text_body)
        self.assertNotIn('method: "PUT"', text_body)
        self.assertNotIn("innerHTML", text_body)

        policy = headers["Content-Security-Policy"]
        match = re.search(r"script-src 'nonce-([^']+)'", policy)
        self.assertIsNotNone(match)
        nonce = match.group(1)
        self.assertIn(f'<script nonce="{nonce}">', text_body)
        self.assertIn(f'<style nonce="{nonce}">', text_body)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertIn("connect-src 'self'", policy)
        self.assertNotIn("'unsafe-inline'", policy)

    def test_index_html_is_the_same_public_shell(self):
        status, _, body = self.page_response("/index.html")
        self.assertEqual(status, 200)
        self.assertIn(b"Claude Usage Limits", body)

    def test_the_page_refuses_a_foreign_host_too(self):
        self.assertEqual(self.page_response(host="evil.example")[0], 421)

    def test_quota_needs_a_token(self):
        self.assertEqual(self.call("GET", "/api/limits", token=None)[0], 403)
        self.assertEqual(self.call("GET", "/api/limits", token="x" * 43)[0], 403)

    def test_a_foreign_origin_is_refused(self):
        self.assertEqual(
            self.call("GET", "/api/limits", origin="http://evil.example")[0], 403)

    def test_an_absent_origin_is_allowed(self):
        """Deliberate: `curl` and a native front end send none, and refusing
        them stops nothing — a hostile page cannot suppress its own Origin."""
        self.assertEqual(self.call("GET", "/api/limits")[0], 200)

    def test_a_foreign_host_is_refused(self):
        """DNS rebinding: a browser can be made to resolve a hostile name to
        127.0.0.1, but it still sends that name as Host."""
        self.assertEqual(
            self.call("GET", "/api/limits", host="evil.example")[0], 421)

    def test_a_loopback_host_with_the_wrong_port_is_refused(self):
        port = self.info["port"]
        wrong_port = 1 if port == 65535 else port + 1
        host = f"127.0.0.1:{wrong_port}"
        self.assertEqual(self.page_response(host=host)[0], 421)
        self.assertEqual(self.call(
            "GET", "/api/limits", host=host,
            origin=f"http://{host}")[0], 421)

    def test_healthz_also_refuses_a_foreign_host(self):
        self.assertEqual(
            self.call("GET", "/healthz", token=None, host="evil.example")[0], 421)

    def test_origin_must_be_http_and_match_the_host_authority(self):
        port = self.info["port"]
        self.assertEqual(self.call(
            "GET", "/api/limits", origin=f"http://127.0.0.1:{port}")[0], 200)
        self.assertEqual(self.call(
            "GET", "/api/limits", origin="http://127.0.0.1:1")[0], 403)
        self.assertEqual(self.call(
            "GET", "/api/limits", origin=f"https://127.0.0.1:{port}")[0], 403)

    def test_writing_thresholds_needs_a_token_too(self):
        status, _ = self.call("PUT", "/api/limits/thresholds",
                              {"claude:session": [30]}, token=None)
        self.assertEqual(status, 403)
        self.assertEqual(limits_core.read_thresholds(), {})

    def test_thresholds_round_trip_over_http(self):
        status, body = self.call("PUT", "/api/limits/thresholds",
                                 {"thresholds": {"claude:session": [30],
                                                 "claude:weekly": [4]}})
        self.assertEqual(status, 200)
        self.assertEqual(body["thresholds"],
                         {"claude:session": [30], "claude:weekly": [4]})
        self.assertEqual(self.call("GET", "/api/limits/thresholds")[1],
                         {"thresholds": {"claude:session": [30],
                                         "claude:weekly": [4]}})

    def test_a_patch_merges_without_replacing_unrelated_windows(self):
        self.assertEqual(self.call(
            "PUT", "/api/limits/thresholds",
            {"thresholds": {"claude:session": [30],
                             "codex:weekly": [70]}})[0], 200)
        status, body = self.call(
            "PATCH", "/api/limits/thresholds",
            {"thresholds": {"claude:weekly": [90]}})
        self.assertEqual(status, 200)
        self.assertEqual(body["thresholds"], {
            "claude:session": [30],
            "claude:weekly": [90],
            "codex:weekly": [70],
        })

    def test_an_invalid_or_empty_patch_is_refused(self):
        limits_core.write_thresholds({"claude:session": [30]})
        self.assertEqual(self.call(
            "PATCH", "/api/limits/thresholds",
            {"thresholds": {" ": [90]}})[0], 400)
        self.assertEqual(self.call(
            "PATCH", "/api/limits/thresholds",
            {"thresholds": {}})[0], 400)
        self.assertEqual(limits_core.read_thresholds(), {"claude:session": [30]})

    def test_a_patch_whose_VALUES_are_invalid_is_refused_not_silently_emptied(self):
        """The documented contract, which the code did not keep.

        `LIMITS-BACKEND.md` says PATCH is strict -- "if any supplied update is
        invalid, the entire request is rejected with 400 and the file is left
        unchanged, so the server never reports success for an edit it skipped."
        The guard was KEY-level (`len`/`set`), and `normalize_thresholds` drops
        invalid ELEMENTS while keeping the key, so `{"k": [999]}` normalized to
        `{"k": []}`, passed both halves and was answered 200 -- storing `[]`,
        which the same document defines as DISABLING alerts for that window.
        Measured against both real servers before the fix: `[999]` -> 200/`[]`,
        `["abc"]` -> 200/`[]`, `[50, 999]` -> 200/`[50]`.

        So a mistyped percentage turned off the notification it was meant to
        set, and the response said it had worked. The shipped page never sent
        one (`58-alerts.js` bounds 1-100 client-side), which is why nobody saw
        it; the document's audience is the third-party front end that has not
        been written yet.
        """
        limits_core.write_thresholds({"claude:session": [30]})
        for bad in ([999], ["abc"], [0], [101], [50, 999], [True]):
            with self.subTest(bad=bad):
                status, _ = self.call("PATCH", "/api/limits/thresholds",
                                      {"thresholds": {"claude:session": bad}})
                self.assertEqual(400, status)
                self.assertEqual(limits_core.read_thresholds(),
                                 {"claude:session": [30]},
                                 "a refused PATCH must not touch the file")

    def test_strictness_does_not_reject_the_normalizer_s_own_tidying(self):
        """Membership, not length: de-duplication, sorting and a whole float
        are not discards, and an empty list is the documented way to disable a
        window. Rejecting these would break ordinary saves."""
        for good, stored in (([50, 50], [50]), ([90, 50], [50, 90]),
                             ([50.0], [50]), ([], [])):
            with self.subTest(good=good):
                status, _ = self.call("PATCH", "/api/limits/thresholds",
                                      {"thresholds": {"claude:session": good}})
                self.assertEqual(200, status)
                self.assertEqual(limits_core.read_thresholds()["claude:session"],
                                 stored)

    def test_a_bare_mapping_is_accepted_as_well_as_the_wrapped_one(self):
        """The GET answers `{"thresholds": ...}`, so a client round-tripping
        its own response must not have to unwrap it first."""
        self.assertEqual(
            self.call("PUT", "/api/limits/thresholds", {"claude:session": [30]})[0],
            200)
        self.assertEqual(limits_core.read_thresholds(), {"claude:session": [30]})

    def test_an_oversized_body_is_refused_rather_than_read(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.info["port"], timeout=10)
        conn.request("PUT", "/api/limits/thresholds", body=b"{}",
                     headers={limits_server.API_TOKEN_HEADER: self.info["token"],
                              "Content-Length": str(limits_server.MAX_REQUEST_BYTES + 1)})
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()

    def test_malformed_json_is_a_400_not_a_traceback(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.info["port"], timeout=10)
        body = b"{not json"
        conn.request("PUT", "/api/limits/thresholds", body=body,
                     headers={limits_server.API_TOKEN_HEADER: self.info["token"],
                              "Content-Length": str(len(body))})
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()

    def test_missing_or_chunked_bodies_cannot_clear_thresholds(self):
        limits_core.write_thresholds({"claude:session": [30]})
        common = [
            f"Host: 127.0.0.1:{self.info['port']}",
            f"{limits_server.API_TOKEN_HEADER}: {self.info['token']}",
            "Content-Type: application/json",
        ]
        self.assertEqual(self.raw_status(
            "PUT /api/limits/thresholds HTTP/1.1", common,
            b'{"claude:session":[90]}'), 411)
        self.assertEqual(self.raw_status(
            "PUT /api/limits/thresholds HTTP/1.1",
            common + ["Transfer-Encoding: chunked"],
            b'1b\r\n{"claude:session":[90]}\r\n0\r\n\r\n'), 501)
        self.assertEqual(limits_core.read_thresholds(), {"claude:session": [30]})

    def test_empty_and_non_object_json_cannot_clear_thresholds(self):
        common = [
            f"Host: 127.0.0.1:{self.info['port']}",
            f"{limits_server.API_TOKEN_HEADER}: {self.info['token']}",
            "Content-Type: application/json",
        ]
        for body in (b"", b"null", b"[]", b'"text"', b"7"):
            with self.subTest(body=body):
                limits_core.write_thresholds({"claude:session": [30]})
                status = self.raw_status(
                    "PUT /api/limits/thresholds HTTP/1.1",
                    common + [f"Content-Length: {len(body)}"], body)
                self.assertEqual(status, 400)
                self.assertEqual(limits_core.read_thresholds(),
                                 {"claude:session": [30]})

    def test_duplicate_security_headers_are_rejected(self):
        port = self.info["port"]
        token = self.info["token"]
        base = [f"Host: 127.0.0.1:{port}",
                f"{limits_server.API_TOKEN_HEADER}: {token}"]
        self.assertEqual(self.raw_status(
            "GET /api/limits HTTP/1.1", base + ["Host: evil.example"]), 421)
        self.assertEqual(self.raw_status(
            "GET /api/limits HTTP/1.1",
            base + ["Origin: http://127.0.0.1:%d" % port,
                    "Origin: http://evil.example"]), 403)
        self.assertEqual(self.raw_status(
            "GET /api/limits HTTP/1.1",
            base + [f"{limits_server.API_TOKEN_HEADER}: wrong"]), 403)

    def test_absolute_form_request_targets_are_accepted(self):
        port = self.info["port"]
        status = self.raw_status(
            f"GET http://127.0.0.1:{port}/api/limits/thresholds HTTP/1.1",
            [f"Host: 127.0.0.1:{port}",
             f"{limits_server.API_TOKEN_HEADER}: {self.info['token']}"])
        self.assertEqual(status, 200)

    @unittest.skipUnless(os.name == "posix", "symlink semantics are POSIX")
    def test_a_refused_threshold_target_is_an_http_error(self):
        victim = self.tmp / "victim.json"
        victim.write_text("keep", encoding="utf-8")
        limits_core.thresholds_path().symlink_to(victim)
        status, _ = self.call("PUT", "/api/limits/thresholds",
                              {"claude:session": [30]})
        self.assertEqual(status, 500)
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

    def test_the_payload_carries_keys_labels_and_orphans(self):
        limits_core.write_thresholds({"claude:not_reported_yet": [4],
                                      "codex:10080m": [90]})
        status, body = self.call("GET", "/api/limits")
        self.assertEqual(status, 200)
        self.assertEqual(set(body) >= {"windows", "orphaned", "available"}, True)
        self.assertIn("claude:not_reported_yet", body["orphaned"],
                      "a configured window absent from the cache vanished")
        self.assertNotIn("codex:10080m", body["orphaned"],
                         "the other assistant was shown as a missing Claude window")
        for window in body["windows"]:
            self.assertTrue(window["key"], "a window shipped with no identity")
            self.assertTrue(window["label"], "a window shipped with no label")
            self.assertIsInstance(window["thresholds"], list)

    def test_the_http_payload_sanitizes_display_values_but_not_identities(self):
        key = "claude:session:\u202eX"
        orphan = "claude:missing\x1b[2J"
        scope = "Sonnet\x1b]8;;http://evil\x07"
        raw = {
            "available": True,
            "reason": "ok\u202e",
            "plan_type": "claude_max\x1b[2J",
            "windows": [{
                "kind": "session",
                "scope": scope,
                "key": key,
                "label": "Session — " + scope,
            }],
            "orphaned": {orphan: [50]},
            "source": "claude",
            "reading": "cache",
            "version": "1.0",
        }
        with mock.patch.object(limits_server, "limits_payload", return_value=raw):
            status, body = self.call("GET", "/api/limits")
        self.assertEqual(status, 200)
        self.assertEqual(body["windows"][0]["key"], key)
        self.assertEqual(body["orphaned"], {orphan: [50]})
        self.assertEqual(body["plan_type"], terminal_safe(raw["plan_type"]))
        self.assertEqual(body["windows"][0]["scope"], terminal_safe(scope))


class TestItRefusesToBindOffLoopback(unittest.TestCase):
    def test_a_routable_host_is_refused_rather_than_rewritten(self):
        for host in ("0.0.0.0", "192.0.2.10", "::", "example.com"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    limits_server.validate_bind_host(host)

    def test_loopback_spellings_are_accepted(self):
        for host in ("localhost", "127.0.0.1", "::1", "LOCALHOST"):
            with self.subTest(host=host):
                self.assertIn(limits_server.validate_bind_host(host),
                              limits_server.LOOPBACK_HOSTS)

    def test_localhost_is_pinned_to_a_numeric_loopback(self):
        self.assertEqual(limits_server.validate_bind_host("localhost"),
                         "127.0.0.1")

    def test_ipv6_uses_an_ipv6_server(self):
        self.assertEqual(limits_server.LimitsHTTPServerV6.address_family,
                         socket.AF_INET6)


if __name__ == "__main__":
    unittest.main()
