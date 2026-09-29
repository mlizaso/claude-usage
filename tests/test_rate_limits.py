"""Tests for the rate-limit view, end to end.

Claude Code records a hit limit as an *assistant* record with `error:
"rate_limit"`, `apiErrorStatus: 429`, a `"<synthetic>"` model, an all-zero usage
block, and a notice reading e.g. "You've hit your session limit · resets 3:20am
(Europe/Madrid)". The all-zero usage is why these were invisible: the scanner
drops zero-token assistant records, so every one of them was discarded before
reaching the database.

The incident view records when a limit was reached and the reset information
available in its notice. It does not infer a remaining allowance from an error.
"""

import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

import scanner
import transcripts
from dashboard_data import get_dashboard_data
from transcripts import extract_limit_event, parse_jsonl_file

from tests.test_dashboard_js import emit, requires_node, run_js


def _limit_record(uuid, timestamp, reset="3:20am", zone="Europe/Madrid",
                  session_id="s1"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": session_id,
        "timestamp": timestamp, "cwd": "/home/u/proj", "gitBranch": "main",
        "error": "rate_limit", "isApiErrorMessage": True, "apiErrorStatus": 429,
        "message": {
            "id": uuid, "model": "<synthetic>", "role": "assistant",
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0},
            "content": [{"type": "text",
                         "text": f"You've hit your session limit · resets {reset} ({zone})"}],
        },
    })


def _normal_turn(message_id, timestamp, session_id="s1"):
    return json.dumps({
        "type": "assistant", "sessionId": session_id, "timestamp": timestamp,
        "cwd": "/home/u/proj", "gitBranch": "main",
        "message": {"id": message_id, "model": "claude-opus-4-8", "content": [],
                    "usage": {"input_tokens": 100, "output_tokens": 50,
                              "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0}},
    })


class TestExtractLimitEvent(unittest.TestCase):
    def test_reads_the_reset_time_and_zone_out_of_the_notice(self):
        record = json.loads(_limit_record("u1", "2026-04-08T10:00:00Z"))
        event = extract_limit_event(record, "s1")
        self.assertIsNotNone(event)
        self.assertEqual(event["reset_hint"], "3:20am")
        self.assertEqual(event["reset_zone"], "Europe/Madrid")
        self.assertEqual(event["status"], 429)

    def test_matches_on_the_error_field_not_the_prose(self):
        """An ordinary message discussing rate limits must never be counted.

        The transcripts contain sessions where rate limiting was discussed at
        length in text and in shell commands; a substring match would turn all
        of that into fake incidents.
        """
        impostor = json.loads(_normal_turn("m1", "2026-04-08T10:00:00Z"))
        impostor["message"]["content"] = [
            {"type": "text", "text": "You've hit your session limit · resets 3:20am"}]
        self.assertIsNone(extract_limit_event(impostor, "s1"))

    def test_a_notice_without_a_reset_time_still_records_the_event(self):
        record = json.loads(_limit_record("u2", "2026-04-08T10:00:00Z"))
        record["message"]["content"] = [{"type": "text", "text": "You've hit your limit"}]
        event = extract_limit_event(record, "s1")
        self.assertIsNotNone(event)
        self.assertEqual(event["reset_hint"], "")

    def test_an_unrecognised_error_value_is_still_captured(self):
        """A future Claude Code release renaming the value must not silently
        empty the panel — the record still lands, tagged with whatever it said."""
        record = json.loads(_limit_record("u9", "2026-04-08T10:00:00Z"))
        record["error"] = "usage_limit_reached"
        event = extract_limit_event(record, "s1")
        self.assertIsNotNone(event)
        self.assertEqual(event["kind"], "usage_limit_reached")

    def test_server_faults_are_captured_but_are_not_limit_incidents(self):
        """529/500 are real friction but not quota events; mixing them would
        make the panel mean two things at once."""
        record = json.loads(_limit_record("u8", "2026-04-08T10:00:00Z"))
        record["error"] = "server_error"
        record["apiErrorStatus"] = 529
        event = extract_limit_event(record, "s1")
        self.assertEqual(event["kind"], "server_error")

    def test_a_record_without_a_uuid_is_skipped(self):
        """The uuid is the dedup key; without one a rescan would duplicate."""
        record = json.loads(_limit_record("", "2026-04-08T10:00:00Z"))
        self.assertIsNone(extract_limit_event(record, "s1"))

    def test_a_record_not_flagged_by_the_client_is_ignored(self):
        record = json.loads(_normal_turn("m9", "2026-04-08T10:00:00Z"))
        self.assertIsNone(extract_limit_event(record, "s1"))

    def test_only_the_json_boolean_true_is_an_api_error_flag(self):
        """Truthiness must not turn malformed transcript data into incidents."""
        invalid_flags = (
            False, None, 0, 1, "", "false", "true", [], [False], {},
            {"value": False},
        )
        for flag in invalid_flags:
            with self.subTest(flag=flag):
                record = json.loads(
                    _limit_record("u-invalid", "2026-04-08T10:00:00Z"))
                record["isApiErrorMessage"] = flag
                self.assertIsNone(extract_limit_event(record, "s1"))

    def test_a_malformed_truthy_flag_does_not_hide_a_real_turn(self):
        """A false incident also skips the assistant turn that carried it."""
        record = json.loads(_normal_turn("m-truthy", "2026-04-08T10:00:00Z"))
        record.update({
            "uuid": "u-truthy",
            "error": "rate_limit",
            "isApiErrorMessage": "false",
            "apiErrorStatus": 429,
        })
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            _sessions, turns, _agents, events, _lines = parse_jsonl_file(path)

        self.assertEqual([turn["message_id"] for turn in turns], ["m-truthy"])
        self.assertEqual(events, [])

    def test_text_collection_stops_once_the_stored_bound_is_full(self):
        """A bounded result must not first build an unbounded joined string."""

        class MustNotBeRead(dict):
            def get(self, key, default=None):
                raise AssertionError("text beyond the 2048-character bound was read")

        message = {
            "content": [
                {"text": "x" * 2048},
                MustNotBeRead(text="unreachable"),
            ]
        }
        self.assertEqual(transcripts._message_text(message), "x" * 2048)


class TestScannerCapturesLimitEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.projects = self.tmp / "projects" / "u" / "p"
        self.projects.mkdir(parents=True)
        self.db_path = self.tmp / "usage.db"
        self.transcript = self.projects / "s1.jsonl"

    def _scan(self):
        return scanner.scan(projects_dir=self.tmp / "projects",
                            db_path=self.db_path, verbose=False)

    def _events(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM limit_events ORDER BY timestamp")]
        conn.close()
        return rows

    def test_a_limit_notice_is_stored_and_is_not_counted_as_a_turn(self):
        self.transcript.write_text(
            _normal_turn("m1", "2026-04-08T10:00:00Z") + "\n"
            + _limit_record("u1", "2026-04-08T10:05:00Z") + "\n", encoding="utf-8")
        result = self._scan()
        self.assertEqual(result["turns"], 1, "the notice must not inflate turn counts")
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], 429)
        self.assertEqual(events[0]["reset_hint"], "3:20am")

    def test_the_parser_reports_limit_events_separately(self):
        self.transcript.write_text(
            _limit_record("u1", "2026-04-08T10:00:00Z") + "\n",
            encoding="utf-8")
        _, turns, _, limit_events, _ = parse_jsonl_file(str(self.transcript))
        self.assertEqual(turns, [])
        self.assertEqual(len(limit_events), 1)

    def test_rescanning_does_not_duplicate_an_event(self):
        """One incident emits a notice per retry and per subagent; a rescan of
        the same file must not multiply them again."""
        import time
        self.transcript.write_text(
            _limit_record("u1", "2026-04-08T10:00:00Z") + "\n",
            encoding="utf-8")
        self._scan()
        time.sleep(0.05)
        with open(self.transcript, "a", encoding="utf-8") as handle:
            handle.write(_normal_turn("m2", "2026-04-08T10:10:00Z") + "\n")
        self._scan()
        self.assertEqual(len(self._events()), 1)

    def test_zero_token_records_that_are_not_limits_are_still_dropped(self):
        """The guard that hid these events must otherwise stay intact."""
        empty = json.loads(_normal_turn("m3", "2026-04-08T10:00:00Z"))
        empty["message"]["usage"] = {"input_tokens": 0, "output_tokens": 0,
                                     "cache_read_input_tokens": 0,
                                     "cache_creation_input_tokens": 0}
        self.transcript.write_text(json.dumps(empty) + "\n", encoding="utf-8")
        self._scan()
        conn = sqlite3.connect(self.db_path)
        turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        conn.close()
        self.assertEqual(turns, 0)
        self.assertEqual(self._events(), [])


class TestIncidentAggregation(unittest.TestCase):
    """One incident produces many notices; the API collapses them."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        projects = self.tmp / "projects" / "u" / "p"
        projects.mkdir(parents=True)
        self.db_path = self.tmp / "usage.db"
        lines = [_normal_turn("m1", "2026-04-08T09:00:00Z")]
        # 20 notices two minutes apart, all announcing the same reset: one incident.
        for i in range(20):
            lines.append(_limit_record(f"a{i}", f"2026-04-08T10:{i:02d}:00Z", reset="3:20am"))
        # A separate incident hours later with a different reset target.
        for i in range(3):
            lines.append(_limit_record(f"b{i}", f"2026-04-08T20:{i:02d}:00Z", reset="11pm"))
        (projects / "s1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        scanner.scan(projects_dir=self.tmp / "projects",
                     db_path=self.db_path, verbose=False)
        self.payload = get_dashboard_data(self.db_path)

    def test_server_faults_do_not_appear_as_limit_incidents(self):
        tmp = Path(tempfile.mkdtemp())
        projects = tmp / "projects" / "u" / "p"
        projects.mkdir(parents=True)
        db_path = tmp / "usage.db"
        fault = json.loads(_limit_record("f1", "2026-04-08T12:00:00Z"))
        fault["error"] = "server_error"
        fault["apiErrorStatus"] = 529
        (projects / "s1.jsonl").write_text(json.dumps(fault) + "\n", encoding="utf-8")
        scanner.scan(projects_dir=tmp / "projects", db_path=db_path, verbose=False)
        conn = sqlite3.connect(db_path)
        stored = conn.execute("SELECT kind FROM limit_events").fetchall()
        conn.close()
        self.assertEqual(stored, [("server_error",)], "the fault should be recorded")
        self.assertEqual(get_dashboard_data(db_path)["limit_incidents"], [],
                         "but it is not a usage-limit incident")

    def test_notices_collapse_into_incidents(self):
        incidents = self.payload["limit_incidents"]
        self.assertEqual(len(incidents), 2, "23 notices should be 2 incidents")
        self.assertEqual(sorted(i["notices"] for i in incidents), [3, 20])

    def test_each_incident_reports_how_long_it_blocked(self):
        first = min(self.payload["limit_incidents"], key=lambda i: i["started"])
        self.assertAlmostEqual(first["blocked_min"], 19.0, places=1)

    def test_each_incident_keeps_its_reset_target(self):
        hints = {i["reset_hint"] for i in self.payload["limit_incidents"]}
        self.assertEqual(hints, {"3:20am", "11pm"})

    def test_incidents_carry_the_project_they_hit(self):
        for incident in self.payload["limit_incidents"]:
            self.assertEqual(incident["projects"], ["u/proj"])  # from cwd, not the dir

    def test_a_different_reset_target_starts_a_new_incident(self):
        """Two limits *inside* the gap window are still two incidents when they
        announce different resets — otherwise hitting a second, later-resetting
        limit while the first is still recent would be silently absorbed.

        The gap alone cannot separate these: they are one minute apart.
        """
        tmp = Path(tempfile.mkdtemp())
        projects = tmp / "projects" / "u" / "p"
        projects.mkdir(parents=True)
        db_path = tmp / "usage.db"
        (projects / "s1.jsonl").write_text("\n".join([
            _limit_record("c0", "2026-04-08T10:00:00Z", reset="11am"),
            _limit_record("c1", "2026-04-08T10:01:00Z", reset="2pm"),
        ]) + "\n", encoding="utf-8")
        scanner.scan(projects_dir=tmp / "projects", db_path=db_path, verbose=False)
        incidents = get_dashboard_data(db_path)["limit_incidents"]
        self.assertEqual(len(incidents), 2,
                         "a new reset target must open a new incident even one "
                         "minute after the previous notice")
        self.assertEqual(sorted(i["reset_hint"] for i in incidents), ["11am", "2pm"])


@requires_node
class TestLimitsPanel(unittest.TestCase):
    INCIDENTS = [
        {"day": "2026-07-30", "started": "2026-07-30 05:18", "blocked_min": 23.2,
         "notices": 109, "projects": ["acme/web-storefront"], "reset_hint": "7:10am",
         "reset_zone": "Europe/Madrid", "status": 429},
    ]

    def _render(self, incidents):
        return run_js(emit("""
          (() => {
            let summary = '', rows = '';
            document.getElementById = (id) => ({
              set innerHTML(v) { if (id === 'limits-summary') summary = v;
                                 if (id === 'limits-body') rows = v; },
              get innerHTML() { return ''; } });
            renderLimits(incidents);
            return { summary, rows };
          })()
        """, incidents=incidents))

    def test_renders_the_incident_with_its_reset_time(self):
        out = self._render(self.INCIDENTS)
        self.assertIn("23.2m", out["rows"])
        self.assertIn("7:10am", out["rows"])
        self.assertIn("Europe/Madrid", out["rows"])
        self.assertIn("109", out["rows"])

    def test_summarises_the_range(self):
        out = self._render(self.INCIDENTS)
        self.assertIn("Times limited", out["summary"])
        self.assertIn("Time blocked", out["summary"])

    def test_says_so_plainly_when_nothing_was_limited(self):
        out = self._render([])
        self.assertIn("No usage limits reached", out["rows"])

    def test_never_renders_a_percentage(self):
        """The allowance is unknown, so a % here would be fabricated."""
        out = self._render(self.INCIDENTS)
        self.assertNotIn("%", out["rows"])
        self.assertNotIn("%", out["summary"])

    def test_project_names_are_escaped(self):
        out = self._render([dict(self.INCIDENTS[0],
                                 projects=["<img src=x onerror=alert(1)>"])])
        self.assertNotIn("<img", out["rows"])


class TestTheCaveatIsPresentInThePage(unittest.TestCase):
    """The incidents table must not let a subscriber read it as a quota gauge.

    The page now has a *separate* Plan Limits panel that genuinely does show
    remaining headroom, sourced from Claude Code's own cache rather than from
    the transcripts. So the old blanket claim that the allowance "is not
    recorded" is no longer true of the page as a whole — but it is still true of
    this table, and the caveat has to keep saying which of the two a reader is
    looking at.
    """

    def test_the_incidents_table_disclaims_being_a_quota_gauge(self):
        import dashboard
        page = dashboard.HTML_TEMPLATE
        self.assertIn("no figure in this table is a percentage of a quota", page)

    def test_the_caveat_points_at_the_panel_that_does_show_headroom(self):
        import dashboard
        page = dashboard.HTML_TEMPLATE
        self.assertIn("Plan Limits", page)
        self.assertIn("remaining headroom", page)

    def test_the_plan_panel_says_it_is_a_cache_not_a_live_reading(self):
        """It lags real usage by tens of minutes; the page must never imply otherwise."""
        import dashboard
        page = dashboard.HTML_TEMPLATE
        self.assertIn("local cache", page)
        self.assertIn("not a live query", page)


@requires_node
class TestThePlanPanelSinksAreGuardedNotMerelyNumeric(unittest.TestCase):
    """Two payload values reach markup outside `esc`, and both are now guarded.

    A security audit found that `renderPlanLimits` interpolated `percent`
    straight into a `style="width:…"` attribute and `renderLimits`
    interpolated `notices` straight into a `<td>`. Both were safe only because
    their producers happen to emit numbers -- and `safejson.safe_dashboard_value`
    passes non-strings through untouched, so nothing in the pipeline promises
    that. The attribute one is the dangerous half: `esc` alone would not save it,
    because escaping the HTML metacharacters still leaves a CSS declaration free
    to close itself and start another.

    These tests feed a string where a number is expected. They fail on the
    unguarded code -- verified by reverting each guard in turn -- so the sinks
    are now protected by something that executes rather than by the current type
    of their inputs.
    """

    def _windows(self, percent):
        return run_js(emit("""
          (() => {
            let html = '';
            document.querySelector = () => null;
            document.getElementById = (id) => ({
              set innerHTML(v) { if (id === 'plan-windows') html = v; },
              get innerHTML() { return ''; },
              setAttribute() {}, removeAttribute() {},
              classList: { add() {}, remove() {}, toggle() {} },
              style: {}, textContent: '', title: '', hidden: false });
            renderPlanLimits(payload);
            return html;
          })()
        """, payload={"available": True, "plan_type": "max", "windows": [
            {"label": "Session", "percent": percent, "is_active": True,
             "resets_at": None, "severity": "normal", "expired": False}]}))

    def test_a_hostile_percent_cannot_inject_a_css_declaration(self):
        html = self._windows("50;background:url(https://evil.test/x)")
        # The bar is the sink under test: the value must not reach the style
        # attribute at all. It DOES still appear in the percentage caption
        # beside it, escaped by `esc` -- that is display, not injection, and is
        # the correct outcome for a value the source sent us.
        self.assertIn('style="width:0%"', html)
        styles = re.findall(r'style="([^"]*)"', html)
        self.assertTrue(styles, "no style attribute rendered; harness is wrong")
        for value in styles:
            self.assertRegex(value, r"^width:\d+(\.\d+)?%$")
            self.assertNotIn("url(", value)
            self.assertNotIn(";", value)

    def test_a_percent_that_is_not_a_number_renders_an_empty_bar(self):
        for hostile in ("abc", "", "NaN", "1e999"):
            with self.subTest(percent=hostile):
                self.assertIn('style="width:0%"', self._windows(hostile))

    def test_a_numeric_percent_is_unchanged_and_still_clamped(self):
        self.assertIn('style="width:73%"', self._windows(73))
        self.assertIn('style="width:100%"', self._windows(140))
        self.assertIn('style="width:0%"', self._windows(-5))

    def test_a_hostile_notices_count_is_escaped_in_the_incident_row(self):
        rows = run_js(emit("""
          (() => {
            let rows = '';
            document.getElementById = (id) => ({
              set innerHTML(v) { if (id === 'limits-body') rows = v; },
              get innerHTML() { return ''; } });
            renderLimits(incidents);
            return rows;
          })()
        """, incidents=[{"day": "2026-07-30", "started": "2026-07-30 05:18",
                         "blocked_min": 1.0,
                         "notices": "<img src=x onerror=alert(1)>",
                         "projects": [], "reset_hint": "", "reset_zone": "",
                         "status": 429}]))
        self.assertNotIn("<img", rows)
        self.assertIn("&lt;img", rows)


if __name__ == "__main__":
    unittest.main()
