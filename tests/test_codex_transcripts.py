"""Tests for reading Codex rollout transcripts.

Codex's format shares nothing with Claude Code's, and four of its differences
are silent costing errors rather than parse failures — the kind that produce a
plausible number rather than a traceback:

* `cached_input_tokens` and `cache_write_input_tokens` are non-overlapping
  SUBSETS of `input_tokens` (Anthropic's are disjoint buckets), so leaving either
  detail in ordinary input bills it twice;
* `reasoning_output_tokens` is a SUBSET of `output_tokens`, so adding it is a
  second double-count;
* repeated `token_count` events can describe the same response without a
  message id, so they must be deduplicated before aggregation.

Each of those gets a test that fails loudly if the normalisation is undone. The
suite also pins the two properties the whole design rests on: that a rescan is
idempotent, and that adding Codex changes nothing about Claude rows.
"""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import codex_transcripts
import scanner
from codex_transcripts import looks_like_codex, parse_jsonl_file
from pricing import calc_cost
from tests.test_dashboard_js import emit, requires_node, run_js
from tests.legacy_database import create_schema as create_legacy_schema


def _rec(rtype, payload, timestamp="2026-08-05T10:00:00.000Z"):
    return json.dumps({"timestamp": timestamp, "type": rtype, "payload": payload})


def _session_meta(thread="019fcf22-aaaa", root=None, cwd="/home/u/proj",
                  branch="main", subagent=False, parent=None,
                  timestamp="2026-08-05T09:59:00.000Z", agent_other="guardian"):
    sub = {"other": agent_other}
    if parent:
        # What a spawned thread records about the thread that spawned it. Its
        # usage history is replayed into this file at the spawn instant.
        sub["thread_spawn"] = {"parent_thread_id": parent,
                               "agent_nickname": "reviewer"}
    payload = {
        "id": thread,
        "session_id": root or thread,
        "cwd": cwd,
        "originator": "codex_vscode",
        "cli_version": "0.146.0",
        "source": {"subagent": sub} if subagent else "vscode",
        "thread_source": "subagent" if subagent else "user",
        "model_provider": "openai",
        "git": {"commit_hash": "abc123", "branch": branch,
                "repository_url": "git@example:me/proj.git"},
    }
    return _rec("session_meta", payload, timestamp)


def _turn_context(model, timestamp="2026-08-05T10:00:00.000Z", effort="max"):
    return _rec("turn_context", {"turn_id": "t-1", "cwd": "/home/u/proj",
                                 "model": model, "effort": effort}, timestamp)


def _thread_settings(model, timestamp="2026-08-05T10:00:00.500Z"):
    """Codex also changes the model with this, not only with turn_context."""
    return _rec("event_msg", {"type": "thread_settings_applied",
                              "thread_settings": {"model": model,
                                                  "service_tier": "default"}}, timestamp)


def _token_count(cum_total, inp, out, cached=0, cache_write=0, reasoning=0,
                 timestamp="2026-08-05T10:00:01.000Z", percent=None,
                 resets_at=1786190420, window=10080, limit_id="codex",
                 reached=None):
    """One API response. `cum_total` is the running per-thread cumulative that
    doubles as the response's identity."""
    payload = {
        "type": "token_count",
        "info": {
            "last_token_usage": {
                "input_tokens": inp, "cached_input_tokens": cached,
                "cache_write_input_tokens": cache_write, "output_tokens": out,
                "reasoning_output_tokens": reasoning, "total_tokens": inp + out,
            },
            "total_token_usage": {
                "input_tokens": 0, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "output_tokens": 0,
                "reasoning_output_tokens": 0, "total_tokens": cum_total,
            },
            "model_context_window": 258400,
        },
    }
    if percent is not None:
        payload["rate_limits"] = {
            "limit_id": limit_id, "limit_name": None,
            "primary": {"used_percent": percent, "window_minutes": window,
                        "resets_at": resets_at},
            "secondary": None,
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
            # rate_limit_reached_type names the tripped limit and can remain
            # null while a window fills. Use a null fixture to cover that
            # ordinary shape.
            "plan_type": "pro", "rate_limit_reached_type": reached,
        }
    return _rec("event_msg", payload, timestamp)


class CodexFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "sessions" / "2026" / "08" / "05"
        self.root.mkdir(parents=True)
        self.db = Path(self.tmp) / "usage.db"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, lines, thread="019fcf22-aaaa", mtime=1_000_000):
        path = self.root / f"rollout-2026-08-05T10-00-00-{thread}.jsonl"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.utime(path, (mtime, mtime))
        return path

    def scan(self):
        return scanner.scan(projects_dir=Path(self.tmp) / "sessions",
                            db_path=self.db, verbose=False)

    def rows(self, sql, *args):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args)]
        finally:
            conn.close()


class TestFormatSniffing(CodexFixture):
    """Which parser a file gets is decided by its content, not its directory —
    `--projects-dir` is 'also look here' and may point anywhere."""

    def test_a_codex_rollout_is_recognised(self):
        path = self.write([_session_meta(), _turn_context("gpt-5.6-sol")])
        self.assertTrue(looks_like_codex(path))

    def test_a_claude_transcript_is_not(self):
        path = self.root / "claude.jsonl"
        path.write_text(json.dumps({
            "type": "assistant", "sessionId": "s1", "cwd": "/x",
            "timestamp": "2026-08-05T10:00:00Z",
            "message": {"id": "m1", "model": "claude-opus-5",
                        "usage": {"input_tokens": 1, "output_tokens": 1}},
        }) + "\n", encoding="utf-8")
        self.assertFalse(looks_like_codex(path))

    def test_junk_is_not_codex_and_does_not_raise(self):
        for body in ("", "\n\n", "not json\n", "[1,2,3]\n", '{"a":1}\n'):
            with self.subTest(body=body):
                path = self.root / "junk.jsonl"
                path.write_text(body, encoding="utf-8")
                self.assertFalse(looks_like_codex(path))

    def test_a_missing_file_is_not_codex(self):
        self.assertFalse(looks_like_codex(self.root / "nope.jsonl"))


class TestTokenNormalisation(CodexFixture):
    """The subset/superset traps, each worth a silent overcharge."""

    def test_cached_input_is_removed_from_the_input_column(self):
        """Anthropic's buckets are disjoint; Codex's cached is INSIDE its input.
        Leaving it would bill the cached portion at the full input rate too."""
        _, turns, _, _, _ = parse_jsonl_file(self.write([
            _session_meta(), _turn_context("gpt-5.6-sol"),
            _token_count(1000, inp=1000, out=50, cached=900),
        ]))
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["input_tokens"], 100, "uncached remainder")
        self.assertEqual(turns[0]["cache_read_tokens"], 900)

    def test_the_three_columns_add_up_to_the_reported_input(self):
        """Invented buckets: 2,000 ordinary + 20,000 cached + 3,000 writes."""
        _, turns, _, _, _ = parse_jsonl_file(self.write([
            _session_meta(), _turn_context("gpt-5.6-sol"),
            _token_count(1000, inp=25000, out=400, cached=20000,
                         cache_write=3000),
        ]))
        t = turns[0]
        self.assertEqual(
            t["input_tokens"] + t["cache_read_tokens"]
            + t["cache_creation_tokens"],
            25000,
        )

    def test_cache_writes_are_removed_from_ordinary_input_and_billed_once(self):
        _, turns, _, _, _ = parse_jsonl_file(self.write([
            _session_meta(), _turn_context("gpt-5.6-sol"),
            _token_count(1000, inp=1000, out=0, cached=600,
                         cache_write=300),
        ]))
        turn = turns[0]
        self.assertEqual(
            (turn["input_tokens"], turn["cache_read_tokens"],
             turn["cache_creation_tokens"]),
            (100, 600, 300),
        )
        self.assertAlmostEqual(
            calc_cost("gpt-5.6-sol", turn["input_tokens"], 0,
                      turn["cache_read_tokens"],
                      turn["cache_creation_tokens"]),
            (100 * 4.00 + 600 * 0.40 + 300 * 5.00) / 1_000_000,
        )

    def test_corrupt_cache_details_are_capped_to_the_reported_total(self):
        usage = codex_transcripts._usage_from({
            "input_tokens": 100,
            "cached_input_tokens": 80,
            "cache_write_input_tokens": 999,
            "output_tokens": 0,
        })
        self.assertEqual(
            (usage["input_tokens"], usage["cache_read_tokens"],
             usage["cache_creation_tokens"]),
            (0, 80, 20),
        )

    def test_partitioning_does_not_invent_a_long_context_request(self):
        usage = codex_transcripts._usage_from({
            "input_tokens": 272_000,
            "cached_input_tokens": 271_999,
            "cache_write_input_tokens": 1,
            "output_tokens": 0,
        })
        from pricing import is_long_context
        self.assertFalse(is_long_context(
            "gpt-5.6-sol", usage["input_tokens"],
            usage["cache_read_tokens"], usage["cache_creation_tokens"],
        ))

    def test_a_cached_count_larger_than_its_input_cannot_credit_tokens_back(self):
        """Two independent integers from a transcript; nothing guarantees order."""
        _, turns, _, _, _ = parse_jsonl_file(self.write([
            _session_meta(), _turn_context("gpt-5.6-sol"),
            _token_count(1000, inp=100, out=10, cached=999_999),
        ]))
        self.assertEqual(turns[0]["input_tokens"], 0)
        self.assertEqual(turns[0]["cache_read_tokens"], 100)

    def test_reasoning_is_recorded_but_never_billed(self):
        """It is a subset of output, so it is already inside the costed figure."""
        _, turns, _, _, _ = parse_jsonl_file(self.write([
            _session_meta(), _turn_context("gpt-5.6-sol"),
            _token_count(1000, inp=100, out=500, reasoning=300),
        ]))
        t = turns[0]
        self.assertEqual(t["reasoning_output_tokens"], 300)
        self.assertEqual(t["output_tokens"], 500, "not 800")
        priced = calc_cost("gpt-5.6-sol", t["input_tokens"], t["output_tokens"],
                           t["cache_read_tokens"], t["cache_creation_tokens"])
        # Whatever the rate table says, the cost must not depend on a field that
        # is already inside output_tokens.
        self.assertEqual(priced, calc_cost("gpt-5.6-sol", 100, 500, 0, 0))

    def test_reasoning_cannot_exceed_the_output_it_is_part_of(self):
        _, turns, _, _, _ = parse_jsonl_file(self.write([
            _session_meta(), _turn_context("gpt-5.6-sol"),
            _token_count(1000, inp=10, out=5, reasoning=9_999),
        ]))
        self.assertLessEqual(turns[0]["reasoning_output_tokens"],
                             turns[0]["output_tokens"])

    def test_a_turn_with_no_billable_tokens_is_dropped(self):
        """The all-zero phantom that accompanies a duplicate emission."""
        _, turns, _, _, _ = parse_jsonl_file(self.write([
            _session_meta(), _turn_context("gpt-5.6-sol"),
            _token_count(1000, inp=0, out=0, cached=0),
        ]))
        self.assertEqual(turns, [])


class TestResponseIdentity(CodexFixture):
    """Codex has no message id, so one is synthesised from the per-thread
    cumulative counter. This is what keeps invariant 1 alive."""

    def test_repeated_emissions_of_one_response_collapse_to_one_turn(self):
        """Repeated token_count records must not multiply a turn's usage."""
        _, turns, _, _, _ = parse_jsonl_file(self.write([
            _session_meta(), _turn_context("gpt-5.6-sol"),
            _token_count(1000, inp=100, out=10),
            _token_count(1000, inp=100, out=10),   # same cumulative = same response
            _token_count(1000, inp=100, out=10),
        ]))
        self.assertEqual(len(turns), 1)

    def test_distinct_responses_stay_distinct(self):
        _, turns, _, _, _ = parse_jsonl_file(self.write([
            _session_meta(), _turn_context("gpt-5.6-sol"),
            _token_count(1000, inp=100, out=10),
            _token_count(2000, inp=200, out=20),
        ]))
        self.assertEqual(len(turns), 2)

    def test_the_id_is_scoped_to_its_lineage(self):
        """Two unrelated lineages legitimately reach the same cumulative; they
        are not the same response, and a global key would merge them.

        Scoped to the lineage rather than to the writing thread because the
        accumulator itself is per-lineage — see TestSubagentReplayIsNotNewUsage.
        These two threads are each their own root, so they stay apart."""
        a = parse_jsonl_file(self.write(
            [_session_meta(thread="t-a"), _turn_context("gpt-5.6-sol"),
             _token_count(1000, inp=100, out=10)], thread="t-a"))[1]
        b = parse_jsonl_file(self.write(
            [_session_meta(thread="t-b"), _turn_context("gpt-5.6-sol"),
             _token_count(1000, inp=100, out=10)], thread="t-b"))[1]
        self.assertNotEqual(a[0]["message_id"], b[0]["message_id"])


class TestHeaderlessFallbackIdentity(CodexFixture):
    """Headerless rollouts still need a stable identity per source path."""

    def test_standard_rollout_name_keeps_its_historical_uuid_identity(self):
        path = Path(self.tmp) / (
            "rollout-2026-08-05T10-00-00-019fcf22aaaa.jsonl"
        )
        self.assertEqual(
            codex_transcripts.thread_id_from_path(path),
            "019fcf22aaaa",
            "an upgrade would mint new turn IDs for an already indexed file",
        )

    def test_same_basename_in_distinct_roots_keeps_both_turns_and_rescans(self):
        name = "headerless.jsonl"
        first_root = Path(self.tmp) / "archive-a"
        second_root = Path(self.tmp) / "archive-b"
        first_root.mkdir()
        second_root.mkdir()
        first = first_root / name
        second = second_root / name
        first.write_text("\n".join([
            _turn_context("gpt-5.6-sol"),
            _token_count(30, inp=10, out=20),
        ]) + "\n", encoding="utf-8")
        second.write_text("\n".join([
            _turn_context("gpt-5.6-sol"),
            _token_count(30, inp=20, out=10),
        ]) + "\n", encoding="utf-8")
        os.utime(first, (1_000_000, 1_000_000))
        os.utime(second, (1_000_000, 1_000_000))

        scanner.scan(projects_dirs=[first_root, second_root], db_path=self.db,
                     verbose=False)
        before = self.rows(
            "SELECT message_id, session_id, input_tokens, output_tokens "
            "FROM turns ORDER BY message_id")
        self.assertEqual(len(before), 2,
                         "distinct headerless files were merged by basename")
        self.assertEqual({(row["input_tokens"], row["output_tokens"])
                          for row in before}, {(10, 20), (20, 10)})

        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE processed_files SET mtime = -1, lines = 0")
        conn.commit()
        conn.close()
        scanner.scan(projects_dirs=[first_root, second_root], db_path=self.db,
                     verbose=False)

        self.assertEqual(
            self.rows("SELECT message_id, session_id, input_tokens, output_tokens "
                      "FROM turns ORDER BY message_id"), before,
            "a forced rescan must not mint new fallback identities")

    def test_legacy_basename_ids_cannot_coexist_with_path_digest_ids(self):
        """The cursor-schema upgrade is the identity migration boundary.

        Older builds keyed an arbitrary headerless file by its basename. The
        persisted file-identity columns were added in the same release as the
        path digest, so an older database must rebuild before any transcript is
        parsed. That removes the legacy turns instead of appending digest-keyed
        duplicates beside them.
        """
        path = self.root / "headerless.jsonl"
        path.write_text("\n".join([
            _turn_context("gpt-5.6-sol"),
            _token_count(30, inp=10, out=20),
        ]) + "\n", encoding="utf-8")
        os.utime(path, (1_000_000, 1_000_000))
        self.scan()
        current_turns = self.rows(
            "SELECT message_id, session_id, input_tokens, output_tokens "
            "FROM turns")
        self.assertEqual(len(current_turns), 1)
        self.assertIn("path:", current_turns[0]["message_id"])

        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "UPDATE turns SET message_id = ?, session_id = ?",
                ("codex:headerless:30", "headerless"),
            )
            conn.execute(
                "UPDATE sessions SET session_id = ?", ("headerless",)
            )
            # This is the released pre-upgrade cursor shape. `init_db` must
            # rebuild it before scanner SQL or parser identity can run.
            conn.execute("ALTER TABLE processed_files DROP COLUMN st_dev")
            conn.execute("ALTER TABLE processed_files DROP COLUMN st_ino")
            conn.commit()
        finally:
            conn.close()

        with contextlib.redirect_stderr(io.StringIO()):
            self.scan()

        self.assertEqual(
            self.rows(
                "SELECT message_id, session_id, input_tokens, output_tokens "
                "FROM turns"
            ),
            current_turns,
            "schema upgrade left legacy and digest fallback identities together",
        )
        self.assertEqual(
            len(self.rows("SELECT session_id FROM sessions")), 1,
            "the legacy fallback session survived the identity-schema rebuild",
        )


class TestSubagentReplayIsNotNewUsage(CodexFixture):
    """A spawned thread replays its parent's whole usage history verbatim.

    A spawned rollout can replay its parent's token_count records and then
    continue the same accumulator. The replayed prefix must be counted once
    for the lineage, with only the child's new response adding usage.

    A cumulative counter belongs to a lineage. Keying replayed parent records
    by their writing thread would count the same response again in each
    descendant."""

    ROOT = "t-root"
    CHILD = "t-child"

    def _lineage(self):
        """A parent with two responses and a child that replays both.

        The accumulator's own arithmetic: 900+100 -> 1000, +1100+100 -> 2200,
        and the child's own response +450+50 -> 2700. Three responses,
        2,450 input and 250 output for the whole lineage.
        """
        parent = self.write([
            _session_meta(thread=self.ROOT, root=self.ROOT,
                          timestamp="2026-08-05T09:00:00.000Z"),
            _turn_context("gpt-5.6-sol", timestamp="2026-08-05T09:00:01.000Z"),
            _token_count(1000, inp=900, out=100,
                         timestamp="2026-08-05T09:00:02.000Z"),
            _token_count(2200, inp=1100, out=100,
                         timestamp="2026-08-05T09:05:00.000Z"),
        ], thread=self.ROOT)
        child = self.write([
            _session_meta(thread=self.CHILD, root=self.ROOT, subagent=True,
                          parent=self.ROOT, timestamp="2026-08-05T10:00:00.000Z"),
            _turn_context("gpt-5.6-sol", timestamp="2026-08-05T10:00:00.001Z"),
            # The replay: the parent's two responses, both stamped with the
            # spawn instant rather than the instant they actually ran.
            _token_count(1000, inp=900, out=100,
                         timestamp="2026-08-05T10:00:00.002Z"),
            _token_count(2200, inp=1100, out=100,
                         timestamp="2026-08-05T10:00:00.002Z"),
            # ... and then the child's own first response, continuing the
            # accumulator from where the replayed prefix left it.
            _token_count(2700, inp=450, out=50,
                         timestamp="2026-08-05T10:00:30.000Z"),
        ], thread=self.CHILD)
        return parent, child

    def test_a_replayed_response_keeps_the_identity_it_already_had(self):
        """The parser-level property: the child must not mint new ids for the
        parent's responses, or nothing downstream can tell them apart."""
        parent_path, child_path = self._lineage()
        parent_ids = {t["message_id"] for t in parse_jsonl_file(parent_path)[1]}
        child_ids = {t["message_id"] for t in parse_jsonl_file(child_path)[1]}
        self.assertEqual(len(parent_ids), 2)
        self.assertEqual(len(child_ids), 3)
        self.assertEqual(
            len(parent_ids | child_ids), 3,
            "the replayed responses were minted a second identity")
        self.assertTrue(
            parent_ids < child_ids,
            "the child's replayed prefix must reuse the parent's ids")

    def test_the_lineage_is_billed_once_through_a_scan(self):
        """The money. The accumulator says this lineage consumed 2,450 input
        and 250 output across three responses; counting the replay as new usage
        reports five responses, 4,450 and 450."""
        self._lineage()
        self.scan()
        rows = self.rows("SELECT input_tokens, output_tokens FROM turns")
        self.assertEqual(len(rows), 3, "the replayed responses were billed again")
        self.assertEqual(sum(r["input_tokens"] for r in rows), 2450)
        self.assertEqual(sum(r["output_tokens"] for r in rows), 250)

    def test_a_replay_reaching_a_response_first_cannot_inflate_it(self):
        """Scan order is `os.walk` order, so the child's copy of a response can
        be stored before the parent's. Either way it is one response with one
        set of figures — the MAX() merge must be a no-op, not an addition."""
        self._lineage()
        self.scan()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE processed_files SET mtime = -1, lines = 0")
        conn.commit()
        conn.close()
        self.scan()
        rows = self.rows("SELECT input_tokens, output_tokens FROM turns")
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(r["input_tokens"] for r in rows), 2450)

    def test_the_session_totals_follow_the_deduplicated_turns(self):
        """Parent and child share one root session, so the lineage's cost is
        one session's cost — the figure the Recent Sessions table shows."""
        self._lineage()
        self.scan()
        sessions = self.rows("SELECT * FROM sessions")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], self.ROOT)
        self.assertEqual(sessions[0]["turn_count"], 3)
        self.assertEqual(sessions[0]["total_input_tokens"], 2450)
        self.assertEqual(sessions[0]["total_output_tokens"], 250)


class TestAReplayedAncestorHeaderDoesNotRedefineTheThread(CodexFixture):
    """`session_meta` is not once per rollout, and only the first one is ours.

    A spawned thread can replay ancestor headers as well as usage. Its own
    identity must remain authoritative after replayed metadata.

    A replayed ancestor header must not replace the writing thread's dispatch
    label. Otherwise a leaf agent would appear as its own parent.

    Replayed headers must not replace root identity. The synthetic tests also
    cover conflicting declared roots instead of assuming every producer keeps
    headers consistent."""

    ROOT = "t-root"
    CHILD = "t-child"
    PARENT = "t-parent"
    INTRUDER = "t-other"

    def _replayed_ancestor(self):
        """The 14-rollout shape: a sub-subagent's own header, its first
        response, the echo of the subagent that spawned it, then its next."""
        return self.write([
            _session_meta(thread=self.CHILD, root=self.ROOT, subagent=True,
                          parent=self.PARENT, agent_other="/root/parent/leaf",
                          timestamp="2026-08-05T10:00:00.000Z"),
            _turn_context("gpt-5.6-sol", timestamp="2026-08-05T10:00:00.001Z"),
            _token_count(1000, inp=900, out=100,
                         timestamp="2026-08-05T10:00:00.002Z"),
            _session_meta(thread=self.PARENT, root=self.ROOT, subagent=True,
                          parent=self.ROOT, agent_other="/root/parent",
                          timestamp="2026-08-05T10:00:00.003Z"),
            _token_count(1500, inp=450, out=50,
                         timestamp="2026-08-05T10:00:30.000Z"),
        ], thread=self.CHILD)

    def test_the_writing_thread_keeps_its_own_dispatch_label(self):
        """The one thing removing the skip actually changes on real data."""
        agents = parse_jsonl_file(self._replayed_ancestor())[2]
        self.assertEqual(
            [(a["agent_id"], a["agent_type"]) for a in agents],
            [(self.CHILD, "/root/parent/leaf")],
            "the replayed ancestor header relabelled this thread's dispatch")

    def test_the_replayed_header_moves_no_money(self):
        """Deliberately an assertion that holds WITH the skip and without it.

        An ancestor in the same lineage records the same session_id. The
        lineage root keeps the synthetic message key stable; skipping a
        replayed header separately preserves the writing thread's metadata."""
        metas, turns, _agents, _limits, _lines = parse_jsonl_file(
            self._replayed_ancestor())
        self.assertEqual([t["message_id"] for t in turns],
                         [f"codex:{self.ROOT}:1000", f"codex:{self.ROOT}:1500"])
        self.assertEqual([m["session_id"] for m in metas], [self.ROOT])
        self.assertEqual(sum(t["input_tokens"] for t in turns), 1350)

    def test_a_header_declaring_a_second_root_cannot_take_over(self):
        """The identity is established once, by the first header.

        A later header must not redefine an established session identity.
        This synthetic malformed rollout verifies that lineage-based message
        keys remain stable even when the input contradicts itself."""
        path = self.write([
            _session_meta(thread=self.CHILD, root=self.ROOT,
                          timestamp="2026-08-05T10:00:00.000Z"),
            _turn_context("gpt-5.6-sol", timestamp="2026-08-05T10:00:00.001Z"),
            _token_count(1000, inp=900, out=100,
                         timestamp="2026-08-05T10:00:00.002Z"),
            _session_meta(thread=self.INTRUDER, root=self.INTRUDER,
                          cwd="/home/u/elsewhere",
                          timestamp="2026-08-05T10:00:00.003Z"),
            _token_count(1500, inp=450, out=50,
                         timestamp="2026-08-05T10:00:30.000Z"),
        ], thread=self.CHILD)
        metas, turns, _agents, _limits, _lines = parse_jsonl_file(path)
        self.assertEqual([t["message_id"] for t in turns],
                         [f"codex:{self.ROOT}:1000", f"codex:{self.ROOT}:1500"],
                         "a later header re-keyed the responses after it")
        self.assertEqual({t["session_id"] for t in turns}, {self.ROOT})
        self.assertEqual([(m["session_id"], m["project_name"]) for m in metas],
                         [(self.ROOT, "u/proj")])


class TestModelAttribution(CodexFixture):
    """Attribute tokens to the active model when a synthetic session switches."""

    def test_astra_is_priced_per_request_from_transcript_to_report(self):
        import dashboard_data
        import reports

        self.write([
            _session_meta(),
            _turn_context("gpt-6-astra", effort="xhigh"),
            _token_count(272050, inp=272000, out=50, cached=71000, cache_write=1000),
            _token_count(544101, inp=272001, out=50, cached=71000, cache_write=1000),
        ])
        self.scan()
        self.scan()  # A warm scan must not double the new model's usage.
        payload = dashboard_data.get_dashboard_data(self.db, source="codex")
        self.assertEqual(payload["all_models"], ["gpt-6-astra"])
        row, = payload["daily_by_model"]
        self.assertEqual(row["turns"], 2)
        self.assertEqual(row["input"], 400001)
        self.assertAlmostEqual(row["cost"], 6.25677, places=9)
        self.assertEqual(payload["effort_by_day_model"][0]["effort"], "xhigh")
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                reports._cmd_stats(conn, "codex")
        self.assertIn("gpt-6-astra", out.getvalue())
        self.assertIn("$6.2568", out.getvalue())

    def test_a_future_model_needs_no_parser_or_filter_allowlist_entry(self):
        import dashboard_data
        from pricing import get_pricing

        model = "future-openai-model"
        self.write([_session_meta(), _turn_context(model),
                    _token_count(110, inp=100, out=10)])
        self.scan()
        payload = dashboard_data.get_dashboard_data(self.db, source="codex")
        self.assertEqual(payload["all_models"], [model])
        row, = payload["daily_by_model"]
        self.assertEqual((row["model"], row["input"], row["output"]),
                         (model, 100, 10))
        self.assertIsNone(get_pricing(model))
        self.assertIsNone(row["cost_parts"])

    def test_each_turn_takes_the_model_in_force_when_it_ran(self):
        _, turns, _, _, _ = parse_jsonl_file(self.write([
            _session_meta(),
            _turn_context("gpt-5.6-luna"),
            _token_count(1000, inp=100, out=10),
            _turn_context("gpt-5.6-sol"),
            _token_count(2000, inp=100, out=10),
        ]))
        self.assertEqual([t["model"] for t in turns],
                         ["gpt-5.6-luna", "gpt-5.6-sol"])

    def test_a_resumed_parse_knows_the_model_from_before_the_split(self):
        """An incremental scan starts mid-file. Without recovering the model in
        force at the split, every appended turn would be attributed to nothing."""
        lines = [_session_meta(), _turn_context("gpt-5.6-sol"),
                 _token_count(1000, inp=100, out=10)]
        path = self.write(lines)
        _, turns, _, _, count = parse_jsonl_file(path)
        self.assertEqual(count, 3)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(_token_count(2000, inp=100, out=10) + "\n")
        _, resumed, _, _, _ = parse_jsonl_file(path, skip_lines=count)
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0]["model"], "gpt-5.6-sol",
                         "the model in force before the split was lost")

    def test_a_resumed_parse_honours_a_settings_driven_model_change(self):
        """Model changes through thread_settings_applied and turn_context must be
restored equally on a full read and across an incremental boundary."""
        lines = [_session_meta(), _turn_context("gpt-5.6-sol"),
                 _token_count(1000, inp=10, out=1),
                 _thread_settings("gpt-5.6-luna")]
        path = self.write(lines)
        count = parse_jsonl_file(path)[4]
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(_token_count(2000, inp=10, out=1) + "\n")
        _, resumed, _, _, _ = parse_jsonl_file(path, skip_lines=count)
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0]["model"], "gpt-5.6-luna",
                         "the resumed parse used a model the file had replaced")

    def test_settings_on_an_unrelated_record_do_not_change_resumed_attribution(self):
        prefix = [_session_meta(), _turn_context("gpt-5.6-sol", effort="high"),
                  _rec("response_item", {
                      "type": "thread_settings_applied",
                      "thread_settings": {"model": "gpt-5.6-luna",
                                          "reasoning_effort": "low"},
                  })]
        path = self.write(prefix)
        self.scan()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(_token_count(110, inp=100, out=10) + "\n")
        full = parse_jsonl_file(path)[1]
        resumed = parse_jsonl_file(path, skip_lines=len(prefix))[1]
        expected = [("gpt-5.6-sol", "high")]
        self.assertEqual([(t["model"], t["reasoning_effort"]) for t in full], expected)
        self.assertEqual([(t["model"], t["reasoning_effort"]) for t in resumed], expected)
        self.scan()
        with sqlite3.connect(self.db) as conn:
            stored = conn.execute("SELECT model, reasoning_effort FROM turns").fetchall()
        self.assertEqual(stored, expected)

    def test_one_shot_and_resumed_parses_agree_on_every_model(self):
        """The property behind it: reading a file in one pass and reading it in
        two must attribute identically, whichever record set the model."""
        lines = [_session_meta(), _turn_context("gpt-5.6-sol"),
                 _token_count(1000, inp=10, out=1),
                 _thread_settings("gpt-5.6-luna"),
                 _token_count(2000, inp=10, out=1),
                 _turn_context("gpt-5.6-sol"),
                 _token_count(3000, inp=10, out=1)]
        path = self.write(lines)
        one_shot = {t["message_id"]: t["model"] for t in parse_jsonl_file(path)[1]}
        for split in range(1, len(lines) + 1):
            with self.subTest(split=split):
                resumed = {t["message_id"]: t["model"]
                           for t in parse_jsonl_file(path, skip_lines=split)[1]}
                for mid, model in resumed.items():
                    self.assertEqual(model, one_shot[mid],
                                     f"split at line {split} changed attribution")

    def test_a_resumed_parse_still_knows_its_session(self):
        """The header is before the split too, and every appended turn has to be
        attributed to it."""
        lines = [_session_meta(thread="t-x", root="root-1"),
                 _turn_context("gpt-5.6-sol"), _token_count(1000, inp=1, out=1)]
        path = self.write(lines, thread="t-x")
        count = parse_jsonl_file(path)[4]
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(_token_count(2000, inp=1, out=1) + "\n")
        _, resumed, _, _, _ = parse_jsonl_file(path, skip_lines=count)
        self.assertEqual(resumed[0]["session_id"], "root-1")


class TestQuotaSnapshots(CodexFixture):
    """The one place Codex beats Claude Code: quota state on an append-only
    transcript rather than in a cache that is overwritten in place."""

    def test_a_usage_record_yields_a_quota_observation(self):
        _, _, _, limits, _ = parse_jsonl_file(self.write([
            _session_meta(), _turn_context("gpt-5.6-sol"),
            _token_count(1000, inp=100, out=10, percent=62),
        ]))
        self.assertEqual(len(limits), 1)
        self.assertEqual(limits[0]["percent"], 62)
        self.assertEqual(limits[0]["group"], "10080m")
        self.assertEqual(limits[0]["plan_type"], "pro")

    def test_the_reset_time_survives_validation(self):
        """Regression: reset times are Unix epochs (~1.79e9) and were being
        rejected by the token-count bound (1e9), which collapsed every window in
        the series into one undated bucket."""
        _, _, _, limits, _ = parse_jsonl_file(self.write([
            _session_meta(), _turn_context("gpt-5.6-sol"),
            _token_count(1000, inp=100, out=10, percent=62, resets_at=1786190420),
        ]))
        self.assertEqual(limits[0]["resets_at_epoch"], 1786190420)
        rows = scanner.codex_snapshot_rows(limits)
        self.assertEqual(rows[0][3], "2026-08-08T12:00", "resets_key lost")

    def test_reset_times_are_floored_to_the_minute(self):
        """Synthetic offsets within one minute must share one window key."""
        keys = set()
        for epoch in (1786190410, 1786190420, 1786190425):
            snap = parse_jsonl_file(self.write([
                _session_meta(), _turn_context("gpt-5.6-sol"),
                _token_count(1000, inp=1, out=1, percent=62, resets_at=epoch),
            ], thread=f"t{epoch}"))[3]
            keys.add(scanner.codex_snapshot_rows(snap)[0][3])
        self.assertEqual(len(keys), 1, f"one window became {len(keys)}: {keys}")

    def test_an_absurd_reset_time_is_dropped_rather_than_stored(self):
        for epoch in (0, -1, 10**18, "soon", None, True):
            with self.subTest(epoch=epoch):
                snap = parse_jsonl_file(self.write([
                    _session_meta(), _turn_context("gpt-5.6-sol"),
                    _token_count(1000, inp=1, out=1, percent=62, resets_at=epoch),
                ], thread=f"t{epoch!r}")[0:4])[3] if False else parse_jsonl_file(
                    self.write([
                        _session_meta(), _turn_context("gpt-5.6-sol"),
                        _token_count(1000, inp=1, out=1, percent=62, resets_at=epoch),
                    ], thread=f"t{abs(hash(str(epoch)))}"))[3]
                self.assertIsNone(snap[0]["resets_at_epoch"])

    def test_a_record_without_rate_limits_yields_no_observation(self):
        _, _, _, limits, _ = parse_jsonl_file(self.write([
            _session_meta(), _turn_context("gpt-5.6-sol"),
            _token_count(1000, inp=100, out=10),
        ]))
        self.assertEqual(limits, [])

    def test_a_level_keeps_the_first_moment_it_was_seen_in_the_file(self):
        """The instant stored for a level is when it was REACHED.

        Repeated observations of the same quota level keep the first instant
        at which that level was seen, rather than the last repeated timestamp."""
        _, _, _, limits, _ = parse_jsonl_file(self.write([
            _session_meta(), _turn_context("gpt-5.6-sol"),
            _token_count(1000, inp=1, out=1, percent=62,
                         timestamp="2026-08-05T10:00:01.000Z"),
            _token_count(2000, inp=1, out=1, percent=62,
                         timestamp="2026-08-05T10:30:00.000Z"),
            _token_count(3000, inp=1, out=1, percent=62,
                         timestamp="2026-08-05T16:07:00.000Z"),
        ]))
        self.assertEqual(len(limits), 1, "one level, one observation")
        self.assertEqual(limits[0]["observed_at"], "2026-08-05T10:00:01.000Z")


class TestSessionAndSubagentShape(CodexFixture):
    def test_a_thread_reports_its_root_session_project_and_branch(self):
        sessions, _, _, _, _ = parse_jsonl_file(self.write([
            _session_meta(thread="t-1", root="root-1", cwd="/home/u/myproj",
                          branch="feature/x"),
            _turn_context("gpt-5.6-sol"), _token_count(1000, inp=1, out=1),
        ], thread="t-1"))
        self.assertEqual(sessions[0]["session_id"], "root-1")
        self.assertEqual(sessions[0]["project_name"], "u/myproj")
        self.assertEqual(sessions[0]["git_branch"], "feature/x")
        self.assertEqual(sessions[0]["source"], "codex")

    def test_a_subagent_thread_is_marked_and_becomes_a_dispatch(self):
        sessions, turns, agents, _, _ = parse_jsonl_file(self.write([
            _session_meta(thread="t-sub", root="root-1", subagent=True),
            _turn_context("codex-auto-review"), _token_count(1000, inp=1, out=1),
        ], thread="t-sub"))
        self.assertEqual(turns[0]["is_subagent"], 1)
        self.assertEqual(turns[0]["agent_id"], "t-sub")
        self.assertEqual(agents[0]["agent_type"], "guardian")
        self.assertEqual(agents[0]["dispatched_in_session"], "root-1")
        self.assertEqual(turns[0]["session_id"], "root-1",
                         "a subagent's tokens belong to the session that spawned it")

    def test_a_user_thread_is_not_a_dispatch(self):
        _, turns, agents, _, _ = parse_jsonl_file(self.write([
            _session_meta(), _turn_context("gpt-5.6-sol"),
            _token_count(1000, inp=1, out=1),
        ]))
        self.assertEqual(turns[0]["is_subagent"], 0)
        self.assertIsNone(turns[0]["agent_id"])
        self.assertEqual(agents, [])


class TestScanIntegration(CodexFixture):
    """The properties the whole design rests on, exercised through scan()."""

    def _corpus(self):
        return [
            _session_meta(thread="t-1", root="root-1"),
            _turn_context("gpt-5.6-sol"),
            _token_count(1000, inp=1000, out=100, cached=900, reasoning=60, percent=10),
            _token_count(1000, inp=1000, out=100, cached=900, reasoning=60, percent=10),
            _turn_context("gpt-5.6-luna"),
            _token_count(2500, inp=500, out=50, cached=100, percent=11),
        ]

    def test_a_scan_stores_normalised_codex_turns(self):
        self.write(self._corpus(), thread="t-1")
        self.scan()
        turns = self.rows("SELECT * FROM turns ORDER BY timestamp, message_id")
        self.assertEqual(len(turns), 2, "the duplicate emission was not collapsed")
        self.assertEqual({t["source"] for t in turns}, {"codex"})
        self.assertEqual(sorted(t["model"] for t in turns),
                         ["gpt-5.6-luna", "gpt-5.6-sol"])
        self.assertEqual(sum(t["input_tokens"] for t in turns), 100 + 400)
        self.assertEqual(sum(t["cache_read_tokens"] for t in turns), 900 + 100)
        self.assertEqual(sum(t["reasoning_output_tokens"] for t in turns), 60)

    def test_rescanning_an_unchanged_corpus_changes_nothing(self):
        """No message id exists in the data, so this is the property the
        synthetic key had to buy back."""
        self.write(self._corpus(), thread="t-1")
        self.scan()
        before = self.rows("SELECT * FROM turns ORDER BY message_id")
        # Force a re-read rather than an mtime skip: this is the case that would
        # double-count if the key were not stable.
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE processed_files SET mtime = -1, lines = 0")
        conn.commit()
        conn.close()
        self.scan()
        self.assertEqual(self.rows("SELECT * FROM turns ORDER BY message_id"), before)

    def test_an_appended_file_adds_only_its_new_turns(self):
        self.write(self._corpus(), thread="t-1", mtime=1_000_000)
        self.scan()
        self.write([_token_count(4000, inp=10, out=5, cached=2)],
                   thread="t-1", mtime=2_000_000)
        result = self.scan()
        self.assertEqual(result["updated"], 1)
        self.assertEqual(len(self.rows("SELECT id FROM turns")), 3)

    def test_session_totals_are_recomputed_from_turns(self):
        self.write(self._corpus(), thread="t-1")
        self.scan()
        session = self.rows("SELECT * FROM sessions")[0]
        turns = self.rows("SELECT * FROM turns")
        self.assertEqual(session["total_input_tokens"],
                         sum(t["input_tokens"] for t in turns))
        self.assertEqual(session["turn_count"], len(turns))
        self.assertEqual(session["source"], "codex")

    def test_the_quota_series_is_recorded(self):
        self.write(self._corpus(), thread="t-1")
        self.scan()
        snaps = self.rows("SELECT * FROM usage_limits_snapshots WHERE kind = 'codex'")
        self.assertEqual(sorted(s["percent"] for s in snaps), [10, 11])
        self.assertEqual({s["resets_key"] for s in snaps}, {"2026-08-08T12:00"})

    def test_codex_turns_are_costed_per_turn_like_claude(self):
        self.write(self._corpus(), thread="t-1")
        self.scan()
        turns = self.rows("SELECT * FROM turns")
        total = sum(calc_cost(t["model"], t["input_tokens"], t["output_tokens"],
                              t["cache_read_tokens"], t["cache_creation_tokens"],
                              t["cache_creation_1h_tokens"]) for t in turns)
        self.assertGreaterEqual(total, 0.0)


class TestClaudeIsUnaffected(CodexFixture):
    """The hard requirement: adding Codex must not change one Claude number."""

    CLAUDE = [
        json.dumps({"type": "assistant", "sessionId": "c-1", "cwd": "/home/u/proj",
                    "timestamp": "2026-08-05T10:00:00Z", "gitBranch": "main",
                    "message": {"id": "msg-1", "model": "claude-opus-5",
                                "usage": {"input_tokens": 100, "output_tokens": 20,
                                          "cache_read_input_tokens": 30,
                                          "cache_creation_input_tokens": 10}}}),
    ]

    def test_a_claude_transcript_still_parses_as_claude(self):
        path = self.root / "claude-sess.jsonl"
        path.write_text("\n".join(self.CLAUDE) + "\n", encoding="utf-8")
        os.utime(path, (1_000_000, 1_000_000))
        self.scan()
        turns = self.rows("SELECT * FROM turns")
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["source"], "claude")
        self.assertEqual(turns[0]["model"], "claude-opus-5")
        self.assertEqual(turns[0]["input_tokens"], 100, "input must NOT be netted")
        self.assertEqual(turns[0]["cache_read_tokens"], 30)
        self.assertEqual(turns[0]["reasoning_output_tokens"], 0)

    def test_both_sources_coexist_without_touching_each_other(self):
        path = self.root / "claude-sess.jsonl"
        path.write_text("\n".join(self.CLAUDE) + "\n", encoding="utf-8")
        os.utime(path, (1_000_000, 1_000_000))
        self.write([_session_meta(thread="t-1", root="root-1"),
                    _turn_context("gpt-5.6-sol"),
                    _token_count(1000, inp=1000, out=100, cached=900)], thread="t-1")
        self.scan()
        by_source = {r["source"]: r for r in self.rows(
            "SELECT source, COUNT(*) n, SUM(input_tokens) inp, SUM(cache_read_tokens) cr "
            "FROM turns GROUP BY source")}
        self.assertEqual(by_source["claude"]["n"], 1)
        self.assertEqual(by_source["claude"]["inp"], 100)
        self.assertEqual(by_source["codex"]["n"], 1)
        self.assertEqual(by_source["codex"]["inp"], 100)
        self.assertEqual(by_source["codex"]["cr"], 900)
        self.assertEqual(len(self.rows("SELECT * FROM sessions")), 2)

    def _pre_codex_database(self):
        """A complete released pre-Codex database with a disposable row."""
        conn = sqlite3.connect(self.db)
        create_legacy_schema(conn)
        conn.execute("INSERT INTO turns (session_id, model, input_tokens) "
                     "VALUES ('old', 'claude-opus-4-8', 42)")
        conn.commit()
        conn.close()

    def test_a_pre_codex_database_is_rebuilt_and_rescans_as_claude(self):
        """A database predating Codex support is REBUILT, not migrated — and
        the rescan the rebuild is licensed by actually puts the turns back.

        This test used to assert the opposite — that `init_db` added the column
        and left the pre-existing row alone with `source` defaulting to
        'claude'. There are no migrations any more (see `db.init_db`), so what
        has to hold instead is that nothing is silently mis-attributed.

        **It also used to promise the rescan in its name and never scan.** Until
        2026-08-16 the body hand-inserted a row and read back the column
        default, so breaking `scanner.scan`'s Claude attribution left it green;
        the round trip now runs, on a real transcript on disk. The column
        default is a genuinely different property — `insert_turns` names
        `source` in its INSERT, so a scan never exercises it — and is asserted
        by the test below rather than folded in here.
        """
        self._pre_codex_database()
        path = self.root / "claude-sess.jsonl"
        path.write_text("\n".join(self.CLAUDE) + "\n", encoding="utf-8")
        os.utime(path, (1_000_000, 1_000_000))
        with contextlib.redirect_stderr(io.StringIO()) as announced:
            self.scan()
        self.assertIn("written by a different version", announced.getvalue())
        turns = self.rows("SELECT * FROM turns")
        self.assertEqual([t["session_id"] for t in turns], ["c-1"],
                         "a rebuild that kept the legacy row is a migration")
        self.assertEqual(turns[0]["source"], "claude")
        self.assertEqual(turns[0]["input_tokens"], 100)

    def test_the_rebuilt_schema_still_defaults_source_to_claude(self):
        """The default is what makes a Claude-only install correct with no
        backfill: a row written without naming a source is Claude's.

        Hand-inserted on purpose — the scan path above cannot reach this,
        because `insert_turns` names `source` explicitly.
        """
        self._pre_codex_database()
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        with contextlib.redirect_stderr(io.StringIO()):
            scanner.init_db(conn)
        conn.execute("INSERT INTO turns (session_id, model, input_tokens) "
                     "VALUES ('fresh', 'claude-opus-4-8', 42)")
        conn.commit()
        row = dict(conn.execute("SELECT * FROM turns").fetchone())
        conn.close()
        self.assertEqual(row["session_id"], "fresh",
                         "a rebuild that kept the legacy row is a migration")
        self.assertEqual(row["source"], "claude")
        self.assertEqual(row["input_tokens"], 42)


class TestSourceSeparationInThePayload(CodexFixture):
    """Every cost-bearing rollup must carry its source, or the dashboard cannot
    keep the two assistants apart — and mixing them adds a real dollar figure to
    an imaginary one."""

    def _payload(self):
        import dashboard_data
        conn = sqlite3.connect(self.db)
        try:
            return dashboard_data._collect_dashboard_data(conn)
        finally:
            conn.close()

    def _both(self):
        path = self.root / "claude-sess.jsonl"
        path.write_text(json.dumps({
            "type": "assistant", "sessionId": "c-1", "cwd": "/home/u/proj",
            "timestamp": "2026-08-05T10:00:00Z", "gitBranch": "main",
            "message": {"id": "msg-1", "model": "claude-opus-5",
                        "usage": {"input_tokens": 100, "output_tokens": 20,
                                  "cache_read_input_tokens": 30,
                                  "cache_creation_input_tokens": 10}}}) + "\n",
            encoding="utf-8")
        os.utime(path, (1_000_000, 1_000_000))
        self.write([_session_meta(thread="t-1", root="root-1"),
                    _turn_context("gpt-5.6-sol"),
                    _token_count(1000, inp=1000, out=100, cached=900, percent=42)],
                   thread="t-1")
        self.scan()
        return self._payload()

    def _both_with_dispatches(self):
        """`_both()` plus a subagent dispatch on each side.

        A separate fixture rather than a richer `_both()`: the sibling tests pin
        exact turn counts against that one, and what is needed here is coverage,
        not different arithmetic. Without the dispatches `subagent_by_type` and
        `top_dispatches` produce zero rows — which is how `top_dispatches`, the
        rollup whose `source` column was added precisely because it is
        cost-bearing, sat in the guard's key list and was never asserted on.
        """
        self._both()
        # Claude: the subagent's own turn, plus the parent tool_result that
        # names it — that record is where `agent_type` comes from.
        path = self.root / "claude-sub.jsonl"
        path.write_text("\n".join([
            json.dumps({"type": "assistant", "sessionId": "c-1",
                        "cwd": "/home/u/proj", "gitBranch": "main",
                        "timestamp": "2026-08-05T10:05:00Z",
                        "isSidechain": True, "agentId": "agent-1",
                        "message": {"id": "msg-sub", "model": "claude-opus-5",
                                    "usage": {"input_tokens": 40,
                                              "output_tokens": 8}}}),
            json.dumps({"type": "user", "sessionId": "c-1",
                        "timestamp": "2026-08-05T10:06:00Z",
                        "toolUseResult": {"agentId": "agent-1",
                                          "agentType": "Explore",
                                          "status": "completed",
                                          "totalTokens": 48,
                                          "totalDurationMs": 1200,
                                          "totalToolUseCount": 2}}),
        ]) + "\n", encoding="utf-8")
        os.utime(path, (1_000_000, 1_000_000))
        # Codex: a subagent is a rollout of its own, flagged in its header.
        self.write([_session_meta(thread="t-2", root="root-1", subagent=True),
                    _turn_context("gpt-5.6-sol"),
                    _token_count(500, inp=200, out=20, cached=150)],
                   thread="t-2")
        self.scan()
        return self._payload()

    # A FLOOR, not the guard itself. The guard below derives its key list from
    # the payload so a new cost-bearing rollup joins it on the commit that adds
    # it; this set is what turns "the fixture stopped producing rows for one of
    # them" into a failure instead of a silent skip.
    MUST_BE_COVERED = frozenset({
        "daily_by_model", "hourly_by_model", "sessions_all",
        "project_by_day_model", "effort_by_day_model",
        "stop_reason_by_day_model", "subagent_by_type", "top_dispatches",
    })

    @staticmethod
    def _cost_bearing(payload):
        """The payload sections that price tokens.

        Identified by their shape rather than by name: AGENTS.md's rule is that
        cost is summed over rows which each know their own model, so a list of
        row dicts carrying `model` IS the cost-bearing shape. Deriving it means
        a rollup added tomorrow is guarded today.
        """
        return {key: value for key, value in payload.items()
                if isinstance(value, list) and value
                and all(isinstance(row, dict) for row in value)
                and all("model" in row for row in value)}

    def test_the_fixture_reaches_every_rollup_the_guard_must_cover(self):
        """An empty rollup used to be reported as passing. It is uncovered."""
        covered = set(self._cost_bearing(self._both_with_dispatches()))
        self.assertEqual(
            sorted(self.MUST_BE_COVERED - covered), [],
            "these rollups produced no rows, so the source guard below did "
            "nothing for them")

    def test_every_cost_bearing_rollup_carries_a_source(self):
        payload = self._both_with_dispatches()
        sections = self._cost_bearing(payload)
        for key, rows in sections.items():
            with self.subTest(rollup=key):
                missing = [r for r in rows if "source" not in r]
                self.assertEqual(missing, [], f"{key} rows without a source")
                self.assertLessEqual({r["source"] for r in rows},
                                     {"claude", "codex"})

    def test_every_cost_bearing_rollup_actually_separates_the_two_sources(self):
        """Why a missing `source` is worse than an empty table: the page's
        `inSource(r)` reads `(r.source || 'claude') === selectedSource`, so a row
        that lost the key does not disappear — it lands inside the CLAUDE totals
        and is priced at Anthropic rates. Asserting both values are present is
        what distinguishes a real column from a constant."""
        payload = self._both_with_dispatches()
        for key, rows in self._cost_bearing(payload).items():
            with self.subTest(rollup=key):
                # .get, so a dropped key fails as a missing source rather than
                # as a KeyError from the test's own bookkeeping.
                self.assertEqual({r.get("source") for r in rows},
                                 {"claude", "codex"},
                                 f"{key} does not report both sources")

    # The next two used to read `payload["sources"]`. They now read the live
    # implementation instead: the picker is fed by /api/sources, and the payload
    # carried a duplicate of that GROUP BY which no line of the page ever read.
    # Asserting against a copy only the tests consulted is what let it survive.
    def _sources(self):
        import dashboard_data
        return dashboard_data.available_sources(self.db)

    def _lopsided(self, claude_turns, codex_turns):
        """Seed a DIFFERENT number of turns per source, each side's count known.

        Deliberately not `_both()`, which writes exactly one turn on each side.
        A group of one cannot tell a count from a constant: mutating the
        endpoint's `COUNT(*)` to the literal `1` left the whole suite green,
        because every group already held one row. It is also not a richer
        `_both()`, for the reason `_both_with_dispatches` gives above — the
        sibling tests pin exact arithmetic against that fixture.

        Both counts are caller-chosen so the ordering can be asserted in both
        directions; `available_sources` orders by the count, and a single
        direction would agree with the alphabet by luck.
        """
        path = self.root / "claude-sess.jsonl"
        path.write_text("\n".join(
            json.dumps({
                "type": "assistant", "sessionId": "c-1", "cwd": "/home/u/proj",
                "timestamp": f"2026-08-05T10:{i:02d}:00Z", "gitBranch": "main",
                "message": {"id": f"msg-{i}", "model": "claude-opus-5",
                            "usage": {"input_tokens": 100, "output_tokens": 20}}})
            for i in range(claude_turns)) + "\n", encoding="utf-8")
        os.utime(path, (1_000_000, 1_000_000))
        # Distinct cumulative totals: `codex:<root>:<cumulative>` IS the
        # response identity, so repeating one would merge the rows away and
        # silently seed fewer turns than the caller asked for.
        self.write([_session_meta(thread="t-1", root="root-1"),
                    _turn_context("gpt-5.6-sol")]
                   + [_token_count(1000 * (i + 1), inp=1000, out=100, cached=900)
                      for i in range(codex_turns)],
                   thread="t-1")
        self.scan()

    def test_the_source_endpoint_reports_which_sources_exist(self):
        """The counts, not merely the names. `turns` is what the picker shows
        beside each assistant, and it is the only figure this endpoint carries
        that the payload does not."""
        self._lopsided(claude_turns=3, codex_turns=2)
        rows = self._sources()
        self.assertEqual({s["source"]: s["turns"] for s in rows},
                         {"claude": 3, "codex": 2})
        self.assertEqual([s["source"] for s in rows], ["claude", "codex"],
                         "ORDER BY turns DESC — the busier source comes first")

    def test_the_busier_source_is_listed_first_whichever_it_is(self):
        """The other direction, which is what stops the assertion above from
        passing on an ordering that merely happens to be alphabetical."""
        self._lopsided(claude_turns=1, codex_turns=4)
        rows = self._sources()
        self.assertEqual({s["source"]: s["turns"] for s in rows},
                         {"codex": 4, "claude": 1})
        self.assertEqual([s["source"] for s in rows], ["codex", "claude"])

    def test_a_claude_only_database_reports_only_claude(self):
        path = self.root / "claude-sess.jsonl"
        path.write_text(json.dumps({
            "type": "assistant", "sessionId": "c-1", "cwd": "/x",
            "timestamp": "2026-08-05T10:00:00Z",
            "message": {"id": "m", "model": "claude-opus-5",
                        "usage": {"input_tokens": 5, "output_tokens": 1}}})
            + "\n", encoding="utf-8")
        os.utime(path, (1_000_000, 1_000_000))
        self.scan()
        self.assertEqual([s["source"] for s in self._sources()], ["claude"])

    def test_the_payload_does_not_duplicate_the_source_endpoint(self):
        """The duplicate is gone, and must not come back.

        /api/data is fetched with both assistants' history behind it; the source
        list is what the page needs *first*, which is why it has its own cheap
        endpoint. A second copy in the payload is a whole-table scan on every
        poll for a reader that does not exist — and `sources` is the name the
        page already uses for the endpoint's response, so the surface guard in
        tests/test_payload_surface.py could only see it once it started asking
        for an access shape rather than a mention."""
        self.assertNotIn("sources", self._both(),
                         "the payload is duplicating available_sources() again")

    def test_filtering_the_payload_by_source_isolates_the_totals(self):
        """The property the whole split exists for: one assistant's tokens can
        never leak into the other's figures."""
        payload = self._both()
        claude = [r for r in payload["daily_by_model"] if r["source"] == "claude"]
        codex = [r for r in payload["daily_by_model"] if r["source"] == "codex"]
        self.assertEqual(sum(r["input"] for r in claude), 100)
        self.assertEqual(sum(r["cache_read"] for r in claude), 30)
        self.assertEqual(sum(r["input"] for r in codex), 100, "uncached remainder")
        self.assertEqual(sum(r["cache_read"] for r in codex), 900)
        self.assertEqual({r["model"] for r in claude}, {"claude-opus-5"})
        self.assertEqual({r["model"] for r in codex}, {"gpt-5.6-sol"})


class TestCodexQuotaProjection(CodexFixture):
    """Codex's quota comes off an append-only transcript, so unlike Claude's
    cache it yields a real series — including a window resetting."""

    def _project(self):
        import dashboard_data
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return (dashboard_data.codex_limits_projection(conn),
                    dashboard_data.codex_limit_history(conn))
        finally:
            conn.close()

    def test_no_codex_usage_means_no_projection(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        scanner.init_db(conn)
        conn.close()
        projection, history = self._project()
        self.assertFalse(projection["available"])
        self.assertEqual(history, [])

    def test_the_newest_window_is_projected_in_the_claude_shape(self):
        """Same shape on purpose: one renderer draws either assistant."""
        self.write([_session_meta(thread="t-1", root="root-1"),
                    _turn_context("gpt-5.6-sol"),
                    _token_count(1000, inp=10, out=1, percent=42)], thread="t-1")
        self.scan()
        projection, _ = self._project()
        self.assertTrue(projection["available"])
        self.assertEqual(projection["source"], "codex")
        self.assertEqual(projection["plan_type"], "pro")
        window = projection["windows"][0]
        for field in ("kind", "group", "percent", "severity", "resets_at",
                      "scope", "is_active", "expired"):
            self.assertIn(field, window, "diverged from the Claude window shape")
        self.assertEqual(window["percent"], 42)

    def test_a_window_keeps_its_highest_observation(self):
        """The projection reads MAX, not last-observed.

        The LOWEST percentage carries the LATEST timestamp deliberately. While
        all three shared one timestamp — the helper's default, which they did
        until this was rewritten — `ORDER BY observed_at` was a three-way tie
        that SQLite resolved through `PRIMARY KEY (kind, grp, scope, resets_key,
        percent)`, i.e. ascending by percent, so the highest observation was
        also the last one and the test could not tell MAX from last-wins:
        replacing the comparison with `if True:` left it green.

        Each level keeps its earliest observation. MAX also handles incomplete
        histories and timestamp skew; ingestion-order checks live in
        TestWhatRecordLimitSnapshotActuallyKeeps."""
        self.write([_session_meta(thread="t-1", root="root-1"),
                    _turn_context("gpt-5.6-sol"),
                    _token_count(1000, inp=10, out=1, percent=42,
                                 timestamp="2026-08-05T10:00:01.000Z"),
                    _token_count(2000, inp=10, out=1, percent=61,
                                 timestamp="2026-08-05T10:00:02.000Z"),
                    _token_count(3000, inp=10, out=1, percent=55,
                                 timestamp="2026-08-05T10:00:03.000Z")],
                   thread="t-1")
        self.scan()
        projection, history = self._project()
        self.assertEqual(projection["windows"][0]["percent"], 61)
        # The gauge takes the max; the series keeps every observation in the
        # order observed. Asserted here rather than beside the other history
        # test because only this fixture's percentages are non-monotonic, and a
        # series sorted by percent instead of by time is indistinguishable from
        # one sorted correctly while they climb.
        self.assertEqual([h["percent"] for h in history], [42, 61, 55])

    def test_only_the_latest_window_per_group_is_shown(self):
        """Six days of history must not render as six stacked gauges."""
        self.write([_session_meta(thread="t-1", root="root-1"),
                    _turn_context("gpt-5.6-sol"),
                    _token_count(1000, inp=10, out=1, percent=69, resets_at=1785672015),
                    _token_count(2000, inp=10, out=1, percent=3, resets_at=1786190420)],
                   thread="t-1")
        self.scan()
        projection, history = self._project()
        self.assertEqual(len(projection["windows"]), 1)
        self.assertEqual(projection["windows"][0]["percent"], 3, "the newer window")
        self.assertEqual(len(history), 2, "but both stay in the history")

    def test_a_snapshot_whose_limit_id_is_not_codex_still_reaches_the_panel(self):
        """`kind` is the routing key — every consumer filters `kind = 'codex'`
        — so taking it from the transcript's own `limit_id` let any other label
        route the rows into a kind nothing reads. Silently, too:
        `route_limit_records` still accepts the record (it has a `percent`), so
        the 'matched no known shape' warning that exists precisely to stop
        limits disappearing without a word never fires. `kind` here means the
        source, which is not the transcript's to name."""
        self.write([_session_meta(thread="t-1", root="root-1"),
                    _turn_context("gpt-5.6-sol"),
                    _token_count(1000, inp=10, out=1, percent=42,
                                 limit_id="codex_weekly")], thread="t-1")
        self.scan()
        projection, history = self._project()
        self.assertTrue(projection["available"],
                        "the quota panel emptied on an unrecognised limit_id")
        self.assertEqual(projection["windows"][0]["percent"], 42)
        self.assertEqual(len(history), 1)
        # Not an equality check on the whole column: scan() also records
        # whatever the machine's ~/.claude.json holds, under Claude's kinds.
        self.assertNotIn("codex_weekly", {r["kind"] for r in self.rows(
            "SELECT kind FROM usage_limits_snapshots")})

    def test_the_history_is_the_series_claude_cannot_provide(self):
        """Distinct timestamps for the same reason as the MAX test above: the
        assertion is about the ORDER the series comes back in, and three rows
        sharing one `observed_at` leave that order to whatever plan SQLite
        picks rather than to `codex_limit_history`'s ORDER BY."""
        self.write([_session_meta(thread="t-1", root="root-1"),
                    _turn_context("gpt-5.6-sol"),
                    _token_count(1000, inp=10, out=1, percent=10,
                                 timestamp="2026-08-05T10:00:01.000Z"),
                    _token_count(2000, inp=10, out=1, percent=20,
                                 timestamp="2026-08-05T10:00:02.000Z"),
                    _token_count(3000, inp=10, out=1, percent=30,
                                 timestamp="2026-08-05T10:00:03.000Z")],
                   thread="t-1")
        self.scan()
        _, history = self._project()
        self.assertEqual([h["percent"] for h in history], [10, 20, 30])
        self.assertTrue(all(h["day"] and h["observed"] for h in history))

    def test_the_age_is_how_old_the_reading_is_not_how_long_the_level_has_held(self):
        """`age_seconds` answers "when did Codex last record a quota figure".

        A quota level can stay constant while new turns arrive. Age must
        consider the newest turn as well as the newest stored observation.

        The fixture is the same two-rollout shape the storage half was measured
        on: the rollout that STARTED first sorts first and supplies the later
        observation of a level the second rollout reached earlier and is still
        using.
        """
        self.write([_session_meta(thread="a-aaa", root="root-a"),
                    _turn_context("gpt-5.6-sol"),
                    _token_count(1000, inp=10, out=1, percent=42,
                                 timestamp="2026-08-05T10:00:05.000Z")],
                   thread="a-aaa")
        self.write([_session_meta(thread="b-bbb", root="root-b"),
                    _turn_context("gpt-5.6-sol"),
                    _token_count(1000, inp=10, out=1, percent=42,
                                 timestamp="2026-08-05T10:00:01.000Z"),
                    _token_count(2000, inp=10, out=1, percent=42,
                                 timestamp="2026-08-05T10:00:09.000Z")],
                   thread="b-bbb")
        self.scan()
        import dashboard_data
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            projection = dashboard_data.codex_limits_projection(
                conn, now=datetime(2026, 8, 5, 10, 0, 19, tzinfo=timezone.utc))
        finally:
            conn.close()
        self.assertEqual(projection["age_seconds"], 10,
                         "aged from the series instead of from the newest turn")
        # And the series itself keeps the moment the level was reached.
        self.assertEqual([r["observed_at"] for r in self.rows(
            "SELECT observed_at FROM usage_limits_snapshots WHERE kind = 'codex'")],
            ["2026-08-05T10:00:01.000Z"])

    def test_a_filling_window_reports_no_level_rather_than_a_reassuring_one(self):
        """`''` here means NOT RECORDED, and must never be dressed up.

        A missing reached-limit type is not a severity level. Derive the
        display band from percentage while preserving the raw quota identity."""
        self.write([_session_meta(thread="t-1", root="root-1"),
                    _turn_context("gpt-5.6-sol"),
                    _token_count(1000, inp=10, out=1, percent=100)], thread="t-1")
        self.scan()
        projection, _ = self._project()
        self.assertEqual(projection["windows"][0]["severity"], "")

    def test_a_limit_that_was_actually_reached_arrives_as_critical(self):
        """The stored column is a WHICH-limit discriminator, not a grade.

        `rate_limit_reached_type` carries something like `primary` once a limit
        has been hit. Shipped verbatim under a key the panel reads as a graded
        level it is worse than the blank beside it: the renderer allow-lists
        `normal`/`warning`/`critical` (a deliberate injection defence, see
        tests/test_server_hardening.py), so an unknown token falls back — and
        every percent-derived fallback would then be reading a window whose
        limit has ALREADY tripped as merely a percentage. Translating it here is
        what keeps `severity` meaning one thing for both assistants.

        A synthetic event pins the translation without requiring a user to
        reach a live quota limit.
        """
        self.write([_session_meta(thread="t-1", root="root-1"),
                    _turn_context("gpt-5.6-sol"),
                    _token_count(1000, inp=10, out=1, percent=40,
                                 reached="primary")], thread="t-1")
        self.scan()
        projection, _ = self._project()
        window = projection["windows"][0]
        self.assertEqual(window["percent"], 40, "the percentage is untouched")
        self.assertEqual(window["severity"], "critical",
                         "a reached limit reported as a raw discriminator")
        # The stored column keeps what the transcript said; only the projection
        # translates, so the series stays a record of what was observed.
        self.assertEqual([r["severity"] for r in self.rows(
            "SELECT severity FROM usage_limits_snapshots WHERE kind = 'codex'")],
            ["primary"])


# Drives the REAL renderPlanLimits and captures what it writes into
# #plan-windows. Both Codex plan fixtures that existed before this class stub
# renderPlanLimits out entirely (tests/test_ui_theme_and_loading.py,
# tests/test_dashboard_js.py), so neither could ever see the gauge's class —
# which is why `grep -rn 'sev-normal' tests/` returned nothing at all before
# this file, in a 1565-test suite.
_PLAN_RENDER = """
  (() => {
    const captured = {};
    document.getElementById = (id) => ({
      set innerHTML(v) { captured[id] = v; },
      set textContent(v) { captured[id + ':text'] = v; },
      set className(v) {}, set hidden(v) {},
      setAttribute: () => {}, closest: () => null,
    });
    document.querySelector = () => ({ set hidden(v) {} });
    renderPlanLimits(info);
    return (captured['plan-windows'].match(/plan-window-pct[^"]*/) || [''])[0];
  })()"""


@requires_node
class TestTheQuotaGaugeNeverCallsAnUnknownLevelFine(unittest.TestCase):
    """A window whose source published no level must not render as `normal`.

    `renderPlanLimits` collapsed anything outside its allow-list to `normal`,
    and `.sev-normal` is `var(--green)` in both themes (web/app.css:614,618).
    Codex publishes no level ever, so its gauge was green at every percentage
    including 100 — while the identical 100% on Claude, whose cache does carry a
    level, rendered red in the same panel. That is the `$0.00`-where-`n/a`-is-
    meant error on the one card whose whole job is to signal.

    The allow-list itself is not the defect and must stay: `sev` is interpolated
    into a class name, so it is a deliberate injection defence against a hostile
    string in `~/.claude.json` (tests/test_server_hardening.py says so in as many
    words). What changed is only what it falls back TO.

    Placed here rather than in the plan-panel suite because the reachable case
    is Codex's, and because this file already owns the projection that feeds it.
    """

    def _class_at(self, percent, severity, source="codex"):
        return run_js(emit(_PLAN_RENDER, info={
            "available": True, "plan_type": "pro", "source": source,
            "age_seconds": 5,
            "windows": [{"kind": "10080m", "group": "10080m", "scope": "",
                         "percent": percent, "severity": severity,
                         "is_active": True, "expired": False,
                         "resets_at": "2099-01-01T00:00:00Z"}],
        }))

    def test_a_full_window_with_no_recorded_level_is_not_rendered_as_fine(self):
        """Synthetic weekly-window fixture at its limit."""
        got = self._class_at(100, "")
        self.assertNotIn("sev-normal", got,
                         "out of quota, painted with the same green as 5%")
        self.assertIn("sev-critical", got)

    def test_the_derived_level_moves_with_the_percentage(self):
        """Relational, not a colour chart: the point is that 5% and 100% differ.

        Exercise the display bands around each configured percentage boundary
        with synthetic quota observations."""
        levels = [self._class_at(p, "") for p in (0, 40, 74, 75, 89, 90, 100)]
        self.assertEqual(
            [l.replace("plan-window-pct ", "") for l in levels],
            ["sev-normal", "sev-normal", "sev-normal", "sev-warning",
             "sev-warning", "sev-critical", "sev-critical"])

    def test_a_recorded_level_still_wins_over_the_derived_one(self):
        """The control. Without it a later refactor could replace the vendor's
        own reading with the inference and nothing would notice."""
        self.assertIn("sev-normal", self._class_at(100, "normal", "claude"))
        self.assertIn("sev-warning", self._class_at(5, "warning", "claude"))
        self.assertIn("sev-critical", self._class_at(5, "critical", "claude"))

    def test_a_window_with_no_percentage_is_given_no_colour_at_all(self):
        """There is nothing to derive from, and green would be a claim.

        Reachable on the Claude side: `account._percent` returns None for a
        figure it does not find credible, and the `five_hour` fallback ships
        `severity: ""` beside it. Emitting no `sev-` class leaves the bar on its
        own track colour and the `—` in the card's text colour, which is the
        neutral fourth state the three-class stylesheet cannot otherwise express.
        """
        self.assertEqual(self._class_at(None, ""), "plan-window-pct")

    def test_an_unrecognised_level_cannot_reach_the_class_name(self):
        """The injection defence, asserted rather than assumed. `severity` comes
        from a file this process does not own."""
        got = self._class_at(40, "warning\"><script>alert(1)</script>")
        self.assertEqual(got, "plan-window-pct sev-normal")


_LIMITS_RENDER = """
  (() => {
    const captured = {};
    document.getElementById = (id) => ({
      set innerHTML(v) { captured[id] = v; },
      set textContent(v) { captured[id + ':text'] = v; },
      set title(v) { captured[id + ':title'] = v; },
      querySelectorAll: () => [], closest: () => null,
    });
    selectedSource = source;
    renderLimits(incidents);
    return captured;
  })()"""


@requires_node
class TestTheRateLimitsTableCannotContradictItsOwnNote(unittest.TestCase):
    """`renderLimits` gates its tiles and its empty-state on the SOURCE, exactly
    as its header comment says, but gated the ROW branch on `incidents.length`.

    Handed a Claude-shaped array while Codex is selected, it therefore printed
    Claude's throttling incidents — project names, reset times and all — above a
    note reading "this table stays empty however often Codex throttled you" and
    beside three tiles reading "—". One predicate, two answers, in one card.

    Two things a later reader must not conclude from this class. The server
    scopes `limit_incidents` (`rollups.limit_incidents` says so, and says the
    guard was put there deliberately), so no `?source=`-carrying fetch produces
    this array — the reachable window is the in-flight source switch, where
    `selectedSource` is already the new assistant while `rawData` is still the
    previous one, and a theme or keyboard-driven re-render lands in between.
    And the fix is emphatically NOT to filter `limit_incidents` through
    `inSource`: those rows carry no `source` key by contract, so that gate would
    be a hard-coded 'claude' wearing a function call, and would invert the moment
    a second assistant ever wrote a limit notice.
    """

    INCIDENT = {"day": "2026-08-09", "started": "2026-08-09 08:22",
                "blocked_min": 23.2, "notices": 10,
                "projects": ["acme/web-storefront"],
                "reset_hint": "9:40am", "reset_zone": "Europe/Madrid",
                "status": 429}

    def _render(self, source, incidents):
        return run_js(emit(_LIMITS_RENDER, source=source, incidents=incidents))

    def test_the_fixture_really_does_render_rows_for_claude(self):
        """The control: without it the assertions below could pass because
        nothing was ever rendered."""
        got = self._render("claude", [self.INCIDENT])
        self.assertIn("23.2m", got["limits-body"])
        self.assertIn("acme/web-storefront", got["limits-body"])

    def test_claude_incidents_are_not_printed_under_a_codex_heading(self):
        got = self._render("codex", [self.INCIDENT])
        self.assertNotIn("23.2m", got["limits-body"],
                         "a Claude incident rendered on the Codex view")
        self.assertNotIn("acme/web-storefront", got["limits-body"])
        self.assertIn("Not recorded", got["limits-body"])

    def test_an_empty_range_still_reads_as_a_measurement_for_claude(self):
        """The other half of the same rule, and the reason the gate cannot
        simply be `!incidents.length`: a Claude reader with a clean range really
        was not limited, and that zero is a fact they must keep."""
        got = self._render("claude", [])
        self.assertIn("No usage limits reached in this range.",
                      got["limits-body"])


class TestLimitRecordRouting(unittest.TestCase):
    """Claude records that a limit was HIT; Codex records the percentage
    continuously. They are different facts, they go to different tables, and
    neither substitutes for the other — so the split must be total."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Path(self.tmp) / "usage.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        scanner.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _counts(self):
        return (self.conn.execute("SELECT COUNT(*) FROM limit_events").fetchone()[0],
                self.conn.execute("SELECT COUNT(*) FROM usage_limits_snapshots").fetchone()[0])

    def test_a_claude_notice_goes_to_limit_events(self):
        scanner.route_limit_records(self.conn, [{
            "event_uuid": "u1", "kind": "rate_limit", "session_id": "s",
            "timestamp": "2026-08-05T10:00:00Z", "status": 429,
            "message": "hit", "reset_hint": "3am", "reset_zone": "UTC"}])
        self.assertEqual(self._counts(), (1, 0))

    def test_a_codex_observation_goes_to_the_snapshot_series(self):
        scanner.route_limit_records(self.conn, [{
            "kind": "codex", "group": "10080m", "scope": "", "percent": 62,
            "severity": "", "is_active": 1, "window_minutes": 10080,
            "resets_at_epoch": 1786190420, "plan_type": "pro",
            "observed_at": "2026-08-05T10:00:00Z"}])
        self.assertEqual(self._counts(), (0, 1))

    def test_both_shapes_in_one_batch_are_split(self):
        scanner.route_limit_records(self.conn, [
            {"event_uuid": "u1", "kind": "rate_limit", "session_id": "s",
             "timestamp": "2026-08-05T10:00:00Z", "status": 429,
             "message": "hit", "reset_hint": "", "reset_zone": ""},
            {"kind": "codex", "group": "10080m", "scope": "", "percent": 62,
             "severity": "", "is_active": 1, "window_minutes": 10080,
             "resets_at_epoch": 1786190420, "plan_type": "pro",
             "observed_at": "2026-08-05T10:00:00Z"}])
        self.assertEqual(self._counts(), (1, 1))

    def test_an_unroutable_record_is_reported_rather_than_dropped(self):
        """Silence here is how a parser change quietly stops recording limits:
        the table just goes empty and nothing says why."""
        import contextlib, io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            scanner.route_limit_records(self.conn, [{"something": "else"}])
        self.assertIn("matched no known shape", buffer.getvalue())
        self.assertEqual(self._counts(), (0, 0))

    def test_an_empty_batch_says_nothing(self):
        import contextlib, io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            scanner.route_limit_records(self.conn, [])
        self.assertEqual(buffer.getvalue(), "")


class TestCumulativeCounterIsNotATurnCount(CodexFixture):
    """The fixtures exercise replayed cumulative counters across a parent and child.
Response keys must resolve to the producer lineage rather than the file that
happens to contain the replay."""

    # Chosen to straddle the old MAX_TRANSCRIPT_INTEGER cap: the first response
    # is below it, every later one above.
    BASE = 999_000_000
    STEP = 5_000_000

    def _long_thread(self, count=20):
        lines = [_session_meta(thread="t-long", root="root-long"),
                 _turn_context("gpt-5.6-sol")]
        expected_output = 0
        for i in range(count):
            lines.append(_token_count(self.BASE + self.STEP * (i + 1),
                                      inp=100, out=100 + i,
                                      timestamp=f"2026-08-05T10:{i:02d}:00.000Z"))
            expected_output += 100 + i
        return self.write(lines, thread="t-long"), expected_output

    def test_a_thread_past_one_billion_keeps_every_response(self):
        """The headline failure: 20 responses and 2,190 output tokens were
        stored as 1 row and 119 — a 94% loss with no warning anywhere."""
        _path, expected_output = self._long_thread()
        self.scan()
        rows = self.rows("SELECT message_id, output_tokens FROM turns")
        self.assertEqual(len(rows), 20)
        self.assertEqual(sum(r["output_tokens"] for r in rows), expected_output)

    def test_each_response_above_the_cap_keeps_its_own_identity(self):
        """The parser-level property the merge depends on: distinct cumulative
        totals must produce distinct ids, above the per-turn bound as below it.

        The literals name the ROOT session (`root-long`), not the writing
        thread (`t-long`), because the accumulator these ids are built from is
        per-lineage — see TestSubagentReplayIsNotNewUsage. Keying on the
        writing thread stored one API response once per descendant that
        replayed it; what this test actually protects, that a thread past 1e9
        does not collapse its tail onto one shared key, is unchanged."""
        path, _ = self._long_thread()
        _s, turns, _a, _l, _c = parse_jsonl_file(path)
        ids = {t["message_id"] for t in turns}
        self.assertEqual(len(ids), 20, "ids collapsed onto a shared key")
        self.assertNotIn(f"codex:root-long:0", ids)
        self.assertIn(f"codex:root-long:{self.BASE + self.STEP}", ids)

    def test_the_merge_is_still_exact_for_a_genuine_duplicate_above_the_cap(self):
        """Raising the bound must not cost the dedupe it exists for: two records
        sharing a cumulative total are still one API response."""
        self.write([_session_meta(thread="t-dup", root="root-dup"),
                    _turn_context("gpt-5.6-sol"),
                    _token_count(2_000_000_000, inp=100, out=50),
                    _token_count(2_000_000_000, inp=100, out=50)], thread="t-dup")
        self.scan()
        rows = self.rows("SELECT output_tokens FROM turns")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["output_tokens"], 50)

    def test_a_record_with_no_usable_counter_is_dropped_not_merged(self):
        """Without the counter there is no message.id analogue. Dropping the
        record loses one turn; the old shared-key fallback DELETED the turns
        already stored beside it, which is the strictly worse failure."""
        good_a = _token_count(1_500_000_000, inp=100, out=11,
                              timestamp="2026-08-05T10:01:00.000Z")
        good_b = _token_count(1_600_000_000, inp=100, out=22,
                              timestamp="2026-08-05T10:03:00.000Z")
        broken = json.loads(_token_count(1_550_000_000, inp=100, out=99,
                                         timestamp="2026-08-05T10:02:00.000Z"))
        del broken["payload"]["info"]["total_token_usage"]["total_tokens"]
        self.write([_session_meta(thread="t-nc", root="root-nc"),
                    _turn_context("gpt-5.6-sol"),
                    good_a, json.dumps(broken), good_b], thread="t-nc")
        self.scan()
        rows = self.rows("SELECT output_tokens FROM turns ORDER BY output_tokens")
        self.assertEqual([r["output_tokens"] for r in rows], [11, 22],
                         "the neighbouring turns were merged away")

    def test_a_negative_or_absurd_counter_is_refused(self):
        for value in (-1, 2 ** 53, True, "1000", 1.5, None):
            with self.subTest(value=value):
                self.assertIsNone(
                    codex_transcripts._cumulative_total({"total_tokens": value}))

    def test_the_normaliser_produces_only_columns_the_turn_stores(self):
        """`_usage_from` exists to encode the three subset/superset rules, and
        every extra key dilutes that: a reader has to grep the module to learn
        whether it is part of the contract. `total_tokens` was one such key —
        unread, not an Anthropic column, and colliding by name with the
        per-lineage accumulator `_cumulative_total` reads out of a different
        block."""
        path = self.write([_session_meta(), _turn_context("gpt-5.6-sol"),
                           _token_count(1000, inp=100, out=10, cached=40,
                                        reasoning=4)])
        _s, turns, _a, _l, _c = parse_jsonl_file(path)
        usage = codex_transcripts._usage_from(
            {"input_tokens": 100, "cached_input_tokens": 40,
             "output_tokens": 10, "reasoning_output_tokens": 4,
             "total_tokens": 110})
        self.assertTrue(
            set(usage) <= set(turns[0]),
            f"normalised fields nothing stores: {sorted(set(usage) - set(turns[0]))}")

    def test_the_per_turn_figures_keep_the_per_turn_bound(self):
        """Only the cumulative counter changed bound. A single turn claiming
        two billion input tokens is still not credible usage and is still
        rejected, so the SUM-overflow protection is intact."""
        usage = codex_transcripts._usage_from(
            {"input_tokens": 2_000_000_000, "output_tokens": 2_000_000_000,
             "cached_input_tokens": 0})
        self.assertEqual(usage["input_tokens"], 0)
        self.assertEqual(usage["output_tokens"], 0)


class TestOneBadRecordCannotStrandTheFile(CodexFixture):
    """`json.loads` accepts the bare tokens NaN, Infinity and -Infinity, and
    `int()` refuses all three. Raised from `_limit_snapshot` the exception
    escaped into the handler wrapped around the whole parse loop, so parsing
    stopped at that record — and `scan()` then wrote the PARTIAL line count to
    `processed_files.lines` beside the file's real mtime, which means no later
    scan ever re-reads the stranded tail. For a finished rollout that is
    permanent."""

    def _poisoned_file(self, literal):
        """A thread of 13 responses whose 4th carries a non-finite percent."""
        lines = [_session_meta(thread="t-nan", root="root-nan"),
                 _turn_context("gpt-5.6-sol")]
        for i in range(3):
            lines.append(_token_count(1000 * (i + 1), inp=100, out=10, percent=5,
                                      timestamp=f"2026-08-05T10:{i:02d}:00.000Z"))
        poisoned = _token_count(9000, inp=100, out=10, percent=1,
                                timestamp="2026-08-05T10:04:00.000Z")
        assert '"used_percent": 1' in poisoned
        lines.append(poisoned.replace('"used_percent": 1',
                                      f'"used_percent": {literal}'))
        for i in range(9):
            lines.append(_token_count(10000 + 1000 * i, inp=100, out=10, percent=6,
                                      timestamp=f"2026-08-05T10:{5 + i:02d}:00.000Z"))
        return self.write(lines, thread="t-nan"), len(lines)

    def test_json_really_does_admit_these_literals(self):
        """The premise. If this ever stops being true the tests below are moot."""
        self.assertNotEqual(json.loads('{"p": NaN}')["p"],
                            json.loads('{"p": NaN}')["p"])
        self.assertEqual(json.loads('{"p": Infinity}')["p"], float("inf"))

    def test_the_rest_of_the_file_survives_a_non_finite_percent(self):
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                self.setUp()
                path, total_lines = self._poisoned_file(literal)
                _s, turns, _a, _l, line_count = parse_jsonl_file(path)
                self.assertEqual(line_count, total_lines,
                                 "line_count stopped short, so scan() would "
                                 "strand the tail")
                self.assertEqual(len(turns), 13)

    def test_the_stranded_tail_is_not_stranded(self):
        """End to end, including the bookkeeping that made the loss permanent."""
        _path, total_lines = self._poisoned_file("NaN")
        self.scan()
        self.assertEqual(len(self.rows("SELECT id FROM turns")), 13)
        stamped = self.rows("SELECT lines FROM processed_files")
        self.assertEqual([r["lines"] for r in stamped], [total_lines])

    def test_the_poisoned_quota_reading_is_dropped_not_clamped(self):
        """A NaN percent is not 0 and not 100 — it is no observation at all."""
        self._poisoned_file("NaN")
        self.scan()
        # Scoped to kind='codex': a scan also records whatever the developer's
        # own ~/.claude.json happens to say, which is not this test's subject.
        percents = {r["percent"] for r in self.rows(
            "SELECT percent FROM usage_limits_snapshots WHERE kind = 'codex'")}
        self.assertEqual(percents, {5, 6})

    def test_a_record_that_raises_anywhere_costs_only_that_record(self):
        """The blast-radius half of the fix, independent of the NaN half: even
        an unforeseen raiser must not abandon the file."""
        lines = [_session_meta(thread="t-boom", root="root-boom"),
                 _turn_context("gpt-5.6-sol")]
        for i in range(6):
            lines.append(_token_count(1000 * (i + 1), inp=100, out=10, percent=7,
                                      timestamp=f"2026-08-05T10:{i:02d}:00.000Z"))
        path = self.write(lines, thread="t-boom")

        real = codex_transcripts._limit_snapshot
        calls = {"n": 0}

        def exploding(rate_limits, timestamp):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("boom")
            return real(rate_limits, timestamp)

        codex_transcripts._limit_snapshot = exploding
        try:
            import contextlib, io
            buffer = io.StringIO()
            # stderr: the warning no longer shares a stream with the URL.
            with contextlib.redirect_stderr(buffer):
                _s, turns, _a, _l, line_count = parse_jsonl_file(path)
        finally:
            codex_transcripts._limit_snapshot = real

        self.assertEqual(line_count, len(lines))
        self.assertEqual(len(turns), 5, "one record lost, not the file")
        self.assertIn("skipped 1 unreadable record", buffer.getvalue())

    def test_an_unreadable_file_is_still_reported_as_such(self):
        """The per-record guard must not swallow the whole-file handler: a
        missing file still returns an empty parse with line_count 0, so scan()
        records nothing rather than stamping a bogus length."""
        import contextlib, io
        # STDERR: this was a bare `print()`, so under
        # `dashboard._background_scan` the warning landed on the server's own
        # stdout where nobody reads it.
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = parse_jsonl_file(self.root / "does-not-exist.jsonl")
        self.assertEqual(result[4], 0)
        self.assertIn("error reading", err.getvalue())
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
