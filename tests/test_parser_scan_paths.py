"""Properties both transcript parsers must hold across the two scan paths.

`scan()` reads a file one of two ways — a full parse the first time, and a
`skip_lines=` parse of everything appended since — and invariant 3 exists
because those were once two copies of the same loop that drifted twice. The
tests here pin the shared contract rather than either parser's grammar:

* A successful parse reports the full line count. The scanner does not stamp
  an unterminated final record as complete, and failed reads remain retryable.
* Unreadable records are counted, including malformed JSON and overlong lines.
* A malformed line must not discard valid records later in the same file.
* Incremental and whole-file reads must agree on the appended records.

Kept in its own file because these are cross-parser rules: a fix applied to one
grammar and not the other is exactly the drift invariant 3 warns about.
"""

import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codex_transcripts
import scanner
import transcripts
from codex_claude_usage import timestamps


def _claude(message_id, ts, out, session="s-1", model="claude-opus-5"):
    return json.dumps({
        "type": "assistant", "sessionId": session, "cwd": "/home/u/proj",
        "timestamp": ts, "gitBranch": "main",
        "message": {"id": message_id, "model": model, "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": out}},
    })


def _claude_no_id(ts, out, session="s-1", model="claude-opus-5", inp=10):
    """An assistant record whose `message` carries no `id`.

    The Claude parser deliberately keeps these (`turns_no_id`), and today's real
    Claude Code has never written one — but `--projects-dir` is documented as
    "also look here", pointing at a container's mounted history or another
    machine's transcripts, so the shape has to survive being read twice.
    """
    record = json.loads(_claude("dropped", ts, out, session, model))
    del record["message"]["id"]
    record["message"]["usage"]["input_tokens"] = inp
    return json.dumps(record)


def _claude_title(kind, title, session="s-1"):
    """A `custom-title` (the user's own label) or an `ai-title` record.

    Neither carries a timestamp or any usage, which is why a chunk holding one
    still needs a turn beside it for the session row to exist at all.
    """
    key = "customTitle" if kind == "custom-title" else "aiTitle"
    return json.dumps({"type": kind, "sessionId": session, key: title})


def _claude_on_branch(message_id, ts, branch, out=10, session="s-1"):
    """An assistant record stamped with a given branch, or with none at all."""
    record = json.loads(_claude(message_id, ts, out, session))
    if branch is None:
        del record["gitBranch"]
    else:
        record["gitBranch"] = branch
    return json.dumps(record)


def _claude_replay(message_id, ts, branch, out=10, session="s-1", uuid="u-7f3a"):
    """The replay shape: one assistant record re-emitted into its own transcript.

    Claude Code sometimes writes an earlier record again — same `uuid`, same
    `parentUuid`, same `timestamp`, byte-identical `usage` — with `gitBranch`
    re-sampled at the moment of the replay. `uuid`/`parentUuid` are carried here
    to document the shape and nothing more: the parser reads neither on an
    assistant record, so what makes a replayed pair discriminating is the tie on
    the tally AND on the timestamp, which leaves the merge nothing to prefer.
    """
    record = json.loads(_claude_on_branch(message_id, ts, branch, out, session))
    record["uuid"] = uuid
    record["parentUuid"] = "u-parent"
    return json.dumps(record)


def _codex_rec(rtype, payload, ts):
    return json.dumps({"timestamp": ts, "type": rtype, "payload": payload})


def _codex_header(thread, branch="main", root="root-1"):
    return _codex_rec("session_meta",
                      {"id": thread, "session_id": root, "cwd": "/home/u/proj",
                       "git": {"branch": branch}},
                      "2026-08-05T09:59:00.000Z")


def _codex_turn_context(model="gpt-5.6-sol", effort="high",
                        ts="2026-08-05T10:00:00.000Z"):
    return _codex_rec("turn_context", {"model": model, "effort": effort}, ts)


def _codex_token(cumulative, out, ts):
    return _codex_rec("event_msg", {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": 100, "cached_input_tokens": 0,
                             "cache_write_input_tokens": 0, "output_tokens": out,
                             "reasoning_output_tokens": 0,
                             "total_tokens": 100 + out},
        "total_token_usage": {"total_tokens": cumulative},
    }}, ts)


class ParserFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.projects = Path(self.tmp) / "projects"
        (self.projects / "proj").mkdir(parents=True)
        self.db = Path(self.tmp) / "usage.db"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, lines, mtime=1_000_000):
        path = self.projects / "proj" / name
        # Explicit encoding, like every other file operation under tests/: the
        # runner's default is cp1252 on windows-latest, while the product reads
        # every transcript as UTF-8, so a fixture written without this is a
        # Windows-only failure that no local run reproduces.
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def write_raw(self, name, text, mtime=1_000_000):
        """Write exact bytes — including a final line with no newline on it,
        which `write` above cannot express and which no fixture in this suite
        could produce before."""
        path = self.projects / "proj" / name
        path.write_text(text, encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def scan(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return scanner.scan(projects_dir=self.projects, db_path=self.db,
                                verbose=False)

    def rows(self, sql):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql)]
        finally:
            conn.close()


class TestJsonlLineByteBound(unittest.TestCase):
    def test_exact_raw_byte_boundaries_and_replacement_decoding(self):
        cap = 10
        cases = (
            ("lf exactly at cap", b"123456789\n", ["123456789\n"]),
            ("lf one byte over", b"1234567890\n", [None]),
            ("unterminated exactly at cap", b"1234567890", ["1234567890"]),
            ("crlf exactly at cap", b"12345678\r\n", ["12345678\r\n"]),
            ("crlf one byte over", b"123456789\r\n", [None]),
            ("multibyte below cap", "éééé\n".encode("utf-8"), ["éééé\n"]),
            ("multibyte above cap", "ééééé\n".encode("utf-8"), [None]),
            ("invalid utf8 at cap", b"12345678\xff\n", ["12345678�\n"]),
            ("oversized line is drained", b"1234567890\nok\n", [None, "ok\n"]),
        )
        for name, raw, expected in cases:
            with self.subTest(name=name), mock.patch.object(
                    transcripts, "MAX_JSONL_LINE_LENGTH", cap):
                self.assertEqual(
                    list(transcripts._iter_jsonl_lines(io.BytesIO(raw))),
                    expected)


class TestLineCountIsAlwaysTheFullLength(ParserFixture):
    """The position `scan()` stamps into `processed_files.lines`. A parse that
    stops early and still returns its position tells every future scan that the
    tail was already read."""

    def test_a_failed_terminator_read_is_unfinished_not_complete(self):
        path = self.write("sess.jsonl", [
            _claude("m-1", "2026-08-05T10:00:00Z", 10)])
        with mock.patch.object(transcripts, "_open_transcript",
                               side_effect=OSError("transient terminator read")):
            self.assertFalse(transcripts.ends_with_newline(path))

    def test_claude_reports_the_full_length_when_a_record_raises(self):
        lines = [_claude(f"m-{i}", f"2026-08-05T10:0{i}:00Z", 10 + i)
                 for i in range(8)]
        path = self.write("sess.jsonl", lines)

        real = transcripts.extract_limit_event
        seen = {"n": 0}

        def exploding(record, session_id):
            seen["n"] += 1
            if seen["n"] == 3:
                raise RuntimeError("boom")
            return real(record, session_id)

        transcripts.extract_limit_event = exploding
        try:
            with contextlib.redirect_stderr(io.StringIO()) as out:
                _m, turns, _a, _l, line_count = transcripts.parse_jsonl_file(path)
        finally:
            transcripts.extract_limit_event = real

        self.assertEqual(line_count, len(lines))
        self.assertEqual(len(turns), len(lines) - 1, "one record lost, not the file")
        self.assertIn("skipped 1 unreadable record", out.getvalue())

    def test_codex_reports_the_full_length_when_a_record_raises(self):
        lines = [_codex_header("t-1"), _codex_turn_context()]
        lines += [_codex_token(1000 * (i + 1), 10 + i, f"2026-08-05T10:0{i}:00.000Z")
                  for i in range(6)]
        path = self.write("rollout-2026-08-05T10-00-00-019fcf22aaaa.jsonl", lines)

        real = codex_transcripts._usage_from
        seen = {"n": 0}

        def exploding(info):
            seen["n"] += 1
            if seen["n"] == 5:
                raise RuntimeError("boom")
            return real(info)

        codex_transcripts._usage_from = exploding
        try:
            with contextlib.redirect_stderr(io.StringIO()) as out:
                _s, turns, _a, _l, line_count = codex_transcripts.parse_jsonl_file(path)
        finally:
            codex_transcripts._usage_from = real

        self.assertEqual(line_count, len(lines))
        self.assertEqual(len(turns), 5, "one record lost, not the file")
        self.assertIn("skipped 1 unreadable record", out.getvalue())

    def test_a_lossy_parse_still_stamps_the_whole_file_as_read(self):
        """End to end. The bookkeeping is what made the loss permanent: without
        the full length here, `processed_files` records a position the file has
        already passed and the mtime check never brings the scan back."""
        lines = [_claude(f"m-{i}", f"2026-08-05T10:0{i}:00Z", 10 + i)
                 for i in range(8)]
        self.write("sess.jsonl", lines)

        real = transcripts.extract_limit_event
        seen = {"n": 0}

        def exploding(record, session_id):
            seen["n"] += 1
            if seen["n"] == 3:
                raise RuntimeError("boom")
            return real(record, session_id)

        transcripts.extract_limit_event = exploding
        try:
            self.scan()
        finally:
            transcripts.extract_limit_event = real

        stamped = self.rows("SELECT lines FROM processed_files")
        self.assertEqual([r["lines"] for r in stamped], [len(lines)])
        self.assertEqual(len(self.rows("SELECT id FROM turns")), len(lines) - 1)

    def test_an_unreadable_file_still_reports_zero(self):
        """The per-record guard must not swallow the whole-file one. A file that
        could not be opened has genuinely had nothing read, and saying so is
        what stops `scan()` recording a length it never reached.

        This is also the boundary of `TranscriptReadError`, which is raised only
        when a read got PAST the open: here `line_count` is 0,
        `scanner._read_nothing_from` already refuses to stamp, and returning
        normally is the behaviour that predates it.

        The warning is on STDERR. It was a bare `print()`, so under
        `dashboard._background_scan` it landed on the server's own stdout beside
        "Scanning in the background...", where no reader ever sees it -- and it
        is printed once and never repeated.
        """
        missing = self.projects / "proj" / "nope.jsonl"
        for parser in (transcripts.parse_jsonl_file,
                       codex_transcripts.parse_jsonl_file):
            with self.subTest(parser=parser.__module__):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    result = parser(missing)
                self.assertEqual(result[4], 0)
                self.assertIn("error reading", err.getvalue())
                self.assertEqual(out.getvalue(), "",
                                 "a diagnostic must not go to stdout")

    def test_a_clean_parse_says_nothing_extra(self):
        """The warning is evidence of loss, so it must not appear without one."""
        self.write("sess.jsonl", [_claude("m-1", "2026-08-05T10:00:00Z", 10)])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            transcripts.parse_jsonl_file(self.projects / "proj" / "sess.jsonl")
        self.assertEqual(out.getvalue(), "")


class TestAFileThatCouldNotBeReadIsRetried(ParserFixture):
    """The other half of the same bookkeeping, and the half that loses data.

    `line_count = 0` on an unreadable file is right, but `scan()` stamped the
    row anyway — with the file's REAL mtime. The skip test at the top of the
    loop compares exactly that, so the file was excluded from every later scan:
    a transient permission error, an EMFILE burst, or a container's history
    owned by another uid cost that transcript's turns permanently, and the scan
    that lost them reported a clean run.

    `_open_transcript` is patched to raise rather than using `chmod 000` so the
    case runs on Windows too; the RuntimeError it raises is what a permission
    error, a symlink and a hard-linked file all produce.
    """

    def _unreadable(self):
        return mock.patch.object(
            transcripts, "_open_transcript",
            side_effect=RuntimeError("Refusing unsafe transcript path"))

    def _turns(self):
        return [r["output_tokens"] for r in self.rows(
            "SELECT output_tokens FROM turns ORDER BY output_tokens")]

    def test_a_new_file_that_could_not_be_opened_is_read_on_the_next_scan(self):
        self.write("sess.jsonl", [_claude("m-1", "2026-08-05T10:00:00Z", 10)])
        with self._unreadable():
            result = self.scan()
        self.assertEqual(self._turns(), [], "guard the fixture: nothing was read")

        # The mtime is untouched, exactly as it is for a finished transcript
        # whose ownership the user has just repaired.
        self.scan()
        self.assertEqual(self._turns(), [10])
        self.assertEqual(result["skipped"], 1,
                         "a file the scan could not read still has to be counted")

    def test_an_appended_tail_that_could_not_be_opened_is_read_on_the_next_scan(self):
        first = _claude("m-1", "2026-08-05T10:00:00Z", 10)
        second = _claude("m-2", "2026-08-05T10:05:00Z", 99)
        self.write("sess.jsonl", [first])
        self.scan()
        self.write("sess.jsonl", [first, second], mtime=2_000_000)

        with self._unreadable():
            self.scan()
        self.assertEqual(self._turns(), [10], "guard the fixture: the tail was lost")

        self.scan()
        self.assertEqual(self._turns(), [10, 99])

    def test_an_empty_transcript_is_still_recorded_as_processed(self):
        """The case the guard must not catch. An empty file legitimately yields
        nothing, and refusing to stamp it would re-read it on every scan
        forever."""
        path = self.write("empty.jsonl", [])
        path.write_text("", encoding="utf-8")
        os.utime(path, (1_000_000, 1_000_000))
        self.scan()
        self.assertEqual([r["lines"] for r in
                          self.rows("SELECT lines FROM processed_files")], [0])
        self.assertEqual(self.scan()["skipped"], 1)


class TestResumingMidFileMatchesAFullRead(ParserFixture):
    """Invariant 3: one parser serves both scan paths. These pin the values the
    appended region yields, which is what drifted the last two times."""

    def test_claude_resumes_with_the_completing_records_values(self):
        partial = _claude("m-A", "2026-08-05T23:59:58Z", 100)
        complete = _claude("m-A", "2026-08-06T00:00:31Z", 900)
        path = self.write("sess.jsonl", [partial, complete])

        _m, full, _a, _l, _c = transcripts.parse_jsonl_file(path)
        _m, resumed, _a, _l, _c = transcripts.parse_jsonl_file(path, skip_lines=1)

        self.assertEqual(len(full), 1)
        self.assertEqual(len(resumed), 1)
        for field in ("timestamp", "output_tokens", "model", "stop_reason"):
            self.assertEqual(full[0][field], resumed[0][field], field)

    def test_claude_resume_keeps_the_first_non_empty_effort(self):
        first = json.loads(_claude("m-effort", "2026-08-05T10:00:00Z", 5))
        first["effort"] = "high"
        second = json.loads(_claude("m-effort", "2026-08-05T10:00:20Z", 20))
        second["effort"] = ""
        path = self.write("sess.jsonl", [json.dumps(first), json.dumps(second)])

        _m, full, _a, _l, _c = transcripts.parse_jsonl_file(path)
        _m, resumed, _a, _l, _c = transcripts.parse_jsonl_file(
            path, skip_lines=1)

        self.assertEqual(full[0]["reasoning_effort"], "high")
        self.assertEqual(resumed[0]["reasoning_effort"], "high")

        # The storage path sees the same invariant when the first record was
        # committed before the completing record arrived.
        self.scan()
        self.write("sess.jsonl", [json.dumps(first), json.dumps(second)],
                   mtime=2_000_000)
        self.scan()
        self.assertEqual(
            self.rows("SELECT reasoning_effort FROM turns"),
            [{"reasoning_effort": "high"}])

    def test_codex_resumes_with_the_model_and_effort_in_force(self):
        """`_context_in_force` re-reads the prefix for exactly this: the model
        and effort are established on records the appended region does not
        contain, and attributing the turn to neither is silent misattribution
        that only ever happens on the incremental path."""
        lines = [_codex_header("t-1"),
                 _codex_turn_context("gpt-5.6-sol", "high"),
                 _codex_token(1000, 10, "2026-08-05T10:00:00.000Z"),
                 _codex_token(2000, 20, "2026-08-05T10:01:00.000Z")]
        path = self.write("rollout-2026-08-05T10-00-00-019fcf22aaaa.jsonl", lines)

        _s, full, _a, _l, _c = codex_transcripts.parse_jsonl_file(path)
        _s, resumed, _a, _l, _c = codex_transcripts.parse_jsonl_file(path, skip_lines=3)

        self.assertEqual(len(resumed), 1)
        tail = [t for t in full if t["message_id"] == resumed[0]["message_id"]]
        self.assertEqual(len(tail), 1)
        for field in ("timestamp", "model", "reasoning_effort", "output_tokens",
                      "session_id", "source"):
            self.assertEqual(tail[0][field], resumed[0][field], field)

    def test_codex_resume_keeps_the_first_non_empty_effort_for_a_replayed_turn(self):
        lines = [_codex_header("t-effort"),
                 _codex_turn_context("gpt-5.6-sol", "high"),
                 _codex_token(1000, 10, "2026-08-05T10:00:00.000Z"),
                 _codex_turn_context("gpt-5.6-sol", "low",
                                     "2026-08-05T10:00:01.000Z"),
                 # Same cumulative response, later tally: the effort label
                 # must remain the first non-empty one on both parse paths.
                 _codex_token(1000, 20, "2026-08-05T10:00:02.000Z")]
        path = self.write("rollout-2026-08-05T10-00-00-019fcf22bbbb.jsonl",
                          lines)

        _s, full, _a, _l, _c = codex_transcripts.parse_jsonl_file(path)
        _s, resumed, _a, _l, _c = codex_transcripts.parse_jsonl_file(
            path, skip_lines=3)

        self.assertEqual(len(full), 1)
        self.assertEqual(len(resumed), 1)
        self.assertEqual(full[0]["output_tokens"], 20)
        self.assertEqual(full[0]["reasoning_effort"], "high")
        self.assertEqual(resumed[0]["output_tokens"], 20)
        self.assertEqual(resumed[0]["reasoning_effort"], "high")

    def test_a_failed_codex_context_read_refuses_to_stamp_blank_attribution(self):
        lines = [_codex_header("t-1"),
                 _codex_turn_context("gpt-5.6-sol", "high"),
                 _codex_token(1000, 10, "2026-08-05T10:00:00.000Z"),
                 _codex_token(2000, 20, "2026-08-05T10:01:00.000Z")]
        path = self.write(
            "rollout-2026-08-05T10-00-00-019fcf22aaaa.jsonl", lines)
        real_open = codex_transcripts._open_transcript
        attempts = []

        def fail_the_prefix_once(*args, **kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise OSError("transient prefix read")
            return real_open(*args, **kwargs)

        with mock.patch.object(codex_transcripts, "_open_transcript",
                               fail_the_prefix_once):
            with self.assertRaises(transcripts.TranscriptReadError):
                codex_transcripts.parse_jsonl_file(path, skip_lines=3)

    def test_the_two_scan_paths_agree_on_the_totals(self):
        """The end-to-end shape of invariant 3: appending to a file and
        rescanning must reach the same tokens as one read of the finished file.

        `turns.timestamp` and `tool_name` are reconciled too — see
        `TestStreamingSplitDoesNotMoveATurnsDay` below."""
        first = _claude("m-1", "2026-08-05T10:00:00Z", 10)
        second = _claude("m-2", "2026-08-05T10:05:00Z", 20)

        self.write("sess.jsonl", [first])
        self.scan()
        self.write("sess.jsonl", [first, second], mtime=2_000_000)
        self.scan()
        incremental = self.rows(
            "SELECT SUM(output_tokens) AS out, COUNT(*) AS n FROM turns")

        shutil.rmtree(self.db, ignore_errors=True)
        self.db.unlink(missing_ok=True)
        self.write("sess.jsonl", [first, second], mtime=3_000_000)
        self.scan()
        one_shot = self.rows(
            "SELECT SUM(output_tokens) AS out, COUNT(*) AS n FROM turns")

        self.assertEqual(incremental, one_shot)
        self.assertEqual(one_shot[0], {"out": 30, "n": 2})


class TestIdlessFinalLineRewrite(ParserFixture):
    def test_control_bytes_cannot_ambiguously_join_identity_fields(self):
        """Transcript strings may contain any proposed field separator."""
        first = transcripts._synthetic_message_id(
            1, "session\x1f2026", "timestamp", "model", "tool")
        second = transcripts._synthetic_message_id(
            1, "session", "2026\x1ftimestamp", "model", "tool")
        self.assertNotEqual(first, second)

    def test_rewriting_an_unterminated_idless_line_updates_one_row(self):
        path = self.write_raw(
            "sess.jsonl",
            _claude_no_id("2026-08-05T10:00:00Z", 10))
        self.scan()
        before = self.rows("SELECT message_id, output_tokens FROM turns")
        self.assertEqual(len(before), 1)
        self.assertTrue(before[0]["message_id"].startswith("claude-noid:1:"))
        self.assertEqual(before[0]["output_tokens"], 10)

        path.write_text(
            _claude_no_id("2026-08-05T10:00:00Z", 20), encoding="utf-8")
        os.utime(path, (2_000_000, 2_000_000))
        self.scan()
        after = self.rows("SELECT message_id, output_tokens FROM turns")
        self.assertEqual(after, [{"message_id": before[0]["message_id"],
                                  "output_tokens": 20}])


class TestSessionSourceIsolation(ParserFixture):
    def test_same_session_id_from_claude_and_codex_stays_two_sessions(self):
        self.write("claude.jsonl", [
            _claude("claude-message", "2026-08-05T10:00:00Z", 10,
                    session="shared-session")])
        self.write("rollout-2026-08-05T10-00-00-019fcf22aaaa.jsonl", [
            _codex_header("codex-thread", root="shared-session"),
            _codex_turn_context("gpt-5.6-sol", "high"),
            _codex_token(1000, 20, "2026-08-05T10:01:00.000Z")])

        self.scan()
        sessions = self.rows(
            "SELECT source, session_id, total_output_tokens, turn_count "
            "FROM sessions ORDER BY source")
        self.assertEqual(sessions, [
            {"source": "claude", "session_id": "shared-session",
             "total_output_tokens": 10, "turn_count": 1},
            {"source": "codex", "session_id": "shared-session",
             "total_output_tokens": 20, "turn_count": 1},
        ])
        turns = self.rows(
            "SELECT source, session_id FROM turns ORDER BY source")
        self.assertEqual(turns, [
            {"source": "claude", "session_id": "shared-session"},
            {"source": "codex", "session_id": "shared-session"},
        ])

    def test_blank_legacy_source_is_normalized_to_claude_before_reconcile(self):
        conn = scanner.get_db(self.db)
        try:
            scanner.init_db(conn, self.db)
            conn.execute(
                "INSERT INTO sessions (session_id, source) VALUES (?, '')",
                ("legacy",))
            conn.execute(
                "INSERT INTO turns (session_id, source, input_tokens) "
                "VALUES (?, '', 7)", ("legacy",))
            scanner._normalize_stored_sources(conn)
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT source FROM sessions").fetchone()[0],
                "claude")
            self.assertEqual(
                conn.execute("SELECT source FROM turns").fetchone()[0],
                "claude")
        finally:
            conn.close()


class TestNeutralTimestampOrdering(unittest.TestCase):
    def test_valid_offsets_and_naive_iso_forms_are_compared_as_utc(self):
        self.assertLess(
            timestamps.timestamp_compare(
                "2026-08-01T01:00:00+02:00", "2026-08-01T00:30:00Z"), 0)
        self.assertEqual(
            timestamps.timestamp_min(
                "2026-08-01T01:00:00+02:00", "2026-08-01T00:30:00Z"),
            "2026-08-01T01:00:00+02:00")
        self.assertEqual(
            timestamps.timestamp_max(
                "2026-08-01T00:30:00", "2026-08-01T00:30:00+00:00"),
            "2026-08-01T00:30:00+00:00")

    def test_malformed_and_empty_values_have_a_deterministic_fallback(self):
        self.assertLess(timestamps.timestamp_compare("", "not-a-date"), 0)
        self.assertGreater(
            timestamps.timestamp_compare("z-malformed", "a-malformed"), 0)
        self.assertEqual(timestamps.timestamp_min("", "2026-01-01"),
                         "2026-01-01")
        self.assertEqual(timestamps.timestamp_max("2026-01-01", ""),
                         "2026-01-01")


class TestParserSelectionFailsClosed(ParserFixture):
    def test_a_failed_codex_sniff_is_retried_instead_of_stamped_as_claude(self):
        path = self.write(
            "rollout-2026-08-05T10-00-00-019fcf22aaaa.jsonl",
            [_codex_header("t-1"), _codex_turn_context(),
             _codex_token(1000, 10, "2026-08-05T10:00:00.000Z")])
        real_open = codex_transcripts._open_transcript
        attempts = []

        def fail_the_sniff_once(*args, **kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise OSError("transient sniff read")
            return real_open(*args, **kwargs)

        with mock.patch.object(codex_transcripts, "_open_transcript",
                               fail_the_sniff_once):
            first = self.scan()
        self.assertEqual(first["skipped"], 1)
        self.assertEqual(self.rows("SELECT * FROM processed_files"), [])
        self.assertEqual(self.rows("SELECT * FROM turns"), [])

        self.scan()
        self.assertEqual(len(self.rows("SELECT * FROM turns")), 1)
        self.assertTrue(path.exists())


class TestSessionMetadataIsScanOrderIndependent(ParserFixture):
    def test_repeated_codex_headers_cannot_move_the_root_or_project(self):
        first = _codex_rec(
            "session_meta",
            {"id": "t-1", "session_id": "root-a", "cwd": "/home/u/first"},
            "2026-08-05T09:59:00.000Z")
        second = _codex_rec(
            "session_meta",
            {"id": "t-1", "session_id": "root-b", "cwd": "/home/u/second"},
            "2026-08-05T10:00:30.000Z")
        path = self.write(
            "rollout-2026-08-05T10-00-00-019fcf22aaaa.jsonl",
            [first, _codex_turn_context(),
             _codex_token(1000, 10, "2026-08-05T10:00:00.000Z"),
             second,
             _codex_token(2000, 20, "2026-08-05T10:01:00.000Z")])

        for skip_lines in (0, 3):
            with self.subTest(skip_lines=skip_lines):
                sessions, turns, _agents, _limits, _lines = (
                    codex_transcripts.parse_jsonl_file(
                        path, skip_lines=skip_lines))
                self.assertEqual(
                    {turn["session_id"] for turn in turns}, {"root-a"})
                self.assertEqual(
                    [session["session_id"] for session in sessions],
                    ["root-a"])
                self.assertEqual(sessions[0]["project_name"], "u/first")

    def test_a_claude_title_before_content_keeps_title_and_fills_project(self):
        title = _claude_title("custom-title", "Release day")
        turn = _claude("m-1", "2026-08-05T10:00:00Z", 10)
        self.write("sess.jsonl", [title])
        self.scan()
        self.assertEqual(self.rows("SELECT * FROM sessions"), [])

        self.write("sess.jsonl", [title, turn], mtime=2_000_000)
        self.scan()
        rows = self.rows(
            "SELECT project_name, topic FROM sessions WHERE session_id = 's-1'")
        self.assertEqual(rows, [{"project_name": "u/proj",
                                 "topic": "Release day"}])


class TestIncrementalResumeProvesItsPrefix(ParserFixture):
    """A stored line cursor is reusable only while its bytes are unchanged."""

    def test_a_same_mtime_append_is_found_by_its_size(self):
        first = _claude("m-1", "2026-08-05T10:00:00Z", 10)
        second = _claude("m-2", "2026-08-05T10:01:00Z", 20)
        path = self.write("sess.jsonl", [first])
        self.scan()

        path.write_text(f"{first}\n{second}\n", encoding="utf-8")
        os.utime(path, (1_000_000, 1_000_000))
        result = self.scan()

        self.assertEqual(result["updated"], 1)
        self.assertEqual(
            {row["message_id"] for row in
             self.rows("SELECT message_id FROM turns")},
            {"m-1", "m-2"})
        stamp = self.rows(
            "SELECT mtime, lines, size, prefix_hash FROM processed_files")[0]
        self.assertEqual(stamp["mtime"], 1_000_000)
        self.assertEqual(stamp["lines"], 2)
        self.assertEqual(stamp["size"], path.stat().st_size)
        self.assertRegex(stamp["prefix_hash"], r"^[0-9a-f]{64}$")

    def test_an_insert_before_the_cursor_forces_a_full_parse(self):
        original = [
            _claude("m-a", "2026-08-05T10:00:00Z", 10),
            _claude("m-b", "2026-08-05T10:01:00Z", 20),
            _claude("m-c", "2026-08-05T10:02:00Z", 30),
        ]
        self.write("sess.jsonl", original)
        self.scan()

        inserted = _claude("m-x", "2026-08-05T09:59:00Z", 5)
        self.write("sess.jsonl", [inserted, *original], mtime=2_000_000)
        self.scan()

        self.assertEqual(
            {row["message_id"] for row in
             self.rows("SELECT message_id FROM turns")},
            {"m-x", "m-a", "m-b", "m-c"})
        self.assertEqual(
            self.rows("SELECT lines FROM processed_files"), [{"lines": 4}])

    def test_a_same_line_count_replacement_is_not_skipped(self):
        first = _claude("m-a", "2026-08-05T10:00:00Z", 10)
        replaced = _claude("m-b", "2026-08-05T10:01:00Z", 20)
        last = _claude("m-c", "2026-08-05T10:02:00Z", 30)
        self.write("sess.jsonl", [first, replaced, last])
        self.scan()

        replacement = _claude("m-x", "2026-08-05T10:01:00Z", 25)
        self.write("sess.jsonl", [first, replacement, last], mtime=2_000_000)
        self.scan()

        self.assertEqual(
            {row["message_id"] for row in
             self.rows("SELECT message_id FROM turns")},
            {"m-a", "m-b", "m-c", "m-x"})
        self.assertEqual(
            self.rows("SELECT lines FROM processed_files"), [{"lines": 3}])

    def test_an_atomic_same_metadata_replacement_is_not_skipped(self):
        """A replacement between scans must invalidate the stored cursor.

        The new file deliberately has the old file's size and mtime. A warm
        scan therefore needs the persisted device/inode identity; checking the
        identity only around a parse cannot protect this between-invocations
        replacement.
        """
        original = _claude("m-a", "2026-08-05T10:00:00Z", 10)
        replacement = _claude("m-b", "2026-08-05T10:00:00Z", 20)
        path = self.write("sess.jsonl", [original], mtime=1_000_000)
        self.scan()
        before = path.stat()

        temporary = path.with_name("sess.jsonl.replacement")
        temporary.write_text(replacement + "\n", encoding="utf-8")
        os.utime(temporary, (before.st_mtime, before.st_mtime))
        os.replace(temporary, path)
        after = path.stat()
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_mtime, before.st_mtime)
        self.assertNotEqual((after.st_dev, after.st_ino),
                            (before.st_dev, before.st_ino))

        self.scan()

        self.assertEqual(
            {row["message_id"] for row in
             self.rows("SELECT message_id FROM turns")},
            {"m-a", "m-b"})

    def test_a_shorter_replacement_resets_the_cursor(self):
        original = [
            _claude("m-a", "2026-08-05T10:00:00Z", 10),
            _claude("m-b", "2026-08-05T10:01:00Z", 20),
            _claude("m-c", "2026-08-05T10:02:00Z", 30),
        ]
        self.write("sess.jsonl", original)
        self.scan()

        replacement = _claude("m-x", "2026-08-05T10:03:00Z", 5)
        self.write("sess.jsonl", [replacement], mtime=2_000_000)
        self.scan()

        self.assertIn(
            "m-x",
            {row["message_id"] for row in
             self.rows("SELECT message_id FROM turns")})
        self.assertEqual(
            self.rows("SELECT lines FROM processed_files"), [{"lines": 1}])


def _streaming_pair(message_id, started, finished, grows=True):
    """The two records one API response writes: a partial, then the completing
    one. Same message_id, cumulative tally, and — the part that matters here —
    different timestamps.

    With grows=False the first record already has the final tally. Equal
    cumulative usage still needs correct completion metadata."""
    return (_claude(message_id, started, 5 if grows else 99),
            _claude(message_id, finished, 99))


class TestStreamingSplitDoesNotMoveATurnsDay(ParserFixture):
    """A response streams as several records sharing one `message.id`, and only
    the last carries the final tally. A full read keeps that last record, so the
    turn is stamped with the moment the response COMPLETED. An incremental scan
    that landed between the two had already stored the first record, and the
    upsert's "first writer wins" rule then pinned the turn to the moment it
    STARTED.

    That is not cosmetic. The local-day bucket is a function of this column
    (invariant 4), so a response streaming across local midnight was filed on a
    different day depending only on whether the scanner happened to run while it
    was in flight — the same transcript, two answers.

    `insert_turns` now takes the timestamp and tool_name of whichever row is the
    more complete one. The two rows that collide on this index are different
    cases and the predicate separates them — but NOT by the tally alone, as its
    first version assumed: a later streaming record's cumulative tally is
    routinely identical to the earlier one's. What actually distinguishes the
    same response seen in a second transcript, which must stay a no-op
    (invariant 1), is that it disagrees about who produced the turn."""

    def _run(self, split, grows=True):
        started, finished = "2026-08-05T10:00:00Z", "2026-08-05T10:00:20Z"
        first, second = _streaming_pair("m-stream", started, finished, grows=grows)
        if split:
            self.write("sess.jsonl", [first])
            self.scan()
            self.write("sess.jsonl", [first, second], mtime=2_000_000)
            self.scan()
        else:
            self.write("sess.jsonl", [first, second], mtime=2_000_000)
            self.scan()
        return self.rows("SELECT timestamp, output_tokens FROM turns")

    def test_both_paths_stamp_the_turn_with_the_completing_record(self):
        incremental = self._run(split=True)
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.setUp()
        one_shot = self._run(split=False)
        self.assertEqual(incremental, one_shot)
        self.assertEqual(one_shot, [{"timestamp": "2026-08-05T10:00:20Z",
                                     "output_tokens": 99}])

    def test_the_last_record_wins_even_when_the_tally_stopped_growing(self):
        """The predicate's blind spot, and the reason it needs a tie-break.

        Completion can repeat an earlier cumulative tally. Incremental and
        full scans must select the same final timestamp without borrowing
        metadata from a different producer."""
        incremental = self._run(split=True, grows=False)
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.setUp()
        one_shot = self._run(split=False, grows=False)
        self.assertEqual(incremental, one_shot)
        self.assertEqual(one_shot, [{"timestamp": "2026-08-05T10:00:20Z",
                                     "output_tokens": 99}])

    def _dup(self, first, second):
        """Store two records that collide on `message_id`, in that order."""
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            scanner.init_db(conn)
            scanner.insert_turns(conn, [first])
            scanner.insert_turns(conn, [second])
            conn.commit()
            return [dict(r) for r in conn.execute(
                "SELECT timestamp, tool_name, model, is_subagent, agent_id "
                "FROM turns WHERE message_id = 'dup'")]
        finally:
            conn.close()

    #: Everything two colliding records share when they are one response's two
    #: streaming records rather than one response seen in two transcripts.
    _DUP = dict(session_id="s", model="claude-opus-5", input_tokens=1,
                output_tokens=2, cache_read_tokens=0, cache_creation_tokens=0,
                cache_creation_1h_tokens=0, cwd=None, message_id="dup",
                is_subagent=0, agent_id=None, reasoning_effort="high",
                stop_reason="end_turn")

    def test_an_identical_duplicate_never_moves_the_attribution(self):
        """Invariant 1's half of the bargain: a record identical in every column
        the merge reads must be a complete no-op."""
        stored = self._dup(
            dict(self._DUP, timestamp="2026-01-01T00:00:00Z", tool_name="First"),
            dict(self._DUP, timestamp="2026-01-01T00:00:00Z", tool_name="Second"))
        self.assertEqual([(r["timestamp"], r["tool_name"]) for r in stored],
                         [("2026-01-01T00:00:00Z", "First")])

    def test_the_cross_transcript_duplicate_still_keeps_the_first_attribution(self):
        """The case the equal-tally rule exists to protect, stated by what makes
        it that case: the same response found in a subagent's file and its
        parent's disagrees about `is_subagent`, `agent_id` and the model. Those
        columns never move on conflict, so a record disagreeing on any of them
        must not donate its timestamp either — the merged row would otherwise
        carry one writer's clock beside the other's attribution."""
        stored = self._dup(
            dict(self._DUP, timestamp="2026-01-01T00:00:00Z", tool_name="First"),
            dict(self._DUP, timestamp="2026-06-06T06:06:06Z", tool_name="Second",
                 model="claude-haiku-4-5", is_subagent=1, agent_id="agent-9"))
        self.assertEqual(stored, [{"timestamp": "2026-01-01T00:00:00Z",
                                   "tool_name": "First",
                                   "model": "claude-opus-5",
                                   "is_subagent": 0, "agent_id": None}])

    def test_which_of_two_tied_records_arrives_first_decides_nothing(self):
        """What the no-op rule was really protecting — the original docstring's
        own words, 'which one won would depend on directory-walk order'. First
        writer wins is order-dependent by construction; it only looked stable
        because the test inserted in one fixed order."""
        early = dict(self._DUP, timestamp="2026-01-01T00:00:00Z", tool_name="First")
        late = dict(self._DUP, timestamp="2026-06-06T06:06:06Z", tool_name="Second")
        forwards = self._dup(early, late)
        self.db.unlink()
        backwards = self._dup(late, early)
        self.assertEqual(forwards, backwards)
        self.assertEqual([(r["timestamp"], r["tool_name"]) for r in forwards],
                         [("2026-06-06T06:06:06Z", "Second")])

    # Each fixture changes one producer-attribution conjunct while keeping the
    # cumulative tally equal. This proves timestamp replacement is allowed
    # only when producer identity agrees.
    _ONE_COLUMN_APART = {
        "session_id": ({}, {"session_id": "s-other"}),
        "model": ({}, {"model": "claude-haiku-4-5"}),
        "is_subagent": ({}, {"is_subagent": 1}),
        "agent_id": ({"is_subagent": 1, "agent_id": "agent-child"},
                     {"agent_id": "agent-grandchild"}),
    }

    def test_the_conjuncts_covered_here_are_the_ones_the_predicate_has(self):
        """Read the column list off `_MORE_COMPLETE` rather than trusting the
        map above to have kept up with it.

        Both directions matter. A conjunct DELETED from the predicate would
        otherwise drop silently out of the sub-test below — the loop iterates
        the map, so a list derived from the predicate alone would simply stop
        testing whatever was removed. A conjunct ADDED gets no coverage until
        someone writes the pair of values that isolates it, and this is the
        failure that asks for them."""
        found = set(re.findall(r"excluded\.(\w+)\s+IS\s+turns\.\1",
                               scanner._MORE_COMPLETE))
        self.assertTrue(found, "no `excluded.<col> IS turns.<col>` conjunct in "
                               "_MORE_COMPLETE — has the predicate moved?")
        self.assertEqual(found, set(self._ONE_COLUMN_APART),
                         "the attribution conjuncts changed; give each new one a "
                         "(shared, differs) pair in _ONE_COLUMN_APART, and do not "
                         "drop one merely because the suite stayed green without it")

    def test_each_attribution_conjunct_alone_keeps_the_first_writers_clock(self):
        """Every conjunct on its own, because collectively they are already
        pinned and individually none of them was.

        Equal-tally collisions still need metadata comparison so replayed
        records cannot silently replace the producing thread's timestamp.

        The synthetic fixture varies agent_id independently from session_id,
        model and is_subagent to exercise each collision discriminator.

        **Those three zeroes are not a licence to delete the conjuncts.** They
        are zero because of what today's two parsers happen to emit: Codex keys
        its synthetic message id on the root session, so two colliding rows
        agree on `session_id` by construction, and it sets
        `agent_id = own_thread if is_subagent else None`, so an `is_subagent`
        disagreement always drags `agent_id` with it. Neither is an invariant —
        `--projects-dir` is documented as "also look here" and may be pointed at
        another machine's history — and AGENTS.md invariant 1 names all four as
        the mechanism that keeps the cross-transcript collision a no-op."""
        for column, (shared, differs) in self._ONE_COLUMN_APART.items():
            with self.subTest(conjunct=column):
                self.db.unlink(missing_ok=True)
                first = dict(self._DUP, **shared,
                             timestamp="2026-01-01T23:50:00Z", tool_name="First")
                second = dict(first, **differs,
                              timestamp="2026-01-02T19:50:00Z", tool_name="Second")
                stored = self._dup(first, second)
                self.assertEqual(
                    [(r["timestamp"], r["tool_name"]) for r in stored],
                    [("2026-01-01T23:50:00Z", "First")],
                    f"a record disagreeing only about `{column}` donated its "
                    f"clock, so the row now carries one writer's timestamp "
                    f"beside the other's attribution")

    def test_a_record_stamped_earlier_never_donates_its_clock(self):
        """The fifth conjunct, `excluded.timestamp > turns.timestamp`, named on
        its own.

        Two records that agree about everything — one response, one writer,
        the tally already final — but arrive newest first. Without the guard the
        equal-tally branch fires on both orders and the stored clock follows
        whichever record the directory walk handed over last.
        (`test_which_of_two_tied_records_arrives_first_decides_nothing` above
        catches the same mutation from the other side, by comparing the two
        insertion orders; this one states what the conjunct itself says.)"""
        late = dict(self._DUP, timestamp="2026-06-06T06:06:06Z", tool_name="First")
        early = dict(self._DUP, timestamp="2026-01-01T00:00:00Z", tool_name="Second")
        stored = self._dup(late, early)
        self.assertEqual([(r["timestamp"], r["tool_name"]) for r in stored],
                         [("2026-06-06T06:06:06Z", "First")])

    def test_equal_tally_offsets_are_compared_by_instant(self):
        earlier = dict(self._DUP,
                       timestamp="2026-08-01T01:00:00+02:00",
                       tool_name="Earlier")
        later = dict(self._DUP,
                     timestamp="2026-08-01T00:30:00Z",
                     tool_name="Later")
        stored = self._dup(earlier, later)
        self.assertEqual(stored, [{"timestamp": "2026-08-01T00:30:00Z",
                                   "tool_name": "Later",
                                   "model": "claude-opus-5",
                                   "is_subagent": 0,
                                   "agent_id": None}])


def _codex_meta(thread, root, parent=None, branch="main",
                ts="2026-08-05T09:00:00.000Z", agent_other="guardian"):
    """A rollout's own header. `parent` set makes it a subagent's.

    Both subagent markers are written because the parser accepts either
    (`thread_source`, or a `source.subagent` object) and a real rollout carries
    both. `agent_id` is not in the file at all: the parser derives it as
    `own_thread if is_subagent else None`, which is exactly why two rollouts of
    one lineage can differ in that column and nothing else."""
    payload = {"id": thread, "session_id": root, "cwd": "/home/u/proj",
               "git": {"branch": branch}}
    if parent is not None:
        payload["thread_source"] = "subagent"
        payload["source"] = {"subagent": {
            "other": agent_other,
            "thread_spawn": {"parent_thread_id": parent,
                             "agent_nickname": "reviewer"}}}
    return _codex_rec("session_meta", payload, ts)


def _codex_response(cumulative, inp, out, ts):
    """One API response. `cumulative` is the per-LINEAGE running total that
    doubles as the response's identity (`codex:<root session>:<cumulative>`),
    which is what makes a replayed copy collide with the original."""
    return _codex_rec("event_msg", {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": inp, "cached_input_tokens": 0,
                             "cache_write_input_tokens": 0, "output_tokens": out,
                             "reasoning_output_tokens": 0,
                             "total_tokens": inp + out},
        "total_token_usage": {"total_tokens": cumulative},
    }}, ts)


class TestAGrandchildReplayKeepsTheSubagentsClock(ParserFixture):
    """The one collision on which `agent_id` is the only thing standing between
    a turn and the wrong clock, driven end to end rather than by hand.

    A child rollout can replay ancestor usage with the child timestamp. Keep
    producer identity and time when discovering the child before the parent.

    The first assertion is about the fixture rather than the code: it proves the
    two parsed copies really are one column apart, so a later parser change that
    made them differ in `model` or `is_subagent` too would fail here instead of
    quietly turning this back into the case that was already covered."""

    ROOT = "019fce00-0000-7000-8000-000000000001"
    CHILD = "019fce00-0001-7000-8000-000000000002"
    GRANDCHILD = "019fce00-0002-7000-8000-000000000003"

    #: The child's own first response, continuing the lineage accumulator
    #: (1000 + 450 + 50). The response both the child and the grandchild write.
    REPLAYED = "codex:019fce00-0000-7000-8000-000000000001:1500"
    #: The root thread's first response, replayed by both descendants — the
    #: already-covered shape, kept here as the contrast.
    ROOT_RESPONSE = "codex:019fce00-0000-7000-8000-000000000001:1000"

    CHILD_SPAWNED = "2026-08-05T23:50:00.000Z"
    CHILD_OWN = "2026-08-05T23:50:30.000Z"
    GRANDCHILD_SPAWNED = "2026-08-06T19:50:00.000Z"

    def _write_lineage(self):
        """Three rollouts, named so that discovery order is spawn order — which
        is the property `test_codex_discovery_order` pins and this one assumes."""
        self.write("codex-a-root.jsonl", [
            _codex_meta(self.ROOT, self.ROOT),
            _codex_turn_context(ts="2026-08-05T09:00:01.000Z"),
            _codex_response(1000, 900, 100, "2026-08-05T09:00:02.000Z"),
        ])
        self.write("codex-b-child.jsonl", [
            _codex_meta(self.CHILD, self.ROOT, parent=self.ROOT,
                        ts=self.CHILD_SPAWNED),
            _codex_turn_context(ts=self.CHILD_SPAWNED),
            _codex_response(1000, 900, 100, self.CHILD_SPAWNED),
            _codex_response(1500, 450, 50, self.CHILD_OWN),
        ])
        self.write("codex-c-grandchild.jsonl", [
            _codex_meta(self.GRANDCHILD, self.ROOT, parent=self.CHILD,
                        ts=self.GRANDCHILD_SPAWNED),
            _codex_turn_context(ts=self.GRANDCHILD_SPAWNED),
            _codex_response(1000, 900, 100, self.GRANDCHILD_SPAWNED),
            _codex_response(1500, 450, 50, self.GRANDCHILD_SPAWNED),
            _codex_response(2000, 450, 50, "2026-08-06T19:50:30.000Z"),
        ])

    def _stored(self, message_id):
        rows = self.rows(
            "SELECT timestamp, session_id, model, is_subagent, agent_id "
            f"FROM turns WHERE message_id = '{message_id}'")
        self.assertEqual(len(rows), 1, "the response was stored more than once")
        return rows[0]

    def test_the_two_copies_disagree_about_agent_id_and_nothing_else(self):
        """The fixture's own claim, checked against the real parser."""
        self._write_lineage()
        copies = {}
        for name in ("codex-b-child.jsonl", "codex-c-grandchild.jsonl"):
            turns = scanner.parse_transcript(self.projects / "proj" / name)[1]
            copies[name] = next(t for t in turns
                                if t["message_id"] == self.REPLAYED)
        child = copies["codex-b-child.jsonl"]
        grandchild = copies["codex-c-grandchild.jsonl"]
        differ = {column for column in
                  ("session_id", "model", "is_subagent", "agent_id")
                  if child[column] != grandchild[column]}
        self.assertEqual(differ, {"agent_id"})
        self.assertEqual((child["agent_id"], grandchild["agent_id"]),
                         (self.CHILD, self.GRANDCHILD))
        self.assertGreater(grandchild["timestamp"], child["timestamp"])
        self.assertEqual(
            (child["input_tokens"], child["output_tokens"]),
            (grandchild["input_tokens"], grandchild["output_tokens"]),
            "the replay must tie on the tally, or the merge never reaches the "
            "clause under test")

    def test_the_grandchilds_replay_does_not_re_stamp_the_childs_response(self):
        self._write_lineage()
        self.scan()
        self.assertEqual(self._stored(self.REPLAYED), {
            "timestamp": self.CHILD_OWN, "session_id": self.ROOT,
            "model": "gpt-5.6-sol", "is_subagent": 1, "agent_id": self.CHILD})

    def test_the_root_threads_response_still_belongs_to_the_root_thread(self):
        """The contrast, and the reason the grandchild had to be added: on this
        collision `is_subagent` and `agent_id` disagree together, so any one of
        them holds the predicate false on its own."""
        self._write_lineage()
        self.scan()
        self.assertEqual(self._stored(self.ROOT_RESPONSE), {
            "timestamp": "2026-08-05T09:00:02.000Z", "session_id": self.ROOT,
            "model": "gpt-5.6-sol", "is_subagent": 0, "agent_id": None})

    def test_the_lineage_stores_each_response_once(self):
        """Invariant 1 on the same fixture: three rollouts carrying six
        `token_count` records hold three distinct API responses between them."""
        self._write_lineage()
        self.scan()
        self.assertEqual(
            self.rows("SELECT COUNT(*) AS n, SUM(input_tokens) AS inp, "
                      "SUM(output_tokens) AS out FROM turns"),
            [{"n": 3, "inp": 900 + 450 + 450, "out": 100 + 50 + 50}])


class TestAReplayedAncestorHeaderDoesNotRedefineTheStoredRows(ParserFixture):
    """The same skip as `TestAReplayedAncestorHeaderDoesNotRedefineTheThread`
    in `test_codex_transcripts`, driven end to end into SQLite.

    A rollout carries its own `session_meta` first and then an echo of every
    ancestor's, and `codex_transcripts` ignores the later ones. Nothing in this
    suite wrote two headers of DIFFERENT threads into one rollout before, so the
    skip's whole non-trivial branch was unreached: replacing
    `if declared != own_thread:` with `if False:` left the suite green.

    Two stored consequences are pinned here rather than at the parser: the
    dispatch row the Dispatches table reads, and the identity every
    `turns.message_id` in the file is built from. The parser-level class carries
    the corpus measurements behind both."""

    ROOT = "019fce00-0000-7000-8000-000000000001"
    CHILD = "019fce00-0001-7000-8000-000000000002"
    PARENT = "019fce00-0003-7000-8000-000000000004"
    INTRUDER = "019fce00-0009-7000-8000-000000000009"

    def test_the_stored_dispatch_keeps_the_subagents_own_label(self):
        """A sub-subagent replays the header of the subagent above it. Its own
        agent path is the leaf; the ancestor's is that path's parent, so letting
        the echo through files the leaf's tokens under the wrong dispatch."""
        self.write("codex-leaf.jsonl", [
            _codex_meta(self.CHILD, self.ROOT, parent=self.PARENT,
                        agent_other="/root/parent/leaf",
                        ts="2026-08-05T10:00:00.000Z"),
            _codex_turn_context(ts="2026-08-05T10:00:00.001Z"),
            _codex_response(1000, 900, 100, "2026-08-05T10:00:00.002Z"),
            _codex_meta(self.PARENT, self.ROOT, parent=self.ROOT,
                        agent_other="/root/parent",
                        ts="2026-08-05T10:00:00.003Z"),
            _codex_response(1500, 450, 50, "2026-08-05T10:00:30.000Z"),
        ])
        self.scan()
        self.assertEqual(
            self.rows("SELECT agent_id, agent_type, dispatched_in_session "
                      "FROM agents"),
            [{"agent_id": self.CHILD, "agent_type": "/root/parent/leaf",
              "dispatched_in_session": self.ROOT}])

    def test_every_stored_turn_keeps_the_first_headers_root(self):
        """The identity is established by the first header and never moves.

        Contradictory later headers must not change the lineage key already
        used for stored turns. Re-keying would insert a duplicate response."""
        self.write("codex-intruded.jsonl", [
            _codex_meta(self.CHILD, self.ROOT, ts="2026-08-05T10:00:00.000Z"),
            _codex_turn_context(ts="2026-08-05T10:00:00.001Z"),
            _codex_response(1000, 900, 100, "2026-08-05T10:00:00.002Z"),
            _codex_meta(self.INTRUDER, self.INTRUDER,
                        ts="2026-08-05T10:00:00.003Z"),
            _codex_response(1500, 450, 50, "2026-08-05T10:00:30.000Z"),
        ])
        self.scan()
        self.assertEqual(
            [r["message_id"] for r in
             self.rows("SELECT message_id FROM turns ORDER BY message_id")],
            [f"codex:{self.ROOT}:1000", f"codex:{self.ROOT}:1500"],
            "a later header re-keyed the responses that followed it")
        self.assertEqual(
            self.rows("SELECT session_id, turn_count FROM sessions"),
            [{"session_id": self.ROOT, "turn_count": 2}])


class TestARereadNeverCountsATurnTwice(ParserFixture):
    """Re-reading bytes that are already stored must change nothing.

    A rebuilt database has an empty `processed_files`, so the very next scan
    re-reads every transcript on disk and re-inserts every turn the corpus
    contains. That whole-corpus re-read rests on one claim: `insert_turns` is
    idempotent.

    It used to be reached a different way — `scan()` cleared `processed_files`
    whenever a one-time backfill marker was missing from `schema_meta`, so an
    upgrade forced the same pass. Those markers are gone (AGENTS.md invariant
    6), and the re-read they forced is now what a schema rebuild produces for
    free. The claim under test did not move with them, and neither did what
    breaks it.

    It is idempotent only for turns the conditional unique index covers, i.e.
    those with a non-empty `message_id`. A turn parsed without one falls outside
    the source-qualified non-empty message-id conflict, so the upsert never fires
    and the INSERT lands a second row — and then a third, growing linearly with
    the number of re-reads. The end-of-scan reconciliation recomputes `sessions`
    from the duplicated `turns`, so every total, every chart and the CSV export
    agree on the inflated figure and nothing anywhere reports a problem.

    Claude Code does not currently write id-less records, so this is a latent
    hole rather than a live over-count; the parser keeps the shape on purpose
    (`turns_no_id`) and foreign roots are a documented input, so the property is
    pinned here rather than left to the corpus's good behaviour.
    """

    def _force_a_full_re_read(self):
        """Rewind the incremental bookkeeping so the next scan re-reads everything.

        This used to delete a `schema_meta` backfill marker, which was the
        release-upgrade path that produced a full re-read. There are no markers
        and no `schema_meta` any more, so it rewinds `processed_files` directly —
        the *same* two-column rewind the markers performed, and still a real
        state rather than a synthetic knob: it is what a rebuilt database looks
        like to the very next scan, and what any transcript whose mtime moved
        looks like to the scan after it.
        """
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE processed_files SET mtime = -1.0, lines = 0")
            conn.commit()
        finally:
            conn.close()
        return self.scan()

    def _totals(self):
        turns = self.rows("SELECT COUNT(*) AS n, SUM(input_tokens) AS inp,"
                          " SUM(output_tokens) AS out FROM turns")
        sessions = self.rows("SELECT turn_count, total_input_tokens,"
                             " total_output_tokens FROM sessions ORDER BY session_id")
        return turns, sessions

    def test_a_forced_re_read_does_not_duplicate_turns_without_a_message_id(self):
        self.write("sess.jsonl", [
            _claude("m-1", "2026-08-05T10:00:00Z", 10),
            _claude_no_id("2026-08-05T10:01:00Z", 20),
            _claude_no_id("2026-08-05T10:02:00Z", 30),
        ])
        self.scan()
        before = self._totals()
        self.assertEqual(before[0], [{"n": 3, "inp": 30, "out": 60}],
                         "guard the fixture: all three records must be stored")

        result = self._force_a_full_re_read()
        self.assertEqual(result["updated"], 1, "the re-read must actually happen")
        self.assertEqual(self._totals(), before)

    def test_the_growth_is_linear_in_the_number_of_re_reads(self):
        """One duplicate would be a bug; unbounded growth is what makes it
        unrecoverable — nothing ever removes the extra rows."""
        self.write("sess.jsonl", [_claude_no_id("2026-08-05T10:01:00Z", 20)])
        self.scan()
        before = self._totals()
        self._force_a_full_re_read()
        self._force_a_full_re_read()
        self.assertEqual(self._totals(), before)

    def test_a_transcript_present_twice_costs_the_same_as_once(self):
        """The second route to the same bytes, and it needs no upgrade: one
        `--projects-dir` copy of a history that is also under `~/.claude`. Two
        distinct paths are two `processed_files` keys, so both are parsed inside
        a SINGLE scan.

        Turns that carry a `message.id` already collapse here — that is the
        documented cross-transcript case insert_turns' MAX() rule exists to make
        a no-op. Id-less ones must reach the same answer rather than a different
        one."""
        lines = [_claude("m-1", "2026-08-05T10:00:00Z", 10),
                 _claude_no_id("2026-08-05T10:01:00Z", 20)]
        self.write("sess.jsonl", lines)
        self.write("sess-copied-from-the-other-machine.jsonl", lines)
        self.scan()
        self.assertEqual(
            self.rows("SELECT COUNT(*) AS n, SUM(output_tokens) AS out FROM turns"),
            [{"n": 2, "out": 30}])

    def test_two_distinct_id_less_turns_are_never_collapsed_into_one(self):
        """The other side of the bargain. Whatever key an id-less turn is given
        must separate genuinely different records — an under-count is not a
        better failure than the over-count it replaces."""
        self.write("sess.jsonl", [
            # Same session, same model, same timestamp, same tallies: everything
            # a content-derived key can see is identical.
            _claude_no_id("2026-08-05T10:00:00Z", 20),
            _claude_no_id("2026-08-05T10:00:00Z", 20),
        ])
        self.scan()
        self.assertEqual(
            self.rows("SELECT COUNT(*) AS n, SUM(output_tokens) AS out FROM turns"),
            [{"n": 2, "out": 40}])

    def test_a_synthesised_key_cannot_be_mistaken_for_a_real_one(self):
        """It has to be distinguishable in the stored data: `message_id` is what
        the dedup contract is written in, and a synthetic value that looked like
        Anthropic's would make the column impossible to reason about."""
        path = self.write("sess.jsonl", [
            _claude("msg_01real", "2026-08-05T10:00:00Z", 10),
            _claude_no_id("2026-08-05T10:01:00Z", 20),
        ])
        _m, turns, _a, _l, _c = transcripts.parse_jsonl_file(path)
        ids = sorted(t["message_id"] for t in turns)
        self.assertEqual(ids[1], "msg_01real")
        self.assertTrue(ids[0].startswith("claude-noid:"), ids[0])

    def test_the_parser_gives_the_same_bytes_the_same_key_every_time(self):
        """The property the re-read rests on, at the parser level: two reads of
        one unchanged file must agree, including across the incremental path
        (`skip_lines`), whose line numbering is absolute for exactly this."""
        path = self.write("sess.jsonl", [
            _claude("m-1", "2026-08-05T10:00:00Z", 10),
            _claude_no_id("2026-08-05T10:01:00Z", 20),
            _claude_no_id("2026-08-05T10:02:00Z", 30),
        ])
        _m, first, _a, _l, _c = transcripts.parse_jsonl_file(path)
        _m, again, _a, _l, _c = transcripts.parse_jsonl_file(path)
        _m, tail, _a, _l, _c = transcripts.parse_jsonl_file(path, skip_lines=2)

        self.assertEqual(sorted(t["message_id"] for t in first),
                         sorted(t["message_id"] for t in again))
        self.assertEqual(len({t["message_id"] for t in first}), 3,
                         "three records, three keys")
        self.assertEqual(len(tail), 1)
        self.assertIn(tail[0]["message_id"], {t["message_id"] for t in first})


class TornTailFixture(ParserFixture):
    """Bytes as a transcript has them while its writer is mid-record."""

    #: How much of the torn record made it to disk. Any prefix that cannot be
    #: decoded works; 40 characters is comfortably inside every fixture here.
    TORN_PREFIX = 40

    def torn(self, lines, complete):
        """`complete` whole lines, then the leading fragment of the next one."""
        return ("\n".join(lines[:complete]) + "\n"
                + lines[complete][:self.TORN_PREFIX])

    def turns(self):
        return self.rows("SELECT message_id, output_tokens, stop_reason"
                         " FROM turns ORDER BY message_id")

    def two_scans(self, name, torn, final):
        """Scan the torn bytes, let the writer finish, scan again."""
        self.write_raw(name, torn, mtime=1_000_000)
        self.scan()
        self.write_raw(name, final, mtime=2_000_000)
        self.scan()
        return self.turns()

    def one_shot(self, name, final):
        """The same final bytes, read once, into a database of their own."""
        self.db.unlink(missing_ok=True)
        self.write_raw(name, final, mtime=3_000_000)
        self.scan()
        return self.turns()


class TestATornFinalLineIsNotStampedAsRead(TornTailFixture):
    """A scan can land while the writer is halfway through a record.

    `line_count` is the parse's own position and is always the file's full
    length (invariant 3, and the class above) — but a final line with no
    newline on it yet was *read*, not *finished*. Stamping it into
    `processed_files.lines` tells every later scan that record was ingested:
    `skip_lines` steps over it, transcripts are append-only, and a finished
    session's mtime never moves again, so nothing revisits it. The same bytes
    read once bill the full amount; read across the tear, they bill less,
    permanently.

    A synthetic unterminated record verifies that a partial final line is
    retried on the next scan instead of being marked complete."""

    def test_claude_re_reads_the_record_that_was_half_written(self):
        lines = [_claude("m-1", "2026-08-05T10:00:00Z", 10),
                 _claude("m-2", "2026-08-05T10:01:00Z", 20),
                 _claude("m-3", "2026-08-05T10:02:00Z", 30)]
        final = "\n".join(lines + [_claude("m-4", "2026-08-05T10:03:00Z", 40)]) + "\n"

        incremental = self.two_scans("sess.jsonl", self.torn(lines, 2), final)
        self.assertEqual(incremental, self.one_shot("sess.jsonl", final))
        self.assertEqual([t["message_id"] for t in incremental],
                         ["m-1", "m-2", "m-3", "m-4"])

    def test_a_streaming_tally_frozen_by_a_torn_line_is_still_repaired(self):
        """The sharper half. When the torn line is the record that COMPLETES a
        response whose partial tally is already stored, the row keeps that
        partial tally and a blank `stop_reason` for the life of the database —
        the precise defect the `MAX()` merge repairs, made unreachable by the
        bookkeeping. A `stream_tally_repair_v1` re-read used to be the other
        half of that repair; it went with the rest of the migrations, and the
        merge rule is what survived."""
        partial = json.loads(_claude("m-A", "2026-08-05T10:00:00Z", 50))
        partial["message"]["stop_reason"] = None
        lines = [json.dumps(partial),
                 _claude("m-A", "2026-08-05T10:00:30Z", 900),
                 _claude("m-B", "2026-08-05T10:05:00Z", 700)]
        final = "\n".join(lines) + "\n"

        incremental = self.two_scans("sess.jsonl", self.torn(lines, 1), final)
        self.assertEqual(incremental, self.one_shot("sess.jsonl", final))
        self.assertEqual(incremental[0], {"message_id": "m-A",
                                          "output_tokens": 900,
                                          "stop_reason": "end_turn"})

    def test_codex_re_reads_the_record_that_was_half_written(self):
        """Same mechanism, other grammar — and the reason this lives here
        rather than beside either parser."""
        lines = [_codex_header("t-1"), _codex_turn_context(),
                 _codex_token(1000, 100, "2026-08-05T10:00:00.000Z"),
                 _codex_token(3000, 200, "2026-08-05T10:01:00.000Z"),
                 _codex_token(6000, 300, "2026-08-05T10:02:00.000Z"),
                 _codex_token(10000, 400, "2026-08-05T10:03:00.000Z")]
        name = "rollout-2026-08-05T10-00-00-019fcf22aaaa.jsonl"
        final = "\n".join(lines) + "\n"

        incremental = self.two_scans(name, self.torn(lines, 4), final)
        self.assertEqual(incremental, self.one_shot(name, final))
        self.assertIn("codex:root-1:6000",
                      [t["message_id"] for t in incremental])

    def test_a_record_finished_while_the_scan_was_reading_is_not_skipped(self):
        """The race the failing scenario actually names, rather than the
        already-torn-at-rest file. The writer completes the half-written record
        WHILE the parse is running, so asking the file about its terminator
        afterwards sees a finished transcript and would stamp a record this read
        never ingested. The mtime `scan()` captured before the parse is what
        reports that the file moved under it."""
        lines = [_claude("m-1", "2026-08-05T10:00:00Z", 10),
                 _claude("m-2", "2026-08-05T10:01:00Z", 20),
                 _claude("m-3", "2026-08-05T10:02:00Z", 30)]
        final = "\n".join(lines) + "\n"
        self.write_raw("sess.jsonl", self.torn(lines, 2))

        real = scanner.parse_transcript

        # **kwargs, not a fixed signature: this double stands in for the real
        # `parse_transcript`, so a keyword added there must not turn a race
        # regression test into a TypeError about the double.
        def finishing(filepath, skip_lines=0, **kwargs):
            result = real(filepath, skip_lines=skip_lines, **kwargs)
            self.write_raw("sess.jsonl", final, mtime=2_000_000)
            return result

        with mock.patch.object(scanner, "parse_transcript", finishing):
            self.scan()
        self.scan()

        self.assertEqual([t["message_id"] for t in self.turns()],
                         ["m-1", "m-2", "m-3"])

    def test_a_same_metadata_replacement_after_parse_is_retried(self):
        """An atomic replacement must not inherit the parsed file's cursor.

        The replacement deliberately has the same byte length and mtime as the
        file stat captured before parsing.  Metadata-only validation therefore
        certifies a prefix hash read from the replacement and the next scan
        skips its turn forever.  The existing cursor is important: merely
        refusing the new stamp is insufficient if the old cursor remains equal
        to the replacement's metadata.
        """
        original = _claude("m-a", "2026-08-05T10:00:00Z", 10) + "\n"
        replacement = _claude("m-b", "2026-08-05T10:00:00Z", 20) + "\n"
        path = self.write_raw("sess.jsonl", original, mtime=1_000_000)
        self.scan()
        os.utime(path, (2_000_000, 2_000_000))
        real = scanner.parse_transcript

        def replacing(filepath, skip_lines=0, **kwargs):
            result = real(filepath, skip_lines=skip_lines, **kwargs)
            temporary = Path(filepath).with_name("sess.jsonl.replacement")
            temporary.write_text(replacement, encoding="utf-8")
            os.utime(temporary, (2_000_000, 2_000_000))
            os.replace(temporary, filepath)
            return result

        with mock.patch.object(scanner, "parse_transcript", replacing):
            self.scan()
        self.scan()

        self.assertEqual(
            [t["message_id"] for t in self.turns()], ["m-a", "m-b"])

    def test_a_transcript_that_is_one_unterminated_line_is_stamped_once(self):
        """An unterminated one-line file still needs a zero-line cursor with its actual
metadata. Otherwise every unchanged scan would reread it. Resume from the
beginning when it changes."""
        self.write_raw("sess.jsonl", _claude("m-1", "2026-08-05T10:00:00Z", 10))
        first = self.scan()

        self.assertEqual([t["message_id"] for t in self.turns()], ["m-1"],
                         "the record itself is complete and must be stored")
        self.assertEqual(first["new"], 1)
        self.assertEqual([r["lines"] for r in
                          self.rows("SELECT lines FROM processed_files")], [0])
        self.assertEqual(self.scan()["skipped"], 1, "not re-read forever")
        self.assertEqual(len(self.turns()), 1)

    def test_a_complete_final_record_without_a_newline_survives_the_re_read(self):
        """The other side of stopping short: the last line is re-read next
        time. That is only harmless because it was already stored on the first
        read and `insert_turns` merges on `message_id` — asserted here rather
        than assumed."""
        first = _claude("m-1", "2026-08-05T10:00:00Z", 10)
        second = _claude("m-2", "2026-08-05T10:01:00Z", 20)
        self.write_raw("sess.jsonl", first + "\n" + second)
        self.scan()
        self.assertEqual([t["message_id"] for t in self.turns()], ["m-1", "m-2"])

        self.write_raw("sess.jsonl",
                       first + "\n" + second + "\n"
                       + _claude("m-3", "2026-08-05T10:02:00Z", 30) + "\n",
                       mtime=2_000_000)
        self.scan()
        self.assertEqual(self.turns(), self.one_shot(
            "sess.jsonl",
            first + "\n" + second + "\n"
            + _claude("m-3", "2026-08-05T10:02:00Z", 30) + "\n"))
        self.assertEqual([t["output_tokens"] for t in self.turns()], [10, 20, 30])


class TestALostRecordIsCounted(ParserFixture):
    """`_load_json_object` returning None is the commonest way a record is
    lost, and it was the one way the loss was never reported.

    The `malformed` counter and its "skipped N unreadable record(s)" warning —
    whose own comment says the point is that the user learns the scan was
    lossy — only ever saw exceptions raised AFTER a record had been decoded. A
    line `json.loads` could not read, and a line too long for
    `_iter_jsonl_lines` to return at all, both took a bare `continue`: the scan
    printed a clean run and the record's tokens were gone.

    The distinction that keeps the warning honest is between a line that could
    not be READ and one that was read and holds nothing this grammar carries.
    `_load_json_object` returns None for both, and a bare `[]` is a real, benign
    line in a real transcript — counting it would print a lossiness warning on a
    clean file, which is worse than the silence being replaced.
    """

    def _parse(self, parser, name, lines):
        path = self.write(name, lines)
        # stderr, not stdout: the warning shares a stream with the read-error
        # warning beside it rather than with `cmd_dashboard`'s printed URL.
        with contextlib.redirect_stderr(io.StringIO()) as out:
            result = parser(path)
        return result, out.getvalue()

    def test_claude_reports_a_line_json_could_not_decode(self):
        (_m, turns, _a, _l, line_count), printed = self._parse(
            transcripts.parse_jsonl_file, "sess.jsonl",
            [_claude("m-1", "2026-08-05T10:00:00Z", 10),
             '{"type": "assistant", "sessionId": "s-1", "messa'])
        self.assertEqual(len(turns), 1)
        self.assertEqual(line_count, 2, "invariant 3 is unchanged")
        self.assertIn("skipped 1 unreadable record", printed)
        self.assertNotIn(": None", printed,
                         "the warning prints the decoder's own error, so the "
                         "reason has to be carried out, not a bare flag")

    def test_codex_reports_a_line_json_could_not_decode(self):
        (_s, turns, _a, _l, line_count), printed = self._parse(
            codex_transcripts.parse_jsonl_file,
            "rollout-2026-08-05T10-00-00-019fcf22aaaa.jsonl",
            [_codex_header("t-1"), _codex_turn_context(),
             _codex_token(1000, 100, "2026-08-05T10:00:00.000Z"),
             '{"type": "event_msg", "payload": {"type": "token_count", "in'])
        self.assertEqual(len(turns), 1)
        self.assertEqual(line_count, 4)
        self.assertIn("skipped 1 unreadable record", printed)

    def test_valid_json_that_is_not_a_record_is_not_called_unreadable(self):
        """`[]` decodes perfectly; there is simply no usage in it. Both parsers
        already skip such a line, and a real transcript in this suite's own
        fixtures contains one."""
        for parser, name in ((transcripts.parse_jsonl_file, "sess.jsonl"),
                             (codex_transcripts.parse_jsonl_file,
                              "rollout-2026-08-05T10-00-00-019fcf22aaaa.jsonl")):
            with self.subTest(parser=parser.__module__):
                (_a, _b, _c, _d, _e), printed = self._parse(parser, name, [
                    _codex_header("t-1"), _codex_turn_context(),
                    _codex_token(1000, 100, "2026-08-05T10:00:00.000Z"),
                    _claude("m-1", "2026-08-05T10:00:00Z", 10),
                    "[]", '"just a string"', "12"])
                self.assertEqual(printed, "")

    def test_an_oversized_utf8_line_is_counted_by_bytes(self):
        """The cap is a byte and memory boundary, not a character boundary.

        ``TextIOWrapper.readline(size)`` counts decoded characters. A line of
        multibyte text can therefore stay under ``size`` while consuming well
        over the documented byte cap. Use valid JSON whose decoded length is
        below the patched cap but whose UTF-8 representation is above it, so
        accepting the record cannot be mistaken for a JSON-decoder failure.
        Both parsers share the reader and must report the same lost record.
        """
        cap = 1024
        oversized = json.dumps(
            {"padding": "é" * 600}, ensure_ascii=False)
        self.assertLessEqual(len(oversized) + 1, cap,
                             "fixture is not below the character cap")
        self.assertGreater(len((oversized + "\n").encode("utf-8")), cap,
                           "fixture is not above the byte cap")
        for parser, name, good in (
                (transcripts.parse_jsonl_file, "sess.jsonl",
                 _claude("m-1", "2026-08-05T10:00:00Z", 10)),
                (codex_transcripts.parse_jsonl_file,
                 "rollout-2026-08-05T10-00-00-019fcf22aaaa.jsonl",
                 _codex_header("t-1"))):
            with self.subTest(parser=parser.__module__):
                path = self.write(name, [oversized, good])
                with mock.patch.object(
                        transcripts, "MAX_JSONL_LINE_LENGTH", cap):
                    with contextlib.redirect_stderr(io.StringIO()) as out:
                        result = parser(path)
                self.assertEqual(result[4], 2, "invariant 3 is unchanged")
                self.assertTrue(result[0] or result[1],
                                "the following valid record was not parsed")
                self.assertIn("skipped 1 unreadable record", out.getvalue())
                if parser is codex_transcripts.parse_jsonl_file:
                    with mock.patch.object(
                            transcripts, "MAX_JSONL_LINE_LENGTH", cap):
                        self.assertTrue(codex_transcripts.looks_like_codex(path))


class TestATitleResolvesTheSameWhicheverWayTheFileWasRead(ParserFixture):
    """A session's title must not depend on when the scanner happened to run.

    Two rules were composed. Inside one parse chunk a `custom-title` overwrites
    and an `ai-title` only fills a blank, so a whole-file read settles on the
    last custom title if the file has one and otherwise on the FIRST ai title.
    Across chunks the last chunk carrying any title simply won — so an
    incremental read tracked roughly the newest ai title, and a later
    ai-title-only chunk overwrote the user's own custom label.

    A schema rebuild can re-read a file previously scanned incrementally.
    Both paths must resolve its title using the same precedence rules.

    The rule is now the same function of the whole file on both paths: a
    custom-title overwrites whatever is stored, an ai-title fills only a blank.
    That needs no persisted provenance, because the incoming record's own kind
    is enough to reproduce the one-shot answer.
    """

    def _topic(self):
        rows = self.rows("SELECT topic FROM sessions WHERE session_id = 's-1'")
        return rows[0]["topic"] if rows else "<<no row>>"

    def _both_paths(self, first_chunk, second_chunk):
        """The same records, read as two chunks and then as one file."""
        self.write("sess.jsonl", first_chunk, mtime=1_000_000)
        self.scan()
        self.write("sess.jsonl", first_chunk + second_chunk, mtime=2_000_000)
        self.scan()
        split = self._topic()

        self.db.unlink(missing_ok=True)
        self.write("sess.jsonl", first_chunk + second_chunk, mtime=3_000_000)
        self.scan()
        return split, self._topic()

    def test_two_ai_titles_settle_on_the_first_on_both_paths(self):
        split, one_shot = self._both_paths(
            [_claude("m-1", "2026-08-05T10:00:00Z", 10),
             _claude_title("ai-title", "Run the release checklist")],
            [_claude("m-2", "2026-08-05T10:05:00Z", 20),
             _claude_title("ai-title", "Update effort in the model")])
        self.assertEqual(split, one_shot)
        self.assertEqual(one_shot, "Run the release checklist")

    def test_a_custom_title_survives_a_later_ai_title_only_chunk(self):
        """Claude Code re-emits an ai-title immediately after every
        custom-title, so a chunk boundary between the two is the ordinary
        shape, not a contrived one."""
        split, one_shot = self._both_paths(
            [_claude("m-1", "2026-08-05T10:00:00Z", 10),
             _claude_title("custom-title", "USER LABEL")],
            [_claude("m-2", "2026-08-05T10:05:00Z", 20),
             _claude_title("ai-title", "ai guess later")])
        self.assertEqual(split, one_shot)
        self.assertEqual(one_shot, "USER LABEL")

    def test_a_custom_title_in_a_later_chunk_still_replaces_an_ai_title(self):
        """The guard on the cheap version of this fix. Making the cross-chunk
        rule a plain fill-when-blank would converge the two paths by refusing
        the user's own label — the one title the parser treats as outranking
        everything."""
        split, one_shot = self._both_paths(
            [_claude("m-1", "2026-08-05T10:00:00Z", 10),
             _claude_title("ai-title", "ai guess")],
            [_claude("m-2", "2026-08-05T10:05:00Z", 20),
             _claude_title("custom-title", "USER LABEL")])
        self.assertEqual(split, one_shot)
        self.assertEqual(one_shot, "USER LABEL")

    def test_the_last_custom_title_wins_on_both_paths(self):
        """Renaming a session is the user doing it on purpose, so the newest
        one is the answer — the opposite of the ai-title rule, and what a
        whole-file read has always produced."""
        split, one_shot = self._both_paths(
            [_claude("m-1", "2026-08-05T10:00:00Z", 10),
             _claude_title("custom-title", "First name")],
            [_claude("m-2", "2026-08-05T10:05:00Z", 20),
             _claude_title("custom-title", "Renamed")])
        self.assertEqual(split, one_shot)
        self.assertEqual(one_shot, "Renamed")


class TestTheBranchResolvesTheSameWhicheverWayTheFileWasRead(ParserFixture):
    """`upsert_sessions` INSERTs `git_branch` and its UPDATE never touched it,
    so the stored branch was whatever the chunk that first created the row
    happened to carry — a blank one stayed blank forever.

    The rule is now first-non-empty-wins at both layers: the two parsers agree
    on it within a chunk (Codex kept the LAST header's branch), and the UPDATE
    fills a blank rather than overwriting, the same shape `topic`,
    `reasoning_effort` and `stop_reason` already use.

    Its opposite — letting the last chunk win — is the trap: Claude stamps a
    branch on essentially every record, so a session that switches branch
    mid-file would then store its first branch on a one-shot read and its last
    on a split read, creating on the priced source exactly the path-dependence
    this class exists to remove. `test_claude_keeps_the_branch_it_started_on`
    is what reports that.

    `rollups.project_by_day_model` takes its branch dimension straight from
    this column, so it is what splits the per-project table's rows — not a
    decoration on the session list.
    """

    def _branch(self, session="s-1"):
        rows = self.rows(
            f"SELECT git_branch FROM sessions WHERE session_id = '{session}'")
        return rows[0]["git_branch"] if rows else "<<no row>>"

    def _both_paths(self, name, first_chunk, second_chunk, session="s-1"):
        self.write(name, first_chunk, mtime=1_000_000)
        self.scan()
        self.write(name, first_chunk + second_chunk, mtime=2_000_000)
        self.scan()
        split = self._branch(session)

        self.db.unlink(missing_ok=True)
        self.write(name, first_chunk + second_chunk, mtime=3_000_000)
        self.scan()
        return split, self._branch(session)

    def test_claude_fills_a_blank_branch_from_a_later_chunk(self):
        """Claude Code omits `gitBranch` on some records, so a session first
        seen in a chunk carrying none had an empty branch pinned for good — the
        row exists, and nothing in the UPDATE could ever set the column."""
        split, one_shot = self._both_paths(
            "sess.jsonl",
            [_claude_on_branch("m-1", "2026-08-05T10:00:00Z", None)],
            [_claude_on_branch("m-2", "2026-08-05T10:05:00Z", "feature-x")])
        self.assertEqual(split, one_shot)
        self.assertEqual(one_shot, "feature-x")

    def test_claude_keeps_the_branch_it_started_on(self):
        """The regression guard on the SESSION LABEL, and the reason the UPDATE
        fills rather than overwrites. A session that switches branch mid-file
        agrees across the two paths today; a merge that let the newest chunk win
        would break exactly this.

        The session branch is a stable display label. Per-turn branches drive
        the cost split and can differ within the same session."""
        split, one_shot = self._both_paths(
            "sess.jsonl",
            [_claude_on_branch("m-1", "2026-08-05T10:00:00Z", "main")],
            [_claude_on_branch("m-2", "2026-08-05T10:05:00Z", "feature-y")])
        self.assertEqual(split, one_shot)
        self.assertEqual(one_shot, "main")

    def test_codex_keeps_the_first_branch_on_both_paths(self):
        """A synthetic rollout carries two headers. The parser kept the last
        one's branch while the row
        kept the first chunk's, so the same bytes gave different answers
        depending on where the scan landed. The chunk boundary has to fall
        BETWEEN the two headers — with both in one chunk there is nothing to
        disagree about, which is why the existing tests missed it."""
        name = "rollout-2026-08-05T10-00-00-019fcf22aaaa.jsonl"
        split, one_shot = self._both_paths(
            name,
            [_codex_header("t-1", "feature/synthetic-branch"),
             _codex_turn_context(),
             _codex_token(1000, 100, "2026-08-05T10:00:00.000Z")],
            [_codex_header("t-1", "master"),
             _codex_token(3000, 200, "2026-08-05T10:05:00.000Z")],
            session="root-1")
        self.assertEqual(split, one_shot)
        self.assertEqual(one_shot, "feature/synthetic-branch")


class TurnBranchFixture(ParserFixture):
    """The two reads and the one query every `turns.git_branch` class wants.

    Extracted so the per-record rule and the first-non-empty rule below are
    asserted through byte-identical machinery: a class that grew its own copy of
    `_both_paths` could drift into testing a different pair of scans, which is
    the drift invariant 3 exists to prevent one level down.
    """

    def _turn_branches(self, session="s-1"):
        return {r["message_id"]: r["git_branch"] for r in self.rows(
            "SELECT message_id, git_branch FROM turns "
            f"WHERE session_id = '{session}'")}

    def _both_paths(self, name, first_chunk, second_chunk, session="s-1"):
        """The same two reads `TestTheBranchResolves...` uses, reading `turns`."""
        self.write(name, first_chunk, mtime=1_000_000)
        self.scan()
        self.write(name, first_chunk + second_chunk, mtime=2_000_000)
        self.scan()
        split = self._turn_branches(session)

        self.db.unlink(missing_ok=True)
        self.write(name, first_chunk + second_chunk, mtime=3_000_000)
        self.scan()
        return split, self._turn_branches(session)


class TestTheTurnBranchIsPerRecord(TurnBranchFixture):
    """`turns.git_branch` — the branch the record itself carried.

    The session row holds one branch label; cost attribution must use the
    branch on each turn when the session changes branch.

    The column obeys invariant 1's THIRD merge rule, fill-when-blank, the one
    `reasoning_effort` and `stop_reason` use. MAX() is meaningless on text, and
    plain first-writer-wins would freeze the `''` an incremental scan stores when
    it lands on a record that carried no `gitBranch`.
    """

    def test_claude_stores_the_branch_each_turn_carried(self):
        """The finding. The session keeps `main` (the test above); the two turns
        keep the branches they were produced on, so the money follows the work
        rather than the label the session opened with."""
        split, one_shot = self._both_paths(
            "sess.jsonl",
            [_claude_on_branch("m-1", "2026-08-05T10:00:00Z", "main")],
            [_claude_on_branch("m-2", "2026-08-05T10:05:00Z", "feature-y")])
        self.assertEqual(split, one_shot)
        self.assertEqual(one_shot, {"m-1": "main", "m-2": "feature-y"})

    def test_a_record_with_no_branch_stores_a_blank_not_the_session_label(self):
        """`''` means NOT RECORDED here exactly as it does for `reasoning_effort`:
        stamping the session's label on a record that carried none would invent an
        attribution. `project_by_day_model` resolves the blank with a COALESCE onto
        `sessions.git_branch`, which is also what carries every row scanned before
        the column existed."""
        split, one_shot = self._both_paths(
            "sess.jsonl",
            [_claude_on_branch("m-1", "2026-08-05T10:00:00Z", None)],
            [_claude_on_branch("m-2", "2026-08-05T10:05:00Z", "feature-x")])
        self.assertEqual(split, one_shot)
        self.assertEqual(one_shot, {"m-1": "", "m-2": "feature-x"})

    def test_a_blank_turn_branch_is_filled_by_a_later_record_of_the_same_turn(self):
        """Fill-when-blank, and why MAX() and first-writer-wins are both wrong
        here. One API response, two streaming records, the chunk boundary between
        them: the first carries no `gitBranch`, so the split read stores `''` and
        a rule that let the first writer stand would freeze it — while a one-shot
        read of the same bytes keeps the last record and stores the branch."""
        split, one_shot = self._both_paths(
            "sess.jsonl",
            [_claude_on_branch("m-1", "2026-08-05T10:00:00Z", None, out=10)],
            [_claude_on_branch("m-1", "2026-08-05T10:00:09Z", "feature-z", out=90)])
        self.assertEqual(split, one_shot)
        self.assertEqual(one_shot, {"m-1": "feature-z"})

    def test_codex_turns_carry_no_branch_of_their_own(self):
        """Codex is deliberately NOT given a per-turn branch, and this pins that.

        A rollout stamps its branch in a `session_meta` header, not on each
        response, and the parser latches the first non-empty one for the whole
        file (`codex_transcripts.py`, the `not git_branch` guard) — so putting it
        on every turn would be file grain wearing turn grain's clothes. It would
        also change stored Codex attribution for a subagent rollout whose own
        first header disagrees with its root's, for no per-response information
        at all. Codex turns keep `''` and the rollup's COALESCE resolves them to
        the session label, which is exactly what the table showed before."""
        name = "rollout-2026-08-05T10-00-00-019fcf22aaaa.jsonl"
        self.write(name, [_codex_header("t-1", "feature/synthetic-branch"),
                          _codex_turn_context(),
                          _codex_token(1000, 100, "2026-08-05T10:00:00.000Z"),
                          _codex_token(3000, 200, "2026-08-05T10:05:00.000Z")])
        self.scan()
        self.assertEqual(
            sorted(self._turn_branches("root-1").values()), ["", ""])
        self.assertEqual(
            self.rows("SELECT git_branch FROM sessions "
                      "WHERE session_id = 'root-1'")[0]["git_branch"],
            "feature/synthetic-branch")


class TestARereadBranchDoesNotRestampTheTurn(TurnBranchFixture):
    """The branch of a response is the FIRST non-empty one its records carried.

    Final streaming usage and first-nonempty branch attribution follow
    different rules. A replay must not move the response to a later checkout.

    The discriminating fixture is one `message_id` written twice with an
    IDENTICAL tally and an IDENTICAL timestamp and a different branch, split by
    a chunk boundary. That pair is what nothing in the suite exercised, and it
    is what separates the three candidate rules:

    * always-overwrite (the old parser) stores the later branch on a one-shot
      read and the earlier one on a split read, because `insert_turns` merges
      this column fill-when-blank — the path-dependence invariant 1 exists to
      kill;
    * moving the column under `_MORE_COMPLETE` changes nothing here (a tie on
      tally AND timestamp is not "more complete"), so the split read still keeps
      the earlier branch while the one-shot read keeps the later one — the same
      disagreement. It had a third leg when this was written, that it also
      stranded `turn_branch_backfill_v1`, whose re-inserted rows tie by
      construction; that leg went with the marker (AGENTS.md invariant 8) and
      the two above are on their own sufficient;
    * first-non-empty, the rule invariant 8 already applies to
      `sessions.git_branch` and `codex_transcripts.py` applies to its headers,
      makes both paths answer with the branch the response STARTED on.

    Read the assertions as pinning the chosen rule, not as proving it correct
    for every case: at most 7 of the 515 are a genuine `git checkout`
    mid-response, and those move from the completing branch to the starting one.
    That half is a judgement call, argued in the comment beside the code.
    """

    def _parse(self, name, lines):
        """The parser alone, with `insert_turns`' fill-when-blank out of the way.

        The DB merge would keep the first chunk's non-blank branch whatever the
        parser decided, so a test that only ever looked at `turns` could pass on
        the split path while the parser did the wrong thing on the one-shot one.
        """
        path = self.write(name, lines)
        turns = transcripts.parse_jsonl_file(path)[1]
        return {t["message_id"]: t for t in turns}

    def test_the_parser_keeps_the_first_non_empty_branch_of_a_response(self):
        """Three records of one response: none, then two disagreeing branches.

        `first NON-EMPTY` rather than `first` — the blank leading record is the
        shape `test_a_blank_turn_branch_is_filled_by_a_later_record_of_the_same_turn`
        pins, and latching onto it would freeze `''` for the whole response.
        """
        turns = self._parse("sess.jsonl", [
            _claude_on_branch("m-1", "2026-08-05T10:00:00Z", None, out=10),
            _claude_on_branch("m-1", "2026-08-05T10:00:04Z", "main", out=50),
            _claude_on_branch("m-1", "2026-08-05T10:00:09Z", "feature-y", out=90),
        ])
        self.assertEqual(turns["m-1"]["git_branch"], "main")

    def test_only_the_branch_latches(self):
        """The guard on the rest of invariant 1: everything but the branch still
        comes from the last record, so the final tally and the completing clock
        are untouched by this."""
        turns = self._parse("sess.jsonl", [
            _claude_on_branch("m-1", "2026-08-05T10:00:00Z", "main", out=10),
            _claude_on_branch("m-1", "2026-08-05T10:00:09Z", "feature-y", out=90),
        ])
        self.assertEqual(
            (turns["m-1"]["git_branch"], turns["m-1"]["output_tokens"],
             turns["m-1"]["timestamp"]),
            ("main", 90, "2026-08-05T10:00:09Z"))

    def test_two_records_tied_on_tally_and_clock_agree_across_both_paths(self):
        """THE discriminating case, through the database this time.

        One `message_id`, two records, identical tally, identical timestamp,
        different branch, a chunk boundary between them. Nothing in the merge can
        prefer either record, so whichever branch is stored is the parser's
        choice — and the two scan paths must reach the same one."""
        split, one_shot = self._both_paths(
            "sess.jsonl",
            [_claude_on_branch("m-1", "2026-08-05T10:00:00Z", "main", out=10)],
            [_claude_on_branch("m-1", "2026-08-05T10:00:00Z", "feature-y",
                               out=10)])
        self.assertEqual(split, one_shot)
        self.assertEqual(one_shot, {"m-1": "main"})
        self.assertEqual(
            self.rows("SELECT COUNT(*) c FROM turns")[0]["c"], 1,
            "the two records are one API response and must stay one row")

    def test_a_replayed_record_does_not_restamp_the_response(self):
        """The real-world population: a verbatim replay with the branch
        re-sampled. Same `uuid`, same `parentUuid`, same timestamp, same usage —
        only `gitBranch` differs, because the checkout moved between the original
        write and the replay."""
        split, one_shot = self._both_paths(
            "sess.jsonl",
            [_claude_replay("m-1", "2026-08-05T10:00:00Z", "main")],
            [_claude_replay("m-1", "2026-08-05T10:00:00Z", "release/2.1")])
        self.assertEqual(split, one_shot)
        self.assertEqual(one_shot, {"m-1": "main"})

    def test_a_replay_inside_one_chunk_is_decided_by_the_parser_alone(self):
        """The same replay with no chunk boundary — the case a full first read of
        a finished transcript actually hits, where `insert_turns` never sees the
        earlier record at all and the parser is the only thing that can be
        right."""
        turns = self._parse("sess.jsonl", [
            _claude_replay("m-1", "2026-08-05T10:00:00Z", "main"),
            _claude_replay("m-1", "2026-08-05T10:00:00Z", "release/2.1"),
        ])
        self.assertEqual(turns["m-1"]["git_branch"], "main")


class TestASessionStartsWhenItsEarliestRecordSays(unittest.TestCase):
    """`first_timestamp` had no merge rule at all, so it was path-dependent.

    `last_timestamp` is `MAX(last_timestamp, ?)`; `first_timestamp` was simply
    whatever the chunk that CREATED the row carried, and nothing could lower it
    afterwards. So a one-shot scan and an incremental scan of identical bytes
    stored different session start times whenever a session's earliest record
    was not in the chunk that created its row -- which happens whenever records
    are written out of timestamp order, as sidechain and subagent records are.

    No total moves. What moves is Duration, which the sessions table computes as
    `last - first`: the same bytes gave two different durations depending on
    where a scan happened to land. That is the path-dependence invariant 1
    exists to kill, on the one column that had no rule.
    """

    def _turn(self, mid, ts):
        return {"type": "assistant", "sessionId": "s1", "uuid": mid,
                "timestamp": ts, "cwd": "/w/p", "gitBranch": "main",
                "message": {"id": mid, "model": "claude-opus-4-8",
                            "usage": {"input_tokens": 1, "output_tokens": 1}}}

    def _write(self, root, records):
        proj = root / "projects" / "p"
        proj.mkdir(parents=True, exist_ok=True)
        path = proj / "s.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        return path

    def _stamps(self, db):
        conn = sqlite3.connect(db)
        try:
            return conn.execute(
                "SELECT first_timestamp, last_timestamp FROM sessions").fetchone()
        finally:
            conn.close()

    def test_a_split_read_agrees_with_a_one_shot_read(self):
        import scanner
        # The later record carries the EARLIER timestamp.
        records = [self._turn("m1", "2026-08-14T18:00:00.000Z"),
                   self._turn("m2", "2026-08-14T09:00:00.000Z")]

        whole = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, whole, ignore_errors=True)
        self._write(whole, records)
        one_shot_db = str(whole / "u.db")
        scanner.scan(projects_dir=whole / "projects", db_path=one_shot_db,
                     verbose=False)

        split = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, split, ignore_errors=True)
        path = self._write(split, records[:1])
        split_db = str(split / "u.db")
        scanner.scan(projects_dir=split / "projects", db_path=split_db,
                     verbose=False)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(records[1]) + "\n")
        later = os.path.getmtime(path) + 10
        os.utime(path, (later, later))
        scanner.scan(projects_dir=split / "projects", db_path=split_db,
                     verbose=False)

        self.assertEqual(
            self._stamps(split_db), self._stamps(one_shot_db),
            "the same bytes gave two different session start times depending "
            "on where the scan was split")
        self.assertEqual(self._stamps(one_shot_db)[0],
                         "2026-08-14T09:00:00.000Z",
                         "and the agreed answer must be the EARLIEST record")

    def test_offsets_are_ordered_by_instant_while_raw_values_are_kept(self):
        records = [
            self._turn("m-later", "2026-08-01T00:30:00Z"),
            self._turn("m-earlier", "2026-08-01T01:00:00+02:00"),
        ]
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        path = self._write(root, records)
        _meta, turns, _agents, _limits, _count = transcripts.parse_jsonl_file(path)
        self.assertEqual(
            [(turn["timestamp"], turn["message_id"]) for turn in turns],
            [("2026-08-01T00:30:00Z", "m-later"),
             ("2026-08-01T01:00:00+02:00", "m-earlier")])

        db_path = str(root / "u.db")
        scanner.scan(projects_dir=root / "projects", db_path=db_path,
                     verbose=False)
        self.assertEqual(self._stamps(db_path),
                         ("2026-08-01T01:00:00+02:00",
                          "2026-08-01T00:30:00Z"))

    def test_a_blank_start_is_filled_rather_than_treated_as_the_minimum(self):
        """`''` is what this schema stores for "not recorded", and it sorts
        below every real timestamp. A naive `MIN()` would make one unstamped
        record set the whole session's start to blank."""
        import scanner
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        blank = self._turn("m1", "2026-08-14T18:00:00.000Z")
        blank["timestamp"] = ""
        path = self._write(root, [blank])
        db = str(root / "u.db")
        scanner.scan(projects_dir=root / "projects", db_path=db, verbose=False)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self._turn("m2", "2026-08-14T09:00:00.000Z")) + "\n")
        later = os.path.getmtime(path) + 10
        os.utime(path, (later, later))
        scanner.scan(projects_dir=root / "projects", db_path=db, verbose=False)
        first, _last = self._stamps(db)
        self.assertEqual(first, "2026-08-14T09:00:00.000Z",
                         "a blank must be filled by a real timestamp, not kept "
                         "as the minimum")


class TestAFailedReadIsNotEndOfFile(unittest.TestCase):
    """A read error partway through a transcript used to be lost PERMANENTLY.

    The outer handler folded any stream failure into a normal return carrying
    the PARTIAL `line_count`, and `scan()` stamped that beside the file's REAL
    mtime. The skip test compares that mtime exactly, and a finished
    transcript's mtime never moves again -- so the unread tail was excluded from
    every later scan even once the fault cleared. Measured before the fix by
    force-unmounting the volume under a running scan: 15,623 of 60,000 responses
    stored, and three later scans of the healthy, remounted, byte-identical file
    recovered none of the rest.

    The parser's own comment already described this defect and said the outer
    handler "cannot tell 'this file is unreadable' from 'this one line is'". The
    per-record guard closed the RECORD half; this is the READ half.

    Reachable without malice: a removable or network volume that goes away
    mid-scan, a stale NFS handle, a cloud-sync FUSE layer, a MemoryError on the
    64 MiB readline buffer.
    """

    RECORDS = 400
    CUT = 120

    def _corpus(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        proj = root / "projects" / "p"
        proj.mkdir(parents=True)
        with (proj / "sess.jsonl").open("w", encoding="utf-8") as handle:
            for i in range(self.RECORDS):
                handle.write(json.dumps({
                    "type": "assistant", "sessionId": "s1", "uuid": f"u{i}",
                    "timestamp": "2026-08-01T12:00:00.000Z", "cwd": "/w/p",
                    "gitBranch": "main",
                    "message": {"id": f"m{i}", "model": "claude-opus-4-8",
                                "usage": {"input_tokens": 1, "output_tokens": 1}},
                }) + "\n")
        return root

    @contextlib.contextmanager
    def _failing_read_after(self, lines):
        """Fail the READ STREAM, not the open and not a record.

        The distinction is the whole finding: `_open_transcript` refusing is
        already covered by `_read_nothing_from`, and a bad record is contained
        by the per-record handler. Only a mid-stream failure produced the
        silent permanent loss.
        """
        import scanner
        import transcripts
        original = transcripts._open_transcript

        class Failing:
            def __init__(self, handle):
                self.handle, self.seen = handle, 0

            def readline(self, *args):
                self.seen += 1
                if self.seen > lines:
                    raise OSError(5, "Input/output error")
                return self.handle.readline(*args)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self.handle.__exit__(*exc)

        def failing(path, **kwargs):
            return Failing(original(path, **kwargs))

        transcripts._open_transcript = failing
        scanner._open_transcript = failing
        try:
            yield
        finally:
            transcripts._open_transcript = original
            scanner._open_transcript = original

    def _stored(self, db):
        conn = sqlite3.connect(db)
        try:
            turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            rows = conn.execute(
                "SELECT mtime, lines FROM processed_files").fetchall()
        finally:
            conn.close()
        return turns, rows

    def test_the_unread_tail_comes_back_on_the_next_scan(self):
        import scanner
        root = self._corpus()
        db = str(root / "usage.db")
        stderr = io.StringIO()
        with self._failing_read_after(self.CUT), \
                contextlib.redirect_stderr(stderr):
            scanner.scan(projects_dir=root / "projects", db_path=db,
                         verbose=False)

        turns, rows = self._stored(db)
        self.assertEqual(turns, self.CUT, "the partial read should be KEPT")
        self.assertEqual(
            rows, [],
            "the file was stamped as processed after a failed read, so no "
            "later scan would ever revisit it")
        self.assertIn("error reading", stderr.getvalue(),
                      "the warning must reach stderr, not the dashboard's stdout")

        scanner.scan(projects_dir=root / "projects", db_path=db, verbose=False)
        turns, rows = self._stored(db)
        self.assertEqual(turns, self.RECORDS,
                         "the tail was not recovered by a healthy rescan")
        self.assertEqual(len(rows), 1, "the healthy scan should stamp the file")

    def test_a_healthy_scan_still_stamps_and_still_skips(self):
        """Anti-vacuity. If `scan` simply stopped stamping, the test above would
        pass while every scan re-read every transcript forever."""
        import scanner
        root = self._corpus()
        db = str(root / "usage.db")
        scanner.scan(projects_dir=root / "projects", db_path=db, verbose=False)
        turns, rows = self._stored(db)
        self.assertEqual(turns, self.RECORDS)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], self.RECORDS,
                         "a clean read must stamp the file's full length")

    def test_the_parser_reports_the_failure_rather_than_returning_it(self):
        """The mechanism, asserted directly: a caller that is not `scan` must
        also be able to tell a failed read from a short file."""
        import transcripts
        root = self._corpus()
        path = str(root / "projects" / "p" / "sess.jsonl")
        with self._failing_read_after(self.CUT), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(transcripts.TranscriptReadError) as caught:
                transcripts.parse_jsonl_file(path)
        self.assertEqual(len(caught.exception.partial[1]), self.CUT,
                         "the records already parsed must be carried out")


if __name__ == "__main__":
    unittest.main()
