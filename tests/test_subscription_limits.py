"""Tests for the plan-limit reader (account.py) and the panel it feeds.

Four things here are load-bearing and none of them is obvious from the code:

1. **The identity fields must never escape.** `~/.claude.json` holds the account
   email, several UUIDs, the organization name and a map of absolute project
   paths (which contain the username) right beside the quota block. This module
   is the only thing in the codebase that opens that file.
2. **It is a cache, and it outlives its own window.** A five-hour window that
   already reset keeps reporting `percent: 100, severity: "critical"` until
   Claude Code next refreshes. Rendered verbatim that tells the user they are
   throttled when they are not.
3. **API-key installs have no plan window.** Showing them an empty or borrowed
   gauge would be worse than showing nothing.
4. **The panel is the only thing that arms the quota alerts.** The classes at
   the end of this file drive the real `renderPlanLimits` under node, because
   the alert rules were tested by calling them directly and the line that calls
   them from the renderer was tested by nothing.
"""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import account
import db
import limits_core
from claude_usage import safefile
import scanner
from tests.test_dashboard_js import emit, requires_node, run_js

NOW = datetime(2026, 8, 6, 1, 0, 0, tzinfo=timezone.utc)
OBSERVED = "2026-08-06T01:00:00+00:00"

# Synthetic account fields exercise redaction without using an account identity.
FULL_CONFIG = {
    "oauthAccount": {
        "accountUuid": "11111111-1111-4111-8111-111111111111",
        "emailAddress": "someone@example.com",
        "organizationUuid": "22222222-2222-4222-8222-222222222222",
        "organizationName": "someone@example.com's Organization",
        "displayName": "Someone",
        "billingType": "stripe_subscription",
        "organizationType": "claude_max",
        "organizationRateLimitTier": "default_claude_max_20x",
        "organizationRole": "admin",
    },
    "userID": "user-abc",
    "machineID": "machine-def",
    "projects": {"/Users/someone/Developer/secret-client": {}},
    "cachedUsageUtilization": {
        "fetchedAtMs": int((NOW - timedelta(minutes=12)).timestamp() * 1000),
        "accountUuid": "11111111-1111-4111-8111-111111111111",
        "utilization": {
            "five_hour": {"utilization": 40, "resets_at": "2026-08-06T03:30:00+00:00"},
            "seven_day": None,
            "limits": [
                {"kind": "session", "group": "session", "percent": 40,
                 "severity": "normal", "resets_at": "2026-08-06T03:30:00+00:00",
                 "scope": None, "is_active": True},
                {"kind": "weekly_scoped", "group": "weekly", "percent": 12,
                 "severity": "warning", "resets_at": "2026-08-10T00:00:00+00:00",
                 "scope": {"model": {"id": "m-1", "display_name": "Opus"},
                           "surface": "code"},
                 "is_active": True},
            ],
            "extra_usage": {"is_enabled": False, "spend_limit_reached": False},
            "spend": {"used": {"amount_minor": 0, "currency": "USD", "exponent": 2},
                      "percent": 0, "enabled": False,
                      "disclaimer": "Buy credits at https://example.com/upsell"},
        },
    },
}

REDACTED = ("someone@example.com", "11111111", "22222222", "Someone",
            "Organization", "user-abc", "machine-def", "secret-client",
            "example.com/upsell")


def _write(tmp, payload, name=".claude.json"):
    path = Path(tmp) / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# One hostile value of every shape `json.loads` can hand this module, crossed
# over every position of a realistic config below. The three arbitrary-width
# integers are the point: `json` parses an integer at any width, they have the
# type `fetchedAtMs` is *supposed* to have, and the first float operation that
# touches one raises rather than returning anything.
HOSTILE_VALUES = (
    None, True, False, 0, -1, 1,
    10 ** 400, -(10 ** 400), 10 ** 309, 2 ** 63, 1e300, 1e308, -1e308,
    float("nan"), float("inf"), float("-inf"), 0.0,
    "", "x" * 200, "12.50", "0", "1786215192108",
    "9999-12-31T23:59:59Z", "0001-01-01T00:00:00+05:00", "not-a-date",
    {}, [], {"a": 1}, [1],
)


def _config_positions(node, prefix=()):
    """Every position in a config a hostile value can be substituted at.

    The whole document counts as one of them: `read_config` only ever returns a
    dict or None, but nothing stops a caller parsing the file itself, and it is
    the entry points' promise that is under test, not the reader's.
    """
    yield prefix
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _config_positions(value, prefix + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _config_positions(value, prefix + (index,))


def _substituted(config, path, value):
    node = config = json.loads(json.dumps(config))
    if not path:
        return value
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value
    return config


class TestAuthModeDetection(unittest.TestCase):
    def test_an_oauth_subscription_is_a_subscription(self):
        self.assertEqual(
            account.detect_auth_mode(FULL_CONFIG, env={}, settings_paths=()),
            "subscription")

    def test_an_exported_api_key_wins_over_a_stale_oauth_account(self):
        """The key is the credential the requests actually go out under, and it
        has no plan window — only per-token billing."""
        for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            with self.subTest(var=var):
                self.assertEqual(
                    account.detect_auth_mode(FULL_CONFIG, env={var: "sk-ant-xxx"},
                                             settings_paths=()),
                    "api_key")

    def test_an_empty_api_key_variable_is_not_a_key(self):
        self.assertEqual(
            account.detect_auth_mode(FULL_CONFIG, env={"ANTHROPIC_API_KEY": ""},
                                     settings_paths=()),
            "subscription")

    def test_an_api_key_helper_in_settings_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"apiKeyHelper": "/usr/local/bin/get-key"},
                          "settings.json")
            self.assertEqual(
                account.detect_auth_mode(FULL_CONFIG, env={}, settings_paths=(path,)),
                "api_key")

    def test_an_api_key_in_the_settings_env_block_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"env": {"ANTHROPIC_API_KEY": "sk-ant-xxx"}},
                          "settings.json")
            self.assertEqual(
                account.detect_auth_mode(FULL_CONFIG, env={}, settings_paths=(path,)),
                "api_key")

    def test_an_unknown_subscription_family_still_counts_as_one(self):
        """organizationType is an open set — claude_team / claude_enterprise and
        whatever ships next must show the panel, not silently hide it."""
        for org_type in ("claude_pro", "claude_team", "claude_enterprise",
                         "claude_something_new"):
            with self.subTest(org_type=org_type):
                config = {"oauthAccount": {"organizationType": org_type,
                                           "billingType": ""}}
                self.assertEqual(
                    account.detect_auth_mode(config, env={}, settings_paths=()),
                    "subscription")

    def test_no_account_at_all_is_unknown_not_a_subscription(self):
        self.assertEqual(account.detect_auth_mode({}, env={}, settings_paths=()),
                         "unknown")


class TestTheProjectionWithholdsIdentity(unittest.TestCase):
    def test_no_identifying_field_survives_the_projection(self):
        blob = json.dumps(account.limits_projection(FULL_CONFIG, now=NOW))
        for secret in REDACTED:
            with self.subTest(field=secret):
                self.assertNotIn(secret, blob)

    def test_it_still_carries_the_facts_the_panel_needs(self):
        got = account.limits_projection(FULL_CONFIG, now=NOW)
        self.assertTrue(got["available"])
        self.assertEqual(got["plan_type"], "claude_max")
        self.assertEqual(got["rate_limit_tier"], "default_claude_max_20x")
        self.assertEqual([w["kind"] for w in got["windows"]],
                         ["session", "weekly_scoped"])
        self.assertEqual(got["windows"][0]["percent"], 40)
        self.assertEqual(got["windows"][1]["scope"], "Opus")

    def test_the_scope_keeps_only_the_display_name(self):
        got = account.limits_projection(FULL_CONFIG, now=NOW)
        self.assertNotIn("m-1", json.dumps(got))
        self.assertNotIn("surface", json.dumps(got))

    def test_same_named_scopes_with_different_identity_do_not_collide(self):
        config = json.loads(json.dumps(FULL_CONFIG))
        base = {
            "kind": "weekly_scoped", "group": "weekly", "percent": 12,
            "severity": "warning", "resets_at": "2026-08-10T00:00:00+00:00",
            "is_active": True,
        }
        config["cachedUsageUtilization"]["utilization"]["limits"] = [
            {**base, "scope": {"model": {"id": "model-a", "display_name": "Opus"},
                                "surface": "claude_code"}},
            {**base, "scope": {"model": {"id": "model-b", "display_name": "Opus"},
                                "surface": "claude_ai"}},
        ]
        projection = account.limits_projection(config, now=NOW)
        described = limits_core.describe_windows(projection["windows"], "claude", {})
        self.assertEqual(len({window["key"] for window in described}), 2)
        self.assertEqual(len({window["label"] for window in described}), 2)
        blob = json.dumps(projection)
        self.assertNotIn("model-a", blob)
        self.assertNotIn("model-b", blob)

    def test_the_age_is_computed_server_side(self):
        got = account.limits_projection(FULL_CONFIG, now=NOW)
        self.assertEqual(got["age_seconds"], 12 * 60)


class TestStaleWindowsAreNotReportedAsFull(unittest.TestCase):
    def test_a_window_past_its_reset_time_is_marked_expired(self):
        """A cached full window is expired once its reset instant has passed."""
        config = json.loads(json.dumps(FULL_CONFIG))
        config["cachedUsageUtilization"]["utilization"]["limits"][0].update(
            percent=100, severity="critical",
            resets_at="2026-08-06T00:29:59.750000+00:00")
        got = account.limits_projection(config, now=NOW)
        self.assertTrue(got["windows"][0]["expired"])

    def test_a_live_window_is_not_marked_expired(self):
        got = account.limits_projection(FULL_CONFIG, now=NOW)
        self.assertFalse(got["windows"][0]["expired"])

    def test_a_window_with_no_reset_time_is_not_guessed_at(self):
        config = json.loads(json.dumps(FULL_CONFIG))
        config["cachedUsageUtilization"]["utilization"]["limits"][0]["resets_at"] = None
        got = account.limits_projection(config, now=NOW)
        self.assertFalse(got["windows"][0]["expired"])


class TestUnavailableCases(unittest.TestCase):
    def test_a_missing_file_is_unavailable_not_an_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(account.read_config(Path(tmp) / "nope.json"))

    def test_malformed_json_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(account.read_config(path))

    def test_a_json_document_that_is_not_an_object_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, ["a", "list"])
            self.assertIsNone(account.read_config(path))

    def test_a_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(account.read_config(Path(tmp)))

    def test_an_oversized_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            path.write_bytes(b"{}" + b" " * (account.MAX_CONFIG_BYTES + 10))
            self.assertIsNone(account.read_config(path))

    @unittest.skipUnless(os.name == "posix", "FIFO semantics are POSIX")
    def test_a_replacement_fifo_between_validation_and_open_cannot_block(self):
        """Validation must describe the descriptor that is actually read."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude.json"
            path.write_text("{}", encoding="utf-8")
            original_stat = os.stat
            original_open = os.open
            swapped = threading.Event()

            def replace_path():
                if not swapped.is_set():
                    path.unlink()
                    os.mkfifo(path)
                    swapped.set()

            def checked_then_replaced(candidate, *args, **kwargs):
                info = original_stat(candidate, *args, **kwargs)
                if Path(candidate) == path:
                    replace_path()
                return info

            def replaced_before_descriptor_open(candidate, flags, *args):
                if Path(candidate) == path:
                    replace_path()
                return original_open(candidate, flags, *args)

            result = []
            finished = threading.Event()

            def read():
                result.append(account.read_config(path))
                finished.set()

            with mock.patch.object(
                    account.os, "stat", side_effect=checked_then_replaced), \
                    mock.patch.object(
                        safefile.os, "open",
                        side_effect=replaced_before_descriptor_open):
                worker = threading.Thread(target=read, daemon=True)
                worker.start()
                self.assertTrue(swapped.wait(1), "the path was never replaced")
                returned_without_writer = finished.wait(0.25)
                if not returned_without_writer:
                    writer = original_open(
                        path, os.O_WRONLY | getattr(os, "O_NONBLOCK", 0))
                    os.close(writer)
                worker.join(2)

            self.assertTrue(
                returned_without_writer,
                "reading the account cache blocked on a replacement FIFO",
            )
            self.assertEqual(result, [None])

    @unittest.skipUnless(os.name == "posix", "symlink semantics are POSIX")
    def test_a_symlinked_dotfile_remains_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "managed.json"
            target.write_text('{"oauthAccount": {}}', encoding="utf-8")
            link = Path(tmp) / ".claude.json"
            link.symlink_to(target)
            self.assertEqual(account.read_config(link), {"oauthAccount": {}})

    def test_no_cache_block_reports_why(self):
        got = account.limits_projection({"oauthAccount": {}}, now=NOW)
        self.assertFalse(got["available"])
        self.assertEqual(got["reason"], "no_cache")

    def test_no_config_at_all_reports_why(self):
        got = account.limits_projection(None, now=NOW)
        self.assertFalse(got["available"])
        self.assertEqual(got["reason"], "no_config")

    def test_five_hour_is_used_only_when_the_limits_list_is_absent(self):
        config = json.loads(json.dumps(FULL_CONFIG))
        del config["cachedUsageUtilization"]["utilization"]["limits"]
        got = account.limits_projection(config, now=NOW)
        self.assertEqual(len(got["windows"]), 1)
        self.assertEqual(got["windows"][0]["kind"], "five_hour")
        self.assertEqual(got["windows"][0]["percent"], 40)


class TestNoConfigCanMakeThisModuleRaise(unittest.TestCase):
    """The docstring promises "unavailable", never an exception.

    `~/.claude.json` is a cache this project does not write, so its field
    *types* are as much someone else's business as its values. Three bare
    `int()` calls and two datetime rolls used to turn a wrongly-typed or
    extreme field into a raise: `amount_minor` as the string "12.50", an
    `exponent` of NaN, a `resets_at` in year 0001 or 9999. Every caller is
    guarded today — which is why nothing has broken — but the guard belongs to
    the caller, and a new one reaching `limits_projection` or `snapshot_rows`
    directly inherits the raise instead of the promise.

    A field this module cannot make sense of reads as an absent one, which is
    what a missing `amount_minor` has always projected to (0). That is not a
    new claim about the account: nothing renders `spend` at all.
    """

    def _spend(self, **used):
        config = json.loads(json.dumps(FULL_CONFIG))
        config["cachedUsageUtilization"]["utilization"]["spend"]["used"].update(used)
        return config

    def test_a_wrongly_typed_spend_figure_reads_as_an_absent_one(self):
        for value in ("12.50", "2026-08-06T03:30:00+00:00", {"a": 1}, [1], True):
            with self.subTest(amount_minor=value):
                got = account.limits_projection(self._spend(amount_minor=value),
                                                now=NOW)
                self.assertTrue(got["available"])
                self.assertEqual(got["spend"]["used_minor"], 0)

    def test_a_wrongly_typed_exponent_reads_as_an_absent_one(self):
        for value in (float("inf"), float("nan"), "2", None, {}):
            with self.subTest(exponent=value):
                got = account.limits_projection(self._spend(exponent=value), now=NOW)
                self.assertTrue(got["available"])
                self.assertEqual(got["spend"]["exponent"], 0)

    def test_a_wrongly_typed_fetch_time_reads_as_no_reading_at_all(self):
        for value in (float("nan"), float("inf"), "1786215192108", {}):
            with self.subTest(fetchedAtMs=value):
                config = json.loads(json.dumps(FULL_CONFIG))
                config["cachedUsageUtilization"]["fetchedAtMs"] = value
                got = account.limits_projection(config, now=NOW)
                self.assertTrue(got["available"])
                self.assertEqual(got["fetched_at_ms"], 0)
                self.assertIsNone(got["age_seconds"])

    def test_an_unrepresentable_reset_time_is_not_guessed_at_either(self):
        """`0001-01-01T00:00:00+05:00` parses, then overflows on the way to UTC.
        It joins None and "not-a-date" in the "no reset time" branch rather than
        taking the whole projection down with it."""
        config = json.loads(json.dumps(FULL_CONFIG))
        config["cachedUsageUtilization"]["utilization"]["limits"][0][
            "resets_at"] = "0001-01-01T00:00:00+05:00"
        got = account.limits_projection(config, now=NOW)
        self.assertTrue(got["available"])
        self.assertFalse(got["windows"][0]["expired"])
        self.assertIsNone(account._parse_instant("0001-01-01T00:00:00+05:00"))

    def test_a_reset_in_the_last_minute_of_year_9999_still_keys_a_snapshot(self):
        """Rounding :59 up to the next minute rolls past `datetime.MAX`. The
        un-rounded minute is still a stable key for that window, and a snapshot
        beats a crash inside the scanner."""
        config = json.loads(json.dumps(FULL_CONFIG))
        limits = config["cachedUsageUtilization"]["utilization"]["limits"]
        del limits[1]
        limits[0]["resets_at"] = "9999-12-31T23:59:59Z"
        rows = account.snapshot_rows(config, "2026-08-06T01:00:00+00:00", now=NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(account._reset_key("9999-12-31T23:59:59Z"),
                         "9999-12-31T23:59")

    def test_a_far_future_reset_bounds_to_nothing_rather_than_overflowing(self):
        """The overflow is in `reset + step`, *before* the anti-spin loop, so
        guarding the loop body alone would not have caught it."""
        self.assertEqual(
            account.current_window_bounds(
                {"kind": "five_hour", "group": "session",
                 "resets_at": "9999-12-31T20:00:00+00:00"}, NOW),
            (None, None))

    def test_a_fetch_time_wider_than_a_float_is_no_reading_at_all(self):
        """The one input that still raised after the first pass at this promise.

        `_whole` let it through because `int` is exactly the type `fetchedAtMs`
        is supposed to have — and `json` parses an integer at any width, so the
        *type* clause of the docstring never covered it. `fetchedAtMs / 1000`
        then raised `OverflowError` past both `limits_projection` and
        `snapshot_rows`. It reads like the infinity beside it: no reading.
        """
        config = json.loads('{"cachedUsageUtilization": {"fetchedAtMs": 1'
                            + "0" * 400 + ', "utilization": {"limits": [{'
                            '"kind": "five_hour", "group": "session", '
                            '"percent": 40, "resets_at": '
                            '"2026-08-06T03:30:00+00:00", "is_active": true'
                            "}]}}}")
        self.assertIsInstance(
            config["cachedUsageUtilization"]["fetchedAtMs"], int)
        got = account.limits_projection(config, now=NOW)
        self.assertTrue(got["available"])
        self.assertEqual(got["fetched_at_ms"], 0)
        self.assertIsNone(got["age_seconds"])
        self.assertEqual(len(account.snapshot_rows(config, OBSERVED, now=NOW)), 1)

    def test_a_config_that_is_not_an_object_is_not_a_subscription(self):
        """`limits_projection` refuses one with `isinstance`; `detect_auth_mode`
        used `or {}`, which passes a truthy non-dict straight to `.get`."""
        for value in (1, "claude_max", [{"organizationType": "claude_max"}], 1.5):
            with self.subTest(config=value):
                self.assertEqual(
                    account.detect_auth_mode(value, env={}, settings_paths=()),
                    "unknown")
                self.assertFalse(
                    account.limits_projection(value, now=NOW)["available"])

    def test_a_window_whose_kind_is_not_a_string_has_no_length(self):
        """`current_window_bounds` is public and takes the window dict as given.
        Inside this file every `kind` has been through `_text`, so the raise
        needs a caller building one by hand — which is the only kind of caller
        the promise at the top of the file is for."""
        for kind in (1, True, {"a": 1}, [1], 2.5):
            with self.subTest(kind=kind):
                self.assertIsNone(account._window_length_hours(kind, kind))
                self.assertEqual(
                    account.current_window_bounds(
                        {"kind": kind, "group": kind,
                         "resets_at": "2026-08-06T03:30:00+00:00"}, NOW),
                    (None, None))

    def test_no_hostile_value_anywhere_in_a_config_reaches_the_caller(self):
        """The property, fuzzed rather than hand-picked.

        Every position of a realistic config crossed with every shape a JSON
        document can hold, driven through all three entry points. The residual
        `fetchedAtMs` overflow was found this way and not by reading the code:
        it needed a value of the *expected* type, in one particular field, to
        show up at all, and a hand-picked literal per field would have missed
        it.
        """
        raised = []
        for path in _config_positions(FULL_CONFIG):
            for value in HOSTILE_VALUES:
                config = _substituted(FULL_CONFIG, path, value)
                for name, call in (
                    ("limits_projection",
                     lambda c=config: account.limits_projection(c, now=NOW)),
                    ("snapshot_rows",
                     lambda c=config: account.snapshot_rows(c, OBSERVED, now=NOW)),
                    ("detect_auth_mode",
                     lambda c=config: account.detect_auth_mode(
                         c, env={}, settings_paths=())),
                ):
                    try:
                        call()
                    except Exception as exc:  # the raise IS the failure
                        raised.append("%s at %r with %r: %s: %s" % (
                            name, path, value, type(exc).__name__, exc))
        self.assertEqual(raised, [])

    def test_no_hostile_value_reaches_the_caller_through_a_helper_either(self):
        """The helpers and `current_window_bounds` take their arguments from
        callers, not only from a config, so they are fuzzed on their own."""
        raised = []
        for value in HOSTILE_VALUES:
            for name, call in (
                ("_percent", lambda v=value: account._percent(v)),
                ("_whole", lambda v=value: account._whole(v)),
                ("_text", lambda v=value: account._text(v)),
                ("_parse_instant", lambda v=value: account._parse_instant(v)),
                ("_reset_key", lambda v=value: account._reset_key(v)),
                ("_window_length_hours",
                 lambda v=value: account._window_length_hours(v, v)),
                ("_window_from_limit",
                 lambda v=value: account._window_from_limit(v, NOW)),
                ("current_window_bounds",
                 lambda v=value: account.current_window_bounds(
                     {"kind": v, "group": v, "resets_at": v}, NOW)),
            ):
                try:
                    call()
                except Exception as exc:  # the raise IS the failure
                    raised.append("%s(%r): %s: %s"
                                  % (name, value, type(exc).__name__, exc))
        self.assertEqual(raised, [])


class TestSnapshotHistory(unittest.TestCase):
    """One row per (window, percentage): the fill curve, not a write log."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def _rows(self):
        return self.conn.execute(
            "SELECT kind, percent, observed_at FROM usage_limits_snapshots "
            "ORDER BY kind, percent").fetchall()

    def test_a_rescan_of_an_unchanged_cache_writes_nothing_new(self):
        for observed in ("2026-08-06T01:00:00+00:00", "2026-08-06T01:05:00+00:00"):
            scanner.record_limit_snapshot(
                self.conn, account.snapshot_rows(FULL_CONFIG, observed, now=NOW))
        self.assertEqual(len(self._rows()), 2)

    def test_the_first_observation_of_each_level_is_the_one_kept(self):
        scanner.record_limit_snapshot(self.conn, account.snapshot_rows(
            FULL_CONFIG, "2026-08-06T01:00:00+00:00", now=NOW))
        scanner.record_limit_snapshot(self.conn, account.snapshot_rows(
            FULL_CONFIG, "2026-08-06T02:00:00+00:00", now=NOW))
        session = [r for r in self._rows() if r["kind"] == "session"]
        self.assertEqual(len(session), 1)
        self.assertEqual(session[0]["observed_at"], "2026-08-06T01:00:00+00:00")

    def test_a_new_percentage_adds_a_row(self):
        scanner.record_limit_snapshot(self.conn, account.snapshot_rows(
            FULL_CONFIG, "2026-08-06T01:00:00+00:00", now=NOW))
        later = json.loads(json.dumps(FULL_CONFIG))
        later["cachedUsageUtilization"]["utilization"]["limits"][0]["percent"] = 55
        scanner.record_limit_snapshot(self.conn, account.snapshot_rows(
            later, "2026-08-06T02:00:00+00:00", now=NOW))
        session = [r["percent"] for r in self._rows() if r["kind"] == "session"]
        self.assertEqual(sorted(session), [40, 55])

    def test_sub_second_jitter_in_the_reset_time_is_not_a_new_window(self):
        """Invented sub-second offsets must not create new window identities."""
        for micro in (".100000", ".200000"):
            config = json.loads(json.dumps(FULL_CONFIG))
            config["cachedUsageUtilization"]["utilization"]["limits"][0][
                "resets_at"] = "2026-08-06T03:30:00" + micro + "+00:00"
            scanner.record_limit_snapshot(self.conn, account.snapshot_rows(
                config, "2026-08-06T01:00:00+00:00", now=NOW))
        self.assertEqual(len([r for r in self._rows() if r["kind"] == "session"]), 1)

    def test_a_second_before_the_minute_rounds_to_the_same_window(self):
        keys = {account._reset_key("2026-08-06T03:29:59.750000+00:00"),
                account._reset_key("2026-08-06T03:30:00.100000+00:00")}
        self.assertEqual(len(keys), 1, keys)

    def test_an_unusable_config_persists_nothing(self):
        scanner.record_limit_snapshot(
            self.conn, account.snapshot_rows({}, "2026-08-06T01:00:00+00:00"))
        self.assertEqual(self._rows(), [])


class TestScanIsNeverBrokenByTheConfig(unittest.TestCase):
    def test_a_scan_succeeds_with_no_config_file_at_all(self):
        """The Docker case: run-docker.sh mounts ~/.claude/projects and nothing
        else, so there is no ~/.claude.json to read."""
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            projects.mkdir()
            os.environ["CLAUDE_USAGE_CONFIG"] = str(Path(tmp) / "absent.json")
            try:
                result = scanner.scan(projects_dir=projects,
                                      db_path=Path(tmp) / "usage.db", verbose=False)
            finally:
                os.environ.pop("CLAUDE_USAGE_CONFIG", None)
            self.assertEqual(result["turns"], 0)


class TestCurrentWindowAfterAReset(unittest.TestCase):
    """The stretch after a reset where the cache has no figure for the window
    you are in. It lasts until Claude Code next refreshes — tens of minutes —
    and it is exactly when a reader looks at the panel and finds it stuck."""

    def _window(self, kind="session", group="session", resets_at=None):
        import account
        return {"kind": kind, "group": group, "resets_at": resets_at}

    def test_the_current_window_starts_where_the_expired_one_reset(self):
        import account
        now = datetime(2025, 1, 1, 12, 45, tzinfo=timezone.utc)
        start, end = account.current_window_bounds(
            self._window(resets_at="2025-01-01T12:30:00+00:00"), now)
        self.assertEqual(start, datetime(2025, 1, 1, 12, 30, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2025, 1, 1, 17, 30, tzinfo=timezone.utc))

    def test_it_rolls_forward_over_however_many_windows_have_passed(self):
        """A machine left idle overnight must land in the window it is in, not
        the one immediately after the stale reading."""
        import account
        now = datetime(2025, 1, 2, 1, 0, tzinfo=timezone.utc)   # 12h+ later
        start, end = account.current_window_bounds(
            self._window(resets_at="2025-01-01T12:30:00+00:00"), now)
        self.assertLessEqual(start, now)
        self.assertGreater(end, now)
        self.assertEqual((end - start), timedelta(hours=5))
        self.assertEqual(start, datetime(2025, 1, 1, 22, 30, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2025, 1, 2, 3, 30, tzinfo=timezone.utc))

    def test_a_weekly_window_uses_a_weekly_length(self):
        import account
        now = datetime(2026, 8, 6, tzinfo=timezone.utc)
        start, end = account.current_window_bounds(
            self._window(kind="weekly_scoped", group="weekly",
                         resets_at="2026-08-01T00:00:00+00:00"), now)
        self.assertEqual(end - start, timedelta(days=7))

    def test_a_window_of_unknown_length_yields_nothing(self):
        """Better to say 'ended' than to invent a start time."""
        import account
        now = datetime(2026, 8, 6, tzinfo=timezone.utc)
        self.assertEqual(
            account.current_window_bounds(
                self._window(kind="mystery", group="", resets_at="2026-08-01T00:00:00+00:00"), now),
            (None, None))

    def test_a_missing_or_unparseable_reset_yields_nothing(self):
        import account
        now = datetime(2026, 8, 6, tzinfo=timezone.utc)
        for value in (None, "", "not-a-date"):
            with self.subTest(resets_at=value):
                self.assertEqual(
                    account.current_window_bounds(self._window(resets_at=value), now),
                    (None, None))

    def test_an_expired_window_carries_its_bounds_through_the_projection(self):
        import account
        now = datetime(2025, 1, 1, 12, 45, tzinfo=timezone.utc)
        config = {"oauthAccount": {"organizationType": "claude_max"},
                  "cachedUsageUtilization": {"fetchedAtMs": 1, "utilization": {"limits": [
                      {"kind": "session", "group": "session", "percent": 100,
                       "severity": "critical", "is_active": True,
                       "resets_at": "2025-01-01T12:30:00+00:00"}]}}}
        window = account.limits_projection(config, now=now)["windows"][0]
        self.assertTrue(window["expired"])
        self.assertEqual(window["window_start"][:16], "2025-01-01T12:30")
        self.assertEqual(window["window_end"][:16], "2025-01-01T17:30")

    def test_a_live_window_carries_no_bounds(self):
        """They are only meaningful once the cached one has expired."""
        import account
        now = datetime(2026, 8, 6, 20, 0, tzinfo=timezone.utc)
        config = {"oauthAccount": {"organizationType": "claude_max"},
                  "cachedUsageUtilization": {"fetchedAtMs": 1, "utilization": {"limits": [
                      {"kind": "session", "group": "session", "percent": 40,
                       "severity": "normal", "is_active": True,
                       "resets_at": "2026-08-06T22:40:00+00:00"}]}}}
        window = account.limits_projection(config, now=now)["windows"][0]
        self.assertFalse(window["expired"])
        self.assertNotIn("window_start", window)


# ── The panel the reader actually sees ─────────────────────────────────────
# Everything below drives the real `web/js` renderers under node, through
# tests.test_dashboard_js's harness. The DOM stub lives here rather than being
# imported so this module depends only on that harness's public helpers — the
# same three names every other JavaScript-driving suite imports.
#
# `captured` maps an element id to the HTML the renderer assigned it, plus
# `<id>:text` for a textContent write.
_CAPTURE_DOM = """
  const captured = {};
  document.getElementById = (id) => ({
    set innerHTML(v) { captured[id] = v; },
    set textContent(v) { captured[id + ':text'] = v; },
    set className(v) {}, set hidden(v) {},
    setAttribute: () => {}, closest: () => null,
  });
  document.querySelector = () => ({ set hidden(v) {} });
"""


def _panel_run(body, **bindings):
    return run_js(emit("(() => {" + _CAPTURE_DOM + body + "})()", **bindings))


# A reset far enough ahead that the window is live whenever this runs. The alert
# path skips a window that has ended, so a fixture with a past reset would stop
# firing the day the clock passed it. The fractional second is invented.
LIVE_RESET = "2099-01-01T12:30:00.100000+00:00"


def _live_window(percent, thresholds=(80,)):
    """A payload shaped the way the server now sends one.

    `thresholds` is part of the WINDOW because that is where it lives now: the
    server resolves each window's own thresholds from the shared file, so the
    page never applies one list to every limit.
    """
    return {"available": True, "plan_type": "claude_max", "source": "claude",
            "age_seconds": 120,
            "windows": [{"kind": "session", "group": "session", "scope": "",
                         "percent": percent, "severity": "normal",
                         "is_active": True, "expired": False,
                         # What the server sends since thresholds became per
                         # window: `limits_core.describe_windows` resolves the
                         # identity and label once, server-side, so the page
                         # never derives a second one.
                         "key": "claude:session",
                         "label": "Session (5-hour)",
                         "thresholds": list(thresholds),
                         "resets_at": LIVE_RESET}]}


@requires_node
class TestThePanelIsWhatFiresTheQuotaAlerts(unittest.TestCase):
    """The WIRING between the panel and the alerts, not the alert rules.

    `renderPlanLimits` holds the only production call to `checkQuotaAlerts`, and
    the only *initial* draw of the threshold chips — web/index.html ships
    `#alert-thresholds` empty and the picker's other callers are the add, remove
    and toggle handlers. Every alert assertion in the suite calls
    `checkQuotaAlerts` directly, so deleting either line from the renderer
    silenced the whole feature while the full suite reported 896 tests OK
    (measured, each line separately).

    That regression has already shipped here once, on the sibling call site in
    `toggleAlertThreshold`. This is the higher-traffic of the two: alerts are on
    by default at 80% and the panel re-renders on every 30-second poll.
    """

    def test_rendering_the_panel_is_what_fires_a_threshold_alert(self):
        got = _panel_run("""
          localStorage.clear();
          saveThresholds([80]);
          selectedSource = 'claude';
          const seen = [];
          deliverAlert = (title, body) => { seen.push(title + ' | ' + body); };
          // Two renders, not one: the first sighting of a window is a baseline,
          // so a single render at 91% correctly announces nothing. A one-render
          // test would pass with the call deleted and rebuild the false green.
          renderPlanLimits(below);
          const afterFirst = seen.length;
          renderPlanLimits(above);
          return { afterFirst, seen };
        """, below=_live_window(13), above=_live_window(91))
        self.assertEqual(got["afterFirst"], 0,
                         "the first sighting of a window announced a crossing")
        self.assertEqual(len(got["seen"]), 1,
                         "rendering the panel past a threshold fired no alert")
        self.assertIn("91%", got["seen"][0])
        self.assertIn("80%", got["seen"][0])

    def test_rendering_the_panel_is_what_first_draws_the_threshold_chips(self):
        """The shipped `#alert-windows` is empty, and nothing else fills it
        until the reader adds or removes one — which they cannot do with no
        chips on screen.

        The container and the storage moved when thresholds became per window;
        the property did not. Each chip now carries the WINDOW it belongs to as
        well as the percentage, because a remove button that does not say which
        limit it meant cannot be acted on.
        """
        got = _panel_run("""
          localStorage.clear();
          windowThresholds = { 'claude:session': [80, 95] };
          selectedSource = 'claude';
          deliverAlert = () => {};
          renderPlanLimits(info);
          return { chips: captured['alert-windows'] || null };
        """, info=_live_window(13, thresholds=(80, 95)))
        self.assertIsNotNone(got["chips"], "the panel drew no threshold chips")
        self.assertIn('data-remove="80"', got["chips"])
        self.assertIn('data-remove="95"', got["chips"])
        self.assertIn('data-window="claude:session"', got["chips"],
                      "a chip did not say which limit it belongs to")


@requires_node
class TestThePanelNamesTheWindowItIsShowing(unittest.TestCase):
    """The labels, the idle branch and the age line.

    Seven mutations of web/js/56-plan.js each left the full suite green:
    relabelling a weekly window "Daily", renaming the session window, losing the
    open-set fallback for an unrecognised kind, dropping the scope suffix,
    moving `fmtAge`'s 90-second boundary, freezing `fmtAge` at "unknown", and
    replacing the "Not in use" branch with an ordinary gauge. The first is not
    hypothetical — every Codex window is named by its length in minutes, so
    `durationWindowLabel` is the only thing that produces a Codex gauge's label
    at all.
    """

    def test_a_window_is_named_by_its_length_in_minutes(self):
        got = run_js(emit("lengths.map(durationWindowLabel)",
                          lengths=[10080, 1440, 300, 120, 2880, 45, 0, -1]))
        self.assertEqual(got, ["Weekly", "Daily", "Session (5-hour)", "2-hour",
                               "2-day", "45-minute", "Limit", "Limit"])

    def test_a_codex_window_reaches_that_naming(self):
        """Codex names a window '10080m'; the panel must not show the raw key."""
        sample = {"kind": "10080m", "group": "10080m", "scope": ""}
        sample["label"] = limits_core.window_label(sample)
        got = run_js(emit("planWindowLabel(sample)", sample=sample))
        self.assertEqual(got, "Weekly")

    def test_each_kind_of_window_gets_its_own_name(self):
        """The whole vocabulary in one place. "Session (5-hour)" is the label
        most Claude users see all day and was reachable by three different
        keys, none of them asserted; the last case is the open-set fallback —
        a window type that ships without warning must name itself rather than
        collapse into "Limit" beside the others."""
        got = run_js(emit("windows.map(planWindowLabel)", windows=[
            {"kind": "session", "group": "session", "scope": ""},
            {"kind": "five_hour", "group": "", "scope": ""},
            {"kind": "", "group": "session", "scope": ""},
            {"kind": "weekly_scoped", "group": "weekly", "scope": ""},
            {"kind": "monthly_spend", "group": "", "scope": ""},
            {"kind": "", "group": "", "scope": ""},
        ]))
        self.assertEqual(got, ["Session (5-hour)", "Session (5-hour)",
                               "Session (5-hour)", "Weekly", "monthly spend",
                               "Limit"])

    def test_two_scoped_windows_of_one_length_stay_distinguishable(self):
        """A plan meters several weekly windows, one per model family. Without
        the scope suffix they render as two identical "Weekly" gauges."""
        got = run_js(emit(
            "scopes.map(s => planWindowLabel("
            "{kind: 'weekly_scoped', group: 'weekly', scope: s}))",
            scopes=["Fable", "Opus", ""]))
        self.assertEqual(got, ["Weekly — Fable", "Weekly — Opus", "Weekly"])

    def test_a_window_the_plan_is_not_metering_draws_no_gauge(self):
        """The real config carries `is_active: false, percent: 0, resets_at: ""`
        for a slot the plan exposes but is not metering. A 0% bar would say it
        is metered and empty, which is a different claim."""
        idle = {"available": True, "plan_type": "claude_max", "source": "claude",
                "age_seconds": 120,
                "windows": [{"kind": "weekly_scoped", "group": "weekly",
                             "scope": "Fable", "percent": 0, "severity": "normal",
                             "is_active": False, "expired": False,
                             "resets_at": ""}]}
        got = _panel_run("""
          deliverAlert = () => {};
          renderPlanLimits(info);
          return { panel: captured['plan-windows'] };
        """, info=idle)
        self.assertIn("Not in use", got["panel"])
        self.assertIn("Weekly — Fable", got["panel"])
        self.assertNotIn("plan-bar", got["panel"],
                         "a gauge was drawn for a window nothing is metering")

    def test_the_age_line_says_how_old_the_reading_is(self):
        """Invariant 7: the panel is a cache reading and must state its age.
        Frozen at "unknown" it stops distinguishing a fresh one from a nine-hour
        old one, which is the whole point of the line.

        Both unit boundaries are pinned, and the negative case with them: a
        clock a few seconds behind the server's makes `age_seconds` negative,
        and "-3s ago" reads as a reading from the future."""
        got = run_js(emit("ages.map(fmtAge)",
                          ages=[-3, 0, 42, 89, 90, 3600, 5399, 5400, 7200, None]))
        self.assertEqual(got, ["0s", "0s", "42s", "89s", "2 min", "60 min",
                               "90 min", "1.5 h", "2.0 h", "unknown"])


@requires_node
class TestTheDisplayedResetTimeDoesNotJitter(unittest.TestCase):
    """One window, one displayed reset time.

    `resets_at` wobbles sub-second between fetches of the SAME window — that is
    why `account._reset_key` and `alertResetKey` both round to the minute. The
    panel formatted the raw value, and `Intl.DateTimeFormat` truncates seconds
    rather than rounding. Synthetic readings on both sides of a minute must
    render the same reset time rather than shifting it on refresh.
    """

    # Seven invented fractional offsets around one synthetic minute boundary.
    JITTER = ["2025-01-01T12:29:59.125000+00:00", "2025-01-01T12:29:59.250000+00:00",
              "2025-01-01T12:30:00.100000+00:00", "2025-01-01T12:30:00.200000+00:00",
              "2025-01-01T12:30:00.300000+00:00", "2025-01-01T12:29:59.750000+00:00",
              "2025-01-01T12:30:00.400000+00:00"]

    def test_every_reading_of_one_window_shows_the_same_time(self):
        got = run_js(emit("readings.map(fmtResetAt)", readings=self.JITTER))
        self.assertEqual(len(set(got)), 1,
                         "one window rendered several reset times: " + repr(got))

    def test_the_minute_shown_is_the_nearest_one(self):
        """Rounded, not ceiled: `alertResetKey` rounds, and a key and a label
        that disagree would file a reading under a minute the panel never shows.
        Asserted as equality between formatted values so it holds in any zone."""
        got = run_js(emit(
            "[fmtResetAt(early), fmtResetAt(late), fmtResetAt(exact)]",
            early="2025-01-01T12:29:59.125000+00:00",
            late="2025-01-01T12:30:29.900000+00:00",
            exact="2025-01-01T12:30:00+00:00"))
        self.assertEqual(got[0], got[2], "a second before the minute read early")
        self.assertEqual(got[1], got[2], "half a minute past it read late")

    def test_a_missing_or_unparseable_reset_still_formats_to_nothing(self):
        got = run_js(emit("[null, '', 'not-a-date'].map(fmtResetAt)"))
        self.assertEqual(got, ["", "", ""])


class TestThePercentageIsAlwaysAPercentage(unittest.TestCase):
    """The figure goes straight into `style="width:N%"`, so an out-of-range one
    draws a bar past the end of its track."""

    def test_a_figure_outside_the_range_is_clamped(self):
        self.assertEqual(account._percent(150), 100)
        self.assertEqual(account._percent(-5), 0)

    def test_a_figure_that_is_not_a_number_has_no_percentage(self):
        for value in (None, "80", True, float("nan"), float("inf"),
                      float("-inf"), [50]):
            with self.subTest(value=value):
                self.assertIsNone(account._percent(value))


class TestAWholeNumberIsAlwaysAWholeNumber(unittest.TestCase):
    """`_whole` is `_percent`'s rejection rules without the 0-100 clamp.

    The three integer fields that reach the payload were bare `int()` calls
    sitting beside `_percent`-guarded siblings in the same dict literal — one
    of them three lines below a half-written isinstance guard. `int(nan)` and
    `int(inf)` raise rather than returning anything, which is what made the
    docstring's promise false.
    """

    def test_a_number_survives_intact(self):
        self.assertEqual(account._whole(42), 42)
        self.assertEqual(account._whole(0), 0)
        self.assertEqual(account._whole(2.7), 2)

    def test_a_value_that_is_not_a_number_takes_the_default(self):
        for value in (None, "12.50", "0", True, False, {"a": 1}, [1],
                      float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertEqual(account._whole(value), 0)
                self.assertEqual(account._whole(value, default=-1), -1)

    def test_a_number_too_wide_to_do_arithmetic_on_takes_it_too(self):
        """An infinity is rejected because `int(inf)` raises. An integer wider
        than a float is the same rejection for the same reason one operation
        later: `json` parses integers at any width, and the division in
        `age_seconds` is where one of them raised."""
        for value in (10 ** 400, -(10 ** 400), 10 ** 309, 2 ** 63, -(2 ** 63),
                      1e300, 1e308, -1e308):
            with self.subTest(value=value):
                self.assertEqual(account._whole(value), 0)
                self.assertEqual(account._whole(value, default=-1), -1)

    def test_a_real_epoch_milliseconds_reading_survives_untouched(self):
        """The bound rejects, it does not clamp — a reading five million times
        smaller than it must come back as itself, to the millisecond."""
        for value in (1786215192108, 2 ** 63 - 1, -(2 ** 63 - 1), 10 ** 15):
            with self.subTest(value=value):
                self.assertEqual(account._whole(value), value)

@requires_node
class TestAStaleReadingSaysWhatMayBeMissing(unittest.TestCase):
    """The cache can outlive the SET of windows, not just their numbers.

    `cachedUsageUtilization` is a cache Claude Code refreshes every few tens of
    minutes (invariant 7), and the panel already marks a reading over an hour
    old as stale. That says the percentages are behind. It does not say the LIST
    may be, and those are different failures: a limit the plan has gained since
    the reading is simply absent, with nothing distinguishing "you have no
    weekly limit" from "this snapshot predates your weekly limit".

    A stale cache can omit a window that still exists on the plan. The page
    must distinguish an old reading from evidence that the plan has no such
    limit."""

    def test_a_reading_old_enough_to_hide_a_limit_says_so(self):
        got = _panel_run("""
          selectedSource = 'claude';
          deliverAlert = () => {};
          renderPlanLimits(info);
          return { note: captured['plan-note:text'] || '' };
        """, info={**_live_window(13), "age_seconds": 4 * 24 * 3600})
        self.assertIn("may be", got["note"].lower())
        self.assertIn("would not", got["note"].lower(),
                      "the note does not say a newer limit would be absent")

    def test_a_fresh_reading_does_not_cry_wolf(self):
        """The warning has to stay rare, or it becomes part of the furniture and
        stops being read at the one moment it matters."""
        got = _panel_run("""
          selectedSource = 'claude';
          deliverAlert = () => {};
          renderPlanLimits(info);
          return { note: captured['plan-note:text'] || '' };
        """, info={**_live_window(13), "age_seconds": 120})
        self.assertNotIn("would not appear", got["note"].lower())
        self.assertIn("as of", got["note"].lower(),
                      "the ordinary age line went missing")

    def test_the_boundary_is_hours_not_the_stale_class(self):
        """`.stale` fires at an hour and means "behind". This means "doubt the
        shape", and must not fire at the same point — an hour behind is the
        ordinary case Claude Code's own refresh interval produces."""
        got = run_js(
            "console.log(JSON.stringify({"
            "  hour: staleEnoughToHideALimit(3600),"
            "  sixHours: staleEnoughToHideALimit(6 * 3600 + 1),"
            "  missing: staleEnoughToHideALimit(null)}))")
        self.assertFalse(got["hour"])
        self.assertTrue(got["sixHours"])
        self.assertFalse(got["missing"])


if __name__ == "__main__":
    unittest.main()
