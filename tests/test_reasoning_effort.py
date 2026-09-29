"""End-to-end proof of `turns.reasoning_effort` and `turns.stop_reason`.

Besides the model, reasoning effort is the main thing that decides what a turn
costs, and it is the only lever a user has over spend that isn't "use a cheaper
model". `stop_reason` exists for `max_tokens`: a response that hit the ceiling
was truncated mid-answer and billed in full, and it is indistinguishable from a
complete one in every other total the dashboard shows.

Neither is a token count, so nothing already in the suite would notice if they
were silently wrong — a blank column looks exactly like an assistant that never
recorded the field. This file therefore walks the whole path, parser -> DB ->
payload, and pins the four places the two columns can go quietly wrong:

* **''ise not a level.** `''` means NOT RECORDED. Folding it into a named bucket
  invents a distribution; dropping it makes the breakdown disagree with every
  total shown beside it. It must survive as its own bucket.
* **The blank must be fillable.** Claude sets `stop_reason` only on the record
  that *completes* a response, so an incremental scan that lands mid-stream
  stores `''`. "First writer wins" would make that blank permanent — and would
  also make the one-time re-read in `scan()` do nothing at all, since every
  existing row conflicts on `message_id`. Filling only *when blank* is what keeps
  the cross-transcript dedup case a no-op.
* **Codex's effort is carried, not parsed per turn.** It is established by a
  `turn_context` record and applies until the next one, so an incremental parse
  that starts after those records would attribute every appended turn to no
  effort at all — correct on a full re-read, blank on the incremental path.
* **A rollup that loses `model` cannot be priced.** SQLite happily returns an
  arbitrary row's value for a column outside the GROUP BY, so an effort bucket
  mixing opus and haiku would be priced entirely at one of them (5x out) with no
  error and no failing total — grouping coarser preserves sums, which is exactly
  why a totals-only assertion cannot catch it.
"""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import codex_transcripts
import rollups
import transcripts
from dashboard_data import get_dashboard_data
from db import APPLICATION_ID
from pricing import calc_cost
from scanner import get_db, init_db, insert_turns, scan
from tests.timestamps import utc_ts_on_local_day

NL = "\n"

# Distinguishes "the key is absent" from "the key is present and null" — real
# partial streaming records carry `"stop_reason": null`, which is a different
# input from a record that omits it.
_ABSENT = object()


# ── Claude Code fixtures ───────────────────────────────────────────────────
# Same shape as tests/test_scanner.py's `_make_assistant_record`, plus the two
# fields under test. `effort` is deliberately built at the TOP LEVEL of the
# record and `stop_reason` inside `message` — that asymmetry is real, and a
# fixture that put them in the same place could not detect a parser reading the
# wrong one.

def _claude_record(session_id="sess-1", model="claude-opus-4-8",
                   input_tokens=100, output_tokens=50, cache_read=10,
                   cache_creation=5, timestamp=None, message_id="msg-1",
                   effort=_ABSENT, stop_reason=_ABSENT, message_extra=None):
    msg = {
        "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        },
        "content": [],
    }
    if message_id:
        msg["id"] = message_id
    if stop_reason is not _ABSENT:
        msg["stop_reason"] = stop_reason
    if message_extra:
        msg.update(message_extra)
    record = {
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": timestamp or utc_ts_on_local_day(0),
        "cwd": "/home/user/project",
        "message": msg,
    }
    if effort is not _ABSENT:
        record["effort"] = effort
    return json.dumps(record)


# ── Codex fixtures ─────────────────────────────────────────────────────────
# Same conventions as tests/test_codex_transcripts.py.

def _rec(rtype, payload, timestamp=None):
    return json.dumps({"timestamp": timestamp or utc_ts_on_local_day(0),
                       "type": rtype, "payload": payload})


def _codex_session_meta(thread="019fcf22-aaaa", cwd="/home/u/proj"):
    return _rec("session_meta", {
        "id": thread, "session_id": thread, "cwd": cwd,
        "originator": "codex_vscode", "cli_version": "0.146.0",
        "source": "vscode", "thread_source": "user",
        "model_provider": "openai",
        "git": {"commit_hash": "abc123", "branch": "main",
                "repository_url": "git@example:me/proj.git"},
    })


def _codex_turn_context(model="gpt-5.6-sol", effort=_ABSENT):
    payload = {"turn_id": "t-1", "cwd": "/home/u/proj"}
    if model is not _ABSENT:
        payload["model"] = model
    if effort is not _ABSENT:
        payload["effort"] = effort
    return _rec("turn_context", payload)


def _codex_thread_settings(model="gpt-5.6-sol", effort=_ABSENT,
                           effort_key="reasoning_effort"):
    """A `thread_settings_applied` record, spelling effort the way Codex does.

    `effort_key` defaults to the REAL key and exists only so the fallback test
    below can build the shape no real rollout has. The two record types disagree
    on the spelling — `turn_context` says `effort`, `thread_settings` says
    `reasoning_effort` — and this fixture used to fabricate `effort` here too,
    which is what let the parser read a key that is never present and still pass:
    the assertion was checking the parser against its own mistake.
    """
    settings = {"service_tier": "default"}
    if model is not _ABSENT:
        settings["model"] = model
    if effort is not _ABSENT:
        settings[effort_key] = effort
    return _rec("event_msg", {"type": "thread_settings_applied",
                              "thread_settings": settings})


def _codex_token_count(cum_total, inp=100, out=40, cached=0, reasoning=0):
    return _rec("event_msg", {
        "type": "token_count",
        "info": {
            "last_token_usage": {
                "input_tokens": inp, "cached_input_tokens": cached,
                "cache_write_input_tokens": 0, "output_tokens": out,
                "reasoning_output_tokens": reasoning, "total_tokens": inp + out,
            },
            "total_token_usage": {
                "input_tokens": 0, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "output_tokens": 0,
                "reasoning_output_tokens": 0, "total_tokens": cum_total,
            },
            "model_context_window": 258400,
        },
    })


def _turn(message_id, model="claude-opus-4-8", effort="", stop_reason="",
          days_ago=0, source="claude", inp=0, out=0, cache_read=0,
          cache_creation=0, cache_creation_1h=0, reasoning=0,
          session_id="sess-1"):
    """One row for insert_turns, in the shape both parsers return."""
    return {
        "session_id": session_id,
        "timestamp": utc_ts_on_local_day(days_ago),
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "cache_creation_1h_tokens": cache_creation_1h,
        "reasoning_output_tokens": reasoning,
        "tool_name": None,
        "cwd": None,
        "message_id": message_id,
        "is_subagent": 0,
        "agent_id": None,
        "source": source,
        "reasoning_effort": effort,
        "stop_reason": stop_reason,
    }


class _TempDir(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "usage.db"
        self._parsed = 0

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def write_transcript(self, lines, name="sess-1.jsonl", subdir="projects/u/p",
                         mtime=1_000_000):
        directory = Path(self.tmpdir) / subdir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(NL.join(lines) + NL)
        os.utime(path, (mtime, mtime))
        return path

    def rows(self, sql, *args):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args)]
        finally:
            conn.close()


# ── 1. The Claude parser ───────────────────────────────────────────────────

class TestClaudeParser(_TempDir):
    """`effort` is top-level on the record; `stop_reason` is inside `message`."""

    def parse(self, *records, skip_lines=0):
        # A fresh file per call: the same records are parsed twice (full, then
        # resumed) and appending to one file would change what is being read.
        self._parsed += 1
        path = self.write_transcript(list(records),
                                     name=f"sess-{self._parsed}.jsonl")
        _sessions, turns, _agents, _limits, _lines = transcripts.parse_jsonl_file(
            path, skip_lines=skip_lines)
        return turns

    def test_a_record_carrying_both_produces_a_turn_with_both(self):
        turns = self.parse(_claude_record(effort="xhigh", stop_reason="tool_use"))
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["reasoning_effort"], "xhigh")
        self.assertEqual(turns[0]["stop_reason"], "tool_use")

    def test_effort_is_read_from_the_record_not_from_the_message(self):
        """The two live in different places, and a parser that confused them
        would still produce a plausible-looking value."""
        turns = self.parse(_claude_record(
            effort="high", message_extra={"effort": "low"}))
        self.assertEqual(turns[0]["reasoning_effort"], "high")

    def test_stop_reason_is_read_from_the_message_not_from_the_record(self):
        turns = self.parse(_claude_record(stop_reason="end_turn"))
        self.assertEqual(turns[0]["stop_reason"], "end_turn")

    def test_a_record_with_neither_records_empty_strings(self):
        """Unknown, not None and not a crash — '' is what every reader treats as
        'not recorded', and a None would reach SQLite as NULL instead."""
        turns = self.parse(_claude_record())
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["reasoning_effort"], "")
        self.assertEqual(turns[0]["stop_reason"], "")
        self.assertIsNotNone(turns[0]["reasoning_effort"])
        self.assertIsNotNone(turns[0]["stop_reason"])

    def test_an_explicit_null_stop_reason_is_blank_not_none(self):
        """Every partial streaming record carries `"stop_reason": null`."""
        turns = self.parse(_claude_record(effort=None, stop_reason=None))
        self.assertEqual(turns[0]["stop_reason"], "")
        self.assertEqual(turns[0]["reasoning_effort"], "")

    def test_a_non_string_effort_is_discarded_rather_than_stored(self):
        turns = self.parse(_claude_record(effort={"level": "high"},
                                          stop_reason=["end_turn"]))
        self.assertEqual(turns[0]["reasoning_effort"], "")
        self.assertEqual(turns[0]["stop_reason"], "")

    def test_both_are_bounded_far_tighter_than_a_free_form_id(self):
        """A transcript is untrusted input and there is no legitimate
        500-character effort level."""
        turns = self.parse(_claude_record(effort="x" * 5000,
                                          stop_reason="y" * 5000))
        self.assertEqual(len(turns[0]["reasoning_effort"]), 64)
        self.assertEqual(len(turns[0]["stop_reason"]), 64)

    def test_the_completing_record_of_a_stream_supplies_the_stop_reason(self):
        """Within one parse, last-record-per-message_id wins (invariant 1), so
        the record that completes the response is the one that is kept."""
        turns = self.parse(
            _claude_record(message_id="msg-s", output_tokens=10,
                           effort="high", stop_reason=None),
            _claude_record(message_id="msg-s", output_tokens=2000,
                           effort="high", stop_reason="end_turn"),
        )
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["stop_reason"], "end_turn")
        self.assertEqual(turns[0]["output_tokens"], 2000)


# ── 2. The Codex parser ────────────────────────────────────────────────────

class _CodexParse:
    """Shared rollout-parsing helper. A plain mixin rather than a base TestCase
    so the resume suite below does not silently re-run every test above it."""

    def parse(self, *records, skip_lines=0):
        # A fresh file per call, for the same reason as the Claude helper: the
        # resume tests parse the same records twice.
        self._parsed += 1
        path = self.write_transcript(list(records),
                                     name=f"rollout-{self._parsed}.jsonl",
                                     subdir="sessions/2026/08/07")
        _sessions, turns, _agents, _limits, _lines = codex_transcripts.parse_jsonl_file(
            path, skip_lines=skip_lines)
        return {t["message_id"]: t for t in turns}


class TestCodexParser(_CodexParse, _TempDir):
    """Codex states effort once, on the `turn_context` record that also names the
    model, and it applies until the next one."""

    def test_effort_comes_from_the_preceding_turn_context(self):
        turns = self.parse(
            _codex_session_meta(),
            _codex_turn_context(model="gpt-5.6-sol", effort="xhigh"),
            _codex_token_count(1000),
        )
        self.assertEqual(len(turns), 1)
        turn = next(iter(turns.values()))
        self.assertEqual(turn["reasoning_effort"], "xhigh")
        self.assertEqual(turn["model"], "gpt-5.6-sol")

    def test_a_later_turn_context_changes_effort_for_later_turns_only(self):
        turns = self.parse(
            _codex_session_meta(),
            _codex_turn_context(effort="low"),
            _codex_token_count(1000),
            _codex_turn_context(effort="xhigh"),
            _codex_token_count(2000),
            _codex_token_count(3000),
        )
        self.assertEqual(
            {mid: t["reasoning_effort"] for mid, t in turns.items()},
            {"codex:019fcf22-aaaa:1000": "low",
             "codex:019fcf22-aaaa:2000": "xhigh",
             "codex:019fcf22-aaaa:3000": "xhigh"})

    def test_thread_settings_applied_also_changes_the_effort(self):
        """Codex changes the model through this record too, not only through
        `turn_context` — and effort rides on the same record."""
        turns = self.parse(
            _codex_session_meta(),
            _codex_turn_context(effort="low"),
            _codex_token_count(1000),
            _codex_thread_settings(model="gpt-5.6-terra", effort="ultra"),
            _codex_token_count(2000),
        )
        self.assertEqual(turns["codex:019fcf22-aaaa:1000"]["reasoning_effort"], "low")
        self.assertEqual(turns["codex:019fcf22-aaaa:2000"]["reasoning_effort"], "ultra")
        self.assertEqual(turns["codex:019fcf22-aaaa:2000"]["model"], "gpt-5.6-terra")

    def test_a_settings_record_changing_both_at_once_changes_both(self):
        """The shape that made the fabricated fixture key matter.

        Codex switches model AND effort in one `thread_settings_applied`, and the
        next `token_count` arrives before any `turn_context`. Reading the wrong
        key applied the model half and dropped the effort half, so the turn was
        stamped with the NEW model and the PREVIOUS model's effort — a wrong
        named level rather than the blank that means 'not recorded', so nothing
        downstream could tell it was unreliable.
        """
        turns = self.parse(
            _codex_session_meta(),
            _codex_turn_context(model="gpt-5.6-sol", effort="xhigh"),
            _codex_token_count(1000),
            _codex_thread_settings(model="codex-auto-review", effort="low"),
            _codex_token_count(2000),
        )
        later = turns["codex:019fcf22-aaaa:2000"]
        self.assertEqual(later["model"], "codex-auto-review")
        self.assertEqual(later["reasoning_effort"], "low")
        # And the turn before it keeps what was in force then.
        self.assertEqual(turns["codex:019fcf22-aaaa:1000"]["reasoning_effort"],
                         "xhigh")

    def test_a_settings_record_naming_only_a_model_does_not_blank_the_effort(self):
        """The twin of the `turn_context` case below, on the branch that was
        dead: model and effort are read together and applied independently, so a
        settings record that omits the effort must leave the effort standing."""
        turns = self.parse(
            _codex_session_meta(),
            _codex_turn_context(model="gpt-5.6-sol", effort="max"),
            _codex_token_count(1000),
            _codex_thread_settings(model="gpt-5.6-terra"),   # no effort key
            _codex_token_count(2000),
        )
        later = turns["codex:019fcf22-aaaa:2000"]
        self.assertEqual(later["model"], "gpt-5.6-terra")
        self.assertEqual(later["reasoning_effort"], "max")

    def test_a_settings_record_spelling_effort_the_turn_context_way_still_reads(self):
        """Exercise the tolerated effort alias without assuming all clients write it.
Unknown or absent values must remain safe."""
        turns = self.parse(
            _codex_session_meta(),
            _codex_turn_context(model="gpt-5.6-sol", effort="max"),
            _codex_token_count(1000),
            _codex_thread_settings(effort="low", effort_key="effort"),
            _codex_token_count(2000),
        )
        self.assertEqual(turns["codex:019fcf22-aaaa:2000"]["reasoning_effort"],
                         "low")

        both = self.parse(
            _codex_session_meta(),
            _codex_turn_context(model="gpt-5.6-sol", effort="max"),
            json.dumps({"timestamp": utc_ts_on_local_day(0), "type": "event_msg",
                        "payload": {"type": "thread_settings_applied",
                                    "thread_settings": {"model": "gpt-5.6-sol",
                                                        "reasoning_effort": "ultra",
                                                        "effort": "low"}}}),
            _codex_token_count(2000),
        )
        self.assertEqual(both["codex:019fcf22-aaaa:2000"]["reasoning_effort"],
                         "ultra")

    def test_a_context_naming_only_a_model_does_not_blank_the_effort(self):
        """Model and effort are recovered together but applied independently: a
        record that sets one and not the other must not erase the other."""
        turns = self.parse(
            _codex_session_meta(),
            _codex_turn_context(model="gpt-5.6-sol", effort="max"),
            _codex_token_count(1000),
            _codex_turn_context(model="gpt-5.6-terra"),      # no effort key
            _codex_token_count(2000),
        )
        later = turns["codex:019fcf22-aaaa:2000"]
        self.assertEqual(later["model"], "gpt-5.6-terra")
        self.assertEqual(later["reasoning_effort"], "max")

    def test_a_context_naming_only_an_effort_does_not_blank_the_model(self):
        turns = self.parse(
            _codex_session_meta(),
            _codex_turn_context(model="gpt-5.6-sol", effort="max"),
            _codex_token_count(1000),
            _codex_turn_context(model=_ABSENT, effort="low"),
            _codex_token_count(2000),
        )
        later = turns["codex:019fcf22-aaaa:2000"]
        self.assertEqual(later["model"], "gpt-5.6-sol")
        self.assertEqual(later["reasoning_effort"], "low")

    def test_a_rollout_that_never_states_an_effort_reports_unknown(self):
        turns = self.parse(
            _codex_session_meta(),
            _codex_turn_context(model="gpt-5.6-sol"),
            _codex_token_count(1000),
        )
        self.assertEqual(next(iter(turns.values()))["reasoning_effort"], "")

    def test_codex_turns_carry_no_stop_reason(self):
        """Codex rollouts have no analogue, so the column stays '' for this
        source rather than claiming Codex never truncates."""
        turns = self.parse(
            _codex_session_meta(),
            _codex_turn_context(effort="max"),
            _codex_token_count(1000),
        )
        self.assertEqual(next(iter(turns.values()))["stop_reason"], "")


# ── 2b. The fixtures describe records that actually exist ──────────────────

# Synthetic context fixtures cover the settings fields accepted by the parser
# and ensure unrelated fields do not become token or effort data.
_REAL_THREAD_SETTINGS_KEYS = frozenset({
    "model", "model_provider_id", "service_tier", "approval_policy",
    "approvals_reviewer", "permission_profile", "active_permission_profile",
    "cwd", "reasoning_effort", "reasoning_summary", "personality",
    "collaboration_mode",
})

_REAL_TURN_CONTEXT_KEYS = frozenset({
    "turn_id", "cwd", "workspace_roots", "current_date", "timezone",
    "approval_policy", "approvals_reviewer", "sandbox_policy",
    "permission_profile", "file_system_sandbox_policy", "model", "comp_hash",
    "personality", "collaboration_mode", "multi_agent_version",
    "multi_agent_mode", "realtime_active", "effort", "summary",
})


class TestFixtureFidelity(unittest.TestCase):
    """The fixtures above must be subsets of the real records, not inventions."""

    def test_the_thread_settings_fixture_uses_only_real_keys(self):
        settings = json.loads(_codex_thread_settings(
            model="gpt-5.6-sol", effort="low"))["payload"]["thread_settings"]
        self.assertLessEqual(set(settings), _REAL_THREAD_SETTINGS_KEYS,
                             "the fixture invents a key Codex never emits here")
        self.assertIn("reasoning_effort", settings)

    def test_the_turn_context_fixture_uses_only_real_keys(self):
        payload = json.loads(_codex_turn_context(
            model="gpt-5.6-sol", effort="low"))["payload"]
        self.assertLessEqual(set(payload), _REAL_TURN_CONTEXT_KEYS,
                             "the fixture invents a key Codex never emits here")
        self.assertIn("effort", payload)

    def test_the_two_records_really_do_spell_effort_differently(self):
        """Stated as an assertion so that 'tidying' the two fixtures into one
        spelling reintroduces the defect loudly instead of silently."""
        self.assertNotIn("effort", _REAL_THREAD_SETTINGS_KEYS)
        self.assertNotIn("reasoning_effort", _REAL_TURN_CONTEXT_KEYS)


# ── 3. The Codex incremental resume ────────────────────────────────────────

class TestCodexIncrementalResume(_CodexParse, _TempDir):
    """A resumed parse starts after the `turn_context` records, so the effort in
    force has to be recovered from the prefix — otherwise the incremental path
    and a full re-read disagree, silently and only for appended turns."""

    def assert_paths_agree(self, records, skip_lines):
        full = self.parse(*records)
        resumed = self.parse(*records, skip_lines=skip_lines)
        self.assertTrue(resumed, "the resumed parse produced no turns at all")
        for message_id, turn in resumed.items():
            self.assertEqual(turn["reasoning_effort"],
                             full[message_id]["reasoning_effort"],
                             f"incremental and full parse disagree for {message_id}")
        return full, resumed

    def test_an_appended_turn_still_gets_the_effort_in_force(self):
        records = [
            _codex_session_meta(),                 # line 1
            _codex_turn_context(effort="max"),     # line 2
            _codex_token_count(1000),              # line 3
            _codex_token_count(2000),              # line 4 — appended since
        ]
        _full, resumed = self.assert_paths_agree(records, skip_lines=3)
        self.assertEqual(list(resumed), ["codex:019fcf22-aaaa:2000"])
        self.assertEqual(resumed["codex:019fcf22-aaaa:2000"]["reasoning_effort"],
                         "max")

    def test_the_resume_recovers_the_latest_of_several_contexts(self):
        records = [
            _codex_session_meta(),                 # line 1
            _codex_turn_context(effort="low"),     # line 2
            _codex_token_count(1000),              # line 3
            _codex_turn_context(effort="xhigh"),   # line 4
            _codex_token_count(2000),              # line 5
            _codex_token_count(3000),              # line 6 — appended since
        ]
        _full, resumed = self.assert_paths_agree(records, skip_lines=5)
        self.assertEqual(list(resumed), ["codex:019fcf22-aaaa:3000"])
        self.assertEqual(resumed["codex:019fcf22-aaaa:3000"]["reasoning_effort"],
                         "xhigh")

    def test_the_resume_honours_a_thread_settings_record_too(self):
        """The recovery scan and the main loop must share one rule; they once had
        a rule each and disagreed at every settings-driven change."""
        records = [
            _codex_session_meta(),                                  # 1
            _codex_turn_context(effort="low"),                      # 2
            _codex_token_count(1000),                               # 3
            _codex_thread_settings(effort="ultra"),                 # 4
            _codex_token_count(2000),                               # 5 appended
        ]
        _full, resumed = self.assert_paths_agree(records, skip_lines=4)
        self.assertEqual(resumed["codex:019fcf22-aaaa:2000"]["reasoning_effort"],
                         "ultra")

    def test_the_resume_recovers_the_model_alongside_the_effort(self):
        records = [
            _codex_session_meta(),
            _codex_turn_context(model="gpt-5.6-terra", effort="low"),
            _codex_token_count(1000),
            _codex_token_count(2000),
        ]
        resumed = self.parse(*records, skip_lines=3)
        self.assertEqual(resumed["codex:019fcf22-aaaa:2000"]["model"],
                         "gpt-5.6-terra")


# ── 4. insert_turns fills the blank, and only the blank ────────────────────

class TestFillOnConflict(_TempDir):
    """`stop_reason` is null on every partial streaming record, so a scan that
    lands mid-response stores ''. Plain 'first writer wins' would make that blank
    permanent — and would make the one-time re-read in scan() a no-op, since
    every existing row conflicts on message_id."""

    def setUp(self):
        super().setUp()
        self.conn = get_db(self.db_path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def stored(self, message_id="msg-s"):
        rows = self.conn.execute(
            "SELECT input_tokens, output_tokens, cache_read_tokens, "
            "       cache_creation_tokens, cache_creation_1h_tokens, "
            "       reasoning_output_tokens, reasoning_effort, stop_reason, "
            "       model, timestamp "
            "FROM turns WHERE message_id = ?", (message_id,)).fetchall()
        self.assertEqual(len(rows), 1, "the conflict must merge, never duplicate")
        return dict(rows[0])

    def test_a_blank_stop_reason_is_filled_by_the_completing_record(self):
        insert_turns(self.conn, [_turn("msg-s", stop_reason="", out=10)])
        insert_turns(self.conn, [_turn("msg-s", stop_reason="end_turn", out=2000)])
        self.conn.commit()
        self.assertEqual(self.stored()["stop_reason"], "end_turn")

    def test_a_blank_effort_is_filled_by_a_later_read(self):
        """This is what makes the one-time reasoning_effort backfill do anything:
        every row it re-reads already exists and conflicts."""
        insert_turns(self.conn, [_turn("msg-s", effort="", out=10)])
        insert_turns(self.conn, [_turn("msg-s", effort="xhigh", out=10)])
        self.conn.commit()
        self.assertEqual(self.stored()["reasoning_effort"], "xhigh")

    def test_a_second_non_blank_value_never_overwrites_the_first(self):
        """The same response can appear in two transcripts (a subagent's file and
        its parent's). The merge must stay a no-op for attribution."""
        insert_turns(self.conn, [_turn("msg-s", effort="high",
                                       stop_reason="tool_use", out=10)])
        insert_turns(self.conn, [_turn("msg-s", effort="low",
                                       stop_reason="max_tokens", out=10)])
        self.conn.commit()
        row = self.stored()
        self.assertEqual(row["reasoning_effort"], "high")
        self.assertEqual(row["stop_reason"], "tool_use")

    def test_a_later_blank_never_erases_a_stored_value(self):
        insert_turns(self.conn, [_turn("msg-s", effort="high",
                                       stop_reason="end_turn", out=10)])
        insert_turns(self.conn, [_turn("msg-s", effort="", stop_reason="", out=10)])
        self.conn.commit()
        row = self.stored()
        self.assertEqual(row["reasoning_effort"], "high")
        self.assertEqual(row["stop_reason"], "end_turn")

    def test_the_token_columns_still_merge_with_max_alongside_the_fill(self):
        """Invariant 1 is intact: only the four token columns (plus the 1-hour
        slice and Codex's reasoning subset) move, and only upward."""
        insert_turns(self.conn, [_turn(
            "msg-s", stop_reason="", inp=100, out=10, cache_read=70,
            cache_creation=30, cache_creation_1h=8, reasoning=4)])
        insert_turns(self.conn, [_turn(
            "msg-s", stop_reason="end_turn", inp=100, out=2000, cache_read=70,
            cache_creation=30, cache_creation_1h=12, reasoning=900)])
        # A replay of the partial record must not walk any of it back.
        insert_turns(self.conn, [_turn(
            "msg-s", stop_reason="", inp=1, out=5, cache_read=0,
            cache_creation=0, cache_creation_1h=0, reasoning=0)])
        self.conn.commit()
        row = self.stored()
        self.assertEqual(
            (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"],
             row["cache_creation_tokens"], row["cache_creation_1h_tokens"],
             row["reasoning_output_tokens"]),
            (100, 2000, 70, 30, 12, 900))
        self.assertEqual(row["stop_reason"], "end_turn")

    def test_the_merge_never_creates_a_second_row(self):
        for _ in range(5):
            insert_turns(self.conn, [_turn("msg-s", effort="high", out=10)])
        insert_turns(self.conn, [_turn("msg-s", effort="", out=10)])
        self.conn.commit()
        count = self.conn.execute(
            "SELECT COUNT(*) FROM turns WHERE message_id = 'msg-s'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_a_turn_without_a_message_id_is_never_merged(self):
        """The unique index is partial, so id-less turns keep their own rows and
        their own efforts."""
        insert_turns(self.conn, [_turn("", effort="high", out=10),
                                 _turn("", effort="low", out=10)])
        self.conn.commit()
        efforts = sorted(r[0] for r in self.conn.execute(
            "SELECT reasoning_effort FROM turns WHERE message_id = ''"))
        self.assertEqual(efforts, ["high", "low"])

    def test_a_caller_that_supplies_neither_column_stores_blanks(self):
        """Both are read with .get() defaults, so a parser or a test fixture that
        predates the columns still inserts rather than raising."""
        legacy = _turn("msg-legacy", out=10)
        del legacy["reasoning_effort"]
        del legacy["stop_reason"]
        insert_turns(self.conn, [legacy])
        self.conn.commit()
        row = self.stored("msg-legacy")
        self.assertEqual(row["reasoning_effort"], "")
        self.assertEqual(row["stop_reason"], "")


# ── 5. The schema migration ────────────────────────────────────────────────

# Every column `turns` carried immediately before reasoning_effort/stop_reason
# were added. Written out rather than derived so the test keeps describing the
# real "old database" even as the live schema moves on.
_PRE_EFFORT_TURNS_SCHEMA = """
    CREATE TABLE turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        timestamp TEXT,
        model TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cache_read_tokens INTEGER,
        cache_creation_tokens INTEGER,
        cache_creation_1h_tokens INTEGER DEFAULT 0,
        tool_name TEXT,
        cwd TEXT,
        message_id TEXT,
        is_subagent INTEGER DEFAULT 0,
        agent_id TEXT,
        source TEXT DEFAULT 'claude',
        reasoning_output_tokens INTEGER DEFAULT 0
    );
"""



class TestAPreEffortDatabaseIsRebuilt(_TempDir):
    """A database predating these columns is REBUILT, not migrated.

    This class used to assert the opposite: that `init_db` added the two columns
    and left every stored figure alone. There are no migrations any more
    (AGENTS.md invariant 6), so what has to hold instead is that the schema ends
    correct and the `''` = NOT RECORDED chain still works end to end — which is
    the half of the old contract that was ever about effort rather than about
    `ALTER TABLE`.

    The rows are gone, and that is the decision rather than a regression: this
    file is a cache, and the very next scan re-reads every transcript because a
    rebuilt database has an empty `processed_files`. What the old build needed a
    marker (`reasoning_effort_backfill_v1`) to force, a rebuild gets for free.
    """

    def _old_database(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(_PRE_EFFORT_TURNS_SCHEMA)
        # This fixture represents an owned intermediate development schema,
        # not one of the four complete released unmarked schemas. Give it the
        # durable product identity instead of weakening legacy recognition to
        # accept a partial `turns` table.
        conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        conn.execute(
            "INSERT INTO turns (session_id, timestamp, model, input_tokens, "
            " output_tokens, cache_read_tokens, cache_creation_tokens, "
            " cache_creation_1h_tokens, tool_name, message_id, is_subagent, "
            " agent_id, source, reasoning_output_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sess-old", utc_ts_on_local_day(0), "claude-opus-4-8",
             111, 222, 333, 444, 55, "Read", "msg-old", 0, None, "claude", 0))
        conn.commit()
        conn.close()

    def _init(self):
        conn = get_db(self.db_path)
        with contextlib.redirect_stderr(io.StringIO()) as announced:
            init_db(conn, self.db_path)
        conn.commit()
        conn.close()
        return announced.getvalue()

    def test_the_old_database_is_announced_and_rebuilt(self):
        self._old_database()
        self.assertIn("written by a different version", self._init())
        self.assertEqual(self.rows("SELECT * FROM turns"), [])

    def test_the_rebuilt_schema_carries_both_columns(self):
        self._old_database()
        self._init()
        conn = get_db(self.db_path)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(turns)")}
        conn.close()
        self.assertIn("reasoning_effort", columns)
        self.assertIn("stop_reason", columns)

    def test_a_row_written_without_them_is_blank_rather_than_a_named_level(self):
        """DEFAULT '' means NOT RECORDED. A turn whose transcript carried no
        effort is unknown, not `medium`."""
        self._old_database()
        self._init()
        conn = get_db(self.db_path)
        conn.execute(
            "INSERT INTO turns (session_id, timestamp, model, output_tokens, "
            " message_id) VALUES (?, ?, 'claude-opus-4-8', 222, 'msg-new')",
            ("sess-new", utc_ts_on_local_day(0)))
        conn.commit()
        conn.close()
        row = self.rows("SELECT reasoning_effort, stop_reason FROM turns")[0]
        self.assertEqual(row["reasoning_effort"], "")
        self.assertEqual(row["stop_reason"], "")

    def test_a_blank_row_is_reported_as_unknown_rather_than_dropped(self):
        """The end of the chain: a turn with no recorded effort must still
        appear in the breakdown, in its own bucket, carrying its real tokens."""
        self._old_database()
        self._init()
        conn = get_db(self.db_path)
        conn.execute(
            "INSERT INTO turns (session_id, timestamp, model, output_tokens, "
            " message_id) VALUES (?, ?, 'claude-opus-4-8', 222, 'msg-new')",
            ("sess-new", utc_ts_on_local_day(0)))
        conn.commit()
        effort = rollups.effort_by_day_model(conn)
        stops = rollups.stop_reason_by_day_model(conn)
        conn.close()
        self.assertEqual([r["effort"] for r in effort], [""])
        self.assertEqual(effort[0]["output"], 222)
        self.assertEqual([r["stop_reason"] for r in stops], [""])
        self.assertEqual(stops[0]["turns"], 1)

    def test_the_rebuild_does_not_repeat(self):
        """Once is a cost; every open would be a defect — and a second rebuild
        would throw away whatever the first scan had put back."""
        self._old_database()
        self.assertIn("written by a different version", self._init())
        conn = get_db(self.db_path)
        conn.execute(
            "INSERT INTO turns (session_id, timestamp, model, output_tokens, "
            " message_id) VALUES (?, ?, 'claude-opus-4-8', 222, 'msg-new')",
            ("sess-new", utc_ts_on_local_day(0)))
        conn.commit()
        conn.close()
        for _ in range(3):
            self.assertEqual(self._init(), "")
        rows = self.rows("SELECT reasoning_effort, output_tokens FROM turns")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["output_tokens"], 222)
        self.assertEqual(rows[0]["reasoning_effort"], "")


# ── 6 & 7. The rollups ─────────────────────────────────────────────────────

# One (day, effort) bucket AND one (day, stop_reason) bucket each deliberately
# hold TWO models 5x apart in price. A rollup grouped by (day, effort) alone
# still emits a `model` — SQLite returns an arbitrary row's value for a column
# outside the GROUP BY — so the bucket would be priced entirely at opus or
# entirely at haiku. Totals would still agree, because grouping coarser preserves
# sums; only the per-model split catches it.
#
# Both rollups need their own mixed bucket. A corpus that mixed models only
# inside an *effort* bucket left the stop-reason rollup's `model` key untested:
# dropping it from that GROUP BY changed no assertion, because every
# (day, stop_reason) bucket already held a single model.
_CORPUS = [
    # today, Claude, effort "high": two models in one bucket
    _turn("m1", model="claude-opus-4-8", effort="high", stop_reason="tool_use",
          inp=100, out=200, cache_read=10, cache_creation=20, cache_creation_1h=5),
    _turn("m2", model="claude-haiku-4-5", effort="high", stop_reason="end_turn",
          inp=7, out=11, cache_read=1, cache_creation=2),
    _turn("m3", model="claude-opus-4-8", effort="high", stop_reason="max_tokens",
          inp=1, out=999),
    # today, Claude, stop_reason "end_turn": the same trap for the OTHER rollup's
    # key. m2 above is haiku on this day and this stop reason; this is sonnet,
    # in a different effort bucket so the effort rollup is unaffected.
    _turn("m6", model="claude-sonnet-4-6", effort="medium", stop_reason="end_turn",
          inp=9, out=13, cache_read=3, cache_creation=4),
    # today, Claude, effort never recorded — its own bucket, not folded away
    _turn("m4", model="claude-opus-4-8", effort="", stop_reason="",
          inp=3, out=5),
    # yesterday, Claude
    _turn("m5", model="claude-sonnet-4-6", effort="medium", stop_reason="end_turn",
          days_ago=1, inp=50, out=60, cache_read=5, cache_creation=6,
          cache_creation_1h=6, session_id="sess-2"),
    # today, Codex — reasoning is a SUBSET of output, and stop_reason is always ''
    _turn("c1", model="gpt-5.6-sol", effort="xhigh", stop_reason="",
          source="codex", inp=40, out=80, reasoning=70, session_id="sess-3"),
    _turn("c2", model="gpt-5.6-sol", effort="low", stop_reason="",
          source="codex", inp=4, out=8, reasoning=6, session_id="sess-3"),
]

_TOTAL_FIELDS = ("input", "output", "cache_read", "cache_creation",
                 "cache_creation_1h", "reasoning", "turns")


def _totals(rows, fields=_TOTAL_FIELDS):
    return {field: sum(r[field] for r in rows) for field in fields}


def _cost(rows):
    """Per-row cost, the only way a mixed-model bucket can be priced correctly."""
    return sum(calc_cost(r["model"], r["input"], r["output"], r["cache_read"],
                         r["cache_creation"], r["cache_creation_1h"])
               for r in rows)


class TestRollupTotals(_TempDir):
    def setUp(self):
        super().setUp()
        self.conn = get_db(self.db_path)
        init_db(self.conn)
        insert_turns(self.conn, _CORPUS)
        self.conn.commit()
        self.daily = rollups.daily_by_model(self.conn)
        self.effort = rollups.effort_by_day_model(self.conn)
        self.stops = rollups.stop_reason_by_day_model(self.conn)

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def test_the_fixture_really_puts_two_models_in_one_effort_bucket(self):
        """Guards the tests below: a single-model corpus would pass even with the
        mis-grouping bug present."""
        bucket = {r["model"] for r in self.effort
                  if r["effort"] == "high" and r["source"] == "claude"}
        self.assertEqual(bucket, {"claude-opus-4-8", "claude-haiku-4-5"})

    def test_effort_rollup_sums_to_the_daily_rollup(self):
        self.assertEqual(_totals(self.effort), _totals(self.daily))
        self.assertEqual(_totals(self.daily)["turns"], len(_CORPUS))

    def test_stop_reason_rollup_sums_to_the_daily_rollup(self):
        self.assertEqual(_totals(self.stops, ("output", "turns")),
                         _totals(self.daily, ("output", "turns")))

    def test_both_rollups_agree_with_daily_per_day_source_and_model(self):
        """A stronger check than one grand total: a row lost or mis-keyed in one
        bucket and gained in another would still balance overall."""
        def keyed(rows, fields):
            out = {}
            for row in rows:
                key = (row["day"], row["source"], row["model"])
                bucket = out.setdefault(key, dict.fromkeys(fields, 0))
                for field in fields:
                    bucket[field] += row[field]
            return out

        self.assertEqual(keyed(self.effort, _TOTAL_FIELDS),
                         keyed(self.daily, _TOTAL_FIELDS))
        self.assertEqual(keyed(self.stops, ("output", "turns")),
                         keyed(self.daily, ("output", "turns")))

    def test_a_mixed_model_bucket_stays_one_row_per_model(self):
        """The whole reason `model` is part of the key. Two rows, each with its
        own tokens — not one row wearing an arbitrary model's name."""
        high = [r for r in self.effort
                if r["effort"] == "high" and r["source"] == "claude"]
        self.assertEqual(len(high), 2)
        by_model = {r["model"]: r for r in high}
        self.assertEqual(by_model["claude-opus-4-8"]["output"], 200 + 999)
        self.assertEqual(by_model["claude-opus-4-8"]["turns"], 2)
        self.assertEqual(by_model["claude-haiku-4-5"]["output"], 11)
        self.assertEqual(by_model["claude-haiku-4-5"]["turns"], 1)

    def test_the_effort_rollup_prices_to_the_same_dollar_as_daily(self):
        """Costing is per row, and each row knows its own model. A bucket that
        merged the two models would price 5x out here while every total above
        still balanced."""
        self.assertAlmostEqual(_cost(self.effort), _cost(self.daily), places=10)
        self.assertGreater(_cost(self.daily), 0)

    def test_the_fixture_really_puts_two_models_in_one_stop_reason_bucket(self):
        """The twin of the guard above, for the other rollup's key. Without a
        (day, stop_reason) bucket that mixes two models, dropping `model` from
        that GROUP BY changes nothing any assertion can see."""
        buckets = {}
        for row in self.stops:
            buckets.setdefault((row["day"], row["source"], row["stop_reason"]),
                               set()).add(row["model"])
        self.assertTrue(any(len(models) > 1 for models in buckets.values()),
                        "no (day, stop_reason) bucket mixes two models")

    def test_a_stop_reason_bucket_also_stays_one_row_per_model(self):
        rows = [r for r in self.stops if r["stop_reason"] == "end_turn"]
        self.assertEqual({r["model"] for r in rows},
                         {"claude-haiku-4-5", "claude-sonnet-4-6"})
        # The mixed bucket: one day's `end_turn` holds haiku and sonnet, 5x apart
        # in price. Grouped without `model` it collapses to a single row wearing
        # an arbitrary one of the two — so assert the split survives, and that
        # each row carries its own tokens rather than the bucket's sum.
        by_day = {}
        for row in rows:
            by_day.setdefault(row["day"], {})[row["model"]] = row
        mixed = [models for models in by_day.values() if len(models) > 1]
        self.assertEqual(len(mixed), 1,
                         "the mixed-model stop_reason bucket collapsed to one row")
        self.assertEqual(mixed[0]["claude-haiku-4-5"]["output"], 11)
        self.assertEqual(mixed[0]["claude-haiku-4-5"]["turns"], 1)
        self.assertEqual(mixed[0]["claude-sonnet-4-6"]["output"], 13)
        self.assertEqual(mixed[0]["claude-sonnet-4-6"]["turns"], 1)

    def test_an_unrecorded_effort_is_kept_as_its_own_bucket(self):
        """'' is NOT a level. It must not be dropped and must not be merged into
        a named one, or the breakdown stops agreeing with the totals beside it."""
        unknown = [r for r in self.effort if r["effort"] == ""]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["model"], "claude-opus-4-8")
        self.assertEqual((unknown[0]["input"], unknown[0]["output"],
                          unknown[0]["turns"]), (3, 5, 1))
        named = {r["effort"] for r in self.effort} - {""}
        self.assertEqual(named, {"high", "medium", "xhigh", "low"})

    def test_codex_rows_land_in_the_blank_stop_reason_bucket(self):
        """Codex records no stop reason, so it is reported as unknown rather than
        as 'Codex never truncates'."""
        codex = [r for r in self.stops if r["source"] == "codex"]
        self.assertTrue(codex)
        self.assertEqual({r["stop_reason"] for r in codex}, {""})
        self.assertEqual(sum(r["turns"] for r in codex), 2)

    def test_codex_reasoning_tokens_travel_with_the_effort_rollup(self):
        """Reasoning is a SUBSET of output — carried for display, never added to
        it and never priced separately."""
        codex = [r for r in self.effort if r["source"] == "codex"]
        self.assertEqual(sum(r["reasoning"] for r in codex), 76)
        self.assertEqual(sum(r["output"] for r in codex), 88)
        self.assertEqual({r["effort"] for r in codex}, {"xhigh", "low"})

    def test_the_source_filter_gates_both_new_rollups(self):
        """Exactly one source is on screen at a time, and that is a correctness
        rule: a Codex plan has no published price, so mixing the two adds a real
        number to an imaginary one."""
        for source in ("claude", "codex"):
            effort = rollups.effort_by_day_model(self.conn, source)
            stops = rollups.stop_reason_by_day_model(self.conn, source)
            daily = rollups.daily_by_model(self.conn, source)
            self.assertEqual({r["source"] for r in effort}, {source})
            self.assertEqual({r["source"] for r in stops}, {source})
            self.assertEqual(_totals(effort), _totals(daily))
            self.assertEqual(_totals(stops, ("output", "turns")),
                             _totals(daily, ("output", "turns")))
        self.assertLess(
            _totals(rollups.effort_by_day_model(self.conn, "codex"))["turns"],
            _totals(self.effort)["turns"])

    def test_a_turn_with_no_recorded_effort_at_all_still_appears(self):
        """A database in which nothing was ever recorded must still balance —
        the buckets are all '' and they still sum to the daily rollup."""
        conn = get_db(Path(self.tmpdir) / "blank.db")
        init_db(conn)
        insert_turns(conn, [_turn("b1", out=10), _turn("b2", out=20)])
        conn.commit()
        effort = rollups.effort_by_day_model(conn)
        daily = rollups.daily_by_model(conn)
        conn.close()
        self.assertEqual([r["effort"] for r in effort], [""])
        self.assertEqual(_totals(effort), _totals(daily))


# ── The whole path: transcript on disk -> DB -> payload ────────────────────

class TestEndToEnd(_TempDir):
    def payload(self, source=None):
        return get_dashboard_data(db_path=self.db_path, source=source)

    def test_a_claude_scan_carries_both_columns_into_the_payload(self):
        self.write_transcript([
            _claude_record(message_id="e1", model="claude-opus-4-8",
                           effort="xhigh", stop_reason="tool_use",
                           input_tokens=100, output_tokens=200),
            _claude_record(message_id="e2", model="claude-haiku-4-5",
                           effort="high", stop_reason="max_tokens",
                           input_tokens=5, output_tokens=7),
        ])
        scan(projects_dir=Path(self.tmpdir) / "projects", db_path=self.db_path,
             verbose=False)

        stored = {r["message_id"]: r for r in self.rows(
            "SELECT message_id, reasoning_effort, stop_reason FROM turns")}
        self.assertEqual(stored["e1"]["reasoning_effort"], "xhigh")
        self.assertEqual(stored["e1"]["stop_reason"], "tool_use")
        self.assertEqual(stored["e2"]["reasoning_effort"], "high")
        self.assertEqual(stored["e2"]["stop_reason"], "max_tokens")

        payload = self.payload()
        self.assertIn("effort_by_day_model", payload)
        self.assertIn("stop_reason_by_day_model", payload)
        self.assertEqual({r["effort"] for r in payload["effort_by_day_model"]},
                         {"xhigh", "high"})
        self.assertEqual(
            {r["stop_reason"] for r in payload["stop_reason_by_day_model"]},
            {"tool_use", "max_tokens"})
        self.assertEqual(_totals(payload["effort_by_day_model"]),
                         _totals(payload["daily_by_model"]))
        self.assertEqual(
            _totals(payload["stop_reason_by_day_model"], ("output", "turns")),
            _totals(payload["daily_by_model"], ("output", "turns")))

    def test_a_codex_scan_carries_the_effort_and_no_stop_reason(self):
        self.write_transcript(
            [_codex_session_meta(),
             _codex_turn_context(model="gpt-5.6-sol", effort="max"),
             _codex_token_count(1000, inp=100, out=40, reasoning=30)],
            name="rollout-1.jsonl", subdir="sessions/2026/08/07")
        scan(projects_dir=Path(self.tmpdir) / "sessions", db_path=self.db_path,
             verbose=False)

        row = self.rows("SELECT reasoning_effort, stop_reason, source FROM turns")[0]
        self.assertEqual(row["reasoning_effort"], "max")
        self.assertEqual(row["stop_reason"], "")
        self.assertEqual(row["source"], "codex")

        payload = self.payload(source="codex")
        self.assertEqual([r["effort"] for r in payload["effort_by_day_model"]],
                         ["max"])
        self.assertEqual(
            [r["stop_reason"] for r in payload["stop_reason_by_day_model"]], [""])

    def test_a_rescan_that_reaches_the_completing_record_fills_the_blank(self):
        """The real shape of the fill-on-conflict rule: the first scan landed
        between the streaming records, so it stored a blank stop reason and no
        effort. Without the fill, both blanks would be permanent."""
        path = self.write_transcript(
            [_claude_record(message_id="s1", output_tokens=10, stop_reason=None)],
            mtime=1_000_000)
        scan(projects_dir=Path(self.tmpdir) / "projects", db_path=self.db_path,
             verbose=False)
        first = self.rows(
            "SELECT reasoning_effort, stop_reason, output_tokens FROM turns")[0]
        self.assertEqual(first["stop_reason"], "")
        self.assertEqual(first["reasoning_effort"], "")

        with open(path, "a", encoding="utf-8") as handle:
            handle.write(_claude_record(message_id="s1", output_tokens=2000,
                                        effort="xhigh",
                                        stop_reason="end_turn") + NL)
        os.utime(path, (2_000_000, 2_000_000))
        scan(projects_dir=Path(self.tmpdir) / "projects", db_path=self.db_path,
             verbose=False)

        rows = self.rows(
            "SELECT reasoning_effort, stop_reason, output_tokens FROM turns")
        self.assertEqual(len(rows), 1, "the rescan must merge, not duplicate")
        self.assertEqual(rows[0]["stop_reason"], "end_turn")
        self.assertEqual(rows[0]["reasoning_effort"], "xhigh")
        self.assertEqual(rows[0]["output_tokens"], 2000)

    def test_an_incremental_codex_scan_agrees_with_a_full_one(self):
        """The incremental path resumes after the `turn_context` records, so this
        is where the two paths would silently disagree about effort."""
        path = self.write_transcript(
            [_codex_session_meta(),
             _codex_turn_context(model="gpt-5.6-sol", effort="xhigh"),
             _codex_token_count(1000, inp=100, out=40)],
            name="rollout-1.jsonl", subdir="sessions/2026/08/07",
            mtime=1_000_000)
        scan(projects_dir=Path(self.tmpdir) / "sessions", db_path=self.db_path,
             verbose=False)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(_codex_token_count(2000, inp=10, out=4) + NL)
        os.utime(path, (2_000_000, 2_000_000))
        scan(projects_dir=Path(self.tmpdir) / "sessions", db_path=self.db_path,
             verbose=False)
        incremental = {r["message_id"]: r["reasoning_effort"] for r in self.rows(
            "SELECT message_id, reasoning_effort FROM turns")}

        one_shot_db = Path(self.tmpdir) / "one-shot.db"
        scan(projects_dir=Path(self.tmpdir) / "sessions", db_path=one_shot_db,
             verbose=False)
        conn = sqlite3.connect(one_shot_db)
        conn.row_factory = sqlite3.Row
        full = {r["message_id"]: r["reasoning_effort"]
                for r in conn.execute("SELECT message_id, reasoning_effort FROM turns")}
        conn.close()

        self.assertEqual(len(incremental), 2)
        self.assertEqual(incremental, full)
        self.assertEqual(set(incremental.values()), {"xhigh"})

    def test_a_hostile_effort_value_cannot_reach_the_browser_raw(self):
        """Transcript metadata is attacker-influenced and both columns cross the
        JSON API."""
        self.write_transcript([_claude_record(
            message_id="h1", effort="hi\x1b]52;c;pwn\x07gh",
            stop_reason="end‮trun")])
        scan(projects_dir=Path(self.tmpdir) / "projects", db_path=self.db_path,
             verbose=False)
        payload = self.payload()
        efforts = [r["effort"] for r in payload["effort_by_day_model"]]
        stops = [r["stop_reason"] for r in payload["stop_reason_by_day_model"]]
        self.assertTrue(efforts and stops)
        for value in efforts + stops:
            self.assertNotIn("\x1b", value)
            self.assertNotIn("‮", value)


if __name__ == "__main__":
    unittest.main()
