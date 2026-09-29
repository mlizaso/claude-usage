"""Codex replay attribution must be independent of transcript discovery order.

Child rollouts replay their ancestors' cumulative token history under the same
lineage-scoped message ids. Generic conflicts retain first-writer attribution,
but Codex conflicts prefer a non-subagent producer over a stored subagent
replay. These tests cover ordinary order, reversed roots, renamed/headerless
files, siblings inside one second, and the daylight-saving fall-back hour where
a child's wall-clock filename genuinely sorts before its parent.

The corpus is synthetic and hermetic. The real `~/.codex/sessions` is historical
read-only evidence only; a test that depends on one machine's corpus is not a
test.
"""

import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import scanner
from transcripts import discover_jsonl_files


def _ts(moment, seconds=0):
    """The transcript timestamp `seconds` after ISO-8601-Z string `moment`."""
    base = datetime.strptime(moment, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _uuid7(millis, tag):
    """A uuid in the shape Codex names rollouts with.

    Use synthetic UUIDv7 identifiers with deliberately ordered timestamp
    prefixes. Preserve the same ordering property without depending on real
    rollout identities."""
    clock = f"{millis:012x}"
    tail = (tag * 18)[:18]
    return f"{clock[:8]}-{clock[8:12]}-7{tail[:3]}-8{tail[3:6]}-{tail[6:18]}"


def _millis(moment):
    """Epoch milliseconds for ISO-8601-Z string `moment`.

    The clock a real rollout's uuidv7 carries. Only `TestTheDaylightSavingWindow`
    derives it: the other classes pick their millis by hand precisely because
    they need the uuid to *disagree* with the filename's wall clock (a
    containerised run stamping UTC, two siblings inside one second). There the
    divergence is the subject; here fidelity is, so the uuid is the instant.
    """
    base = datetime.strptime(moment, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc)
    return int(base.timestamp() * 1000)


def _seconds_between(earlier, later):
    """Seconds from ISO-8601-Z string `earlier` to ISO-8601-Z string `later`."""
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    return (datetime.strptime(later, fmt)
            - datetime.strptime(earlier, fmt)).total_seconds()


def _rec(rtype, payload, timestamp):
    return json.dumps({"timestamp": timestamp, "type": rtype, "payload": payload})


def _session_meta(thread, root, timestamp, subagent=False, parent=None):
    source = {"other": "guardian"}
    if parent:
        source["thread_spawn"] = {"parent_thread_id": parent,
                                  "agent_nickname": "reviewer"}
    return _rec("session_meta", {
        "id": thread,
        "session_id": root,
        "cwd": "/home/u/proj",
        "originator": "codex_vscode",
        "source": {"subagent": source} if subagent else "vscode",
        "thread_source": "subagent" if subagent else "user",
        "git": {"commit_hash": "abc123", "branch": "main"},
    }, timestamp)


def _turn_context(timestamp, model="gpt-5.6-sol"):
    return _rec("turn_context", {"turn_id": "t-1", "cwd": "/home/u/proj",
                                 "model": model, "effort": "max"}, timestamp)


def _token_count(cum_total, inp, out, timestamp):
    """One API response. `cum_total` is the per-LINEAGE running total that
    doubles as the response's identity."""
    return _rec("event_msg", {
        "type": "token_count",
        "info": {
            "last_token_usage": {
                "input_tokens": inp, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "output_tokens": out,
                "reasoning_output_tokens": 0, "total_tokens": inp + out,
            },
            "total_token_usage": {
                "input_tokens": 0, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "output_tokens": 0,
                "reasoning_output_tokens": 0, "total_tokens": cum_total,
            },
            "model_context_window": 258400,
        },
    }, timestamp)


class CodexTreeFixture(unittest.TestCase):
    """A synthetic corpus in the real `sessions/YYYY/MM/DD/rollout-...` layout.

    The layout is the point: `discover_jsonl_files` sorts whole path strings, so
    the day directory and the filename's wall clock are both load-bearing and a
    fixture that flattened them would pin nothing.
    """

    ROOT = "019fce00-0000-7000-8000-000000000001"
    CHILD = "019fce00-0001-7000-8000-000000000002"
    SIBLING = "019fce00-0002-7000-8000-000000000003"
    UNRELATED = "019fce00-0003-7000-8000-000000000004"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sessions = Path(self.tmp) / "sessions"
        self.db = Path(self.tmp) / "usage.db"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rollout(self, lines, day, wall_clock, millis, tag):
        """Write one rollout. `day` is "YYYY-MM-DD"; `wall_clock` is the LOCAL
        clock Codex stamps into the name; `millis` is the uuidv7's clock."""
        directory = self.sessions.joinpath(*day.split("-"))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"rollout-{wall_clock}-{_uuid7(millis, tag)}.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def write_parent(self, day, wall_clock, millis, opened):
        """The thread that actually produced the lineage's first two responses.

        The accumulator's own arithmetic: 900+100 -> 1000, +1100+100 -> 2200.
        """
        return self.rollout([
            _session_meta(self.ROOT, self.ROOT, opened),
            _turn_context(_ts(opened, 1)),
            _token_count(1000, 900, 100, _ts(opened, 2)),
            _token_count(2200, 1100, 100, _ts(opened, 300)),
        ], day, wall_clock, millis, tag="a")

    def write_child(self, day, wall_clock, millis, spawned, thread=None,
                    tag="b", own=(450, 50)):
        """A subagent thread: the parent's whole history replayed verbatim at
        the spawn instant, then the child's own first response continuing the
        SAME accumulator (2200 + 450 + 50 -> 2700)."""
        used_in, used_out = own
        return self.rollout([
            _session_meta(thread or self.CHILD, self.ROOT, spawned,
                          subagent=True, parent=self.ROOT),
            _turn_context(_ts(spawned, 0.001)),
            _token_count(1000, 900, 100, _ts(spawned, 0.002)),
            _token_count(2200, 1100, 100, _ts(spawned, 0.002)),
            _token_count(2200 + used_in + used_out, used_in, used_out,
                         _ts(spawned, 30)),
        ], day, wall_clock, millis, tag=tag)

    def relative(self, paths):
        """Paths relative to the temp root, in the order given.

        Order is the whole subject here, and a list of a dozen
        `/var/folders/qd/...` absolutes is unreadable in an assertion message —
        which is the message a future reader gets when discovery order breaks.
        """
        return [Path(p).relative_to(self.tmp).as_posix() for p in paths]

    def discovered(self):
        return self.relative(discover_jsonl_files([self.sessions]))

    def scan(self):
        return scanner.scan(projects_dir=self.sessions, db_path=self.db,
                            verbose=False)

    def rows(self, sql, *args):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args)]
        finally:
            conn.close()

    def earliest_copies(self):
        """Every parsed copy of every response, reduced to the earliest one.

        This was `codex_lineage_message_id_v1`'s rule — keep the
        earliest-timestamped copy — computed here independently of scan order,
        so a test can ask whether the shipped *first writer wins* rule chose the
        same row without trusting either implementation. The rewrite is gone
        (AGENTS.md invariant 6); the rule survives here as the yardstick it
        always was, which is why this helper is worth keeping.
        """
        best = {}
        for path in discover_jsonl_files([self.sessions]):
            for turn in scanner.parse_transcript(path)[1]:
                message_id = turn["message_id"]
                if message_id not in best or turn["timestamp"] < best[message_id]["timestamp"]:
                    best[message_id] = turn
        return best


class TestDiscoveryIsSorted(CodexTreeFixture):
    """Property 1. Every other property here rests on one line of
    `transcripts.discover_jsonl_files`: `return sorted(set(files))`."""

    def _plain(self, *paths):
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")

    def test_files_come_back_sorted_when_the_roots_are_not(self):
        """The discriminating case for the sort itself.

        Two things had to be arranged for a broken sort to show up here. Root
        order is ours — `discover_jsonl_files` walks `project_dirs` in the order
        given and appends — so handing it two roots in reverse catches any
        replacement that keeps traversal order (`list(...)`, `dict.fromkeys`, a
        bare generator) on every platform, including the ones whose file system
        hands back directory entries already sorted. And the twelve files are
        what catch a replacement that keeps the `set()`: two strings out of a
        set come back sorted about half the time, twelve essentially never.
        """
        roots = [Path(self.tmp) / "aaa", Path(self.tmp) / "zzz"]
        expected = []
        for root in roots:
            for n in range(6):
                path = root / f"{n}-transcript.jsonl"
                self._plain(path)
                expected.append(str(path))
        found = discover_jsonl_files(list(reversed(roots)))
        self.assertEqual(self.relative(found), self.relative(sorted(expected)),
                         "discovery stopped sorting")

    def test_nested_files_sort_ahead_of_the_ones_walk_yields_first(self):
        """The single-root half. Inside one directory the order `os.walk` yields
        is the file system's — hash order on APFS and ext4, name order on NTFS —
        so a flat corpus cannot prove the sort ran. Depth can: `os.walk` emits a
        directory's own files before it descends, so nested paths that sort
        *earlier* than the top-level ones are inverted by the traversal
        everywhere.
        """
        root = Path(self.tmp) / "root"
        day = root / "2026" / "08" / "05"
        expected = []
        for n in range(4):
            nested = day / f"rollout-{n}.jsonl"
            top = root / f"zz-{n}.jsonl"
            self._plain(nested, top)
            expected += [str(nested), str(top)]
        self.assertEqual(self.relative(discover_jsonl_files([root])),
                         self.relative(sorted(expected)))

    def test_a_file_reachable_through_two_roots_is_returned_once(self):
        """The `set()` in the same expression. Roots may legitimately overlap
        now that extra ones can be configured (a parent and its child, a bind
        mount and its host path), and a file parsed twice writes a second
        `processed_files` row for the same bytes."""
        root = Path(self.tmp) / "root"
        inner = root / "inner"
        path = inner / "one.jsonl"
        self._plain(path)
        self.assertEqual(self.relative(discover_jsonl_files([root, inner])),
                         self.relative([path]))


class TestPathOrderIsSpawnOrderInsideALineage(CodexTreeFixture):
    """Property 2, and only as far as it actually goes.

    A parent and the threads it spawns are named by one process off one clock,
    and a spawning thread necessarily starts first — so *within a lineage* the
    sorted list puts the ancestor before its descendants. That is the whole
    claim. It says nothing about two unrelated rollouts, and the last test here
    exists to stop it from being read as if it did.
    """

    def test_a_parent_is_discovered_before_the_child_it_spawns(self):
        parent = self.write_parent("2026-08-05", "2026-08-05T09-00-00",
                                   1_785_920_400_000, "2026-08-05T09:00:00.000Z")
        child = self.write_child("2026-08-05", "2026-08-05T10-00-00",
                                 1_785_924_000_000, "2026-08-05T10:00:00.000Z")
        self.assertEqual(self.discovered(), self.relative([parent, child]))

    def test_a_spawn_across_midnight_keeps_the_parent_first(self):
        """The day directory is part of the sorted string, so a child spawned
        after local midnight lands under the next `YYYY/MM/DD` — which still
        sorts after its parent's, because the directory and the name carry the
        same clock in the same order."""
        parent = self.write_parent("2026-08-04", "2026-08-04T23-50-00",
                                   1_785_887_400_000, "2026-08-04T23:50:00.000Z")
        child = self.write_child("2026-08-05", "2026-08-05T00-10-00",
                                 1_785_888_600_000, "2026-08-05T00:10:00.000Z")
        self.assertEqual(self.discovered(), self.relative([parent, child]))

    def test_siblings_spawned_in_one_second_order_by_their_uuid7(self):
        """Filename timestamps have one-second resolution. Synthetic sibling rollouts
        share that timestamp and use the UUIDv7 millisecond prefix to break ties
        while keeping their shared ancestor first."""
        parent = self.write_parent("2026-08-05", "2026-08-05T09-00-00",
                                   1_785_920_400_000, "2026-08-05T09:00:00.000Z")
        first = self.write_child("2026-08-05", "2026-08-05T09-10-00",
                                 1_785_921_000_123, "2026-08-05T09:10:00.123Z",
                                 thread=self.CHILD, tag="b")
        second = self.write_child("2026-08-05", "2026-08-05T09-10-00",
                                  1_785_921_000_456, "2026-08-05T09:10:00.456Z",
                                  thread=self.SIBLING, tag="c", own=(600, 40))
        self.assertEqual(self.discovered(),
                         self.relative([parent, first, second]))

    def test_two_unrelated_rollouts_may_sort_against_the_clock(self):
        """Filename wall time is not globally chronological. A daylight-saving fold can
place a later run before an earlier one in lexical order."""
        container = self.rollout([
            _session_meta(self.UNRELATED, self.UNRELATED,
                          "2026-08-05T09:30:00.000Z"),
            _turn_context("2026-08-05T09:30:01.000Z"),
            _token_count(700, 600, 100, "2026-08-05T09:30:02.000Z"),
        ], "2026-08-05", "2026-08-05T09-30-00", 1_785_922_200_000, tag="d")
        host = self.write_parent("2026-08-05", "2026-08-05T11-00-00",
                                 1_785_920_400_000, "2026-08-05T09:00:00.000Z")
        self.assertEqual(self.discovered(), self.relative([container, host]),
                         "the later run sorts first in the synthetic clock-fold fixture")
        self.scan()
        stamps = {r["message_id"]: r["timestamp"]
                  for r in self.rows("SELECT message_id, timestamp FROM turns")}
        self.assertEqual(len(stamps), 3, "no response is shared between them")
        self.assertEqual(stamps[f"codex:{self.ROOT}:1000"],
                         "2026-08-05T09:00:02.000Z")


class TestTheStoredCopyIsTheEarliestCopy(CodexTreeFixture):
    """Property 3, and the join of all three: the two merge rules agree.

    A spawned thread opens its own rollout, replays its ancestors' entire
    `token_count` history into it verbatim at the spawn instant, and then
    continues the same accumulator — so the ancestor's copy of a shared response
    is always the earlier one, and discovery reaches it first.
    """

    def lineage(self):
        return (self.write_parent("2026-08-05", "2026-08-05T09-00-00",
                                  1_785_920_400_000, "2026-08-05T09:00:00.000Z"),
                self.write_child("2026-08-05", "2026-08-05T10-00-00",
                                 1_785_924_000_000, "2026-08-05T10:00:00.000Z"))

    def test_the_replay_is_later_than_the_records_it_copies(self):
        """The fixture's own faithfulness, asserted rather than assumed: if the
        replay were not strictly later there would be nothing for the rest of
        this class to choose between."""
        parent_path, child_path = self.lineage()
        parent = {t["message_id"]: t["timestamp"]
                  for t in scanner.parse_transcript(parent_path)[1]}
        child = {t["message_id"]: t["timestamp"]
                 for t in scanner.parse_transcript(child_path)[1]}
        shared = set(parent) & set(child)
        self.assertEqual(len(shared), 2, "the child must replay both responses")
        for message_id in shared:
            self.assertLess(parent[message_id], child[message_id])

    def test_first_writer_wins_picks_the_row_earliest_wins_would(self):
        """The property the whole design silently depends on, stated as the
        comparison itself: `insert_turns` keeps the first copy read, the
        earliest-wins rule the deleted `codex_lineage_message_id_v1` rewrite
        applied keeps the earliest-timestamped copy, and on a corpus obeying
        properties 1-3 those are the same row. Reverse discovery order and this
        fails on every shared response."""
        self.lineage()
        earliest = self.earliest_copies()
        self.scan()
        stored = self.rows("SELECT message_id, timestamp, is_subagent, agent_id, "
                           "session_id FROM turns")
        self.assertEqual(len(stored), 3)
        for row in stored:
            want = earliest[row["message_id"]]
            self.assertEqual(row["timestamp"], want["timestamp"])
            self.assertEqual(row["is_subagent"], want["is_subagent"])
            self.assertEqual(row["agent_id"], want["agent_id"])
            self.assertEqual(row["session_id"], want["session_id"])

    def test_the_parent_keeps_the_response_the_child_only_replayed(self):
        """The same thing spelled out, because the failure it guards against is
        specific: a turn dated and attributed to the thread that REPLAYED it
        rather than the thread that produced it. Only the child's own third
        response is a subagent turn."""
        self.lineage()
        self.scan()
        stored = {r["message_id"]: r for r in self.rows(
            "SELECT message_id, timestamp, is_subagent, agent_id FROM turns")}
        for cumulative in (1000, 2200):
            row = stored[f"codex:{self.ROOT}:{cumulative}"]
            self.assertEqual(row["is_subagent"], 0, "attributed to the replay")
            self.assertIsNone(row["agent_id"])
            self.assertTrue(row["timestamp"].startswith("2026-08-05T09:"),
                            f"dated by the replay: {row['timestamp']}")
        own = stored[f"codex:{self.ROOT}:2700"]
        self.assertEqual(own["is_subagent"], 1)
        self.assertEqual(own["agent_id"], self.CHILD)

    def test_a_fan_out_leaves_the_parents_responses_with_the_parent(self):
        """Synthetic siblings share a filename second and replay the same parent prefix.
Their own new turns must remain distinct."""
        self.write_parent("2026-08-05", "2026-08-05T09-00-00",
                          1_785_920_400_000, "2026-08-05T09:00:00.000Z")
        self.write_child("2026-08-05", "2026-08-05T09-10-00",
                         1_785_921_000_123, "2026-08-05T09:10:00.123Z",
                         thread=self.CHILD, tag="b")
        self.write_child("2026-08-05", "2026-08-05T09-10-00",
                         1_785_921_000_456, "2026-08-05T09:10:00.456Z",
                         thread=self.SIBLING, tag="c", own=(600, 40))
        self.scan()
        stored = {r["message_id"]: r for r in self.rows(
            "SELECT message_id, timestamp, is_subagent, agent_id, "
            "input_tokens, output_tokens FROM turns")}
        self.assertEqual(len(stored), 4, "a replay was billed as new usage")
        for cumulative in (1000, 2200):
            row = stored[f"codex:{self.ROOT}:{cumulative}"]
            self.assertEqual(row["is_subagent"], 0)
            self.assertTrue(row["timestamp"].startswith("2026-08-05T09:0"))
        self.assertEqual(stored[f"codex:{self.ROOT}:2700"]["agent_id"], self.CHILD)
        self.assertEqual(stored[f"codex:{self.ROOT}:2840"]["agent_id"], self.SIBLING)
        self.assertEqual(sum(r["input_tokens"] for r in stored.values()), 3050)
        self.assertEqual(sum(r["output_tokens"] for r in stored.values()), 290)


class TestTheDaylightSavingWindow(CodexTreeFixture):
    """The one window where property 2 genuinely fails, on a zone that exists.

    Codex stamps the *local* wall clock into the rollout name, so a fall-back
    repeats an hour of that clock and a thread spawned twenty real minutes after
    its parent can be named forty minutes *before* it. Discovery then reads the
    child's replay first, and `insert_turns` is first-writer-wins.

    **The zone is Europe/Madrid on 2026-10-25** — a real, reproducible
    transition that actually happens: 03:00 CEST becomes 02:00 CET at 01:00 UTC,
    so the local wall clock 02:00-02:59 is stamped into rollout names twice, an
    hour of real time apart.

    An earlier version of this class used 2026-11-01 — the *US* fall-back date —
    with a UTC+2 -> UTC+1 shift, which is the *EU* one, and the EU fell back a
    week earlier. That combination puts the transition at exactly 00:00 UTC.
    Re-deriving the 2026 tz database here with `zoneinfo` (598 zones scanned at
    15-minute resolution, 199 fall-back transitions, instants located to the
    second) says **no zone falls back at 00:00 UTC at all** — the fixture
    modelled a configuration no user is ever in, and that invented zone was the
    sole source of the "the turn moves to a different calendar day" claim this
    class used to make. What the same census says about the real thing:

    - **0 of 598** zones have a repeated local window crossing local midnight. A
      fall-back repeats a wall-clock hour and both copies necessarily lie inside
      it, so the LOCAL day — what invariant 4 buckets on, and with it
      `cli.py today/week/stats`, the daily chart, project-by-day, effort-by-day
      and stop-reason-by-day — can never move.
    - **3 of 598** span two local *hour* buckets: Antarctica/Troll,
      Pacific/Chatham and NZ-CHAT. So even the hourly chart usually holds.
    - **1 of 598** reaches across UTC midnight: Antarctica/Troll, whose
      fall-back is two hours. It is the only zone on Earth that produces the
      day-crossing variant, and nothing here claims otherwise.

    What is left is real, and is what these tests pin: the parent's whole
    pre-spawn history is dated by the replay and filed as the subagent's work.
    AGENTS.md invariant 9 names this window and says it does not fire today; it
    is untested rather than impossible, so it is tested here — but note what
    `test_the_inversion_moves_the_attribution_onto_the_replay` asserts. It pins
    the CURRENT, WRONG answer.

    The fixture reads no tz database. `zoneinfo` needs a system tz source that
    Windows does not ship, nothing else in this suite consults one, and the
    corpus has to stay independent of the runner's own `TZ` — so the transition
    is three constants and the two offsets either side of it, and the census
    above is recorded evidence rather than a runtime dependency.
    """

    # Europe/Madrid's 2026 fall-back, and the offsets either side of it.
    TRANSITION = "2026-10-25T01:00:00.000Z"
    CEST = timezone(timedelta(hours=2))     # before: the first pass of 02:00-02:59
    CET = timezone(timedelta(hours=1))      # after: the second pass of the same hour
    PARENT_OPENED = "2026-10-25T00:50:00.000Z"   # 02:50 CEST
    CHILD_SPAWNED = "2026-10-25T01:10:00.000Z"   # 02:10 CET, twenty real minutes later

    def wall(self, stamp):
        """ISO-8601-Z `stamp` on the Madrid wall clock, as a `datetime`."""
        moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        return moment.astimezone(self.CEST if stamp < self.TRANSITION
                                 else self.CET)

    def local(self, stamp):
        """`stamp` as the (local day, local hour) every day-bucketed view sees."""
        wall = self.wall(stamp)
        return wall.strftime("%Y-%m-%d"), wall.hour

    def lineage(self):
        """Parent opens 02:50 CEST (00:50 UTC), in the first pass through the
        repeated hour; the child spawns twenty real minutes later at 02:10 CET
        (01:10 UTC), in the second pass, after the clock went back.

        The day directory, the filename's wall clock and the uuidv7's clock are
        all *derived* from the two UTC instants rather than typed beside them —
        which is the specific thing that went wrong before, when a hand-typed
        name and a hand-typed record drifted onto different transitions.
        """
        parent = self.write_parent(
            self.wall(self.PARENT_OPENED).strftime("%Y-%m-%d"),
            self.wall(self.PARENT_OPENED).strftime("%Y-%m-%dT%H-%M-%S"),
            _millis(self.PARENT_OPENED), self.PARENT_OPENED)
        child = self.write_child(
            self.wall(self.CHILD_SPAWNED).strftime("%Y-%m-%d"),
            self.wall(self.CHILD_SPAWNED).strftime("%Y-%m-%dT%H-%M-%S"),
            _millis(self.CHILD_SPAWNED), self.CHILD_SPAWNED)
        return parent, child

    def test_the_fixture_models_a_fall_back_that_really_happens(self):
        """The fixture's own faithfulness, asserted rather than commented.

        Three things have to hold together, and the version of this class that
        mixed the US date with the EU offsets held none of them: the two opens
        must straddle the transition, the shift must be the one hour Madrid
        actually performs, and the two names must therefore land on one local
        day and one repeated local hour. A transition at exactly 00:00 UTC — the
        one the invented zone implied — appears 0 times among the 199 fall-backs
        in the 2026 tz database; the 76 at 01:00 UTC include this one.
        """
        self.assertLess(self.PARENT_OPENED, self.TRANSITION,
                        "the parent must open in the first pass of the hour")
        self.assertGreater(self.CHILD_SPAWNED, self.TRANSITION,
                           "the child must spawn in the second pass")
        self.assertNotEqual(self.TRANSITION[11:], "00:00:00.000Z",
                            "0 of 199 fall-backs in the 2026 tz database are at "
                            "00:00 UTC — a transition there is an invented zone")
        self.assertEqual(self.CEST.utcoffset(None) - self.CET.utcoffset(None),
                         timedelta(hours=1), "Madrid falls back by one hour")
        self.assertEqual(
            _seconds_between(self.PARENT_OPENED, self.CHILD_SPAWNED), 20 * 60,
            "the child spawns twenty real minutes after the parent")
        self.assertEqual(self.local(self.PARENT_OPENED), ("2026-10-25", 2))
        self.assertEqual(self.local(self.CHILD_SPAWNED), ("2026-10-25", 2))

    def test_a_fall_back_hour_inverts_path_order_inside_one_lineage(self):
        """Property 2 broken, from the outside: the child sorts first."""
        parent, child = self.lineage()
        self.assertEqual(self.discovered(), self.relative([child, parent]))
        ran_first = scanner.parse_transcript(parent)[1][0]["timestamp"]
        ran_second = scanner.parse_transcript(child)[1][0]["timestamp"]
        self.assertLess(ran_first, ran_second,
                        "the parent still ran first in real time")

    def test_the_producer_replaces_a_replay_that_sorted_first(self):
        """Path order may invert, but producer attribution may not.

        The child's replay arrives first in this fixture. The later parent copy
        ties on usage and therefore needs the Codex-specific producer rule in
        `insert_turns`; generic `_MORE_COMPLETE` deliberately requires matching
        attribution and cannot adjudicate the two copies.
        """
        self.lineage()
        self.scan()
        stored = {r["message_id"]: r for r in self.rows(
            "SELECT message_id, timestamp, is_subagent, agent_id FROM turns")}
        expected = {
            1000: "2026-10-25T00:50:02.000Z",
            2200: "2026-10-25T00:55:00.000Z",
        }
        for cumulative, timestamp in expected.items():
            row = stored[f"codex:{self.ROOT}:{cumulative}"]
            self.assertEqual(row["timestamp"], timestamp)
            self.assertEqual(row["is_subagent"], 0)
            self.assertIsNone(row["agent_id"])
        # The control: the child's own response is subagent work whatever the
        # discovery order, so it must NOT flip when this class is fixed.
        own = stored[f"codex:{self.ROOT}:2700"]
        self.assertEqual(own["is_subagent"], 1)
        self.assertEqual(own["agent_id"], self.CHILD)

    def test_the_whole_replayed_prefix_is_restored_not_one_response(self):
        """A child replays its ancestors' entire token-count history.

        Every response in a replayed parent prefix must retain its producer.
        Derive the checked set from the fixture files so longer prefixes remain
        covered without maintaining a second list of expected responses."""
        parent_path, child_path = self.lineage()
        parent = {t["message_id"] for t in scanner.parse_transcript(parent_path)[1]}
        child = {t["message_id"] for t in scanner.parse_transcript(child_path)[1]}
        replayed = parent & child
        self.assertEqual(len(replayed), 2, "the child must replay a PREFIX")
        self.scan()
        stored = {r["message_id"]: r for r in self.rows(
            "SELECT message_id, is_subagent, agent_id FROM turns")}
        restored = {m for m in replayed if stored[m]["agent_id"] is None}
        self.assertEqual(restored, replayed,
                         "only part of the replayed prefix was restored to its "
                         "producer")

    def test_the_replay_keeps_the_producers_instant(self):
        """The stored timestamp is independent of inverted filename order."""
        message_id = f"codex:{self.ROOT}:1000"
        parent_path, _ = self.lineage()
        truth = {t["message_id"]: t["timestamp"]
                 for t in scanner.parse_transcript(parent_path)[1]}[message_id]
        self.scan()
        stored = self.rows("SELECT timestamp FROM turns WHERE message_id = ?",
                           message_id)[0]["timestamp"]
        self.assertEqual(stored, truth)
        self.assertEqual(truth, "2026-10-25T00:50:02.000Z")

    def test_the_inversion_does_not_move_any_money(self):
        """The blast radius, bounded. Both copies of a replayed response carry
        identical tallies, so the six token columns merge under MAX() to the
        same numbers whichever is read first: this window costs attribution and
        dating, never a token."""
        self.lineage()
        self.scan()
        rows = self.rows("SELECT input_tokens, output_tokens FROM turns")
        self.assertEqual(len(rows), 3, "the replay was billed again")
        self.assertEqual(sum(r["input_tokens"] for r in rows), 2450)
        self.assertEqual(sum(r["output_tokens"] for r in rows), 250)


if __name__ == "__main__":
    unittest.main()
