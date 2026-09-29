"""Tests for dashboard.py - API endpoint and data retrieval."""

import contextlib
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import dashboard
import dashboard_data
import db
from scanner import get_db, init_db, upsert_sessions, insert_turns
# The plan-window fixture builders live beside the hardening tests that also
# need them, in the same spirit as tests/timestamps.py: a window is only
# enriched once it has EXPIRED, so a fixture is the only way either suite can
# see the enrichment at all.
from tests.test_server_hardening import (
    seed_window_turns,
    write_expired_window_config,
)
from dashboard import (
    API_TOKEN,
    API_TOKEN_HEADER,
    DashboardHTTPServer,
    DashboardHandler,
    HTML_TEMPLATE,
    _content_security_policy,
    _script_safe_json,
    authenticated_dashboard_url,
    find_chart_file,
    get_dashboard_data,
    validate_bind_host,
)

class TestGetDashboardData(unittest.TestCase):
    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        # Insert sample data
        sessions = [{
            "session_id": "sess-abc123", "project_name": "user/myproject",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 5000, "total_output_tokens": 2000,
            "total_cache_read": 500, "total_cache_creation": 200,
            "turn_count": 10,
        }]
        upsert_sessions(conn, sessions)
        turns = [
            {
                "session_id": "sess-abc123", "timestamp": "2026-04-08T09:30:00Z",
                "model": "claude-sonnet-4-6", "input_tokens": 500,
                "output_tokens": 200, "cache_read_tokens": 50,
                "cache_creation_tokens": 20, "tool_name": None, "cwd": "/tmp",
            },
            {
                "session_id": "sess-abc123", "timestamp": "2026-04-08T14:15:00Z",
                "model": "claude-sonnet-4-6", "input_tokens": 300,
                "output_tokens": 150, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "tool_name": None, "cwd": "/tmp",
            },
        ]
        insert_turns(conn, turns)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_returns_valid_structure(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("all_models", data)
        self.assertIn("daily_by_model", data)
        self.assertIn("sessions_all", data)
        self.assertIn("generated_at", data)

    def test_models_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("claude-sonnet-4-6", data["all_models"])

    def test_sessions_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertEqual(len(data["sessions_all"]), 1)
        session = data["sessions_all"][0]
        self.assertEqual(session["project"], "user/myproject")
        self.assertEqual(session["model"], "claude-sonnet-4-6")
        self.assertEqual(session["input"], 5000)

    def test_daily_by_model_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertGreater(len(data["daily_by_model"]), 0)
        day = data["daily_by_model"][0]
        self.assertIn("day", day)
        self.assertIn("model", day)
        self.assertIn("input", day)

    def test_missing_db_returns_error(self):
        data = get_dashboard_data(db_path=Path("/nonexistent/path/usage.db"))
        self.assertIn("error", data)

    def test_session_id_sent_in_full(self):
        # The API returns the full session id; the table truncates it for
        # display client-side, but the CSV export needs the whole value.
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        self.assertEqual(session["session_id"], "sess-abc123")

    def test_session_duration_calculated(self):
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        # 1 hour = 60 minutes
        self.assertEqual(session["duration_min"], 60.0)

    def test_api_data_escapes_terminal_and_bidi_controls(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE sessions SET project_name = ?, topic = ?",
            ("project\x1b]52;c;payload\x07", "title\u202eexe"),
        )
        conn.commit()
        conn.close()

        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        self.assertEqual(
            session["project"],
            r"project\x1b]52;c;payload\x07",
        )
        self.assertEqual(session["topic"], r"title\u202eexe")

    def test_api_rejects_text_in_numeric_columns_from_legacy_database(self):
        payload = '<img src=x onerror="fetch(\'https://attacker.example\')">'
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE sessions SET turn_count = ?, total_input_tokens = ?",
            (payload, payload),
        )
        conn.execute(
            "UPDATE turns SET is_subagent = 1, agent_id = ? WHERE id = "
            "(SELECT MIN(id) FROM turns)",
            ("agent-hostile",),
        )
        conn.execute(
            "INSERT INTO agents "
            "(agent_id, agent_type, total_duration_ms, tool_use_count) "
            "VALUES (?, ?, ?, ?)",
            ("agent-hostile", "Plan", payload, payload),
        )
        conn.commit()
        conn.close()

        data = get_dashboard_data(db_path=self.db_path)

        self.assertEqual(data["sessions_all"][0]["turns"], 0)
        self.assertEqual(data["sessions_all"][0]["input"], 0)
        dispatch = data["top_dispatches"][0]
        self.assertIsNone(dispatch["duration_ms"])
        self.assertIsNone(dispatch["tool_uses"])

    def test_hourly_by_model_present(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("hourly_by_model", data)
        self.assertIsInstance(data["hourly_by_model"], list)

    def test_hourly_by_model_buckets_by_utc_hour(self):
        data = get_dashboard_data(db_path=self.db_path)
        rows = data["hourly_by_model"]
        # Two turns at UTC 09:30 and 14:15 → two hour buckets
        by_hour = {r["hour"]: r for r in rows}
        self.assertIn(9, by_hour)
        self.assertIn(14, by_hour)
        self.assertEqual(by_hour[9]["turns"], 1)
        self.assertEqual(by_hour[9]["output"], 200)
        self.assertEqual(by_hour[14]["turns"], 1)
        self.assertEqual(by_hour[14]["output"], 150)

    def test_hourly_by_model_carries_day_and_model(self):
        data = get_dashboard_data(db_path=self.db_path)
        rows = data["hourly_by_model"]
        self.assertTrue(all("day" in r and "model" in r for r in rows))
        self.assertTrue(all(r["model"] == "claude-sonnet-4-6" for r in rows))
        self.assertTrue(all(r["day"] == "2026-04-08" for r in rows))


class TestEmptyStringModelNormalization(unittest.TestCase):
    """Regression: turns with model='' (empty string) must group as 'unknown'.
    COALESCE(model, 'unknown') alone returns '' because empty string isn't NULL;
    NULLIF(model, '') is needed first."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-empty", "project_name": "u/p",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T09:05:00Z",
            "git_branch": "", "model": "",
            "total_input_tokens": 100, "total_output_tokens": 50,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 1,
        }])
        insert_turns(conn, [{
            "session_id": "sess-empty", "timestamp": "2026-04-08T09:05:00Z",
            "model": "", "input_tokens": 100, "output_tokens": 50,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_name": None, "cwd": "/tmp",
        }])
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_all_models_contains_unknown_not_empty(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("unknown", data["all_models"])
        self.assertNotIn("", data["all_models"])

    def test_daily_by_model_contains_unknown_not_empty(self):
        data = get_dashboard_data(db_path=self.db_path)
        models = {r["model"] for r in data["daily_by_model"]}
        self.assertIn("unknown", models)
        self.assertNotIn("", models)

    def test_hourly_by_model_contains_unknown_not_empty(self):
        data = get_dashboard_data(db_path=self.db_path)
        models = {r["model"] for r in data["hourly_by_model"]}
        self.assertIn("unknown", models)
        self.assertNotIn("", models)


class TestMixedNullAndEmptyModel(unittest.TestCase):
    """Regression: a mix of model=NULL and model='' rows must collapse into a
    SINGLE 'unknown' group across all aggregations. Without `GROUP BY
    COALESCE(NULLIF(model, ''), 'unknown')` (matching the SELECT expression),
    SQLite groups by raw value and emits two distinct 'unknown' rows."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-mix", "project_name": "u/p",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "", "model": "",
            "total_input_tokens": 200, "total_output_tokens": 100,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 2,
        }])
        # Insert one turn with model='' and one with model=NULL on the same day.
        # Use raw INSERT for the NULL row because insert_turns() requires the
        # model key to exist (would error on missing key, not on None).
        insert_turns(conn, [{
            "session_id": "sess-mix", "timestamp": "2026-04-08T09:00:00Z",
            "model": "", "input_tokens": 100, "output_tokens": 50,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_name": None, "cwd": "/tmp",
        }])
        conn.execute("""
            INSERT INTO turns (session_id, timestamp, model, input_tokens,
                output_tokens, cache_read_tokens, cache_creation_tokens,
                tool_name, cwd)
            VALUES ('sess-mix', '2026-04-08T09:30:00Z', NULL, 100, 50, 0, 0, NULL, '/tmp')
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_all_models_collapses_to_single_unknown(self):
        data = get_dashboard_data(db_path=self.db_path)
        unknowns = [m for m in data["all_models"] if m == "unknown"]
        self.assertEqual(len(unknowns), 1, f"got duplicate 'unknown' rows: {data['all_models']}")

    def test_daily_collapses_to_single_unknown(self):
        data = get_dashboard_data(db_path=self.db_path)
        unknown_rows = [r for r in data["daily_by_model"] if r["model"] == "unknown"]
        # One day, one model bucket
        self.assertEqual(len(unknown_rows), 1, f"got {unknown_rows}")
        self.assertEqual(unknown_rows[0]["turns"], 2)
        self.assertEqual(unknown_rows[0]["input"], 200)

    def test_hourly_collapses_to_single_unknown(self):
        data = get_dashboard_data(db_path=self.db_path)
        # Both turns are in UTC hour 9 — must be one row, not two
        hour9 = [r for r in data["hourly_by_model"]
                 if r["hour"] == 9 and r["model"] == "unknown"]
        self.assertEqual(len(hour9), 1, f"got {hour9}")
        self.assertEqual(hour9[0]["turns"], 2)


class TestNonBillableModelFallback(unittest.TestCase):
    """Regression: when the user has only non-billable models (e.g. gemma, glm,
    local LLMs) — or all turns lack a model field — the default model selection
    must fall back to ALL models so the dashboard isn't blank."""

    def test_readurlmodels_fallback_in_html_template(self):
        # The fallback logic is JS; we assert the source contains the guard so
        # a future refactor doesn't silently remove it.
        self.assertIn("billable.length ? billable : allModels", HTML_TEMPLATE)


class TestScanActivityCoordinator(unittest.TestCase):
    def test_activity_is_visible_before_the_worker_gets_to_run(self):
        """The first status request cannot win the thread-scheduling race."""
        worker_called = threading.Event()

        class PausedThread:
            def __init__(self, target, daemon):
                self.target = target
                self.daemon = daemon

            def start(self):
                pass

        thread = dashboard.start_scan_thread(
            worker_called.set, PausedThread)
        self.assertTrue(thread.daemon)
        self.assertFalse(worker_called.is_set())
        self.assertEqual(dashboard.scan_status()["state"], "scanning")
        thread.target()
        self.assertTrue(worker_called.is_set())
        self.assertEqual(dashboard.scan_status()["state"], "idle")

    def test_finishing_one_scan_does_not_hide_a_queued_second_scan(self):
        """The status is a reference count, not the first worker's boolean.

        Startup does not hold ``RESCAN_LOCK``. A manual scan can therefore be
        accepted and park on the scanner's own lock behind startup; when startup
        returns, the page must remain busy until that queued scan also finishes.
        """
        scan_lock = threading.Lock()
        first_entered = threading.Event()
        second_entered = threading.Event()
        release_first = threading.Event()
        release_second = threading.Event()
        first_thread = None
        second_thread = None

        def scan(entered, release):
            with scan_lock:
                entered.set()
                release.wait(timeout=5)

        self.assertEqual(dashboard.scan_status()["state"], "idle")
        try:
            first_thread = dashboard.start_scan_thread(
                lambda: scan(first_entered, release_first), threading.Thread)
            self.assertTrue(first_entered.wait(timeout=5))
            second_thread = dashboard.start_scan_thread(
                lambda: scan(second_entered, release_second), threading.Thread)
            self.assertEqual(dashboard.scan_status()["state"], "scanning")

            release_first.set()
            first_thread.join(timeout=5)
            self.assertFalse(first_thread.is_alive())
            self.assertTrue(second_entered.wait(timeout=5))
            self.assertEqual(
                dashboard.scan_status()["state"],
                "scanning",
                "the first completion hid a second scan already queued behind it",
            )
        finally:
            release_first.set()
            release_second.set()
            if first_thread is not None:
                first_thread.join(timeout=5)
            if second_thread is not None:
                second_thread.join(timeout=5)

        self.assertEqual(dashboard.scan_status()["state"], "idle")

    def test_the_last_queued_scan_outcome_wins_after_all_work_finishes(self):
        """A failure cannot look idle, and an earlier result cannot win.

        Startup and a manual rescan can both be registered while the scanner
        serializes their actual work.  The page stays provisional until the
        counter reaches zero, then the outcome of the worker that completed
        last describes the database left behind.
        """
        class DeferredThread:
            def __init__(self, target, daemon):
                self.target = target
                self.daemon = daemon

            def start(self):
                pass

        # A failed startup followed by a successful queued rescan recovers.
        first = dashboard.start_scan_thread(lambda: False, DeferredThread)
        second = dashboard.start_scan_thread(lambda: True, DeferredThread)
        self.assertEqual(dashboard.scan_status()["state"], "scanning")
        first.target()
        self.assertEqual(
            dashboard.scan_status()["state"], "scanning",
            "the first failure hid a successful worker already queued behind it",
        )
        failed_generation = dashboard.scan_status()["generation"]
        second.target()
        recovered = dashboard.scan_status()
        self.assertEqual(recovered["state"], "idle")
        self.assertGreater(recovered["generation"], failed_generation)

        # The reverse ordering leaves the final failure visible.  Run one last
        # successful worker in the cleanup so this process-global coordinator
        # cannot poison the HTTP tests that follow this class.
        first = dashboard.start_scan_thread(lambda: True, DeferredThread)
        second = dashboard.start_scan_thread(lambda: False, DeferredThread)
        first.target()
        self.assertEqual(dashboard.scan_status()["state"], "scanning")
        second.target()
        try:
            self.assertEqual(dashboard.scan_status()["state"], "failed")
        finally:
            recovery = dashboard.start_scan_thread(
                lambda: True, DeferredThread)
            recovery.target()
        self.assertEqual(dashboard.scan_status()["state"], "idle")


class TestDashboardHTTP(unittest.TestCase):
    """Integration test: start server and make HTTP requests."""

    @classmethod
    def setUpClass(cls):
        # Redirect DB_PATH + projects dirs to a tempdir so /api/rescan
        # writes to a throwaway DB and scans a throwaway transcript dir
        # instead of the user's real ~/.claude/usage.db and transcripts.
        import dashboard as _d
        import scanner as _s
        cls._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmpdir.name)
        tmp_projects = tmp / "projects"
        tmp_projects.mkdir()
        cls._patches = {
            (_d, "DB_PATH"):                (_d.DB_PATH,                tmp / "usage.db"),
            (_s, "DB_PATH"):                (_s.DB_PATH,                tmp / "usage.db"),
            (_s, "PROJECTS_DIR"):           (_s.PROJECTS_DIR,           tmp_projects),
            (_s, "DEFAULT_PROJECTS_DIRS"):  (_s.DEFAULT_PROJECTS_DIRS,  [tmp_projects]),
            # `do_POST` prefers `dashboard.PROJECTS_DIRS` over the scanner
            # default patched above, and `dashboard.serve` assigns that global
            # without ever restoring it — so an earlier module that reached
            # `serve` (tests.test_port_in_use did, at a port that refuses to
            # bind) left the developer's real ~/.claude/projects in it, and
            # /api/rescan scanned every transcript they own into this temp
            # database. Fixed at the source there too; pinned here so this
            # class cannot be poisoned by whatever runs before it.
            (_d, "PROJECTS_DIRS"):          (_d.PROJECTS_DIRS,          [tmp_projects]),
        }
        for (mod, name), (_orig, new) in cls._patches.items():
            setattr(mod, name, new)

        # A plan window is only enriched with "what this database recorded in
        # it" once the CACHED window has expired, so against a real
        # ~/.claude.json — whose window is usually live, and which on CI does
        # not exist at all — every assertion about that contract compared None
        # with None and could not fail. Point the config reader at a window that
        # reset twenty minutes ago, and seed turns inside it, so the four tests
        # below are testing something.
        cls.window_reset = (datetime.now(timezone.utc)
                            - timedelta(minutes=20)).replace(second=0, microsecond=0)
        cls.config_path = tmp / "claude.json"
        write_expired_window_config(cls.config_path, cls.window_reset)
        cls.expected_recorded = seed_window_turns(tmp / "usage.db", cls.window_reset)
        cls._env = mock.patch.dict(os.environ, {
            "CLAUDE_USAGE_CONFIG": str(cls.config_path),
            # Neutralise the developer's own ~/.claude/settings.json: an
            # api-key declaration there resolves to available:false and would
            # vacuum these assertions on their machine but not on CI. Empty
            # rather than absent for CLAUDE_CONFIG_DIR, which now moves the
            # settings lookup as well as the config one: patch.dict merges, so
            # a developer who exports it would otherwise send this fixture at
            # their real settings file past the HOME redirect.
            "HOME": str(tmp),
            "USERPROFILE": str(tmp),
            "CLAUDE_CONFIG_DIR": "",
        })
        cls._env.start()
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            os.environ.pop(name, None)

        cls.server = DashboardHTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls._env.stop()
        for (mod, name), (orig, _new) in cls._patches.items():
            setattr(mod, name, orig)
        cls._tmpdir.cleanup()

    def api_request(self, path, method="GET", token=API_TOKEN, origin=None):
        headers = {}
        if token is not None:
            headers[API_TOKEN_HEADER] = token
        if origin is not None:
            headers["Origin"] = origin
        return urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method=method,
            headers=headers,
        )

    def test_index_returns_html(self):
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])

    def test_index_with_query_string_returns_html(self):
        # Regression: ?range=... and ?models=... must not 404. The dashboard
        # itself rewrites the URL with these params via history.replaceState,
        # so anything that reloads or bookmarks the page hits this path.
        for qs in ("?range=all", "?range=30d&models=claude-opus-4-7"):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/{qs}") as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn(b"Claude Code Usage", resp.read())

    def test_api_data_with_query_string(self):
        # /api/data is fetched without query parameters today, but the route
        # should be tolerant if any are tacked on (e.g. cache-busting).
        with urllib.request.urlopen(self.api_request("/api/data?_=cachebust")) as resp:
            self.assertEqual(resp.status, 200)

    def test_api_data_returns_json(self):
        with urllib.request.urlopen(self.api_request("/api/data")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            # Should have expected keys (or error if no DB)
            self.assertTrue("all_models" in data or "error" in data)

    def test_instance_proof_carries_neither_recovery_secret(self):
        import dashboard
        challenge = "c" * 43
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/instance"
                f"?challenge={challenge}") as resp:
            self.assertEqual(resp.status, 200)
            body_bytes = resp.read()
        body = json.loads(body_bytes)
        self.assertEqual(body["service"], "claude-usage")
        self.assertEqual(body["proof"], dashboard._liveness_proof(challenge))
        self.assertNotIn(API_TOKEN.encode("ascii"), body_bytes)
        self.assertNotIn(dashboard.LIVENESS_TOKEN.encode("ascii"), body_bytes)

    def test_a_live_server_is_recognised(self):
        """The link recovery check, against a real server.

        And the check must not reject a working link — a false negative sends
        the reader to restart a server that is running fine.

        `assertIs(True)` rather than `assertTrue`: the answer is tri-state, and
        the caller deletes the file on one of the three."""
        import dashboard
        self.assertIs(dashboard.url_is_live(
            dashboard.authenticated_dashboard_url(
                "127.0.0.1", self.port, API_TOKEN
            ), timeout=3), True,
            "the running test server was reported dead")

    def test_a_link_whose_token_no_longer_matches_is_not_live(self):
        """A restarted server mints a new token, so a stale file can name a live
        port and still be useless. Liveness has to mean 'this link works'.

        A rejected token is *proof* the link is dead, not a failed probe, so it
        must be `False` and not the `None` that means "could not tell" —
        `assertFalse` alone accepts both and would let the file survive."""
        import dashboard
        self.assertIs(dashboard.url_is_live(
            dashboard.authenticated_dashboard_url(
                "127.0.0.1", self.port, "z" * 43
            ), timeout=3), False)

    def test_api_limits_returns_the_plan_projection(self):
        """The panel polls this on its own interval, so it must stand alone."""
        with urllib.request.urlopen(self.api_request("/api/limits")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
        self.assertTrue(data["available"], data.get("reason"))
        self.assertEqual(len(data["windows"]), 1)
        self.assertTrue(data["windows"][0]["expired"])

    def test_api_limits_carries_no_identifying_account_fields(self):
        """Same privacy contract as the embedded copy in /api/data."""
        with urllib.request.urlopen(self.api_request("/api/limits")) as resp:
            body = resp.read().decode()
        # The control: the config this was projected from really does carry all
        # of them, so an empty or unavailable payload cannot pass by default.
        source = self.config_path.read_text(encoding="utf-8")
        for leak in ("email", "accountUuid", "organizationName",
                     "displayName", "userID", "projects"):
            self.assertIn(leak, source, f"the fixture never had {leak} to leak")
            self.assertNotIn(leak, body, f"/api/limits leaked {leak}")
        self.assertNotIn("account_uuid", body)

    def test_api_limits_requires_the_token(self):
        for token in (None, "wrong-token"):
            with self.subTest(token=token):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(self.api_request("/api/limits", token=token))
                self.assertEqual(raised.exception.code, 403)

    def cross_origins(self):
        """One origin per clause the guard is made of.

        The https one is the only thing pinning the scheme check — and it was
        for a long time the ONLY origin either rejection test sent, so the
        authority comparison that does the actual work was refused by the
        scheme clause before it was ever consulted, and could be replaced with
        `True` with the whole suite green. The two http ones reach it: one
        differs in host, one only in port, so a comparison that dropped the
        port half is caught as well.
        """
        other_port = self.port + 1 if self.port < 65535 else self.port - 1
        return (
            "https://attacker.example",
            "http://attacker.example",
            f"http://127.0.0.1:{other_port}",
        )

    def test_api_limits_rejects_cross_origin(self):
        for origin in self.cross_origins():
            with self.subTest(origin=origin):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(self.api_request(
                        "/api/limits", origin=origin))
                self.assertEqual(raised.exception.code, 403)

    def test_both_routes_report_the_same_recorded_usage(self):
        """The panel is painted from /api/data and refreshed from /api/limits.
        When only the endpoint enriched the windows, the first paint said "no
        usage recorded here yet" for thirty seconds while the database held a
        thousand turns."""
        with urllib.request.urlopen(self.api_request("/api/limits")) as resp:
            standalone = json.loads(resp.read())
        with urllib.request.urlopen(self.api_request("/api/data")) as resp:
            payload = json.loads(resp.read())
        self.assertNotIn("error", payload)
        embedded = payload.get("subscription_limits", {})
        # Assert the enrichment is THERE before asserting the two agree about
        # it: zipping two lists of windows that both carry recorded=None is a
        # comparison that cannot fail, which is what this test used to be.
        for route, windows in (("/api/limits", standalone.get("windows")),
                               ("/api/data", embedded.get("windows"))):
            self.assertEqual(len(windows or []), 1, route)
            self.assertEqual(windows[0].get("recorded"), self.expected_recorded,
                             f"{route} did not report what the database recorded")
        for a, b in zip(standalone["windows"], embedded["windows"]):
            self.assertEqual(a.get("window_start"), b.get("window_start"))
            self.assertEqual(a.get("recorded"), b.get("recorded"),
                             "the two routes disagree about recorded usage")

    def test_api_limits_agrees_with_the_copy_embedded_in_api_data(self):
        """Two routes, one definition — they must not be able to disagree about
        whether this install has plan windows at all."""
        with urllib.request.urlopen(self.api_request("/api/limits")) as resp:
            standalone = json.loads(resp.read())
        with urllib.request.urlopen(self.api_request("/api/data")) as resp:
            payload = json.loads(resp.read())
        self.assertNotIn("error", payload)
        embedded = payload.get("subscription_limits", {})
        self.assertTrue(standalone.get("available"), standalone.get("reason"))
        self.assertEqual(standalone.get("available"), embedded.get("available"))
        self.assertEqual(standalone.get("reason"), embedded.get("reason"))
        self.assertEqual(len(standalone.get("windows") or []),
                         len(embedded.get("windows") or []))

    # Every route behind _api_request_is_authorized, with the method that
    # reaches it. The rejection tests used to name /api/data alone, so the guard
    # on POST /api/rescan — the only endpoint that writes to the database and
    # the only one that costs minutes of CPU — was covered by nothing. Deleting
    # its three-line check left the suite green, and a form POST needs no CORS
    # preflight, so with the check gone any page the user visited could fire one.
    AUTHENTICATED_ROUTES = (
        ("GET", "/api/snapshot"),
        ("GET", "/api/snapshot?source=claude"),
        ("GET", "/api/data"),
        ("GET", "/api/sources"),
        ("GET", "/api/scan-status"),
        ("GET", "/api/limits"),
        ("POST", "/api/rescan"),
    )

    def test_snapshot_remains_readable_during_a_scan(self):
        import dashboard as app
        with urllib.request.urlopen(self.api_request("/api/data?source=claude")) as resp:
            expected = json.loads(resp.read())
        app._scan_activity_started()
        try:
            with mock.patch.object(app, "get_dashboard_data",
                                   side_effect=AssertionError("recomputed data")):
                with urllib.request.urlopen(self.api_request(
                        "/api/snapshot?source=claude")) as resp:
                    self.assertEqual(resp.headers["Cache-Control"], "no-store")
                    saved = json.loads(resp.read())["snapshot"]
            self.assertEqual(saved["data"], expected)
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(self.api_request("/api/data?source=claude"))
            self.assertEqual(raised.exception.code, 409)
        finally:
            app._scan_activity_finished(True)

    def test_snapshot_source_is_strictly_scoped(self):
        for query in ("source=../claude", "source=claude&source=codex", "source=", "other=x"):
            with self.subTest(query=query), self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(self.api_request("/api/snapshot?" + query))
            self.assertEqual(raised.exception.code, 400)

    def test_scan_status_reports_the_server_scan_activity(self):
        """The authenticated wire value follows the tracked worker itself."""
        entered = threading.Event()
        release = threading.Event()

        def scan():
            entered.set()
            release.wait()

        worker = dashboard.start_scan_thread(scan, threading.Thread)
        try:
            self.assertTrue(entered.wait(timeout=5))
            with urllib.request.urlopen(
                self.api_request("/api/scan-status")) as resp:
                self.assertEqual(resp.status, 200)
                status = json.loads(resp.read())
                self.assertEqual(status["state"], "scanning")
                self.assertIsInstance(status["generation"], int)
        finally:
            release.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        with urllib.request.urlopen(
                self.api_request("/api/scan-status")) as resp:
            status = json.loads(resp.read())
            self.assertEqual(status["state"], "idle")

    def test_data_routes_reject_a_payload_built_across_a_scan_generation(self):
        """A status check before a query is not enough.

        Another tab can start and finish a scan while a slow payload is being
        assembled.  The scanner commits incrementally, so returning that body
        can expose a mixture of provisional and final rows even though the
        status is idle again by the time the response is sent.
        """
        import claude_usage.dashboard_data as dashboard_data

        def crossed_generation(value):
            def build(*_args, **_kwargs):
                dashboard._scan_activity_started()
                dashboard._scan_activity_finished(True)
                return value
            return build

        cases = (
            ("/api/data", mock.patch.object(
                dashboard, "get_dashboard_data",
                side_effect=crossed_generation({"partial": True}))),
            ("/api/sources", mock.patch.object(
                dashboard_data, "available_sources",
                side_effect=crossed_generation([]))),
        )
        for path, patcher in cases:
            with self.subTest(path=path), patcher:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(self.api_request(path))
                self.assertEqual(raised.exception.code, 409)
                body = json.loads(raised.exception.read())
                self.assertEqual(body["scan"]["state"], "idle")
                self.assertIn("generation", body["scan"])

    def test_failed_scan_blocks_data_routes_until_a_successful_scan(self):
        """A failed ingestion cannot expose provisional rows as final data.

        Exercise the real handler for both database-derived routes.  The body
        is deliberately fixed: scan exceptions and local paths belong in the
        terminal, never in the page.  A later successful lifecycle transition
        must reopen the same routes without restarting the server.
        """
        class DeferredThread:
            def __init__(self, target, daemon):
                self.target = target
                self.daemon = daemon

            def start(self):
                pass

        failed = dashboard.start_scan_thread(lambda: False, DeferredThread)
        failed.target()
        failed_status = dashboard.scan_status()
        self.assertEqual(failed_status["state"], "failed")
        try:
            for path in ("/api/data", "/api/sources"):
                with self.subTest(path=path):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(self.api_request(path))
                    self.assertEqual(raised.exception.code, 503)
                    self.assertEqual(
                        json.loads(raised.exception.read()),
                        {"error": "Usage scan failed", "scan": failed_status},
                    )

            recovered = dashboard.start_scan_thread(
                lambda: True, DeferredThread)
            recovered.target()
            self.assertEqual(dashboard.scan_status()["state"], "idle")
            for path in ("/api/data", "/api/sources"):
                with self.subTest(recovered_path=path), \
                        urllib.request.urlopen(self.api_request(path)) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertIsInstance(json.loads(resp.read()), dict)
        finally:
            if dashboard.scan_status()["state"] != "idle":
                recovery = dashboard.start_scan_thread(
                    lambda: True, DeferredThread)
                recovery.target()

    def test_api_rejects_missing_or_invalid_token(self):
        for method, path in self.AUTHENTICATED_ROUTES:
            for token in (None, "wrong-token"):
                with self.subTest(path=path, token=token):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(
                            self.api_request(path, method=method, token=token))
                    self.assertEqual(raised.exception.code, 403)

    def test_api_rejects_cross_origin_request(self):
        for method, path in self.AUTHENTICATED_ROUTES:
            for origin in self.cross_origins():
                with self.subTest(path=path, origin=origin):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(self.api_request(
                            path,
                            method=method,
                            origin=origin,
                        ))
                    self.assertEqual(raised.exception.code, 403)

    def test_api_accepts_the_matching_same_origin(self):
        """The comparison has to accept as well as reject.

        Inverting it passes every rejection test above, and every browser sends
        an Origin on POST /api/rescan — so the page's own Rescan button would
        403 for every user, through a green CI and into a release.
        """
        for method, path in self.AUTHENTICATED_ROUTES:
            with self.subTest(path=path):
                with urllib.request.urlopen(self.api_request(
                    path,
                    method=method,
                    origin=f"http://127.0.0.1:{self.port}",
                )) as resp:
                    self.assertEqual(resp.status, 200)

    def test_server_rejects_untrusted_host_header(self):
        wrong_port = 1 if self.port == 65535 else self.port + 1
        for host in (
            "attacker.example",
            "user@127.0.0.1",
            "127.0.0.1:not-a-port",
            "127.0.0.1:",
            "127.0.0.1:0",
            f"127.0.0.1:{wrong_port}",
            "127.0.0.1/path",
        ):
            with self.subTest(host=host):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}/",
                    headers={"Host": host},
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(req)
                self.assertEqual(raised.exception.code, 421)

    def test_api_rejects_malformed_same_authority_origin(self):
        for method, path in self.AUTHENTICATED_ROUTES:
            for origin in (
                f"http://127.0.0.1:{self.port}/path",
                f"http://user@127.0.0.1:{self.port}",
                f"http://127.0.0.1:{self.port}?query",
            ):
                with self.subTest(path=path, origin=origin):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(self.api_request(
                            path,
                            method=method,
                            origin=origin,
                        ))
                    self.assertEqual(raised.exception.code, 403)

    def test_chart_asset_is_served_locally(self):
        url = f"http://127.0.0.1:{self.port}/assets/chart.umd.js"
        with urllib.request.urlopen(url) as resp:
            body = resp.read()
            self.assertEqual(resp.status, 200)
            self.assertIn("text/javascript", resp.headers["Content-Type"])
        self.assertIn(b"Chart.js v4.5.1", body)

    def test_health_endpoint_is_non_sensitive_and_requires_no_token(self):
        url = f"http://127.0.0.1:{self.port}/healthz"
        with urllib.request.urlopen(url) as resp:
            data = json.loads(resp.read())
        self.assertEqual(data["service"], "claude-usage")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(set(data), {"service", "status", "version"})

    def test_health_proof_secret_prevents_stale_service_confusion(self):
        import dashboard
        token = "instance-token-abcdefghijklmnopqrstuvwxyz"
        url = f"http://127.0.0.1:{self.port}/healthz"
        challenge = "challenge-abcdefghijklmnopqrstuvwxyz"
        with mock.patch.object(dashboard, "HEALTH_PROOF_SECRET", token):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(url)
            self.assertEqual(raised.exception.code, 404)

            with urllib.request.urlopen(
                    url + "?challenge=" + challenge) as resp:
                data = json.loads(resp.read())
            self.assertEqual(
                data["instance"], dashboard._health_proof(challenge, token)
            )

    def test_health_challenge_returns_a_proof_without_disclosing_its_secret(self):
        import dashboard
        token = "health-secret-abcdefghijklmnopqrstuvwxyz"
        challenge = "challenge-abcdefghijklmnopqrstuvwxyz"
        url = (f"http://127.0.0.1:{self.port}/healthz"
               f"?challenge={challenge}")
        with mock.patch.object(dashboard, "HEALTH_PROOF_SECRET", token):
            with urllib.request.urlopen(url) as resp:
                raw = resp.read()
        data = json.loads(raw)
        self.assertEqual(data["instance"], dashboard._health_proof(challenge, token))
        self.assertNotIn(token.encode("ascii"), raw)

    def test_index_has_security_headers_and_matching_nonce(self):
        import re
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url) as resp:
            body = resp.read().decode("utf-8")
            csp = resp.headers["Content-Security-Policy"]
            self.assertEqual(resp.headers["Cache-Control"], "no-store")
            self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(resp.headers["Referrer-Policy"], "no-referrer")
        nonces = set(re.findall(r'<script(?: src="[^"]+")? nonce="([^"]+)"', body))
        self.assertEqual(len(nonces), 1)
        nonce = nonces.pop()
        self.assertIn("script-src 'self' 'nonce-" + nonce + "'", csp)
        self.assertIn("script-src-attr 'none'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertNotIn("https:", csp)

    def test_api_rescan_returns_json(self):
        req = self.api_request("/api/rescan", method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            self.assertIn("new", data)
            self.assertIn("updated", data)
            self.assertIn("skipped", data)

    def test_api_rejects_overlapping_rescans(self):
        import dashboard
        dashboard.RESCAN_LOCK.acquire()
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(self.api_request("/api/rescan", method="POST"))
            self.assertEqual(raised.exception.code, 409)
        finally:
            dashboard.RESCAN_LOCK.release()

    def test_api_rescan_is_non_destructive(self):
        # Regression (#138): /api/rescan must NOT wipe the DB. usage.db is the
        # only durable store of history once Claude Code prunes old transcripts
        # (cleanupPeriodDays), so a rescan with nothing left on disk must keep
        # the existing rows. Seed history that has no corresponding JSONL file
        # (the projects dir is empty), rescan, and assert it survives.
        import dashboard as _d
        db_path = _d.DB_PATH
        conn = get_db(db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "pruned-sess", "project_name": "user/oldproject",
            "first_timestamp": "2026-01-01T09:00:00Z",
            "last_timestamp": "2026-01-01T10:00:00Z",
            "git_branch": "main", "model": "claude-opus-4-8",
            "total_input_tokens": 1000, "total_output_tokens": 400,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 1,
        }])
        insert_turns(conn, [{
            "session_id": "pruned-sess", "timestamp": "2026-01-01T09:30:00Z",
            "model": "claude-opus-4-8", "input_tokens": 1000,
            "output_tokens": 400, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "tool_name": None, "cwd": "/tmp",
            "message_id": "msg-pruned-1",
        }])
        conn.commit()
        conn.close()

        req = self.api_request("/api/rescan", method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)

        conn = sqlite3.connect(db_path)
        try:
            turn_count = conn.execute(
                "SELECT COUNT(*) FROM turns WHERE session_id = 'pruned-sess'"
            ).fetchone()[0]
            sess_count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE session_id = 'pruned-sess'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(turn_count, 1, "rescan must not delete existing turns")
        self.assertEqual(sess_count, 1, "rescan must not delete existing sessions")

    def test_404_for_unknown_path(self):
        url = f"http://127.0.0.1:{self.port}/nonexistent"
        try:
            urllib.request.urlopen(url)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_the_served_csp_follows_the_surface(self):
        """And the wiring from the SURFACE global to the response header: the
        branch is useless if `/` never passes the surface through."""
        import dashboard
        # A context manager, not a bare assignment: the same class-scoped server
        # serves test_index_injects_only_non_secret_app_config, which asserts
        # the config JSON still says "web".
        with mock.patch.object(dashboard, "SURFACE", "vscode"):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as resp:
                self.assertIn("frame-ancestors vscode-webview:",
                              resp.headers["Content-Security-Policy"])
                body = resp.read().decode("utf-8")
            self.assertIn("Command Palette: Claude Usage: Rescan Transcripts",
                          body)
            self.assertNotIn('"scan": "python cli.py scan"', body)
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as resp:
            self.assertIn("frame-ancestors 'none'",
                          resp.headers["Content-Security-Policy"])

    def test_index_injects_only_non_secret_app_config(self):
        # do_GET must substitute the __APP_CONFIG_JSON__ placeholder with a real
        # non-secret JSON object. The raw placeholder must never reach the
        # browser, or window.APP_CONFIG would be a syntax error.
        from scanner import VERSION
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url) as resp:
            body = resp.read().decode("utf-8")
        self.assertNotIn("__APP_CONFIG_JSON__", body)
        self.assertNotIn("__CSP_NONCE__", body)
        self.assertIn("window.APP_CONFIG =", body)
        self.assertIn(VERSION, body)
        self.assertNotIn(API_TOKEN, body)
        self.assertNotIn('"apiToken"', body)
        # The HTTP test server keeps the default surface ("web").
        self.assertIn('"surface": "web"', body)
        self.assertIn('"scan": "python cli.py scan"', body)

    def test_console_script_pages_name_the_installed_command(self):
        import dashboard
        with mock.patch.object(sys, "argv", ["/usr/local/bin/claude-usage"]), \
                mock.patch.dict(os.environ,
                                {"CLAUDE_USAGE_INVOKED_AS": ""}):
            commands = dashboard._commands_for_page("web")
        self.assertEqual(commands["scan"], "claude-usage scan")
        self.assertEqual(commands["diagnose"], "claude-usage stats")
        self.assertEqual(commands["reconnect"], "claude-usage url --open")


class TestHTMLTemplate(unittest.TestCase):
    def test_template_is_valid_html(self):
        self.assertIn("<!DOCTYPE html>", HTML_TEMPLATE)
        self.assertIn("</html>", HTML_TEMPLATE)

    def test_template_has_esc_function(self):
        """Verify XSS protection is present (PR #10)."""
        self.assertIn("function esc(", HTML_TEMPLATE)
        self.assertIn("'\"': '&quot;'", HTML_TEMPLATE)
        self.assertIn('"\'": \'&#39;\'', HTML_TEMPLATE)

    def test_template_has_chart_js(self):
        self.assertIn('src="/assets/chart.umd.js"', HTML_TEMPLATE)
        self.assertNotIn("cdn.jsdelivr.net", HTML_TEMPLATE)

    def test_template_has_substring_matching(self):
        """Verify getPricing falls back to substring match for unknown models."""
        self.assertIn("m.includes('opus')", HTML_TEMPLATE)
        self.assertIn("m.includes('sonnet')", HTML_TEMPLATE)
        self.assertIn("m.includes('haiku')", HTML_TEMPLATE)

    def test_unknown_models_return_null(self):
        """Verify getPricing returns null for non-Anthropic models."""
        self.assertIn("return null;", HTML_TEMPLATE)

    def test_hourly_chart_canvas_present(self):
        """Hourly distribution chart has a canvas + TZ toggle."""
        self.assertIn('id="chart-hourly"', HTML_TEMPLATE)
        self.assertIn('data-tz="local"', HTML_TEMPLATE)
        self.assertIn('data-tz="utc"', HTML_TEMPLATE)

    def test_hourly_peak_hour_constants(self):
        """The PDT-placement fallback, used when Pacific cannot be resolved.

        UTC 12–17 is Mon–Fri 05:00–11:00 PT *during PDT only*, which is why it is
        no longer the window: it is what `peakHoursUTCOn` degrades to for a day
        that will not parse, or where `Intl` cannot resolve Pacific. Both strings
        stay in the template deliberately — assert the fallback still exists, not
        that it is the answer.
        """
        self.assertIn('PEAK_HOURS_UTC', HTML_TEMPLATE)
        self.assertIn('[12, 13, 14, 15, 16, 17]', HTML_TEMPLATE)

    def test_today_range_option_present(self):
        """The 'Today' range is wired into RANGE_LABELS, RANGE_TICKS,
        getRangeBounds, and the filter-bar range dropdown."""
        self.assertIn("<option value=\"today\">", HTML_TEMPLATE)
        self.assertIn("'today': 'Today'", HTML_TEMPLATE)
        self.assertIn("'today': 1", HTML_TEMPLATE)
        # Bounds case: today returns start === end === today's ISO date
        self.assertIn("range === 'today'", HTML_TEMPLATE)

    def test_app_config_placeholder_present(self):
        """The head carries the server-substituted config placeholder and the
        footer carries the local-only version metadata."""
        self.assertIn("__APP_CONFIG_JSON__", HTML_TEMPLATE)
        self.assertIn("__CSP_NONCE__", HTML_TEMPLATE)
        self.assertIn("window.APP_CONFIG", HTML_TEMPLATE)
        self.assertIn('id="footer-meta"', HTML_TEMPLATE)
        self.assertIn("function initFooterMeta(", HTML_TEMPLATE)

    def test_api_token_comes_only_from_a_valid_url_fragment(self):
        self.assertIn("function readApiTokenFromFragment()", HTML_TEMPLATE)
        self.assertIn("getAll('token')", HTML_TEMPLATE)
        self.assertIn("values.length === 1", HTML_TEMPLATE)
        self.assertIn("API_TOKEN_PATTERN.test(values[0])", HTML_TEMPLATE)
        self.assertIn("'#token=' + encodeURIComponent(API_TOKEN)", HTML_TEMPLATE)
        self.assertNotIn("APP_CONFIG.apiToken", HTML_TEMPLATE)
        # A token-less load has to SAY so. It cannot fetch one — that is the
        # whole point of keeping the token out of this document — so the page
        # states what happened and names the command that gets the link back.
        self.assertIn("showAuthNotice", HTML_TEMPLATE)
        self.assertIn("This link has no access token", HTML_TEMPLATE)
        self.assertIn("cli.py url", HTML_TEMPLATE)

    def test_the_page_never_contains_a_usable_token(self):
        """The document is served unauthenticated, so anything token-shaped in
        it is readable by any local account that can reach the port."""
        import re as _re
        from dashboard import API_TOKEN
        self.assertNotIn(API_TOKEN, HTML_TEMPLATE)
        # Nothing that merely LOOKS like one either — a 32+ char urlsafe run
        # sitting next to the word token would be the same leak by another name.
        for match in _re.finditer(r"token[\"'=: ]{1,4}([A-Za-z0-9_-]{32,128})",
                                  HTML_TEMPLATE, _re.IGNORECASE):
            self.fail(f"token-shaped literal in the page: {match.group(1)[:12]}…")

    def test_dashboard_has_no_automatic_external_request(self):
        self.assertNotIn("api.github.com", HTML_TEMPLATE)
        self.assertNotIn("fetch('https://", HTML_TEMPLATE)
        self.assertNotIn('fetch("https://', HTML_TEMPLATE)

    def test_template_has_no_inline_event_handlers(self):
        import re
        self.assertIsNone(re.search(r"\son(?:click|change|load|error)=", HTML_TEMPLATE))
        self.assertIn("script-src-attr 'none'", __import__("dashboard")._content_security_policy("nonce", "web"))

    def test_untrusted_dispatch_cells_are_html_escaped(self):
        self.assertIn("${esc(d.tool_uses != null ? d.tool_uses : '—')}", HTML_TEMPLATE)
        self.assertIn("${esc(d.turns)}", HTML_TEMPLATE)

    def test_untrusted_grouping_keys_cannot_pollute_object_prototypes(self):
        for name in (
            "dailyMap", "modelMap", "projMap", "projBranchMap",
            "subagentTypeMap",
        ):
            self.assertIn(f"const {name} = Object.create(null);", HTML_TEMPLATE)
        self.assertIn("hasOwnProperty.call(PRICING, model)", HTML_TEMPLATE)

    def test_csv_export_neutralizes_spreadsheet_formulas(self):
        self.assertIn(
            r"/^[\s\u0000-\u001f\u007f-\u009f]*[=+\-@]/u",
            HTML_TEMPLATE,
        )
        self.assertIn('s = "\'" + s', HTML_TEMPLATE)


class TestDashboardSecurityHelpers(unittest.TestCase):
    def test_fresh_process_honors_trusted_api_token_environment(self):
        token = "b" * 43
        env = os.environ.copy()
        env["CLAUDE_USAGE_API_TOKEN"] = token
        # Pin BOTH ends of the pipe. `encoding=` alone fixes only the parent:
        # a Python child left to `locale.getencoding()` encodes its stdout as
        # cp1252 on windows-latest, and decoding that as UTF-8 here is a NEW
        # mismatch — a worse one, because cp1252 decoding never raises while
        # UTF-8 does. No `errors=`: mojibake that quietly satisfies an
        # assertion is the defect this rule exists to remove.
        env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [sys.executable, "-c", "import dashboard; print(dashboard.API_TOKEN)"],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.stdout.strip(), token)

    def test_authenticated_url_keeps_token_in_fragment(self):
        token = "a" * 43
        self.assertEqual(
            authenticated_dashboard_url("127.0.0.1", 8080, token),
            f"http://127.0.0.1:8080/#token={token}"
            f"&liveness={dashboard.LIVENESS_TOKEN}",
        )
        self.assertEqual(
            authenticated_dashboard_url("::1", 8080, token),
            f"http://[::1]:8080/#token={token}"
            f"&liveness={dashboard.LIVENESS_TOKEN}",
        )
        with self.assertRaises(ValueError):
            authenticated_dashboard_url("127.0.0.1", 8080, "predictable")

    def test_script_json_cannot_break_out_of_script_element(self):
        # The `&` has to be IN the payload. Without one, assertNotIn("&", ...)
        # held whether or not the escape existed, so the only rule of the three
        # that json.dumps does not already handle was covered by nothing.
        payload = {"surface": "</script><script>alert(1)</script>",
                   "entity": "fish & chips &amp; more", "line": "\u2028"}
        encoded = _script_safe_json(payload)
        self.assertNotIn("<", encoded)
        self.assertNotIn(">", encoded)
        self.assertNotIn("&", encoded)
        self.assertIn("\\u0026", encoded)
        self.assertEqual(json.loads(encoded), payload)

    def test_the_csp_lets_only_the_vscode_surface_be_framed(self):
        """The one line that decides whether the extension's sidebar can embed
        the page at all — and it was called once in the whole suite, with
        "web", so both directions could be broken with CI green."""
        self.assertIn("frame-ancestors vscode-webview:",
                      _content_security_policy("n", "vscode"))
        self.assertIn("frame-ancestors 'none'",
                      _content_security_policy("n", "web"))

    def test_non_loopback_bind_is_rejected_by_default(self):
        for host in ("127.0.0.1", "::1"):
            self.assertEqual(validate_bind_host(host), host)
        self.assertEqual(validate_bind_host("localhost"), "127.0.0.1")
        self.assertEqual(validate_bind_host("LOCALHOST."), "127.0.0.1")
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_ALLOW_CONTAINER_BIND": ""}):
            with self.assertRaises(ValueError):
                validate_bind_host("0.0.0.0")
            with self.assertRaises(ValueError):
                validate_bind_host("192.0.2.10")

    def test_ipv6_loopback_selects_an_ipv6_server(self):
        self.assertEqual(dashboard.DashboardHTTPServer.address_family,
                         socket.AF_INET)
        self.assertEqual(dashboard.DashboardHTTPServerV6.address_family,
                         socket.AF_INET6)

        selected = []

        class FakeServer:
            def __init__(self, address, handler):
                selected.append((address, handler))
                self.server_address = address

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                pass

        with mock.patch.object(dashboard, "DashboardHTTPServerV6", FakeServer), \
                mock.patch.object(dashboard, "write_url_file"), \
                mock.patch.object(dashboard, "remove_url_file"), \
                mock.patch("builtins.print"):
            dashboard.serve(host="::1", port=8123)

        self.assertEqual(selected, [(("::1", 8123), DashboardHandler)])

    def assert_url_file_was_redirected(self, real):
        """The two tests below drive the real serve(), which writes URL_FILE and
        then unlinks it.

        Nothing about either test fails if that redirection is ever dropped —
        they just do it to the developer's own ~/.claude/dashboard-url, where
        write_url_file truncates whatever is there and remove_url_file's
        `only_if` then matches what the test itself just wrote, deleting the
        link of a dashboard they actually have running. Both reported OK while
        doing exactly that, so the guard has to be stated.
        """
        import dashboard
        self.assertNotEqual(
            dashboard.URL_FILE, real,
            "serve() is still pointed at the developer's real dashboard-url")

    def test_serve_canonicalizes_localhost_before_binding(self):
        import dashboard

        fake_server = mock.Mock()
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        # URL_FILE is a module global derived from Path.home() at import, and
        # serve() reaches it directly.
        real_url_file = dashboard.URL_FILE
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            dashboard, "URL_FILE", Path(tmpdir) / "dashboard-url"
        ), mock.patch.object(
            dashboard, "DashboardHTTPServer", return_value=fake_server
        ) as server_class, mock.patch.dict(
            os.environ, {dashboard.SUPPRESS_AUTH_URL_ENV: "1"}
        ), mock.patch("builtins.print"):
            self.assert_url_file_was_redirected(real_url_file)
            dashboard.serve(host="localhost", port=18080)
        server_class.assert_called_once_with(
            ("127.0.0.1", 18080), dashboard.DashboardHandler
        )
        fake_server.server_close.assert_called_once()

    def test_serve_leaves_the_url_readable_only_while_it_serves(self):
        """Covers the WIRING of the link-recovery feature, not the helpers.

        `write_url_file` and `remove_url_file` are tested in isolation, and
        `cmd_url`'s use of them is tested — but serve()'s two call sites were
        covered by nothing, so either could be dropped in a refactor of the
        try/finally with the suite green and every reader who closed the
        launching terminal left with no way back.
        """
        import dashboard

        expected = dashboard.authenticated_dashboard_url("127.0.0.1", 18080)
        real_url_file = dashboard.URL_FILE
        seen = []
        fake_server = mock.Mock()

        def serving():
            # The only moment the file is supposed to exist. serve() has
            # already canonicalized "localhost", so the URL names 127.0.0.1.
            seen.append(dashboard.read_url_file())
            raise KeyboardInterrupt

        fake_server.serve_forever.side_effect = serving
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            dashboard, "URL_FILE", Path(tmpdir) / "dashboard-url"
        ), mock.patch.object(
            dashboard, "DashboardHTTPServer", return_value=fake_server
        ), mock.patch.dict(
            os.environ, {dashboard.SUPPRESS_AUTH_URL_ENV: "1"}
        ), mock.patch("builtins.print"):
            self.assert_url_file_was_redirected(real_url_file)
            dashboard.serve(host="localhost", port=18080)
            after = dashboard.read_url_file()
        self.assertEqual(seen, [expected], "serve() left no link while it served")
        self.assertIsNone(after, "the link outlived the server that owned it")

    def test_container_wildcard_bind_requires_explicit_opt_in(self):
        import dashboard
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_ALLOW_CONTAINER_BIND": "1"}):
            with mock.patch.object(dashboard, "_running_in_docker", return_value=True):
                self.assertEqual(validate_bind_host("0.0.0.0"), "0.0.0.0")
            with mock.patch.object(dashboard, "_running_in_docker", return_value=False):
                with self.assertRaises(ValueError):
                    validate_bind_host("0.0.0.0")

    def test_vendored_chart_asset_is_pinned_and_network_inert(self):
        import hashlib
        chart = find_chart_file()
        self.assertIsNotNone(chart)
        body = chart.read_bytes()
        self.assertEqual(
            hashlib.sha256(body).hexdigest(),
            "ecc3cd1eeb8c34d2178e3f59fd63ec5a3d84358c11730af0b9958dc886d7652a",
        )
        text = body.decode("utf-8")
        for primitive in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "eval(", "new Function"):
            self.assertNotIn(primitive, text)

    def test_tampered_chart_asset_is_rejected(self):
        import dashboard
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "vendor").mkdir()
            (root / "vendor" / "chart.umd.js").write_text(
                "tampered", encoding="utf-8")
            with mock.patch.object(dashboard, "__file__", str(root / "dashboard.py")):
                self.assertIsNone(dashboard.find_chart_file())


class TestPricingParity(unittest.TestCase):
    """Verify CLI and dashboard pricing tables stay in sync."""

    def _extract_js_pricing(self):
        """Extract pricing values from the dashboard JS PRICING object."""
        import re
        prices = {}
        for match in re.finditer(
            r"'([a-z0-9.\-]+)':\s*\{\s*input:\s*([\d.]+),\s*output:\s*([\d.]+)",
            HTML_TEMPLATE
        ):
            model, inp, out = match.group(1), float(match.group(2)), float(match.group(3))
            prices[model] = {"input": inp, "output": out}
        return prices

    def test_all_cli_models_in_dashboard(self):
        from cli import PRICING as CLI_PRICING
        js_prices = self._extract_js_pricing()
        for model in CLI_PRICING:
            self.assertIn(model, js_prices, f"{model} missing from dashboard JS")

    def test_prices_match(self):
        from cli import PRICING as CLI_PRICING
        js_prices = self._extract_js_pricing()
        for model in CLI_PRICING:
            self.assertAlmostEqual(
                CLI_PRICING[model]["input"], js_prices[model]["input"],
                msg=f"{model} input price mismatch"
            )
            self.assertAlmostEqual(
                CLI_PRICING[model]["output"], js_prices[model]["output"],
                msg=f"{model} output price mismatch"
            )


def _unused_port():
    """A port nothing is listening on, for the dead-server case."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _stalled_port(test):
    """A port that accepts the connection and then answers nothing, ever.

    The shape a dashboard presents while it is paused mid-request — SIGSTOP, a
    machine resuming from sleep — and the shape of a stale port taken over by
    an unrelated local peer that accepts and hangs. Indistinguishable from a
    dead server to anything that only asks "did the probe succeed?".
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    test.addCleanup(listener.close)
    held = []
    test.addCleanup(lambda: [conn.close() for conn in held])

    def accept_forever():
        while True:
            try:
                held.append(listener.accept()[0])
            except OSError:
                return

    threading.Thread(target=accept_forever, daemon=True).start()
    return listener.getsockname()[1]


class TestUrlFileRecovery(unittest.TestCase):
    """Losing the link used to be a dead end.

    The token is deliberately absent from the page — another local account could
    otherwise recover it by requesting `/` — so a browser sitting at the bare
    address cannot authorize itself, and before this the only way back was to
    restart the server. A running server now leaves its URL where the same user,
    and only the same user, can read it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "sub" / "dashboard-url"

    def test_a_running_server_leaves_a_readable_url(self):
        import dashboard
        written = dashboard.write_url_file("127.0.0.1", 8080, self.path)
        self.assertEqual(written, self.path)
        url = dashboard.read_url_file(self.path)
        self.assertEqual(url, dashboard.authenticated_dashboard_url("127.0.0.1", 8080))
        self.assertIn("#token=", url)

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits only")
    def test_the_url_is_no_more_readable_than_the_database(self):
        """It carries the token, so it is exactly as sensitive as usage.db."""
        import dashboard
        dashboard.write_url_file("127.0.0.1", 8080, self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_stopping_the_server_removes_it(self):
        """A URL for a dead server is a token lying around for no reason."""
        import dashboard
        dashboard.write_url_file("127.0.0.1", 8080, self.path)
        dashboard.remove_url_file(self.path)
        self.assertFalse(self.path.exists())
        self.assertIsNone(dashboard.read_url_file(self.path))

    def test_no_file_means_no_url_rather_than_a_crash(self):
        import dashboard
        self.assertIsNone(dashboard.read_url_file(self.path))

    def test_removing_an_absent_url_does_not_create_its_parent(self):
        import dashboard
        self.assertFalse(self.path.parent.exists())
        dashboard.remove_url_file(self.path)
        self.assertFalse(
            self.path.parent.exists(),
            "cleanup created a directory and sidecar for an absent URL",
        )

    def test_a_tampered_file_is_refused(self):
        """`cli.py url --open` launches whatever this says, so it must not be
        able to say anything but a loopback dashboard URL."""
        import dashboard
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for bad in ("http://evil.example/#token=" + "a" * 40,
                    "https://127.0.0.1:8080/#token=" + "a" * 40,
                    "http://127.0.0.1:8080/#token=short",
                    "http://127.0.0.1:99999/#token=" + "a" * 40,
                    "file:///etc/passwd",
                    "http://127.0.0.1:8080/",
                    ""):
            with self.subTest(contents=bad):
                self.path.write_text(bad, encoding="utf-8")
                self.assertIsNone(dashboard.read_url_file(self.path))

    def test_a_url_for_a_dead_server_is_not_offered(self):
        """A crash, a SIGTERM or a closed terminal never runs the cleanup, so
        the file outlives the server. Printing it anyway hands the reader a link
        that silently does nothing — and `--open` launches a browser at it,
        which is the exact failure this command exists to prevent."""
        import dashboard
        dashboard.write_url_file("127.0.0.1", _unused_port(), self.path)
        self.assertIsNotNone(dashboard.read_url_file(self.path),
                             "the file is there; the question is whether it works")
        self.assertIs(dashboard.url_is_live(dashboard.read_url_file(self.path),
                                            timeout=0.4), False,
                      "a refused connection is proof of death, not a failed probe")

    def test_a_probe_that_could_not_tell_says_so(self):
        """A failed probe is not evidence of a dead server.

        The answer feeds a deletion of the only way back into a running
        dashboard, so "the request did not complete" and "this link is dead"
        cannot be the same answer. A server paused mid-request accepts the
        connection and never replies; before this it was reported dead."""
        import dashboard
        url = dashboard.authenticated_dashboard_url(
            "127.0.0.1", _stalled_port(self), "a" * 43)
        self.assertIsNone(dashboard.url_is_live(url, timeout=0.4),
                          "a stalled server was reported as definitively dead")

    def test_the_liveness_probe_never_sends_the_api_bearer(self):
        """A stale port may belong to any local process after a crash."""
        import dashboard

        api_token = "s" * 43
        liveness_token = "l" * 43

        class Response:
            status = 200

            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, amount=-1):
                return self.body[:amount] if amount >= 0 else self.body

        requests = []

        def answer(request, timeout=None):
            requests.append(request)
            from urllib.parse import parse_qs, urlparse
            challenge = parse_qs(urlparse(request.full_url).query)["challenge"][0]
            proof = dashboard._liveness_proof(
                challenge, api_token, liveness_token
            )
            return Response(json.dumps({"proof": proof}).encode("utf-8"))

        url = dashboard.authenticated_dashboard_url(
            "127.0.0.1", 8080, api_token, liveness_token
        )
        with mock.patch.object(dashboard, "open_loopback_probe", side_effect=answer):
            self.assertIs(dashboard.url_is_live(url), True)
        self.assertEqual(len(requests), 1)
        headers = {name.lower(): value
                   for name, value in requests[0].header_items()}
        self.assertNotIn(
            dashboard.API_TOKEN_HEADER.lower(), headers,
            "the bearer was disclosed to the unverified port owner",
        )
        self.assertNotIn(api_token, requests[0].full_url)
        self.assertNotIn(liveness_token, requests[0].full_url)

    def test_a_liveness_proof_cannot_be_replayed_for_the_next_probe(self):
        import dashboard
        from urllib.parse import parse_qs, urlparse

        api_token = "a" * 43
        liveness_token = "b" * 43
        first_challenge = None
        requests = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, amount=-1):
                proof = dashboard._liveness_proof(
                    first_challenge, api_token, liveness_token
                )
                return json.dumps({"proof": proof}).encode("utf-8")

        def replay_first_proof(request, timeout=None):
            nonlocal first_challenge
            challenge = parse_qs(urlparse(request.full_url).query)["challenge"][0]
            requests.append(challenge)
            if first_challenge is None:
                first_challenge = challenge
            return Response()

        url = dashboard.authenticated_dashboard_url(
            "127.0.0.1", 8080, api_token, liveness_token
        )
        with mock.patch.object(
                dashboard, "open_loopback_probe", side_effect=replay_first_proof):
            self.assertIs(dashboard.url_is_live(url), True)
            self.assertIs(dashboard.url_is_live(url), False)
        self.assertEqual(len(requests), 2)
        self.assertNotEqual(requests[0], requests[1])

    def test_a_response_body_cannot_renew_the_total_liveness_deadline(self):
        """Headers are not completion; a body may drip forever after them."""
        import dashboard

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
                return b"{}"

        url = dashboard.authenticated_dashboard_url(
            "127.0.0.1", 8080, "a" * 43, "b" * 43
        )
        began = time.monotonic()
        try:
            with mock.patch.object(dashboard, "open_loopback_probe", return_value=Response()):
                self.assertIsNone(dashboard.url_is_live(url, timeout=0.05))
            self.assertTrue(started.wait(1), "the fixture never reached the body")
            self.assertLess(time.monotonic() - began, 1)
        finally:
            release.set()
        self.assertTrue(closed.wait(1), "the released response was not closed")

    def test_one_server_does_not_delete_another_servers_link(self):
        """Two dashboards share one file, so the last to start owns it. Without
        a guard the second one's shutdown would make the first unreachable via
        `cli.py url` while it is still running perfectly well."""
        import dashboard
        dashboard.write_url_file("127.0.0.1", 9001, self.path)   # server A
        dashboard.write_url_file("127.0.0.1", 9002, self.path)   # server B takes over
        # A stops first and must not remove B's link.
        dashboard.remove_url_file(
            self.path, only_if=dashboard.authenticated_dashboard_url("127.0.0.1", 9001))
        self.assertEqual(dashboard.read_url_file(self.path),
                         dashboard.authenticated_dashboard_url("127.0.0.1", 9002))
        # B stops, and does remove its own.
        dashboard.remove_url_file(
            self.path, only_if=dashboard.authenticated_dashboard_url("127.0.0.1", 9002))
        self.assertIsNone(dashboard.read_url_file(self.path))

    def test_a_writer_waits_for_the_guarded_remove_transaction(self):
        """The ownership check and unlink are one transaction with writers.

        Pausing server A after it has read its own URL used to leave a window
        where server B could replace the file, only for A to unlink B's new
        recovery URL.  The writer must not reach its atomic replace until A's
        conditional removal has completed.
        """
        import dashboard

        a_url = dashboard.authenticated_dashboard_url("127.0.0.1", 9001)
        b_url = dashboard.authenticated_dashboard_url("127.0.0.1", 9002)
        dashboard.write_url_file("127.0.0.1", 9001, self.path)
        original_read = dashboard.read_url_file
        original_replace = dashboard.os.replace
        remover_read = threading.Event()
        release_remover = threading.Event()
        writer_replaced = threading.Event()
        results = []

        def paused_read(path=None):
            result = original_read(path)
            remover_read.set()
            release_remover.wait(5)
            return result

        def observed_replace(source, destination):
            if Path(destination) == self.path:
                writer_replaced.set()
            return original_replace(source, destination)

        remover = threading.Thread(
            target=lambda: dashboard.remove_url_file(self.path, only_if=a_url),
            daemon=True,
        )
        writer = threading.Thread(
            target=lambda: results.append(
                dashboard.write_url_file("127.0.0.1", 9002, self.path)),
            daemon=True,
        )
        try:
            with mock.patch.object(
                    dashboard, "read_url_file", side_effect=paused_read), \
                    mock.patch.object(
                        dashboard.os, "replace", side_effect=observed_replace):
                remover.start()
                self.assertTrue(
                    remover_read.wait(1),
                    "the remover never reached its ownership check",
                )
                writer.start()
                self.assertFalse(
                    writer_replaced.wait(0.25),
                    "the writer overtook a conditional removal in progress",
                )
                release_remover.set()
                remover.join(2)
                writer.join(2)
        finally:
            release_remover.set()
            remover.join(2)
            writer.join(2)

        self.assertFalse(remover.is_alive(), "the remover did not finish")
        self.assertFalse(writer.is_alive(), "the writer did not finish")
        self.assertEqual(results, [self.path])
        self.assertEqual(dashboard.read_url_file(self.path), b_url)

    def test_a_writer_waits_for_the_cross_process_sidecar_lock(self):
        """The in-process lock is not mistaken for cross-process coverage."""
        import dashboard

        ready = Path(self._tmp.name) / "child-ready"
        release = Path(self._tmp.name) / "release-child"
        script = "\n".join((
            "import sys, time",
            "from pathlib import Path",
            "from claude_usage import dashboard",
            "target, ready, release = map(Path, sys.argv[1:])",
            "with dashboard._url_file_lock(target) as locked:",
            "    if not locked:",
            "        raise SystemExit(3)",
            "    ready.touch()",
            "    while not release.exists():",
            "        time.sleep(0.01)",
        ))
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.path), str(ready),
             str(release)],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )

        def stop_child():
            if process.poll() is None:
                try:
                    release.touch()
                except OSError:
                    # Cleanup must still reap the child if an earlier failure
                    # already removed its temporary signalling directory.
                    pass
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=2)
            else:
                process.communicate()

        self.addCleanup(stop_child)
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                self.fail("the child did not acquire the URL sidecar lock")
            time.sleep(0.01)
        if not ready.exists():
            stdout, stderr = process.communicate(timeout=2)
            self.fail(
                f"the child failed before acquiring the lock: "
                f"stdout={stdout!r}, stderr={stderr!r}"
            )

        result = []
        writer_done = threading.Event()

        def write():
            result.append(
                dashboard.write_url_file("127.0.0.1", 9002, self.path))
            writer_done.set()

        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        try:
            self.assertFalse(
                writer_done.wait(0.25),
                "a second process's advisory lock did not block the writer",
            )
        finally:
            release.touch()
            child_stdout, child_stderr = process.communicate(timeout=5)
            writer.join(5)
        self.assertFalse(writer.is_alive(), "the released writer did not finish")
        self.assertEqual(
            process.returncode, 0,
            f"child stdout={child_stdout!r}, stderr={child_stderr!r}",
        )
        self.assertEqual(result, [self.path])

    def test_the_sidecar_is_retained_after_the_url_is_removed(self):
        """Deleting a lock inode after unlock would split the next waiters."""
        import dashboard

        dashboard.write_url_file("127.0.0.1", 9001, self.path)
        lock_path = dashboard._url_lock_path(self.path)
        self.assertTrue(lock_path.is_file())
        dashboard.remove_url_file(self.path)
        self.assertFalse(self.path.exists())
        self.assertTrue(lock_path.is_file(), "the stable sidecar was removed")
        if os.name == "posix":
            self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)

    def test_an_unsafe_sidecar_fails_closed(self):
        """Neither writer nor remover proceeds without a trustworthy lock."""
        import dashboard

        a_url = dashboard.authenticated_dashboard_url("127.0.0.1", 9001)
        dashboard.write_url_file("127.0.0.1", 9001, self.path)
        lock_path = dashboard._url_lock_path(self.path)
        lock_path.unlink()
        lock_path.mkdir()

        self.assertIsNone(
            dashboard.write_url_file("127.0.0.1", 9002, self.path),
            "the writer proceeded with a non-regular lock path",
        )
        dashboard.remove_url_file(self.path, only_if=a_url)
        self.assertEqual(
            dashboard.read_url_file(self.path), a_url,
            "the remover proceeded without acquiring a safe lock",
        )

    def test_an_unguarded_removal_still_works(self):
        """The plain call is what a caller with no port in hand uses."""
        import dashboard
        dashboard.write_url_file("127.0.0.1", 9003, self.path)
        dashboard.remove_url_file(self.path)
        self.assertIsNone(dashboard.read_url_file(self.path))

    def _run_cmd_url(self, open_browser=False):
        """Run the actual command against this fixture's file, capturing what a
        user would see — and every browser it would have launched."""
        import contextlib, io, dashboard, webbrowser
        from cli import cmd_url
        buffer = io.StringIO()
        opened = []
        with mock.patch.object(dashboard, "URL_FILE", self.path):
            with mock.patch.object(webbrowser, "open", opened.append):
# stderr is merged into the same buffer: these assertions are about the
                # MESSAGE the user gets, not the stream it arrives on, and the diagnostics
                # moved to stderr when `url`'s stdout was narrowed to a URL or nothing.
                # Tests that ARE about the stream capture the two separately -- see
                # tests/test_cli_streams.py and test_the_rebuild_is_announced_on_stderr_only.
                with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                    try:
                        cmd_url(open_browser=open_browser)
                        code = 0
                    except SystemExit as exit_code:
                        code = exit_code.code
        return code, buffer.getvalue(), opened

    def test_the_command_refuses_to_print_a_dead_link(self):
        """Covers the WIRING, not just the helpers: with both `url_is_live` and
        `read_url_file` tested in isolation, removing the liveness check from
        the command left every test green while it went back to handing out
        links to servers that are not there."""
        import dashboard
        dashboard.write_url_file("127.0.0.1", _unused_port(), self.path)
        code, output, opened = self._run_cmd_url(open_browser=True)
        self.assertEqual(code, 1)
        self.assertIn("No running dashboard", output)
        self.assertNotIn("#token=", output, "a dead link was printed anyway")
        self.assertEqual(opened, [], "a browser was launched at a dead port")
        self.assertFalse(self.path.exists(), "the stale file was left behind")

    def test_an_unreachable_server_keeps_its_link(self):
        """The file is the ONLY way back into a running dashboard, so a probe
        that proved nothing must not spend it.

        Before this, one stalled request deleted the file and every later
        invocation said "No running dashboard found" — permanently, while the
        server went on serving. The link still is not printed and `--open`
        still launches nothing: unverified is not the same as live either."""
        import dashboard
        dashboard.write_url_file("127.0.0.1", _stalled_port(self), self.path)
        code, output, opened = self._run_cmd_url(open_browser=True)
        self.assertEqual(code, 1)
        self.assertNotIn("#token=", output, "an unverified link was printed")
        self.assertEqual(opened, [], "a browser was launched at an unverified port")
        self.assertTrue(self.path.exists(),
                        "one failed probe threw away the only way back in")
        self.assertIsNotNone(dashboard.read_url_file(self.path))
        self.assertIn("Could not reach", output,
                      "the reader was told the dashboard is gone, not unreachable")

    def test_a_dashboard_that_starts_during_the_probe_keeps_its_link(self):
        """The removal is guarded on the file still naming what was probed.

        `read_url_file` and the verdict are a network round trip apart, and
        `write_url_file` runs once — at startup — so a dashboard that takes the
        file over inside that window loses its only link for its whole life,
        which is the outcome AGENTS.md calls "spent the reader's only link for
        good". `serve()`'s shutdown has used `only_if` for this since it was
        written; `cmd_url` held the exact URL it probed and did not.

        The stub stands in for a probe that is slow enough to be overtaken —
        it does what a real one does, in the order a real one does it. And the
        message matters as much as the file: preserving B's link while still
        printing "No running dashboard found" would be the opposite of the
        truth, so both halves are asserted."""
        import dashboard
        dashboard.write_url_file("127.0.0.1", 9001, self.path)     # stale: server A
        b_url = None

        def probe_overtaken_by_server_b(url, timeout=1.5):
            nonlocal b_url
            dashboard.write_url_file("127.0.0.1", 9002, self.path)  # server B starts
            b_url = dashboard.read_url_file(self.path)
            return False                                           # A really is dead

        with mock.patch.object(dashboard, "url_is_live", probe_overtaken_by_server_b):
            code, output, opened = self._run_cmd_url(open_browser=True)
        self.assertEqual(code, 1)
        self.assertEqual(opened, [], "a browser was launched at an unprobed link")
        self.assertEqual(dashboard.read_url_file(self.path), b_url,
                         "a probe of A's link deleted B's")
        self.assertNotIn("No running dashboard", output,
                         "a live dashboard's link was in the file and the user "
                         "was told to start another one")

    def test_the_command_reports_nothing_when_there_is_no_file(self):
        code, output, _ = self._run_cmd_url()
        self.assertEqual(code, 1)
        self.assertIn("No running dashboard", output)

    @unittest.skipUnless(os.name == "posix", "creating a symlink needs privilege on Windows")
    def test_a_symlinked_path_is_neither_written_nor_read(self):
        """A pre-created symlink would otherwise redirect the token elsewhere.

        POSIX-only because creating the symlink requires Developer Mode or
        elevation on Windows. The guard the test protects is not skipped there —
        `write_url_file` still refuses a symlinked path — only the setup is.
        """
        import dashboard
        self.path.parent.mkdir(parents=True, exist_ok=True)
        target = self.path.parent / "elsewhere"
        self.path.symlink_to(target)
        self.assertIsNone(dashboard.write_url_file("127.0.0.1", 8080, self.path))
        self.assertFalse(target.exists(), "the token was written through the link")
        self.assertIsNone(dashboard.read_url_file(self.path))

    def test_rewriting_truncates_rather_than_leaving_a_stale_tail(self):
        import dashboard
        dashboard.write_url_file("127.0.0.1", 65535, self.path)
        dashboard.write_url_file("127.0.0.1", 80, self.path)
        self.assertEqual(dashboard.read_url_file(self.path),
                         dashboard.authenticated_dashboard_url("127.0.0.1", 80))

    def test_a_symlink_swap_immediately_before_replace_cannot_receive_the_token(self):
        """The final operation replaces the directory entry, never its target."""
        import dashboard

        self.path.parent.mkdir(parents=True, exist_ok=True)
        victim = self.path.parent / "attacker-readable"
        victim.write_text("victim contents\n", encoding="utf-8")
        original_replace = os.replace
        swapped = False

        def replace_after_swap(source, destination):
            nonlocal swapped
            self.assertEqual(Path(destination), self.path)
            self.path.unlink(missing_ok=True)
            try:
                self.path.symlink_to(victim)
            except (OSError, NotImplementedError) as exc:
                raise unittest.SkipTest(
                    f"symlink creation is unavailable: {exc}") from exc
            swapped = True
            return original_replace(source, destination)

        with mock.patch.object(dashboard.os, "replace",
                               side_effect=replace_after_swap):
            written = dashboard.write_url_file("127.0.0.1", 8080, self.path)

        self.assertTrue(swapped, "the destination was not swapped before replace")
        self.assertEqual(written, self.path)
        self.assertEqual(victim.read_text(encoding="utf-8"), "victim contents\n")
        self.assertFalse(self.path.is_symlink())
        self.assertEqual(
            dashboard.read_url_file(self.path),
            dashboard.authenticated_dashboard_url("127.0.0.1", 8080),
        )

    @unittest.skipUnless(os.name == "posix", "hard links need elevation on Windows")
    def test_a_hard_linked_path_is_not_written_through(self):
        """`O_NOFOLLOW` does not see a hard link, and this file carries a token.

        A symlink and a hard link are different objects: `O_NOFOLLOW` refuses the
        first and is blind to the second, so a path pre-created as a second name
        for a file the attacker can read used to receive the API token verbatim.
        `st_nlink` is the only thing that sees it -- which is how
        `db.secure_db_permissions` already refuses a hard-linked database, and
        this is that same refusal given to the token file.

        Three separate harms are asserted, because closing only the first would
        leave the mechanism open: the token must not be written, the victim's
        mode must not be rewritten to 0600 by the `fchmod`, and the victim's
        bytes must not be truncated. The last is why `O_TRUNC` is not in the open
        flags -- truncation there happens before any check can run.
        """
        import dashboard
        self.path.parent.mkdir(parents=True, exist_ok=True)
        victim = self.path.parent / "attacker-readable"
        victim.write_text("victim contents\n", encoding="utf-8")
        os.chmod(victim, 0o644)
        os.link(victim, self.path)
        self.assertEqual(victim.stat().st_nlink, 2, "setup did not hard-link")

        self.assertIsNone(dashboard.write_url_file("127.0.0.1", 8080, self.path))
        after = victim.read_text(encoding="utf-8")
        self.assertNotIn("token=", after, "the API token went through a hard link")
        self.assertEqual(after, "victim contents\n",
                         "the victim's bytes were truncated or overwritten")
        self.assertEqual(victim.stat().st_mode & 0o777, 0o644,
                         "fchmod rewrote a file this function had no business "
                         "touching")

    @unittest.skipUnless(os.name == "posix", "POSIX ownership only")
    def test_a_file_owned_by_another_user_is_not_overwritten(self):
        """Ownership is checked on the opened descriptor before truncation."""
        import dashboard

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("leave this alone\n", encoding="utf-8")
        real_fstat = os.fstat

        def foreign_owner(descriptor):
            info = real_fstat(descriptor)
            return types.SimpleNamespace(
                st_mode=info.st_mode,
                st_nlink=info.st_nlink,
                st_uid=os.getuid() + 1,
            )

        with mock.patch.object(
                dashboard, "_url_lock_descriptor_is_safe", return_value=True), \
                mock.patch.object(
                    dashboard.os, "fstat", side_effect=foreign_owner):
            self.assertIsNone(
                dashboard.write_url_file("127.0.0.1", 8080, self.path)
            )
        self.assertEqual(
            self.path.read_text(encoding="utf-8"),
            "leave this alone\n",
            "the bearer writer changed a file its process does not own",
        )

    @unittest.skipUnless(os.name == "posix", "hard links need elevation on Windows")
    def test_a_hard_linked_recovery_file_is_not_read(self):
        """Reading the bearer has the same single-link boundary as writing it."""
        import dashboard
        self.path.parent.mkdir(parents=True, exist_ok=True)
        victim = self.path.parent / "attacker-readable"
        victim.write_text(
            dashboard.authenticated_dashboard_url("127.0.0.1", 8080),
            encoding="utf-8",
        )
        os.link(victim, self.path)
        self.assertIsNone(
            dashboard.read_url_file(self.path),
            "a second name for another file was accepted as the bearer store",
        )

    @unittest.skipUnless(os.name == "posix", "FIFO semantics are POSIX")
    def test_a_replacement_fifo_between_validation_and_read_cannot_block(self):
        """The safety decision must describe the descriptor actually read."""
        import claude_usage.safefile as safefile
        import dashboard

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            dashboard.authenticated_dashboard_url("127.0.0.1", 8080),
            encoding="utf-8",
        )
        original_is_file = Path.is_file
        original_open = os.open
        swapped = threading.Event()

        def replace_path():
            if not swapped.is_set():
                self.path.unlink()
                os.mkfifo(self.path)
                swapped.set()

        def checked_then_replaced(candidate):
            result = original_is_file(candidate)
            if candidate == self.path:
                replace_path()
            return result

        def replaced_before_descriptor_open(candidate, flags, *args):
            if Path(candidate) == self.path:
                replace_path()
            return original_open(candidate, flags, *args)

        result = []
        finished = threading.Event()

        def read():
            result.append(dashboard.read_url_file(self.path))
            finished.set()

        with mock.patch.object(Path, "is_file", new=checked_then_replaced), \
                mock.patch.object(
                    safefile.os, "open", side_effect=replaced_before_descriptor_open):
            worker = threading.Thread(target=read, daemon=True)
            worker.start()
            self.assertTrue(swapped.wait(1), "the path was never replaced")
            returned_without_writer = finished.wait(0.25)
            if not returned_without_writer:
                writer = original_open(
                    self.path, os.O_WRONLY | getattr(os, "O_NONBLOCK", 0))
                os.close(writer)
            worker.join(2)

        self.assertTrue(
            returned_without_writer,
            "reading the recovery URL blocked on a replacement FIFO",
        )
        self.assertEqual(result, [None])

    @unittest.skipUnless(os.name == "posix", "mkfifo is POSIX-only")
    def test_a_fifo_someone_is_reading_does_not_receive_the_token(self):
        """The same mechanism in its other shape: a path of the wrong KIND.

        A FIFO is neither a symlink nor a hard link, so neither check above sees
        it. `S_ISREG` does, and the reader is what makes this the case that
        needs it: a reader-less FIFO opened `O_WRONLY|O_NONBLOCK` fails ENXIO and
        is refused by the `except OSError` this function already had, so it
        cannot demonstrate the check. Hold the read end open and the open
        SUCCEEDS -- and without `S_ISREG` the very next lines fchmod the
        attacker's pipe and write the API token into it, where a `read()` on the
        other end collects it.
        """
        import select

        import dashboard
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(self.path)
        # `r+b` rather than a read-only open: opening a FIFO read-write returns
        # immediately instead of blocking for a writer, so the reader is in place
        # before `write_url_file` runs with no race to lose, and the pipe never
        # reaches EOF -- which is what lets `select` below distinguish "nothing
        # was written" from "the writer closed". Binary on purpose: these are
        # bytes off a pipe, not text.
        reader = open(self.path, "r+b", buffering=0)
        self.addCleanup(reader.close)

        self.assertIsNone(dashboard.write_url_file("127.0.0.1", 8080, self.path))
        readable, _, _ = select.select([reader], [], [], 0)
        self.assertEqual(readable, [],
                         "the API token was written into the attacker's pipe")

    @unittest.skipUnless(os.name == "posix", "mkfifo is POSIX-only")
    def test_a_reader_less_fifo_returns_instead_of_blocking_forever(self):
        """What `O_NONBLOCK` buys, asserted rather than assumed.

        `write_url_file`'s contract is that it always returns. Opening a FIFO
        `O_WRONLY` with no reader and no `O_NONBLOCK` blocks until one appears --
        which on a dashboard startup path means the server never starts, and in a
        test run means a hung worker rather than a failure. The watchdog turns
        that into an ordinary red.
        """
        import threading

        import dashboard
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(self.path)
        box = []
        worker = threading.Thread(
            target=lambda: box.append(
                dashboard.write_url_file("127.0.0.1", 8080, self.path)),
            daemon=True)
        worker.start()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(),
                         "write_url_file blocked on a reader-less FIFO")
        self.assertEqual(box, [None])

    def test_an_ordinary_rewrite_is_unaffected_by_those_refusals(self):
        """Refuse to pass vacuously: the happy path must still work.

        A `write_url_file` that returned None for everything would satisfy all
        three refusals above, so the ordinary case is asserted in the same class.
        """
        import dashboard
        self.assertIsNotNone(dashboard.write_url_file("127.0.0.1", 8080, self.path))
        self.assertEqual(dashboard.read_url_file(self.path),
                         dashboard.authenticated_dashboard_url("127.0.0.1", 8080))
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.path.stat().st_nlink, 1)


class TestDashboardModuleHygiene(unittest.TestCase):
    """The import block is the first thing a reader uses to place a module."""

    def test_no_module_is_imported_and_never_used(self):
        """`import math` sat at the top of dashboard.py using nothing.

        Restricted to plain `import X` statements, which bind a module and
        nothing else. The `from X import Y` lines are deliberately exempt:
        AGENTS.md documents that `scanner`, `cli` and `dashboard` re-export the
        names they used to define, so a name this module no longer uses itself
        may still be part of its surface. If a plain module import ever becomes
        load-bearing without being referenced — scanner.py keeps `import
        sqlite3` for a test to patch — it needs a named exemption here, not a
        weaker check.
        """
        import ast
        import dashboard
        tree = ast.parse(Path(dashboard.__file__).read_text(encoding="utf-8"))
        used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        used |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        imported = [alias.asname or alias.name.split(".")[0]
                    for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names]
        self.assertEqual(
            sorted({name for name in imported if name not in used}), [],
            "dashboard.py imports a module it never uses")


class TestTheConfigDirectoryOverrideMovesBothHalves(unittest.TestCase):
    """`CLAUDE_CONFIG_DIR` has to move `settings.json` as well as `.claude.json`.

    `config_path()` honoured it and `detect_auth_mode`'s default `settings_paths`
    did not, so the two halves of account.py described two different installs:
    one read the relocated config, the other asked the UNRELOCATED directory
    whether that config's account was still the credential in use. It went wrong
    in both directions, which is why both are pinned below.

    `scripts/run-docker.sh` is the repository's own statement of what the
    variable means — `CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"`, then
    `$CLAUDE_DIR/projects` — i.e. the replacement for the whole of `~/.claude`.
    """

    API_KEY_SETTINGS = '{"env": {"ANTHROPIC_API_KEY": "sk-not-a-real-key"}}'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.home = root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.relocated = root / "claude-config"
        self.relocated.mkdir()
        # A live, alarming-looking window, so a wrong "subscription" answer is
        # visible as the panel a user would actually be shown.
        reset = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        (self.relocated / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"organizationType": "claude_max",
                             "billingType": "stripe_subscription"},
            "cachedUsageUtilization": {
                "fetchedAtMs": int(datetime.now(timezone.utc).timestamp() * 1000),
                "utilization": {"limits": [{
                    "kind": "five_hour", "group": "session", "percent": 97,
                    "severity": "critical", "resets_at": reset,
                    "is_active": True}]}},
        }), encoding="utf-8")
        self._env = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.relocated),
        })
        self._env.start()
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                     "CLAUDE_USAGE_CONFIG"):
            os.environ.pop(name, None)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_a_relocated_api_key_is_not_reported_as_a_subscription(self):
        """The reported case: the key is declared where Claude Code reads it,
        and the plan panel was built from a cache that stopped updating when the
        user switched credentials — 97%, "critical", for an install that has no
        quota window at all."""
        import account
        (self.relocated / "settings.json").write_text(
            self.API_KEY_SETTINGS, encoding="utf-8")
        self.assertEqual(account.config_path(), self.relocated / ".claude.json")
        self.assertEqual(account.detect_auth_mode(account.read_config()), "api_key")
        self.assertEqual(account.current_limits(),
                         {"available": False, "reason": "api_key"})

    def test_a_leftover_settings_file_does_not_hide_a_relocated_subscriber(self):
        """The other direction, and the one the developer's own suite is already
        working around: a `~/.claude/settings.json` left behind by an install
        that has since moved is not the credential in use, and reading it hid a
        genuine subscription panel."""
        import account
        (self.home / ".claude" / "settings.json").write_text(
            self.API_KEY_SETTINGS, encoding="utf-8")
        self.assertEqual(account.detect_auth_mode(account.read_config()),
                         "subscription")
        self.assertTrue(account.current_limits()["available"])

    def test_the_override_is_read_from_the_mapping_it_was_handed(self):
        """`detect_auth_mode` already resolves `env` before looking at anything,
        so the settings lookup has to come from the same mapping — reaching past
        it to `os.environ` would recreate the split one level down."""
        import account
        (self.relocated / "settings.json").write_text(
            self.API_KEY_SETTINGS, encoding="utf-8")
        config = account.read_config()
        os.environ.pop("CLAUDE_CONFIG_DIR")
        self.assertEqual(account.detect_auth_mode(config, env={}), "subscription")
        self.assertEqual(
            account.detect_auth_mode(
                config, env={"CLAUDE_CONFIG_DIR": str(self.relocated)}),
            "api_key")

    def test_the_ordinary_install_still_reads_the_ordinary_paths(self):
        """The two files are not siblings by default — `~/.claude.json` sits
        BESIDE `~/.claude`, `settings.json` inside it — so only the override can
        be shared. A `config_dir()` supplying both defaults resolves the config
        to `~/.claude/.claude.json`, which nobody has."""
        import account
        os.environ.pop("CLAUDE_CONFIG_DIR")
        self.assertEqual(account.config_path(), self.home / ".claude.json")
        self.assertEqual(account.detect_auth_mode({}), "unknown")
        (self.home / ".claude" / "settings.local.json").write_text(
            self.API_KEY_SETTINGS, encoding="utf-8")
        self.assertEqual(account.detect_auth_mode({}), "api_key")

    def test_a_non_string_override_is_ignored_rather_than_raising(self):
        """account.py's promise is that no entry point here raises, and it keeps
        it by validating what comes out of a mapping it does not write —
        `Path(5)` raises TypeError."""
        import account
        for bogus in (5, None, [], ""):
            with self.subTest(value=bogus):
                self.assertEqual(
                    account.detect_auth_mode({}, env={"CLAUDE_CONFIG_DIR": bogus}),
                    "unknown")


class TestCliRejectsArgumentsItWouldDrop(unittest.TestCase):
    """`main` hand-parses, so anything it did not recognise vanished in silence.

    `validate_source` rejects a bad --source VALUE, but a typo in the flag NAME
    never reached it: `today --sourcex codex` printed a complete, correctly
    formatted *Claude* report and exited 0, and so did `today --source=codex`,
    the standard equals form. The only signal was the one-line `Source:` header
    — nothing on stderr, nothing in the exit status a wrapper script can read.

    Lives here rather than in test_cli_reports.py because these drive `main`'s
    argument handling, not the reports; the `cmd_url` tests above set the same
    precedent for CLI behaviour tested from this file.
    """

    def _run(self, argv):
        """Drive `cli.main()` with every command stubbed out.

        Returns (exit code, printed output, [(command, kwargs)]). Stubbing the
        commands is what lets a valid invocation be asserted on: the point is
        which command ran with which arguments, not what it printed.
        """
        import contextlib, io
        import cli
        calls = []

        def record(name):
            def stub(*args, **kwargs):
                calls.append((name, kwargs))
            return stub

        buffer = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(
                cli.COMMANDS, {name: record(name) for name in cli.COMMANDS}))
            # `main` calls these three by name rather than through COMMANDS.
            for name in ("cmd_dashboard", "cmd_scan", "cmd_url"):
                stack.enter_context(mock.patch.object(cli, name, record(name)))
            stack.enter_context(mock.patch.object(cli.sys, "argv", ["cli.py"] + argv))
# stderr is merged into the same buffer: these assertions are about the
            # MESSAGE the user gets, not the stream it arrives on, and the diagnostics
            # moved to stderr when `url`'s stdout was narrowed to a URL or nothing.
            # Tests that ARE about the stream capture the two separately -- see
            # tests/test_cli_streams.py and test_the_rebuild_is_announced_on_stderr_only.
            stack.enter_context(contextlib.redirect_stdout(buffer))
            stack.enter_context(contextlib.redirect_stderr(buffer))
            try:
                cli.main()
                code = 0
            except SystemExit as exit_code:
                code = exit_code.code or 0
        return code, buffer.getvalue(), calls

    def test_a_misspelt_flag_is_an_error_rather_than_a_different_report(self):
        """The reported defect: `--sourcex` reported Claude, confidently, at 0."""
        code, output, calls = self._run(["today", "--sourcex", "codex"])
        self.assertEqual(code, 1, "a typo produced a successful run of the wrong report")
        self.assertIn("--sourcex", output)
        self.assertEqual(calls, [], "the report ran anyway")

    def test_the_flag_set_is_per_command_not_global(self):
        """A flag the command never reads is the same silent drop as a typo.

        A single global allow-list still accepts every one of these, so this is
        the case that decides whether the check is worth having."""
        for argv in (["scan", "--source", "codex"],
                     ["today", "--port", "9000"],
                     ["week", "--no-browser"],
                     ["url", "--surface", "vscode"],
                     ["stats", "--projects-dir", "/tmp"],
                     ["dashboard", "--source", "codex"]):
            with self.subTest(argv=argv):
                code, _, calls = self._run(argv)
                self.assertEqual(code, 1)
                self.assertEqual(calls, [])

    def test_the_equals_form_is_parsed_rather_than_rejected(self):
        """`--source=codex` is what argparse-trained fingers type, and it was
        dropped in silence too. Rejecting it would trade one silent wrong
        report for a hard failure on a form users reasonably expect to work."""
        code, _, calls = self._run(["today", "--source=codex"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("today", {"source": "codex"})])

    def test_a_flag_with_no_value_is_an_error_rather_than_an_absence(self):
        """A dangling `--source` read as "not supplied" and scoped the report to
        the default. `--host --port` was worse: `--port` became the host."""
        for argv in (["today", "--source"],
                     ["today", "--source="],
                     ["dashboard", "--host", "--port"],
                     ["scan", "--projects-dir"]):
            with self.subTest(argv=argv):
                code, _, calls = self._run(argv)
                self.assertEqual(code, 1)
                self.assertEqual(calls, [])

    def test_a_positional_is_rejected_rather_than_dropped(self):
        """The same silent drop one token shape over.

        `validate_flags` used to skip anything not starting with `-`, on the
        grounds that it was "a value already consumed, or a stray word". Only
        the second half was ever reachable — the space form consumes its value
        with `index += 2` and the equals form carries it inside the token — so
        the leniency protected nothing and let `today codex` print a complete,
        correctly formatted *Claude* report at exit 0, which is byte for byte
        the `today --sourcex codex` defect this whole check exists to remove.
        No command takes a positional."""
        for argv in (["today", "codex"],
                     ["stats", "all"],
                     ["week", "--source", "claude", "extra"],
                     ["scan", "/tmp"],
                     ["dashboard", "9000"],
                     ["url", "open"]):
            with self.subTest(argv=argv):
                code, output, calls = self._run(argv)
                self.assertEqual(code, 1, output)
                self.assertEqual(calls, [], "the command ran anyway")
                self.assertIn(argv[-1], output, "the message never named the token")

    def test_the_rejected_positional_is_echoed_safely(self):
        """It is the user's own argv on its way back to the terminal, and the
        empty string renders as no characters at all — a message ending in a
        bare colon names nothing."""
        code, output, _ = self._run(["today", "\x1b[31mcodex"])
        self.assertEqual(code, 1)
        self.assertNotIn("\x1b", output)
        self.assertIn("\\x1b", output)
        code, output, calls = self._run(["today", ""])
        self.assertEqual(code, 1, output)
        self.assertEqual(calls, [])
        self.assertEqual(output.strip(), "unexpected argument for `today`: ''")

    def test_an_empty_value_is_an_error_in_both_spellings(self):
        """`--projects-dir=` was refused and `--projects-dir ""` was not.

        `Path("")` is `Path(".")` and `resolve_scan_roots` accepts it, so a
        wrapper building `--projects-dir "$MOUNT"` with `MOUNT` unset made the
        whole working directory a scan root and reported it as a normal scan at
        exit 0. `CLAUDE_USAGE_PROJECTS_DIRS`, documented as the same surface,
        has always dropped blanks.

        `["today", "--source", ""]` is deliberately absent: `validate_source`
        already rejects it downstream, so it would pass with or without this
        check and prove nothing about it."""
        for argv in (["scan", "--projects-dir", ""],
                     ["dashboard", "--host", ""],
                     ["dashboard", "--surface", ""]):
            with self.subTest(argv=argv):
                code, output, calls = self._run(argv)
                self.assertEqual(code, 1, output)
                self.assertIn("needs a value", output)
                self.assertEqual(calls, [])

    def test_a_whitespace_value_is_still_accepted_in_both_spellings(self):
        """Emptiness, not blankness — the `=` branch tests `not value`, so
        testing `not value.strip()` here would recreate the same asymmetry
        mirrored. `Path(" ")` is a directory a POSIX user may legally have, and
        one that does not exist already gets the "not found, skipping" warning
        rather than an error."""
        for argv in (["scan", "--projects-dir", " "],
                     ["scan", "--projects-dir= "]):
            with self.subTest(argv=argv):
                code, output, calls = self._run(argv)
                self.assertEqual(code, 0, output)
                self.assertEqual(len(calls), 1, output)

    def test_a_repeated_single_valued_flag_is_an_error(self):
        """`parse_named_arg` returns the FIRST match, so `--source codex
        --source claude` silently meant codex."""
        code, _, calls = self._run(["stats", "--source", "codex", "--source", "claude"])
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])

    def test_a_repeatable_flag_is_still_repeatable(self):
        """--projects-dir means "also scan here", any number of times.

        Real directories because resolve_scan_roots drops the ones that do not
        exist, so a made-up path would prove nothing about the parsing."""
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            code, output, calls = self._run(
                ["scan", "--projects-dir", first, "--projects-dir", second])
            self.assertEqual(code, 0, output)
            self.assertEqual([name for name, _ in calls], ["cmd_scan"])
            roots = [str(p) for p in calls[0][1]["projects_dirs"]]
            self.assertIn(first, roots)
            self.assertIn(second, roots)

    def test_every_shipped_invocation_still_starts(self):
        """The launchers are the reason a strict check needs a per-command set.

        Dockerfile's CMD, the VS Code extension's spawn args and README's `url
        --open` all go through here; rejecting one of them means the image and
        the extension fail to start on the next release."""
        for argv in (["dashboard", "--no-browser"],                    # Dockerfile CMD
                     ["dashboard", "--no-browser", "--host", "127.0.0.1",
                      "--port", "8080", "--surface", "vscode"],        # the extension
                     ["url", "--open"],                                # README
                     ["scan", "--projects-dir", "/tmp"],               # the brew test
                     ["today"], ["week"], ["stats"], ["scan"], ["url"], ["dashboard"]):
            with self.subTest(argv=argv):
                code, output, calls = self._run(argv)
                self.assertEqual(code, 0, output)
                self.assertEqual(len(calls), 1, output)

    def test_a_mistyped_command_does_not_look_like_success(self):
        """Same silent drop one token earlier: `cli.py todya` printed the banner
        and exited 0, which a wrapper script cannot tell from a report."""
        code, output, calls = self._run(["todya"])
        self.assertEqual(code, 1)
        self.assertIn("todya", output)
        self.assertEqual(calls, [])

    def test_the_exit_zero_paths_survive(self):
        """No args is what `brew test` runs under exit-0-expecting shell_output,
        and --version is its own documented fast path."""
        for argv in ([], ["--help"], ["-h"], ["help"]):
            with self.subTest(argv=argv):
                code, output, _ = self._run(argv)
                self.assertEqual(code, 0)
                self.assertIn("Claude Code Usage Dashboard", output)
        code, output, _ = self._run(["--version"])
        self.assertEqual(code, 0)
        self.assertNotIn("Usage:", output)

    def test_every_command_declares_the_flags_it_reads(self):
        """A command missing from the table would accept no flags at all, which
        is a silent drop wearing the opposite mask."""
        import cli
        self.assertEqual(set(cli.COMMAND_FLAGS), set(cli.COMMANDS))


class TestTheCommandListHasOneCopy(unittest.TestCase):
    """`USAGE` is the only prose copy of the command set, and it is pinned.

    cli.py's module docstring carried a second `Commands:` list that named four
    of the six: it was written when four was all there was, `week` and `url`
    each arrived in a commit that updated `COMMANDS` and `USAGE` together, and
    neither touched line 4. Synchronising it would have made a third copy of a
    list that this repository has twice decided not to keep in duplicate — see
    `tests/test_version.py::test_the_docstring_counts_no_bundled_python_files`
    ("Drop the count rather than correcting it") — so the list was deleted
    instead and the surviving copy is asserted here.
    """

    def test_usage_documents_every_command(self):
        """Anchored on the documented line form rather than on the bare name:
        `"scan" in USAGE` is satisfied by the word "scanned" in `--projects-dir`'s
        own description and `"dashboard"` by three further sentences, so a
        substring check stays green through the deletion of either entry."""
        import cli
        for name in cli.COMMANDS:
            with self.subTest(command=name):
                self.assertIn(f"python cli.py {name}", cli.USAGE)

    def test_the_module_docstring_keeps_no_second_command_list(self):
        """Without this the block simply grows back, and the next command drifts
        out of it exactly as `week` and `url` did."""
        import cli
        doc = cli.__doc__ or ""
        self.assertNotIn("Commands:", doc)
        named = sorted(name for name in cli.COMMANDS if f"  {name}" in doc)
        self.assertEqual(named, [], "a second command list is accreting again")


class PayloadCacheTestCase(unittest.TestCase):
    """A real database, with the cache's build-cost floor lowered to zero.

    `dashboard_data.PAYLOAD_CACHE_MIN_BUILD_SECONDS` is 1.0 in production, so a
    fixture this size is never retained and the whole mechanism is inert for the
    rest of the suite — which is deliberate (see its comment), and is why every
    test that wants the cache has to ask for it here.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "usage.db"
        self._seed(self.db_path)
        patcher = mock.patch.object(
            dashboard_data, "PAYLOAD_CACHE_MIN_BUILD_SECONDS", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Every test leaves the module-level cache and its probe connection as it
        # found them; an entry surviving into another test would be a cross-test
        # dependency in the one mechanism whose whole risk is serving stale data.
        self.addCleanup(dashboard_data.reset_payload_cache)
        self.addCleanup(self._tmp.cleanup)
        dashboard_data.reset_payload_cache()

    @staticmethod
    def _seed(path, session_id="sess-cache", model="claude-opus-5",
              source="claude"):
        conn = get_db(path)
        init_db(conn)
        conn.execute(
            "INSERT INTO sessions (session_id, project_name, first_timestamp,"
            " last_timestamp, git_branch, total_input_tokens,"
            " total_output_tokens, total_cache_read, total_cache_creation,"
            " model, turn_count, topic, source)"
            " VALUES (?, 'proj', '2026-04-08T09:00:00Z', '2026-04-08T10:00:00Z',"
            " 'main', 10, 20, 0, 0, ?, 1, 'topic', ?)",
            (session_id, model, source))
        conn.execute(
            "INSERT INTO turns (session_id, timestamp, model, input_tokens,"
            " output_tokens, cache_read_tokens, cache_creation_tokens,"
            " message_id, source, reasoning_effort, stop_reason)"
            " VALUES (?, '2026-04-08T09:30:00Z', ?, 10, 20, 0, 0, ?, ?,"
            " 'high', 'end_turn')",
            (session_id, model, "msg-" + session_id, source))
        conn.commit()
        conn.close()

    def _append_session(self, session_id, source="claude"):
        """Commit one new session, the way a scan does: through db.get_db."""
        conn = get_db(self.db_path)
        conn.execute(
            "INSERT INTO sessions (session_id, project_name, first_timestamp,"
            " last_timestamp, git_branch, total_input_tokens,"
            " total_output_tokens, total_cache_read, total_cache_creation,"
            " model, turn_count, topic, source)"
            " VALUES (?, 'proj', '2026-04-09T09:00:00Z', '2026-04-09T10:00:00Z',"
            " 'main', 7, 9, 0, 0, 'claude-opus-5', 1, 'topic', ?)",
            (session_id, source))
        conn.execute(
            "INSERT INTO turns (session_id, timestamp, model, input_tokens,"
            " output_tokens, cache_read_tokens, cache_creation_tokens,"
            " message_id, source, reasoning_effort, stop_reason)"
            " VALUES (?, '2026-04-09T09:30:00Z', 'claude-opus-5', 7, 9, 0, 0,"
            " ?, ?, 'high', 'end_turn')",
            (session_id, "msg-" + session_id, source))
        conn.commit()
        conn.close()

    @staticmethod
    def _session_ids(payload):
        return {row["session_id"] for row in payload["sessions_all"]}


class TestThePayloadCacheServesRepeatRequests(PayloadCacheTestCase):
    def test_a_second_call_does_not_rebuild(self):
        with mock.patch.object(
                dashboard_data, "_collect_dashboard_data",
                wraps=dashboard_data._collect_dashboard_data) as build:
            first = get_dashboard_data(self.db_path)
            second = get_dashboard_data(self.db_path)
        self.assertEqual(build.call_count, 1,
                         "the second call rebuilt the whole payload")
        self.assertEqual(first["sessions_all"], second["sessions_all"])

    def test_a_build_cheaper_than_the_floor_is_never_retained(self):
        """The default floor, unpatched — which is what the rest of the suite,
        and every small install, actually runs with."""
        with mock.patch.object(dashboard_data,
                               "PAYLOAD_CACHE_MIN_BUILD_SECONDS", 1.0), \
             mock.patch.object(
                dashboard_data, "_collect_dashboard_data",
                wraps=dashboard_data._collect_dashboard_data) as build:
            get_dashboard_data(self.db_path)
            get_dashboard_data(self.db_path)
        self.assertEqual(build.call_count, 2)
        self.assertEqual(len(dashboard_data._PAYLOAD_CACHE), 0)

    def test_nothing_holds_the_database_open_when_nothing_is_cached(self):
        """The probe connection is the one thing this adds to a process's open
        files, and it is held only while there is an entry to validate. On
        Windows an open connection is also what would stop a caller unlinking
        the database it just read."""
        with mock.patch.object(dashboard_data,
                               "PAYLOAD_CACHE_MIN_BUILD_SECONDS", 1.0):
            get_dashboard_data(self.db_path)
        self.assertIsNone(dashboard_data._VERSION_PROBE)


class TestThePayloadCacheCannotServeStaleData(PayloadCacheTestCase):
    """The only way this mechanism can be wrong, and the tests that say it is not.

    Read `dashboard_data`'s cache comment first. The key deliberately is NOT the
    database file's mtime, and `test_a_commit_that_leaves_the_file_untouched...`
    below is the measurement that rules it out rather than an opinion about it.
    """

    def test_a_commit_that_leaves_the_file_untouched_still_invalidates(self):
        conn = sqlite3.connect(self.db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        if (mode or "").lower() != "wal":
            self.skipTest(f"journal mode is {mode!r}, not wal — the file-level "
                          f"claim below is only about write-ahead logging")

        first = get_dashboard_data(self.db_path)
        self.assertNotIn("sess-appended", self._session_ids(first))
        before = os.stat(self.db_path)
        self._append_session("sess-appended")
        after = os.stat(self.db_path)

        # Guard the guard. Were this to move, an mtime-keyed cache would work
        # and every assertion below would pass without proving anything.
        self.assertEqual(
            (before.st_size, before.st_mtime_ns),
            (after.st_size, after.st_mtime_ns),
            "the commit moved the main database file after all; under WAL it "
            "lands in usage.db-wal and this file is not written until a "
            "checkpoint")

        served = get_dashboard_data(self.db_path)
        self.assertIn(
            "sess-appended", self._session_ids(served),
            "a committed session was not served — the cache key cannot see a "
            "write-ahead-logged commit")

    def test_pressing_rescan_shows_the_new_numbers(self):
        """The end-to-end shape of the regression this must never introduce:
        a user presses Rescan, the scan commits in-process, and the next poll
        must not read back the numbers from before it."""
        before = get_dashboard_data(self.db_path)
        self._append_session("sess-rescanned")
        after = get_dashboard_data(self.db_path)
        self.assertEqual(len(self._session_ids(before)) + 1,
                         len(self._session_ids(after)))
        self.assertIn("sess-rescanned", self._session_ids(after))

    def test_a_cache_miss_holds_admission_through_payload_assembly(self):
        """The uncached rollups cannot escape their admission boundary.

        A stateful context makes this a lexical contract rather than a timing
        test: moving the build below the ``with`` fails deterministically, even
        on a worker that has not scheduled a contender yet. Cache publication
        must instead follow the context's final pathname-identity check.
        """
        state = {"held": False, "entered": 0}
        observations = []
        real_identity = dashboard_data._database_identity
        real_version = dashboard_data._database_version
        real_cached = dashboard_data._cached_payload
        real_collect = dashboard_data._collect_dashboard_data
        real_store = dashboard_data._store_payload

        @contextlib.contextmanager
        def observed_admission(*_args, **_kwargs):
            self.assertFalse(state["held"])
            state["held"] = True
            state["entered"] += 1
            try:
                yield False
            finally:
                state["held"] = False

        def observed(name, real):
            def call(*args, **kwargs):
                observations.append((name, state["held"]))
                return real(*args, **kwargs)
            return call

        with mock.patch.object(dashboard_data, "database_admission",
                               observed_admission), \
                mock.patch.object(dashboard_data, "_database_identity",
                                  observed("identity", real_identity)), \
                mock.patch.object(dashboard_data, "_database_version",
                                  observed("version", real_version)), \
                mock.patch.object(dashboard_data, "_cached_payload",
                                  observed("cached", real_cached)), \
                mock.patch.object(dashboard_data, "_collect_dashboard_data",
                                  observed("collect", real_collect)), \
                mock.patch.object(dashboard_data, "_store_payload",
                                  observed("store", real_store)):
            payload = get_dashboard_data(self.db_path)

        self.assertEqual(state, {"held": False, "entered": 1})
        self.assertEqual(observations, [
            ("identity", True), ("version", True), ("cached", True),
            ("collect", True), ("version", True), ("store", False),
        ])
        self.assertIn("sess-cache", self._session_ids(payload))

    def test_a_cache_response_and_marked_rebuild_are_mutually_exclusive(self):
        """A cache hit owns admission through its last database-derived field."""
        first = get_dashboard_data(self.db_path)
        self.assertIn("sess-cache", self._session_ids(first))

        state = {"held": False, "entered": 0}
        observations = []
        real_identity = dashboard_data._database_identity
        real_version = dashboard_data._database_version
        real_cached = dashboard_data._cached_payload
        real_live_fields = dashboard_data._payload_with_live_fields

        @contextlib.contextmanager
        def observed_admission(*_args, **_kwargs):
            self.assertFalse(state["held"])
            state["held"] = True
            state["entered"] += 1
            try:
                yield False
            finally:
                state["held"] = False

        def observed(name, real):
            def call(*args, **kwargs):
                observations.append((name, state["held"]))
                return real(*args, **kwargs)
            return call

        with mock.patch.object(dashboard_data, "database_admission",
                               observed_admission), \
                mock.patch.object(dashboard_data, "_database_identity",
                                  observed("identity", real_identity)), \
                mock.patch.object(dashboard_data, "_database_version",
                                  observed("version", real_version)), \
                mock.patch.object(dashboard_data, "_cached_payload",
                                  observed("cached", real_cached)), \
                mock.patch.object(dashboard_data, "_payload_with_live_fields",
                                  observed("live_fields", real_live_fields)):
            payload = get_dashboard_data(self.db_path)

        self.assertEqual(state, {"held": False, "entered": 1})
        self.assertEqual(observations, [
            ("identity", True), ("version", True), ("cached", True),
            ("live_fields", True),
        ])
        self.assertIn("sess-cache", self._session_ids(payload))

    def test_available_sources_queries_inside_admission(self):
        """The page's no-retry source request cannot escape admission."""
        state = {"held": False, "entered": 0}
        observations = []
        real_connect = sqlite3.connect

        @contextlib.contextmanager
        def observed_admission(*_args, **_kwargs):
            self.assertFalse(state["held"])
            state["held"] = True
            state["entered"] += 1
            try:
                yield False
            finally:
                state["held"] = False

        def observed_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(
                lambda sql: observations.append(state["held"])
                if "FROM turns GROUP BY" in sql else None)
            return conn

        with mock.patch.object(dashboard_data, "database_admission",
                               observed_admission), \
                mock.patch.object(dashboard_data.sqlite3, "connect",
                                  observed_connect):
            sources = dashboard_data.available_sources(self.db_path)

        self.assertEqual(state, {"held": False, "entered": 1})
        self.assertEqual(observations, [True])
        self.assertEqual(sources, [{"source": "claude", "turns": 1}])

    def test_a_build_that_spans_a_commit_is_returned_but_not_retained(self):
        """RESCAN_LOCK serialises scans against each other, not against readers.

        The read transaction keeps the sections consistent. A commit during the
        build still makes that snapshot older than the current database, so the
        live query cache must not retain it as a current answer.
        """
        real = dashboard_data._collect_dashboard_data

        def build_then_commit(conn, source=None):
            payload = real(conn, source)
            self._append_session("sess-mid-build")
            return payload

        with mock.patch.object(dashboard_data, "_collect_dashboard_data",
                               build_then_commit):
            get_dashboard_data(self.db_path)
        self.assertEqual(
            len(dashboard_data._PAYLOAD_CACHE), 0,
            "a payload built across another connection's commit was retained")
        served = get_dashboard_data(self.db_path)
        self.assertIn("sess-mid-build", self._session_ids(served))

    def test_saved_payload_sections_share_one_snapshot_during_an_external_write(self):
        real = dashboard_data.rollups.daily_by_model

        def read_then_commit(conn, source=None):
            daily = real(conn, source)
            self._append_session("committed-between-sections")
            return daily

        with mock.patch.object(dashboard_data.rollups, "daily_by_model", read_then_commit):
            payload = get_dashboard_data(self.db_path)
        self.assertEqual(self._session_ids(payload), {"sess-cache"})
        self.assertEqual(sum(row["input"] for row in payload["daily_by_model"]), 10)
        self.assertIn("committed-between-sections",
                      self._session_ids(get_dashboard_data(self.db_path)))

    def test_a_database_replaced_in_place_is_not_served_from_the_old_one(self):
        """Copied OVER, not unlinked and rebuilt.

        A new file would carry a new inode, which the identity notices on its
        own; `shutil.copyfile` truncates and rewrites the existing one, so the
        path, the device and the inode are all unchanged and the probe
        connection is still attached to the bytes that were replaced under it.
        That is what the size and mtime in `_database_identity` are for.
        """
        first = get_dashboard_data(self.db_path)
        self.assertIn("sess-cache", self._session_ids(first))
        before = os.stat(self.db_path)

        other = Path(self._tmp.name) / "other.db"
        self._seed(other, session_id="sess-replacement")
        replacement = sqlite3.connect(other)
        replacement.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        replacement.close()
        for suffix in ("-wal", "-shm"):
            candidate = Path(str(self.db_path) + suffix)
            if candidate.exists():
                candidate.unlink()
        shutil.copyfile(other, self.db_path)

        after = os.stat(self.db_path)
        self.assertEqual((before.st_dev, before.st_ino),
                         (after.st_dev, after.st_ino),
                         "the copy replaced the inode, so this is testing the "
                         "easy case rather than the one it was written for")
        served = get_dashboard_data(self.db_path)
        self.assertEqual({"sess-replacement"}, self._session_ids(served))

    @unittest.skipUnless(os.name == "posix", "replacing an open database is POSIX-only")
    def test_a_refused_inode_swap_cannot_publish_the_old_payload(self):
        """Admission's final path check must precede cache publication.

        The connection still reads the admitted inode after its pathname is
        replaced. Publishing that payload before the context manager performs
        its final identity check leaves a valid-looking cache entry for data
        the request ultimately refused to serve.
        """
        replacement = Path(self._tmp.name) / "replacement.db"
        self._seed(replacement, session_id="sess-replacement")
        replacement_conn = sqlite3.connect(replacement)
        replacement_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        replacement_conn.close()
        real_admission = dashboard_data.database_admission
        swapped = False

        @contextlib.contextmanager
        def swap_before_final_identity_check(conn, db_path):
            nonlocal swapped
            with real_admission(conn, db_path) as rebuilt:
                yield rebuilt
                os.replace(replacement, self.db_path)
                swapped = True

        with mock.patch.object(
                dashboard_data, "database_admission",
                swap_before_final_identity_check):
            with self.assertRaisesRegex(
                    RuntimeError, "Database path changed during admission"):
                get_dashboard_data(self.db_path)

        self.assertTrue(swapped, "the fixture never replaced the database")
        self.assertEqual(
            len(dashboard_data._PAYLOAD_CACHE), 0,
            "the refused old-inode payload survived in the cache")
        self.assertIsNone(dashboard_data._VERSION_PROBE)
        served = get_dashboard_data(self.db_path)
        self.assertEqual({"sess-replacement"}, self._session_ids(served))

    @unittest.skipUnless(os.name == "posix", "POSIX inode verification only")
    def test_the_version_probe_cannot_be_labelled_with_another_inode(self):
        identity = dashboard_data._database_identity(self.db_path)
        other = Path(self._tmp.name) / "probe-replacement.db"
        self._seed(other, session_id="sess-probe-replacement")
        real_guard = db.secure_db_permissions
        swapped = False

        def swap_after_validation(*args, **kwargs):
            nonlocal swapped
            result = real_guard(*args, **kwargs)
            if (not swapped
                    and Path(args[0]).resolve() == self.db_path.resolve()):
                os.replace(other, self.db_path)
                swapped = True
            return result

        with mock.patch.object(db, "secure_db_permissions",
                               swap_after_validation):
            with dashboard_data._CACHE_LOCK:
                version = dashboard_data._database_version(identity)

        self.assertTrue(swapped, "the fixture never replaced the file")
        self.assertIsNone(version)
        self.assertIsNone(dashboard_data._VERSION_PROBE)

    def test_a_probe_reopened_mid_build_cannot_validate_that_build(self):
        """SQLite's counter restarts from its baseline on a new connection, so
        the same number read either side of a reopen does not mean "unchanged" —
        it means "two different connections, both of which have seen nothing".
        The probe generation is what makes those two readings compare unequal.
        """
        real = dashboard_data._collect_dashboard_data

        def build_then_reopen_the_probe(conn, source=None):
            payload = real(conn, source)
            with dashboard_data._CACHE_LOCK:
                dashboard_data._close_version_probe()
            return payload

        with mock.patch.object(dashboard_data, "_collect_dashboard_data",
                               build_then_reopen_the_probe):
            get_dashboard_data(self.db_path)
        self.assertEqual(
            len(dashboard_data._PAYLOAD_CACHE), 0,
            "a payload was stored against a counter from a connection that had "
            "already been replaced")

    def test_a_probe_switched_before_publication_cannot_validate_the_build(self):
        """The final identity check and cache publication are separate steps.

        Another request for a different database can replace the module's one
        probe between them. A generation from the old probe must not be stored
        merely because some probe is open at publication time.
        """
        identity = dashboard_data._database_identity(self.db_path)
        with dashboard_data._CACHE_LOCK:
            version = dashboard_data._database_version(identity)

        other = Path(self._tmp.name) / "other-probe.db"
        self._seed(other, session_id="sess-other-probe")
        other_identity = dashboard_data._database_identity(other)
        with dashboard_data._CACHE_LOCK:
            dashboard_data._close_version_probe()
            self.assertIsNotNone(
                dashboard_data._database_version(other_identity))

        dashboard_data._store_payload(
            identity, version, version, None,
            {"sessions_all": [{"session_id": "sess-cache"}]}, seconds=99.0)
        self.assertEqual(
            len(dashboard_data._PAYLOAD_CACHE), 0,
            "a payload was stored against a different file's probe")

    def test_the_source_is_part_of_the_key(self):
        """`?source=` scopes every usage rollup in SQL, so an entry built for one
        assistant is another assistant's wrong answer, not a stale one."""
        self._append_session("sess-codex-only", source="codex")
        claude = get_dashboard_data(self.db_path, "claude")
        codex = get_dashboard_data(self.db_path, "codex")
        everything = get_dashboard_data(self.db_path, None)
        self.assertEqual({"sess-cache"}, self._session_ids(claude))
        self.assertEqual({"sess-codex-only"}, self._session_ids(codex))
        self.assertEqual({"sess-cache", "sess-codex-only"},
                         self._session_ids(everything))
        # And again, now that each is cached, in a different order.
        self.assertEqual({"sess-codex-only"},
                         self._session_ids(get_dashboard_data(self.db_path,
                                                              "codex")))
        self.assertEqual({"sess-cache"},
                         self._session_ids(get_dashboard_data(self.db_path,
                                                              "claude")))

    def test_a_missing_database_answers_and_recovers_without_an_entry(self):
        """The page's retry loop watches for `error` to stop appearing, so an
        error body that could be retained would never stop appearing."""
        missing = Path(self._tmp.name) / "absent.db"
        self.assertIn("error", get_dashboard_data(missing))
        self.assertEqual(len(dashboard_data._PAYLOAD_CACHE), 0)
        self._seed(missing, session_id="sess-late")
        self.assertNotIn("error", get_dashboard_data(missing))

    @unittest.skipUnless(os.name == "posix", "unlinking an open SQLite file is POSIX-only")
    def test_removing_a_cached_database_releases_its_entry_and_probe(self):
        """A failed reopen is invalidation, not permission to retain old data.

        The long-lived probe keeps the old inode readable after the pathname is
        gone. Leaving its cache beside the retryable error holds stale usage in
        memory indefinitely and, on Windows, can also prevent recovery code
        from replacing the file after an equivalent open failure.
        """
        get_dashboard_data(self.db_path)
        self.assertTrue(dashboard_data._PAYLOAD_CACHE)
        self.assertIsNotNone(dashboard_data._VERSION_PROBE)

        self.db_path.unlink()
        self.assertIn("error", get_dashboard_data(self.db_path))

        self.assertEqual(len(dashboard_data._PAYLOAD_CACHE), 0)
        self.assertIsNone(dashboard_data._VERSION_PROBE)

    @unittest.skipUnless(os.name == "posix", "unlinking an open SQLite file is POSIX-only")
    def test_missing_sources_database_releases_cached_payload_state(self):
        """The lightweight first request owns the same invalidation contract."""
        get_dashboard_data(self.db_path)
        self.assertTrue(dashboard_data._PAYLOAD_CACHE)
        self.assertIsNotNone(dashboard_data._VERSION_PROBE)

        self.db_path.unlink()
        self.assertEqual(dashboard_data.available_sources(self.db_path), [])

        self.assertEqual(len(dashboard_data._PAYLOAD_CACHE), 0)
        self.assertIsNone(dashboard_data._VERSION_PROBE)

    @unittest.skipUnless(os.name == "posix", "unlinking an open SQLite file is POSIX-only")
    def test_invalidation_recognises_absolute_and_relative_path_spellings(self):
        """One inode must not become two cache owners through path spelling."""
        previous = Path.cwd()
        os.chdir(self._tmp.name)
        try:
            get_dashboard_data(Path("usage.db"))
        finally:
            os.chdir(previous)
        self.assertTrue(dashboard_data._PAYLOAD_CACHE)

        self.db_path.unlink()
        self.assertIn("error", get_dashboard_data(self.db_path))

        self.assertEqual(len(dashboard_data._PAYLOAD_CACHE), 0)
        self.assertIsNone(dashboard_data._VERSION_PROBE)

    def test_cache_identity_does_not_trust_process_wide_normcase(self):
        """A case-sensitive directory can override Windows' usual policy."""
        exact = Path(self._tmp.name) / "CaseOnly.DB"
        collapsed = Path(self._tmp.name) / "different.db"
        self._seed(exact, session_id="sess-exact-case")
        self._seed(collapsed, session_id="sess-collapsed-case")
        exact_path = os.path.realpath(exact)
        collapsed_path = os.path.realpath(collapsed)
        exact_info = os.stat(exact)

        def simulated_windows_normcase(value):
            return collapsed_path if value == exact_path else value

        with mock.patch.object(
                dashboard_data.os.path, "normcase",
                side_effect=simulated_windows_normcase):
            identity = dashboard_data._database_identity(exact)

        self.assertEqual(identity[0], exact_path)
        self.assertEqual(identity[1:3],
                         (exact_info.st_dev, exact_info.st_ino))

    def test_case_alias_invalidation_follows_a_case_insensitive_volume(self):
        """Darwin's normcase is identity even when the volume folds case."""
        upper = Path(self._tmp.name) / "Usage.DB"
        os.replace(self.db_path, upper)
        if not self.db_path.exists():
            self.skipTest("temporary volume is case-sensitive")

        get_dashboard_data(upper)
        self.assertTrue(dashboard_data._PAYLOAD_CACHE)
        self.assertIsNotNone(dashboard_data._VERSION_PROBE)

        upper.unlink()
        self.assertIn("error", get_dashboard_data(self.db_path))

        self.assertEqual(dashboard_data._PAYLOAD_CACHE, {})
        self.assertIsNone(dashboard_data._VERSION_PROBE)

    def test_unicode_case_alias_invalidation_follows_an_insensitive_volume(self):
        """Case probing must not assume every cased character is ASCII."""
        upper = Path(self._tmp.name) / "\u00c4"
        lower = Path(self._tmp.name) / "\u00e4"
        os.replace(self.db_path, upper)
        if not lower.exists():
            self.skipTest("temporary volume does not fold this Unicode case")

        get_dashboard_data(upper)
        self.assertTrue(dashboard_data._PAYLOAD_CACHE)
        self.assertIsNotNone(dashboard_data._VERSION_PROBE)

        upper.unlink()
        self.assertIn("error", get_dashboard_data(lower))

        self.assertEqual(dashboard_data._PAYLOAD_CACHE, {})
        self.assertIsNone(dashboard_data._VERSION_PROBE)

    def test_case_alias_probe_checks_later_characters(self):
        """One unsupported case mapping must not hide a later valid alias."""
        lower = Path(self._tmp.name) / "\u0131a"
        upper = Path(self._tmp.name) / "\u0131A"
        os.replace(self.db_path, lower)
        if not upper.exists():
            self.skipTest("temporary volume does not fold the later character")

        get_dashboard_data(lower)
        self.assertTrue(dashboard_data._PAYLOAD_CACHE)
        self.assertIsNotNone(dashboard_data._VERSION_PROBE)

        lower.unlink()
        self.assertIn("error", get_dashboard_data(upper))

        self.assertEqual(dashboard_data._PAYLOAD_CACHE, {})
        self.assertIsNone(dashboard_data._VERSION_PROBE)

    def test_unicode_normalization_alias_invalidation_follows_the_volume(self):
        """Canonical-equivalent names can alias even when their bytes differ."""
        composed = Path(self._tmp.name) / "\u00e9"
        decomposed = Path(self._tmp.name) / "e\u0301"
        os.replace(self.db_path, composed)
        if not decomposed.exists():
            self.skipTest("temporary volume is normalization-sensitive")

        get_dashboard_data(composed)
        self.assertTrue(dashboard_data._PAYLOAD_CACHE)
        self.assertIsNotNone(dashboard_data._VERSION_PROBE)

        composed.unlink()
        self.assertIn("error", get_dashboard_data(decomposed))

        self.assertEqual(dashboard_data._PAYLOAD_CACHE, {})
        self.assertIsNone(dashboard_data._VERSION_PROBE)

    def test_case_only_names_remain_distinct_on_a_case_sensitive_volume(self):
        """Case folding for invalidation must follow the filesystem, not OS."""
        upper = Path(self._tmp.name) / "CaseOnly.DB"
        lower = Path(self._tmp.name) / "caseonly.db"
        self._seed(upper, session_id="sess-case-sensitive")
        if lower.exists():
            self.skipTest("temporary volume is case-insensitive")

        get_dashboard_data(upper)
        probe = dashboard_data._VERSION_PROBE
        self.assertTrue(dashboard_data._PAYLOAD_CACHE)
        self.assertIsNotNone(probe)

        self.assertIn("error", get_dashboard_data(lower))

        self.assertEqual(len(dashboard_data._PAYLOAD_CACHE), 1)
        self.assertIs(dashboard_data._VERSION_PROBE, probe)

    def test_parent_case_alias_invalidation_follows_an_insensitive_volume(self):
        """A numeric filename can rely only on a parent component for proof."""
        mixed_parent = Path(self._tmp.name) / "DataDir"
        mixed_parent.mkdir()
        aliased_parent = Path(self._tmp.name) / "datadir"
        path = mixed_parent / "123"
        self._seed(path, session_id="sess-parent-case")
        if not aliased_parent.exists():
            self.skipTest("temporary volume is case-sensitive")

        get_dashboard_data(path)
        self.assertTrue(dashboard_data._PAYLOAD_CACHE)
        self.assertIsNotNone(dashboard_data._VERSION_PROBE)

        path.unlink()
        self.assertIn("error", get_dashboard_data(aliased_parent / "123"))

        self.assertEqual(dashboard_data._PAYLOAD_CACHE, {})
        self.assertIsNone(dashboard_data._VERSION_PROBE)

    def test_component_alias_proof_crosses_a_mount_device_boundary(self):
        """A mount point's spelling belongs to its parent filesystem."""
        outer = Path(self._tmp.name) / "Outer"
        mixed_parent = outer / "Mount"
        mixed_parent.mkdir(parents=True)
        aliased_parent = outer / "mount"
        path = mixed_parent / "usage.db"
        self._seed(path, session_id="sess-mounted-case")
        if not aliased_parent.exists():
            self.skipTest("temporary volume is case-sensitive")

        real_lstat = os.lstat
        outer_absolute = os.path.abspath(outer)

        def simulated_mount(candidate):
            info = real_lstat(candidate)
            if os.path.abspath(candidate) != outer_absolute:
                return info
            return types.SimpleNamespace(
                st_dev=info.st_dev + 1,
                st_ino=info.st_ino,
                st_mode=info.st_mode,
                st_nlink=info.st_nlink,
            )

        with mock.patch.object(dashboard_data.os, "lstat",
                               side_effect=simulated_mount):
            equivalence = dashboard_data._cache_component_equivalence(
                str(path))

        mount_index = Path(path).parts.index("Mount")
        self.assertTrue(
            equivalence[mount_index]
            & dashboard_data._COMPONENT_CASE_INSENSITIVE)

    def test_parent_case_only_names_stay_distinct_on_a_sensitive_volume(self):
        """Parent folding also requires evidence from the database's volume."""
        mixed_parent = Path(self._tmp.name) / "ParentCase"
        mixed_parent.mkdir()
        aliased_parent = Path(self._tmp.name) / "parentcase"
        if aliased_parent.exists():
            self.skipTest("temporary volume is case-insensitive")
        path = mixed_parent / "123"
        self._seed(path, session_id="sess-sensitive-parent")

        get_dashboard_data(path)
        probe = dashboard_data._VERSION_PROBE
        self.assertTrue(dashboard_data._PAYLOAD_CACHE)
        self.assertIsNotNone(probe)

        self.assertIn("error", get_dashboard_data(aliased_parent / "123"))

        self.assertEqual(len(dashboard_data._PAYLOAD_CACHE), 1)
        self.assertIs(dashboard_data._VERSION_PROBE, probe)

    def test_a_failure_for_another_path_preserves_the_current_cache(self):
        """Invalidation is path-scoped, not a global cache-reset side effect."""
        get_dashboard_data(self.db_path)
        probe = dashboard_data._VERSION_PROBE
        self.assertTrue(dashboard_data._PAYLOAD_CACHE)

        missing = Path(self._tmp.name) / "some-other-database.db"
        self.assertIn("error", get_dashboard_data(missing))

        self.assertEqual(len(dashboard_data._PAYLOAD_CACHE), 1)
        self.assertIs(dashboard_data._VERSION_PROBE, probe)
        with mock.patch.object(
                dashboard_data, "_collect_dashboard_data",
                wraps=dashboard_data._collect_dashboard_data) as build:
            get_dashboard_data(self.db_path)
        self.assertEqual(build.call_count, 0)

    @unittest.skipUnless(os.name == "posix", "replacing an open SQLite file is POSIX-only")
    def test_rejecting_a_foreign_replacement_releases_old_cache_state(self):
        """Admission failure must retire state tied to the displaced inode."""
        get_dashboard_data(self.db_path)
        self.assertTrue(dashboard_data._PAYLOAD_CACHE)
        self.assertIsNotNone(dashboard_data._VERSION_PROBE)

        foreign = Path(self._tmp.name) / "foreign.db"
        conn = sqlite3.connect(foreign)
        conn.execute("CREATE TABLE private_notes (body TEXT)")
        conn.execute("INSERT INTO private_notes VALUES ('keep me')")
        conn.commit()
        conn.close()
        os.replace(foreign, self.db_path)

        with self.assertRaises(db.ForeignDatabaseError):
            get_dashboard_data(self.db_path)

        self.assertEqual(len(dashboard_data._PAYLOAD_CACHE), 0)
        self.assertIsNone(dashboard_data._VERSION_PROBE)

    @unittest.skipUnless(os.name == "posix", "replacing an open SQLite file is POSIX-only")
    def test_rejected_final_symlink_replacement_releases_old_cache_state(self):
        """Invalidation must retain the requested name as well as its target."""
        get_dashboard_data(self.db_path)
        self.assertTrue(dashboard_data._PAYLOAD_CACHE)
        self.assertIsNotNone(dashboard_data._VERSION_PROBE)

        moved = Path(self._tmp.name) / "moved.db"
        os.replace(self.db_path, moved)
        self.db_path.symlink_to(moved.name)

        with self.assertRaises(RuntimeError):
            get_dashboard_data(self.db_path)

        self.assertEqual(len(dashboard_data._PAYLOAD_CACHE), 0)
        self.assertIsNone(dashboard_data._VERSION_PROBE)

    @unittest.skipUnless(os.name == "posix", "replacing an open SQLite file is POSIX-only")
    def test_sources_rejecting_a_foreign_replacement_releases_cache_state(self):
        """The lightweight endpoint must retire state on admission failure."""
        get_dashboard_data(self.db_path)
        self.assertTrue(dashboard_data._PAYLOAD_CACHE)
        self.assertIsNotNone(dashboard_data._VERSION_PROBE)

        foreign = Path(self._tmp.name) / "foreign-sources.db"
        conn = sqlite3.connect(foreign)
        conn.execute("CREATE TABLE private_notes (body TEXT)")
        conn.commit()
        conn.close()
        os.replace(foreign, self.db_path)

        with self.assertRaises(db.ForeignDatabaseError):
            dashboard_data.available_sources(self.db_path)

        self.assertEqual(len(dashboard_data._PAYLOAD_CACHE), 0)
        self.assertIsNone(dashboard_data._VERSION_PROBE)

    def test_an_error_body_offered_to_the_cache_is_refused(self):
        """`get_dashboard_data` returns its error before reaching the store, so
        the guard inside it is only reachable directly — and it is the one that
        would still hold if a future `_collect_dashboard_data` learned to answer
        with an error of its own rather than raising."""
        get_dashboard_data(self.db_path)
        (key, (version, _)), = dashboard_data._PAYLOAD_CACHE.items()
        identity, source = key
        dashboard_data._store_payload(
            identity, version, version, source,
            {"error": "Failed to read the usage database"}, seconds=99.0)
        (_, still), = dashboard_data._PAYLOAD_CACHE.values()
        self.assertNotIn("error", still)


class TestThePayloadCacheRebuildsWhatIsNotInTheDatabase(PayloadCacheTestCase):
    """The fields a hit may not serve. See dashboard_data.LIVE_PAYLOAD_FIELDS."""

    def test_the_stamp_advances_on_a_hit(self):
        first = get_dashboard_data(self.db_path)
        with mock.patch.object(dashboard_data, "datetime") as clock:
            clock.now.return_value = datetime(2031, 2, 3, 4, 5, 6)
            second = get_dashboard_data(self.db_path)
        self.assertEqual(second["generated_at"], "2031-02-03 04:05:06")
        self.assertNotEqual(first["generated_at"], second["generated_at"])

    def test_the_plan_panel_is_not_frozen_by_a_hit(self):
        """~/.claude.json can change without anything committed here, so a
        cached `subscription_limits` would go on reporting
        a window that has already reset (invariant 7)."""
        first = get_dashboard_data(self.db_path)
        sentinel = {"available": True, "plan_type": "sentinel", "windows": []}
        with mock.patch.object(dashboard_data, "claude_limits",
                               return_value=sentinel) as limits:
            second = get_dashboard_data(self.db_path)
        self.assertEqual(limits.call_count, 1,
                         "a cache hit did not re-read the plan config")
        self.assertEqual(second["subscription_limits"]["plan_type"], "sentinel")
        self.assertNotEqual(first["subscription_limits"], sentinel)

    def test_a_hit_serves_exactly_the_declared_database_only_fields(self):
        get_dashboard_data(self.db_path)
        (_, cached), = dashboard_data._PAYLOAD_CACHE.values()
        served = get_dashboard_data(self.db_path)
        self.assertEqual(sorted(served), sorted(cached))
        rebuilt = {k for k in served if served[k] is not cached[k]}
        self.assertEqual(
            sorted(rebuilt), sorted(dashboard_data.LIVE_PAYLOAD_FIELDS),
            "a cache hit rebuilt a different set of fields than "
            "LIVE_PAYLOAD_FIELDS declares")


class TestThePayloadCacheIsBounded(PayloadCacheTestCase):
    def test_it_never_holds_more_than_its_cap(self):
        """The entry cap bounds how many payloads a long-lived server retains."""
        self._append_session("sess-codex-only", source="codex")
        for source in (None, "claude", "codex", None, "claude"):
            get_dashboard_data(self.db_path, source)
            self.assertLessEqual(len(dashboard_data._PAYLOAD_CACHE),
                                 dashboard_data.PAYLOAD_CACHE_MAX_ENTRIES)
        self.assertEqual(len(dashboard_data._PAYLOAD_CACHE),
                         dashboard_data.PAYLOAD_CACHE_MAX_ENTRIES)

    def test_the_evicted_entry_is_the_least_recently_used(self):
        self._append_session("sess-codex-only", source="codex")
        get_dashboard_data(self.db_path, "claude")
        get_dashboard_data(self.db_path, "codex")
        get_dashboard_data(self.db_path, "claude")   # claude is now the newest
        get_dashboard_data(self.db_path, None)       # evicts codex, not claude
        sources = {key[1] for key in dashboard_data._PAYLOAD_CACHE}
        self.assertEqual(sources, {"claude", None})

    def test_reset_releases_both_the_entries_and_the_connection(self):
        get_dashboard_data(self.db_path)
        self.assertTrue(dashboard_data._PAYLOAD_CACHE)
        self.assertIsNotNone(dashboard_data._VERSION_PROBE)
        dashboard_data.reset_payload_cache()
        self.assertEqual(len(dashboard_data._PAYLOAD_CACHE), 0)
        self.assertIsNone(dashboard_data._VERSION_PROBE)

    def test_a_cache_may_not_outlive_the_connection_its_versions_came_from(self):
        """SQLite's counter is a local property of a connection: three fresh
        connections read the same baseline against a database a long-lived one
        reads a much larger number at. An entry surviving its probe would be
        compared against a restarted counter and answer "unchanged"."""
        get_dashboard_data(self.db_path)
        self.assertTrue(dashboard_data._PAYLOAD_CACHE)
        with dashboard_data._CACHE_LOCK:
            dashboard_data._close_version_probe()
        self.assertEqual(len(dashboard_data._PAYLOAD_CACHE), 0,
                         "closing the probe left entries keyed on its counter")


class TestADashboardTriggeredRebuildNamesTheFileItThrewAway(unittest.TestCase):
    """`db_path` is threaded through both dashboard entry points for one reason.

    `init_db` announces a rebuild on stderr and `_announce_rebuild` prints the
    `  file: …` line only when it was given a path, so `init_db(conn)` would
    still rebuild and still announce — just without saying which database. That
    matters because there is rarely only one: `CLAUDE_USAGE_DB`, a Docker bind
    mount and the VS Code extension's own server can all be live on one machine.

    Both call sites are covered separately on purpose. Reverting either one to
    `init_db(conn)` used to leave the whole suite green, so a single test over
    one of them would have re-admitted the other.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db_path = Path(self.tmpdir.name) / "usage.db"
        conn = get_db(self.db_path)
        try:
            init_db(conn)
            # An extra table this build does not declare: the shape a database
            # written by any v1.5.4+ release has, and the one `schema_mismatches`
            # reports as `not part of this schema`.
            conn.execute("CREATE TABLE schema_meta (key TEXT, value TEXT)")
            conn.commit()
        finally:
            conn.close()

    def _stderr_of(self, call):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            call()
        return buffer.getvalue()

    def test_available_sources_names_the_file(self):
        err = self._stderr_of(lambda: dashboard_data.available_sources(self.db_path))
        self.assertIn("written by a different version", err)
        self.assertIn(str(self.db_path), err,
                      "the rebuild notice did not say which database it emptied")

    def test_the_payload_collector_names_the_file(self):
        err = self._stderr_of(lambda: get_dashboard_data(self.db_path))
        self.assertIn("written by a different version", err)
        self.assertIn(str(self.db_path), err,
                      "the rebuild notice did not say which database it emptied")


if __name__ == "__main__":
    unittest.main()
