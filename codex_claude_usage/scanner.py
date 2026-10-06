"""
scanner.py - Scans Claude Code JSONL transcript files and stores data in SQLite.
"""

import hashlib
import os
import sys
import threading
from pathlib import Path
from datetime import datetime, timezone

# Storage lives in db.py. Re-exported so `from scanner import init_db` and
# `scanner.DB_PATH` keep resolving for cli.py, dashboard.py and the tests.
from .safetext import _bounded_text, invocation, terminal_safe
from .transcripts import (
    TranscriptReadError,
    extract_limit_event,
    MAX_JSONL_LINE_LENGTH, MAX_TRANSCRIPT_INTEGER, discover_jsonl_files,
    ends_with_newline, extract_agent_dispatch, is_subagent_record,
    parse_jsonl_file,
    project_name_from_cwd, record_agent_id, _extract_title, _iter_jsonl_lines,
    _load_json_object, _nonnegative_integer, _open_transcript,
    _optional_nonnegative_integer,
)
from . import codex_transcripts
from .timestamps import (
    register_timestamp_order, timestamp_compare, timestamp_max, timestamp_min,
    timestamp_order,
)
from .db import (
    DB_PATH, PROCESSED_FILE_KEY_PREFIX, get_db, init_db, secure_db_permissions,
    normalize_source, normalize_stored_sources as _normalize_stored_sources,
    _is_processed_file_key,
    _processed_file_key, _processed_line_count, _processed_mtime,
    _processed_prefix_hash, _processed_size, _processed_identity,
)

if sys.version_info < (3, 11):
    raise RuntimeError("Codex / Claude Usage requires Python 3.11 or newer.")

# Single source of truth for the app version reported by the CLI (`--version`)
# and the dashboard footer. docs/CHANGELOG.md is the canonical version reference, but
# it isn't bundled into the .vsix, so the runtime version has to live here as a
# constant. Keep this in lockstep with the top CHANGELOG heading and
# vscode-extension/package.json (a parity test guards all three; see
# tests/test_version.py).
VERSION = "1.7.0"

PROJECTS_DIR = Path.home() / ".claude" / "projects"
XCODE_PROJECTS_DIR = Path.home() / "Library" / "Developer" / "Xcode" / "CodingAssistant" / "ClaudeAgentConfig" / "projects"
# Codex writes one rollout per thread under ~/.codex/sessions/YYYY/MM/DD/.
# Scanned by default alongside Claude's roots: which assistant a transcript came
# from is decided by sniffing the file (see codex_transcripts.looks_like_codex),
# not by which directory it was found in, so an extra root can hold either.
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
# Scanned by default now that every view is source-separated: Codex tokens land
# in their own rows, and the dashboard shows one assistant at a time. A machine
# that has never run Codex simply finds nothing here — an absent default root is
# not something to warn about (see resolve_scan_roots).
DEFAULT_PROJECTS_DIRS = [PROJECTS_DIR, XCODE_PROJECTS_DIR, CODEX_SESSIONS_DIR]

# Additional transcript roots, os.pathsep-separated (':' on POSIX, ';' on
# Windows). Always scanned *in addition to* DEFAULT_PROJECTS_DIRS — the point of
# extra roots is somewhere Claude Code also wrote (a container that bind-mounts
# its own ~/.claude, a second machine's history copied across), not a
# replacement for your own.
EXTRA_PROJECTS_DIRS_ENV = "CODEX_CLAUDE_USAGE_PROJECTS_DIRS"

# Higher number = higher priority when choosing a session's primary model.
# Fable / Mythos are Anthropic's most capable class, so they outrank Opus.
MODEL_PRIORITY = {"fable": 5, "mythos": 5, "opus": 3, "sonnet": 2, "haiku": 1}
# Individual transcript counters above one billion are not credible Claude
# usage values. Rejecting them prevents malicious JSON from overflowing later
# SQLite SUM operations while leaving several orders of magnitude of headroom.
_SCAN_LOCK = threading.Lock()


def _register_timestamp_functions(conn):
    """Expose the neutral timestamp ordering to SQLite upserts."""
    register_timestamp_order(conn)
    conn.create_function("timestamp_min_raw", 2, timestamp_min)
    conn.create_function("timestamp_max_raw", 2, timestamp_max)
    conn.create_function(
        "timestamp_is_later", 2,
        lambda incoming, stored: timestamp_compare(incoming, stored) > 0)

def _model_priority(model):
    """Return a priority score for a model name (higher = more capable)."""
    if not model:
        return 0
    m = model.lower()
    for keyword, priority in MODEL_PRIORITY.items():
        if keyword in m:
            return priority
    return 0


def _model_priority_sql(column):
    """`_model_priority` as a SQL expression over `column`.

    Generated from MODEL_PRIORITY rather than written out, so the priority table
    stays a single source of truth: a keyword added there must not need a second,
    hand-synced copy in SQL. `instr` mirrors Python's `in` exactly and, unlike
    LIKE, carries no wildcard characters to escape; CASE takes the first matching
    WHEN, which is the same "first keyword found, in insertion order" rule the
    Python loop applies.
    """
    whens = " ".join(
        "WHEN instr(lower({col}), '{kw}') > 0 THEN {p}".format(
            col=column, kw=keyword.replace("'", "''"), p=int(priority))
        for keyword, priority in MODEL_PRIORITY.items())
    return f"CASE {whens} ELSE 0 END"


def upsert_agents(conn, agents):
    """Insert or update dispatch metadata per source-qualified agent id."""
    if not agents:
        return
    _normalize_stored_sources(conn)
    conn.executemany("""
        INSERT INTO agents
            (source, agent_id, agent_type, dispatched_in_session, completed_at,
             status, total_tokens, total_duration_ms, tool_use_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, agent_id) DO UPDATE SET
            agent_type            = excluded.agent_type,
            dispatched_in_session = excluded.dispatched_in_session,
            completed_at          = excluded.completed_at,
            status                = excluded.status,
            total_tokens          = excluded.total_tokens,
            total_duration_ms     = excluded.total_duration_ms,
            tool_use_count        = excluded.tool_use_count
    """, [
        (normalize_source(a.get("source")), a["agent_id"], a["agent_type"],
         a.get("dispatched_in_session"),
         a.get("completed_at"), a.get("status"),
         a.get("total_tokens"), a.get("total_duration_ms"), a.get("tool_use_count"))
        for a in agents
    ])


def upsert_limit_events(conn, events):
    """Record rate-limit notices, keyed by the record's own uuid.

    A rescan updates an existing notice rather than duplicating it. Incident
    grouping separately combines the notices generated by retries and
    subagents."""
    if not events:
        return
    conn.executemany("""
        INSERT INTO limit_events
            (event_uuid, kind, session_id, timestamp, timestamp_order, status,
             message, reset_hint, reset_zone)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_uuid) DO UPDATE SET
            kind       = excluded.kind,
            session_id = excluded.session_id,
            timestamp  = excluded.timestamp,
            timestamp_order = excluded.timestamp_order,
            status     = excluded.status,
            message    = excluded.message,
            reset_hint = excluded.reset_hint,
            reset_zone = excluded.reset_zone
    """, [
        (e["event_uuid"], e.get("kind", ""), e["session_id"], e["timestamp"],
         timestamp_order(e.get("timestamp")), e.get("status"),
         e.get("message", ""), e.get("reset_hint", ""), e.get("reset_zone", ""))
        for e in events
    ])


def _env_extra_dirs(env=None):
    """Extra roots from CODEX_CLAUDE_USAGE_PROJECTS_DIRS, in order, blanks dropped."""
    raw = (os.environ if env is None else env).get(EXTRA_PROJECTS_DIRS_ENV, "")
    return [Path(part) for part in raw.split(os.pathsep) if part.strip()]


def resolve_scan_roots(projects_dir=None, projects_dirs=None,
                       include_defaults=False, env=None):
    """The directories a scan should walk, de-duplicated, in a stable order.

    `include_defaults` is what separates the two callers. The CLI passes True:
    `--projects-dir` there means "also look here", and the defaults are never
    dropped — scanning a container's mounted history must not make your own
    disappear from the same database. The Python API defaults to False, so
    `scan(projects_dir=tmp)` still means *exactly* that one directory; the
    tests depend on it, and so does anything wanting a scan of a known corpus
    rather than of whatever happens to be in the caller's home directory.

    Returns `(roots, missing)`. `missing` holds only roots that were asked for
    explicitly and do not exist — a default that is absent (the Xcode directory,
    on every machine without Xcode) is not something to complain about once per
    scan.
    """
    requested = []
    if projects_dirs:
        requested.extend(Path(d) for d in projects_dirs)
    elif projects_dir:
        requested.append(Path(projects_dir))
    requested.extend(_env_extra_dirs(env))

    ordered = (list(DEFAULT_PROJECTS_DIRS) + requested) if include_defaults else list(requested)

    roots, missing, seen = [], [], set()
    for candidate in ordered:
        # Resolve for identity only — the original path is what gets walked and
        # printed, so a symlinked root still reads the way the user wrote it.
        # Overlapping roots would otherwise walk the same tree twice and inflate
        # processed_files with a second hashed key for every file.
        try:
            key = candidate.resolve()
        except OSError:
            key = candidate
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_dir():
            roots.append(candidate)
        elif candidate in requested:
            missing.append(candidate)
    return roots, missing


def record_limit_snapshot(conn, rows):
    """Persist the EARLIEST observation of each plan-limit window.

    One row per (window, percentage), so a rescan that re-reads an unchanged
    cache writes nothing and each new level costs exactly one row. The instant
    kept for a level is the first one at which it was seen — so the stored
    series answers "when did this window reach this level", not "when did I
    last look".

    Upsert the earliest observation rather than keeping the first row offered.
    Overlapping rollouts and imported directories can arrive in either order.
    The parser also keeps the first record of a repeated level within a file.

    Two details are load-bearing:

    - the persisted `observed_at_order` guard, without which the no-op promise
      above becomes false — and using the shared UTC key rather than raw ISO
      text is what makes mixed offsets chronological;
    - updating the whole row rather than `observed_at` alone, so what is stored
      stays one real observation instead of one observation's instant beside
      another observation's severity.

    Note the one behaviour this gives up. `INSERT OR IGNORE` silently dropped a
    row violating ANY constraint, not just the primary key; `ON CONFLICT` raises
    on the others (a NULL `observed_at` becomes `IntegrityError` instead of no
    row and no word). Neither caller can produce one — `codex_snapshot_rows`
    defaults every field and `account.snapshot_rows` runs inside `scan()`'s own
    try/except — and a limit row vanishing in silence is exactly the failure
    `route_limit_records` exists to make noisy.
    """
    if not rows:
        return
    _register_timestamp_functions(conn)
    conn.executemany("""
        INSERT INTO usage_limits_snapshots
            (kind, grp, scope, resets_key, percent, severity, is_active,
             resets_at, resets_at_order, fetched_at_ms, observed_at,
             observed_at_order)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(kind, grp, scope, resets_key, percent) DO UPDATE SET
            severity      = excluded.severity,
            is_active     = excluded.is_active,
            resets_at     = excluded.resets_at,
            resets_at_order = excluded.resets_at_order,
            fetched_at_ms = excluded.fetched_at_ms,
            observed_at   = excluded.observed_at,
            observed_at_order = excluded.observed_at_order
        WHERE excluded.observed_at_order <
              COALESCE(NULLIF(usage_limits_snapshots.observed_at_order, ''),
                       timestamp_order(usage_limits_snapshots.observed_at))
    """, [
        (*row[:8], timestamp_order(row[7]), row[8], row[9],
         timestamp_order(row[9]))
        for row in rows
    ])


def codex_snapshot_rows(snapshots):
    """Codex quota observations -> `usage_limits_snapshots` rows.

    This is the one place Codex beats Claude Code outright. Claude's quota state
    lives in a single mutable cache that is overwritten in place, so the only
    recoverable view is "whatever the last refresh left behind" — which is what
    invariant 7 exists to apologise for. Codex stamps the same state onto an
    append-only transcript with every API response, so the *history* is real: a
    window filling up, and a window resetting, are both directly observable.

    Keyed like the Claude side on (window, percent), so re-reading an unchanged
    figure writes nothing and each new level costs exactly one row, stamped with
    the first moment that level was seen (see `record_limit_snapshot`).
    `resets_at` is floored to the minute for the same reason it is there: the raw
    value jitters by up to 26 seconds within one window, and the unrounded string
    would invent a new window on every read.
    """
    rows = []
    for snap in snapshots:
        epoch = snap.get("resets_at_epoch")
        if epoch:
            moment = datetime.fromtimestamp(epoch, timezone.utc).replace(second=0, microsecond=0)
            resets_key = moment.strftime("%Y-%m-%dT%H:%M")
            resets_at = moment.isoformat()
        else:
            resets_key, resets_at = "", ""
        rows.append((
            snap.get("kind", "codex"),
            snap.get("group", ""),
            snap.get("plan_type", ""),
            resets_key,
            snap.get("percent", -1),
            snap.get("severity", "") or "",
            1 if snap.get("is_active") else 0,
            resets_at,
            0,                       # Codex has no fetchedAt: the record IS the observation
            snap.get("observed_at", ""),
        ))
    return rows


def route_limit_records(conn, records):
    """Send each limit record to the table its shape belongs in.

    The parsers share a five-tuple, and its fourth slot means "what this file
    said about limits" — which is two different facts. Claude records a notice
    that a limit was HIT (no percentage exists anywhere in its transcripts);
    Codex records the percentage continuously and has never recorded a hit. They
    go to different tables and neither is a substitute for the other.
    """
    notices, snapshots, unroutable = [], [], []
    for record in records:
        if "event_uuid" in record:
            notices.append(record)
        elif "percent" in record:
            snapshots.append(record)
        else:
            unroutable.append(record)
    # The partition has to be total. A record matching neither shape would
    # otherwise be dropped in silence, which is how a parser change quietly
    # stops recording limits at all — the table simply goes empty and nothing
    # says why.
    if unroutable:
        print(f"  Warning: {len(unroutable)} limit record(s) matched no known "
              f"shape and were not stored; keys seen: "
              f"{terminal_safe(sorted({k for r in unroutable for k in r})[:8])}")
    upsert_limit_events(conn, notices)
    record_limit_snapshot(conn, codex_snapshot_rows(snapshots))


def aggregate_sessions(session_metas, turns):
    """Aggregate turn data back into session-level stats."""
    from collections import defaultdict, Counter

    session_stats = defaultdict(lambda: {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read": 0,
        "total_cache_creation": 0,
        "total_cache_creation_1h": 0,
        "turn_count": 0,
        "model": None,
    })
    session_model_counts = defaultdict(Counter)

    for t in turns:
        source = normalize_source(t.get("source"))
        s = session_stats[(source, t["session_id"])]
        s["total_input_tokens"] += t["input_tokens"]
        s["total_output_tokens"] += t["output_tokens"]
        s["total_cache_read"] += t["cache_read_tokens"]
        s["total_cache_creation"] += t["cache_creation_tokens"]
        s["total_cache_creation_1h"] += t.get("cache_creation_1h_tokens", 0)
        s["turn_count"] += 1
        if t["model"]:
            session_model_counts[(source, t["session_id"])][t["model"]] += 1

    # The session's primary model, by the SAME rule upsert_sessions applies when
    # it updates an existing session: capability first (fable/mythos > opus >
    # sonnet > haiku), turn count only as a tiebreak within one priority. Picking
    # the most frequent model here instead made the answer depend on how the file
    # happened to be read — a session whose haiku subagent turns outnumbered its
    # opus ones was labelled haiku when scanned in one pass and opus when the
    # transcript grew between scans, from identical bytes on disk.
    for key, counts in session_model_counts.items():
        if counts:
            session_stats[key]["model"] = max(
                counts.items(), key=lambda pair: (_model_priority(pair[0]), pair[1]))[0]

    # Merge into session_metas
    result = []
    for meta in session_metas:
        source = normalize_source(meta.get("source"))
        sid = meta["session_id"]
        stats = session_stats[(source, sid)]
        result.append({**meta, **stats})
    return result


def upsert_sessions(conn, sessions):
    _register_timestamp_functions(conn)
    _normalize_stored_sources(conn)
    for s in sessions:
        source = normalize_source(s.get("source"))
        session_id = s["session_id"]
        # Check if session exists
        existing = conn.execute(
            "SELECT total_input_tokens, total_output_tokens, total_cache_read, "
            "total_cache_creation, turn_count FROM sessions "
            "WHERE source = ? AND session_id = ?",
            (source, session_id)
        ).fetchone()

        # A session seen only via a title record (custom-title / ai-title carry a
        # sessionId but no timestamp) has no real content. Don't let it INSERT a
        # phantom, token-less row; if the session already exists it still falls
        # through to the UPDATE below and sets its topic.
        if existing is None and not s.get("first_timestamp"):
            continue

        if existing is None:
            # ON CONFLICT, not a bare INSERT. The SELECT above holds no lock —
            # sqlite3 opens a transaction for DML only — so a second scanner
            # *process* that read the same absent row can insert first, and the
            # loser's bare INSERT raised `UNIQUE constraint failed:
            # sessions.session_id` straight out of `scan()`: `cli.py scan` exited
            # 1 with a traceback and a dashboard's startup scan printed
            # "Background scan failed" and stopped there. `_SCAN_LOCK` only
            # serialises scans inside ONE process, and the product creates the
            # cross-process case itself — one dashboard process per VS Code
            # window, all on the same ~/.claude/usage.db, plus a terminal
            # `cli.py scan` beside them. Every other write in this loop was
            # already conflict-safe (turns, agents, limit events, snapshots,
            # processed_files); `sessions` was the one bare INSERT that was
            # missed. `db.rebuild_database` answers the same race differently
            # because it cannot use a conflict target: it re-checks the schema
            # under BEGIN IMMEDIATE, so the loser drops nothing.
            #
            # Naming the conflict target beats both `INSERT OR IGNORE` and
            # `except sqlite3.IntegrityError`: every *other* integrity failure
            # still raises rather than being swallowed, and `rowcount == 0` says
            # precisely that we lost the race — so fall through to the UPDATE
            # below and behave exactly as if the SELECT had run a moment later,
            # contributing this chunk's topic, branch and last_timestamp instead
            # of dropping them. That also adds this chunk's tokens on top of the
            # winner's identical ones; the end-of-scan reconciliation rewrites
            # every session's totals from `turns` (invariant 2) and is what
            # repairs the double count — narrowing it to the sessions one scan
            # touched would leave it in place.
            inserted = conn.execute("""
                INSERT INTO sessions
                    (session_id, project_name, first_timestamp, last_timestamp,
                     first_timestamp_order, last_timestamp_order,
                     git_branch, total_input_tokens, total_output_tokens,
                     total_cache_read, total_cache_creation,
                     total_cache_creation_1h, model, turn_count, topic, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, session_id) DO NOTHING
            """, (
                session_id, s["project_name"], s["first_timestamp"],
                s["last_timestamp"], timestamp_order(s["first_timestamp"]),
                timestamp_order(s["last_timestamp"]), s["git_branch"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s.get("total_cache_creation_1h", 0),
                s["model"], s["turn_count"], s.get("topic"),
                source
            )).rowcount
            if inserted:
                continue

        # Update: add new tokens on top of existing (since we only insert new turns)
        # Keep the highest-priority model (e.g. opus over haiku from subagents)
        existing_row = conn.execute(
            "SELECT model, topic, project_name FROM sessions "
            "WHERE source = ? AND session_id = ?",
            (source, session_id)
        ).fetchone()
        existing_model = existing_row["model"]
        new_model = s["model"]
        if _model_priority(new_model) > _model_priority(existing_model):
            model_to_set = new_model
        else:
            model_to_set = existing_model

        # Apply the parser title rule across chunks: the last custom title
        # wins, while an AI title fills only a blank. This makes full and
        # incremental scans agree.
        new_topic = s.get("topic")
        existing_topic = existing_row["topic"]
        if new_topic and (s.get("topic_is_custom") or not existing_topic):
            topic_to_set = new_topic
        else:
            topic_to_set = existing_topic

        conn.execute("""
            UPDATE sessions SET
                -- The mirror of `last_timestamp` below, and it was missing:
                -- whichever chunk CREATED the row set `first_timestamp` and
                -- nothing could ever lower it, so a one-shot scan and an
                -- incremental scan of identical bytes stored different session
                -- start times whenever a session's earliest record was not in
                -- the chunk that created its row. Measured on a session whose
                -- later record carries the earlier timestamp: one-shot stored
                -- 09:00, incremental stored 18:00. No total moves, but the
                -- sessions table's Duration is `last - first`, so the same
                -- bytes gave two different durations depending on where a scan
                -- happened to land -- the path-dependence invariant 1 exists
                -- to kill, on the one column that had no merge rule at all.
                --
                -- Written out rather than `MIN(first_timestamp, ?)` because
                -- SQLite's two-argument `min()` returns NULL if either side is
                -- NULL, and '' is what this schema stores for "not recorded":
                -- a blank must be FILLED, not treated as the minimum, or every
                -- session with one unstamped record would start at ''.
                first_timestamp = timestamp_min_raw(first_timestamp, ?),
                first_timestamp_order = timestamp_order(
                    timestamp_min_raw(first_timestamp, ?)),
                last_timestamp = timestamp_max_raw(last_timestamp, ?),
                last_timestamp_order = timestamp_order(
                    timestamp_max_raw(last_timestamp, ?)),
                total_input_tokens = total_input_tokens + ?,
                total_output_tokens = total_output_tokens + ?,
                total_cache_read = total_cache_read + ?,
                total_cache_creation = total_cache_creation + ?,
                total_cache_creation_1h = total_cache_creation_1h + ?,
                turn_count = turn_count + ?,
                model = ?,
                topic = ?,
                -- A title record can introduce a session before any record
                -- carrying cwd. Fill that placeholder later, but never replace
                -- one real project with another merely because a later chunk
                -- or racing scanner observed a different cwd.
                project_name = CASE
                    WHEN COALESCE(project_name, '') IN ('', 'unknown')
                         AND ? NOT IN ('', 'unknown') THEN ?
                    ELSE project_name END,
                -- Fill-when-blank, the shape `topic` above and
                -- `reasoning_effort`/`stop_reason` in `insert_turns` already
                -- use. The INSERT set this column and the UPDATE never
                -- touched it, so a session first seen in a chunk that
                -- carried no branch kept an empty one for good. Filling
                -- rather than overwriting is the load-bearing half: Claude
                -- stamps a branch on essentially every record, so "newest
                -- chunk wins" would make a session that switches branch
                -- mid-file store its first branch on a one-shot read and its
                -- last on a split read — inventing on the priced source the
                -- path-dependence this is fixing on the other one.
                -- `rollups.project_by_day_model` takes its branch dimension
                -- straight from here, so this column splits money rows.
                git_branch = CASE WHEN COALESCE(git_branch, '') = ''
                                  THEN ? ELSE git_branch END
            WHERE source = ? AND session_id = ?
        """, (
            s.get("first_timestamp", ""), s.get("first_timestamp", ""),
            s["last_timestamp"], s["last_timestamp"],
            s["total_input_tokens"], s["total_output_tokens"],
            s["total_cache_read"], s["total_cache_creation"],
            s.get("total_cache_creation_1h", 0),
            s["turn_count"], model_to_set, topic_to_set,
            s.get("project_name", "unknown"),
            s.get("project_name", "unknown"),
            s.get("git_branch", ""),
            source, session_id
        ))


# Choose the more complete response once and reuse this predicate for
# timestamp and tool metadata. Equal cumulative tallies can still carry a
# later completion timestamp. Accept that update only when producer
# attribution agrees, so a replay from another session cannot replace the
# producer clock. IS handles NULL model equality. Compare timestamps with the
# shared instant-ordering helper.
_TALLY = "{t}.input_tokens + {t}.output_tokens + {t}.cache_read_tokens + {t}.cache_creation_tokens"
_ORDER = "COALESCE(NULLIF({t}.timestamp_order, ''), timestamp_order({t}.timestamp))"
_MORE_COMPLETE = f"""
                ({_TALLY.format(t="excluded")}) > ({_TALLY.format(t="turns")})
             OR (({_TALLY.format(t="excluded")}) = ({_TALLY.format(t="turns")})
                 AND {_ORDER.format(t="excluded")} > {_ORDER.format(t="turns")}
                 AND excluded.session_id  IS turns.session_id
                 AND excluded.model       IS turns.model
                 AND excluded.is_subagent IS turns.is_subagent
                 AND excluded.agent_id    IS turns.agent_id)"""

# Codex child rollouts replay their ancestor's cumulative token history. The
# lineage-scoped message id correctly makes those copies conflict, but path
# order is not chronological during a daylight-saving fall-back: a child opened
# in the repeated hour can sort before its parent. In that one ordering, generic
# first-writer-wins attribution assigns the parent's work to the child forever.
# A non-subagent copy of one Codex lineage id is the producer; a subagent copy is
# its replay. Prefer the producer whichever file was discovered first. Keep this
# source-specific: Claude's cross-transcript duplicates have a different shape
# and retain the generic first-writer rule.
_PREFER_CODEX_PRODUCER = """
                turns.source = 'codex'
            AND excluded.source = 'codex'
            AND turns.is_subagent = 1
            AND excluded.is_subagent = 0"""


def insert_turns(conn, turns):
    """Insert turns, merging usage per source-qualified message id.

    An incremental scan may stop mid-response. Raise each stored token count
    to the larger tally for that message so a later complete record repairs
    the partial value without creating a duplicate turn.

    MAX() is what makes this safe for the other dedup case this index handles:
    the same response appearing in two transcripts (e.g. a subagent's file and
    its parent's) carries identical tallies, so the update is a no-op and the
    first writer's model and is_subagent attribution still stand. Codex lineage
    replay is the deliberate exception: when a child is discovered before its
    parent, the later non-subagent producer replaces the replay's attribution.

    THREE rules decide what may move, not one. This paragraph named only the
    first of them ("only the six token columns move") while text columns
    accumulated under the second — `reasoning_effort` and `stop_reason`, then
    `git_branch`, which is the one that moves money — so a reader checking
    "what happens to my column on a conflict" against this summary got a wrong
    answer for three of them. The rules are: the six token columns rise under
    MAX(); the three text columns fill only when what is stored is blank;
    `timestamp` and `tool_name` follow the more complete record, which requires
    agreement about who produced it (see _MORE_COMPLETE). Every other column is
    the first writer's and never moves. Each rule is spelled out beside the SQL
    that implements it below — read that, not this summary, which is the copy
    that goes stale.
    """
    _register_timestamp_functions(conn)
    conn.executemany(f"""
        INSERT INTO turns
            (session_id, timestamp, timestamp_order, model, input_tokens, output_tokens,
             cache_read_tokens, cache_creation_tokens, cache_creation_1h_tokens,
             tool_name, cwd, message_id, is_subagent, agent_id,
             source, reasoning_output_tokens, reasoning_effort, stop_reason,
             git_branch)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, message_id)
            WHERE message_id IS NOT NULL AND message_id != ''
        DO UPDATE SET
            input_tokens             = MAX(turns.input_tokens, excluded.input_tokens),
            output_tokens            = MAX(turns.output_tokens, excluded.output_tokens),
            cache_read_tokens        = MAX(turns.cache_read_tokens, excluded.cache_read_tokens),
            cache_creation_tokens    = MAX(turns.cache_creation_tokens, excluded.cache_creation_tokens),
            -- Same monotonic rule as the other four: the 1-hour slice is part
            -- of the same cumulative streaming tally, so it only ever rises.
            cache_creation_1h_tokens = MAX(turns.cache_creation_1h_tokens, excluded.cache_creation_1h_tokens),
            -- Same monotonic rule again. On the Codex side this is what absorbs
            -- the all-zero phantom that accompanies a duplicate emission: the
            -- stored row can only ever be raised, never blanked.
            reasoning_output_tokens  = MAX(turns.reasoning_output_tokens, excluded.reasoning_output_tokens),
            -- The three non-token columns that DO move, and the only ones. They
            -- are filled rather than maximised: keep whatever is already stored
            -- unless it is blank, in which case take the incoming value.
            --
            -- This is required, not defensive. `stop_reason` is null on every
            -- partial streaming record and set only on the one that completes the
            -- response, so an incremental scan that lands mid-response stores ''
            -- and the plain "first writer wins" rule would make that blank
            -- permanent — the same defect MAX() exists to fix for the tallies. It
            -- is also what makes a forced re-read (a rebuilt database, or any
            -- transcript whose mtime moved) do anything at all: every existing
            -- row conflicts on message_id, so a rule that ignored the incoming
            -- value would re-read those rows and change nothing. Until
            -- 2026-08-16 this named "the one-time re-read in scan()", which was
            -- the marker-gated pass that went with the migrations; the arm
            -- below already said it the current way.
            --
            -- Filling only when blank keeps the cross-transcript dedup case a
            -- no-op (the same response in two files carries the same effort), so
            -- the first writer's attribution still stands wherever it said
            -- anything at all.
            reasoning_effort = CASE WHEN {_PREFER_CODEX_PRODUCER}
                                    THEN excluded.reasoning_effort
                                    WHEN turns.reasoning_effort IS NULL
                                      OR turns.reasoning_effort = ''
                                    THEN excluded.reasoning_effort
                                    ELSE turns.reasoning_effort END,
            stop_reason      = CASE WHEN turns.stop_reason IS NULL
                                      OR turns.stop_reason = ''
                                    THEN excluded.stop_reason
                                    ELSE turns.stop_reason END,
            -- Keep the first non-empty branch. Codex can supply no per-turn
            -- branch; replayed Claude records can carry a later branch label.
            --
            -- What this rule is load-bearing for is the parser's first-non-empty
            -- latch (`transcripts.parse_jsonl_file`). Claude Code REPLAYS records
            -- with `gitBranch` re-sampled at replay time, so a response's branch
            -- is the first non-empty one its records carried; a chunk boundary
            -- between two of those hands this upsert the LATER branch second, and
            -- filling only when blank is what stops it overwriting the earlier
            -- one — which is what makes a split read and a one-shot read of the
            -- same bytes agree (invariant 1). MAX() is meaningless on text, and
            -- `_MORE_COMPLETE` is worse than useless here: replayed records tie on
            -- tally AND timestamp so it would never fire, and a forced re-read
            -- (a rebuilt database, or any transcript whose mtime moved)
            -- re-inserts rows that tie by construction, so gating on it would
            -- leave the re-read unable to reach a single existing row -- which
            -- is the whole point of filling when blank. It also keeps the
            -- cross-transcript
            -- dedup a no-op: the same response in two files resolves to the same
            -- first non-empty branch in each.
            git_branch       = CASE WHEN turns.git_branch IS NULL
                                      OR turns.git_branch = ''
                                    THEN excluded.git_branch
                                    ELSE turns.git_branch END,
            -- `timestamp` and `tool_name` follow the *more complete* record.
            --
            -- A full read of a file keeps only the last record per message_id, so
            -- a turn ends up stamped with the moment the response COMPLETED. An
            -- incremental scan that landed mid-response had already stored the
            -- first partial record, and "first writer wins" then pinned the turn
            -- to the moment the response STARTED — so the same transcript, read
            -- in one pass or two, produced different timestamps for the same
            -- turn. Because the local-day bucket is a function of this column
            -- (invariant 4), a response streaming across local midnight landed on
            -- a different day depending on when the scanner happened to run.
            --
            -- The predicate is what keeps invariant 1 intact: it separates a
            -- later record of the SAME response from the same response found in
            -- a SECOND transcript, which must stay a no-op. See _MORE_COMPLETE
            -- above for why a tie on the tally is the ordinary case rather than
            -- the impossible one the first version of this comment claimed.
            timestamp = CASE WHEN {_PREFER_CODEX_PRODUCER}
                                  OR {_MORE_COMPLETE}
                             THEN excluded.timestamp ELSE turns.timestamp END,
            timestamp_order = CASE WHEN {_PREFER_CODEX_PRODUCER}
                                        OR {_MORE_COMPLETE}
                                   THEN excluded.timestamp_order
                                   ELSE turns.timestamp_order END,
            tool_name = CASE WHEN {_PREFER_CODEX_PRODUCER}
                                  OR {_MORE_COMPLETE}
                             THEN excluded.tool_name ELSE turns.tool_name END,
            model = CASE WHEN {_PREFER_CODEX_PRODUCER}
                         THEN excluded.model ELSE turns.model END,
            session_id = CASE WHEN {_PREFER_CODEX_PRODUCER}
                              THEN excluded.session_id ELSE turns.session_id END,
            is_subagent = CASE WHEN {_PREFER_CODEX_PRODUCER}
                               THEN excluded.is_subagent ELSE turns.is_subagent END,
            agent_id = CASE WHEN {_PREFER_CODEX_PRODUCER}
                            THEN excluded.agent_id ELSE turns.agent_id END
    """, [
        (t["session_id"], t["timestamp"], timestamp_order(t["timestamp"]),
         t["model"],
         t["input_tokens"], t["output_tokens"],
         t["cache_read_tokens"], t["cache_creation_tokens"],
         t.get("cache_creation_1h_tokens", 0),
         t["tool_name"], None, t.get("message_id", ""),
         t.get("is_subagent", 0), t.get("agent_id"),
         normalize_source(t.get("source")), t.get("reasoning_output_tokens", 0),
         t.get("reasoning_effort", ""), t.get("stop_reason", ""),
         t.get("git_branch", ""))
        for t in turns
    ])


def _ingest_partial(conn, parsed, total_sessions):
    """Store what a failed read did manage to parse. Returns the turn count.

    Those records are real, and `insert_turns` dedupes on `message_id`, so
    keeping them costs nothing and re-reading them once the fault clears is a
    no-op. Discarding them instead would be the safe-looking choice and the
    worse one: if the volume never comes back, the alternative to a partial
    account is no account at all.

    What the caller must NOT do after this is stamp `processed_files` -- see
    `_read_nothing_from`, which exists for the same reason one step earlier.
    """
    session_metas, turns, agents, limit_events, _line_count = parsed
    upsert_agents(conn, agents)
    route_limit_records(conn, limit_events)
    if turns or session_metas:
        sessions = aggregate_sessions(session_metas, turns)
        upsert_sessions(conn, sessions)
        insert_turns(conn, turns)
        for s in sessions:
            total_sessions.add((normalize_source(s.get("source")),
                                s["session_id"]))
    return len(turns)


def _read_nothing_from(filepath, line_count):
    """True when a parse returned no lines from a file that has bytes in it.

    That is the signature of a read `_open_transcript` refused: a permission
    error, an EMFILE burst, a symlink or hard link, a discovery-to-open race.
    `scan()` must not then record the file as processed, because the mtime it
    would store is the file's REAL one and the skip test at the top of the loop
    compares exactly that — the transcript would be excluded from every later
    scan and its turns lost for good, even once the condition clears. Finished
    sessions never change mtime again, so nothing else would ever bring the scan
    back.

    Any non-empty file yields at least one logical line from `_iter_jsonl_lines`,
    so this separates the failure from a legitimately empty transcript, which
    must still be stamped or every scan would re-read it forever. A file that
    vanished between discovery and here also returns false, but the later
    snapshot-identity check refuses to stamp that missing/replaced file. Only a
    genuinely empty, unchanged transcript records a zero-line cursor.
    """
    if line_count:
        return False
    try:
        return os.path.getsize(filepath) > 0
    except OSError:
        return False


def _lines_consumed(filepath, line_count, mtime, *, root=None):
    """How much of the file the next scan may treat as already read.

    This helper is reached only after a successful parse, so `line_count` is the
    file's full length. What may be *stamped* is narrower: a transcript is
    appended to one record at a time, so a final line with no newline on it yet
    was read but not finished. Writing that line into
    `processed_files.lines` beside the file's real mtime and identity tells every
    later scan the record was already ingested — `skip_lines` steps straight over
    it, transcripts are append-only, and a finished session's mtime and identity
    never move again, so nothing ever revisits it. Reproduced three ways: a whole record
    lost; a streaming response frozen at 50 of its 900 output tokens with a
    blank stop_reason, which is precisely what the MAX() merge on the
    `message_id` conflict exists to repair; and the same on a Codex rollout.

    Stopping one line short is safe in both directions. Re-reading a line that
    was in fact complete is a no-op — `insert_turns` merges on `message_id`, and
    an id-less record's synthetic key carries its absolute line number, which the
    parser numbers identically on the retry. A truncated transcript nobody writes
    to again keeps its real mtime, so the mtime check skips it instead of
    re-reading it forever, and a file that is one unterminated line is still
    stamped — at length zero — for the same reason an empty one is.

    `mtime` and `(st_dev, st_ino)` are read before the parse, and comparing them
    exactly is what closes the race the scenario is actually about: a scan
    running while Claude Code or Codex is mid-write. Asking about the terminator
    after the parse would otherwise see a line the writer finished in between and
    stamp a record this read never ingested. A file that moved under us is simply
    treated as unfinished, which cannot lose a line. An unchanged mtime, size and
    identity still take the fast skip; if the file later changes, the conservative
    cursor costs one harmless re-read before the new records. That remains safe only
    because the skip test compares exactly too. While that test allowed 0.01 s
    the step-back was unpayable: a writer who finished the line inside the band
    could still look unchanged even after a real append, so the re-read this is
    buying never happened.

    An unterminated last line must remain pending until it becomes a complete
    record."""
    if line_count <= 0:
        return line_count
    try:
        settled = os.path.getmtime(filepath) == mtime
    except OSError:
        settled = False
    if settled and ends_with_newline(filepath, root=root):
        return line_count
    return line_count - 1


def _transcript_prefix_hash(filepath, line_count, *, root=None):
    """SHA-256 of exactly the first ``line_count`` newline-ended records.

    The digest is the resume proof stored beside ``processed_files.lines``.
    Size says that a same-mtime append happened; this says the old prefix is
    still the prefix. Without it, inserting or replacing records before the
    resume boundary makes ``skip_lines`` step over data this database has never
    seen. Reads are bounded by the requested prefix and use the transcript's
    existing path-safety gate.
    """
    if isinstance(line_count, bool) or not isinstance(line_count, int):
        return None
    if line_count < 0:
        return None
    digest = hashlib.sha256()
    remaining = line_count
    if remaining == 0:
        return digest.hexdigest()
    try:
        with _open_transcript(filepath, root=root) as handle:
            while remaining:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    return None
                start = 0
                while remaining:
                    end = chunk.find(b"\n", start)
                    if end < 0:
                        digest.update(chunk[start:])
                        break
                    digest.update(chunk[start:end + 1])
                    remaining -= 1
                    start = end + 1
                    if remaining == 0:
                        return digest.hexdigest()
    except Exception:
        return None
    return None


def _same_file_snapshot(filepath, mtime, size, identity=None):
    """Whether the file still matches the metadata and identity before parse."""
    try:
        current = os.stat(filepath)
    except OSError:
        return False
    if current.st_mtime != mtime or current.st_size != size:
        return False
    if identity is not None:
        return (current.st_dev, current.st_ino) == identity
    return True


def _discard_processed_cursor(conn, file_key):
    """Forget a cursor that was proved to describe a different file."""
    conn.execute("DELETE FROM processed_files WHERE path = ?", (file_key,))
    # The parsed rows are idempotent, but leaving the deletion uncommitted would
    # let a later scan after an unrelated exception restore the stale cursor.
    conn.commit()


def parse_transcript(filepath, skip_lines=0, verbose=True, *, root=None):
    """Parse one transcript with whichever grammar it is written in.

    The two parsers return the identical five-tuple, so everything downstream —
    the scan loop, the upserts, the reconciliation — is source-agnostic. The
    choice is made per FILE rather than per root because `--projects-dir` is
    explicitly "also look here", and there is no rule that says what a user
    points it at.

    ``verbose`` is forwarded rather than read here: it decides only whether the
    parsers' malformed-record warning may name the file, and both parsers
    document the rule. It defaults True so a caller reading one transcript on
    purpose still gets told which one was lossy.
    """
    try:
        is_codex = codex_transcripts.looks_like_codex(filepath, strict=True, root=root)
    except Exception as exc:
        raise TranscriptReadError(
            filepath, exc, ([], [], [], [], 0)) from exc
    if is_codex:
        return codex_transcripts.parse_jsonl_file(
            filepath, skip_lines=skip_lines, verbose=verbose, root=root)
    return parse_jsonl_file(filepath, skip_lines=skip_lines, verbose=verbose, root=root)


def scan(projects_dir=None, projects_dirs=None, db_path=None, verbose=True,
         include_defaults=False, include_docker=False):
    """Serialize in-process scans so concurrent rescans cannot corrupt totals.

    This function owns the connection so it is released even when the scan
    raises. The dashboard is long-lived (the VS Code extension keeps it running
    for the whole session) and calls this on every /api/rescan, so a scan that
    died on a locked or damaged database used to leak its connection — and with
    it the SQLite handle — for the lifetime of the process.
    """
    # Resolved at call time, not frozen into the signature: a def-time
    # default captures the module global as it was at IMPORT, so patching
    # `DB_PATH` moved the name and not the default, and the call went to the
    # real ~/.claude/usage.db. See the comment on `db.get_db`.
    if db_path is None:
        db_path = DB_PATH
    with _SCAN_LOCK:
        conn = get_db(db_path)
        try:
            return _scan_unlocked(conn, projects_dir, projects_dirs, verbose,
                                  include_defaults, db_path=db_path,
                                  include_docker=include_docker)
        finally:
            conn.close()


def _scan_unlocked(conn, projects_dir=None, projects_dirs=None, verbose=True,
                   include_defaults=False, db_path=None, include_docker=False):
    # `db_path` is passed on so that a rebuild notice can name the file it is
    # about. All four call sites pass one; `init_db` keeps the parameter
    # optional so a caller holding only a connection (a test, an embedder) can
    # still bring it to the current schema, and then the notice simply omits
    # the path line.
    #
    # The return value needs no handling HERE, and this is the one caller of
    # which that is true by construction rather than by argument: a rebuild
    # empties `processed_files`, so the scan this line precedes re-reads every
    # transcript and refills what was dropped. `cli.require_db` is the contrast
    # -- it reads and exits, with no scan behind it.
    init_db(conn, db_path)
    _register_timestamp_functions(conn)
    _normalize_stored_sources(conn)

    scan_roots, missing_roots = resolve_scan_roots(
        projects_dir, projects_dirs, include_defaults=include_defaults)
    if include_docker:
        from . import docker_sources
        extra_roots = docker_sources.collect(db_path, local_roots=scan_roots)
        scan_roots, _ = resolve_scan_roots(
            projects_dirs=[*scan_roots, *extra_roots], env={})
        docker_status = docker_sources.status()
        if docker_status["state"] in ("partial", "unavailable"):
            print("  Warning: Docker usage could not be fully refreshed; "
                  "previously imported usage is retained.", file=sys.stderr)
        elif docker_status["state"] == "upgrade_required":
            print("  Warning: automatic Docker collection requires Docker Engine "
                  "29.5.1 or newer. Stored usage is retained.", file=sys.stderr)
        elif verbose and docker_status["state"] == "ready":
            print(f"Docker: found usage directories in {docker_status['containers']} containers.")

    if verbose:
        for d in scan_roots:
            print(f"Scanning {terminal_safe(d)} ...")
    # A root that was asked for and isn't there used to be skipped in total
    # silence, so "--projects-dir /typo" reported a clean scan of nothing.
    # Warn regardless of `verbose`: this is a mistake in the invocation, not
    # progress chatter.
    #
    # On STDERR, for the reason `cli.scan_roots_for`'s docstring already gives
    # about its own copy of this line -- "a diagnostic is not report output" --
    # and it is that copy which makes the stream matter here rather than taste.
    # `scan_roots_for` was added with its copy already on stderr and these two
    # were left where they were, so ONE `cli.py scan` reported an absent
    # CODEX_CLAUDE_USAGE_PROJECTS_DIRS root on both streams at once, and `cli.py
    # dashboard` put it on the stdout carrying the authenticated URL a reader is
    # told to copy.
    #
    # A stated limit, not a fix: an absent ENV root is still reported twice for
    # one `cli.py scan` -- measured 2026-08-16, two lines on stderr and none on
    # stdout. `cli.scan_roots_for` resolves and reports before dispatch and
    # hands on only the roots that exist, so a `--projects-dir` miss never
    # reaches this loop and is reported once; the environment is read again
    # here, by `resolve_scan_roots`, because `cmd_scan` passes
    # `include_defaults=True` and must -- a scan that reaches `db.init_db` can
    # empty the database, so it must never walk fewer roots than the resolve
    # before it saw. Neither copy can be removed on the strength of the other:
    # this one is the only report for `python scanner.py` and `python
    # dashboard.py`, and the CLI's is the only report for the roots it filters
    # out.
    for d in missing_roots:
        print(f"  Warning: transcript directory not found, skipping: {terminal_safe(d)}",
              file=sys.stderr)
    if not scan_roots:
        print("  Warning: no transcript directories to scan.", file=sys.stderr)

    jsonl_files = discover_jsonl_files(scan_roots)
    # Preserve the user's root spelling (including an explicitly selected link).
    # Prefer the most specific configured root when scan trees overlap.
    read_roots = sorted(
        (Path(os.path.abspath(root)) for root in scan_roots),
        key=lambda root: len(root.parts), reverse=True,
    )

    # The plan-limit cache is not a transcript, so it is read once per scan
    # rather than per file — and a scan that finds no new transcripts still
    # records it, because the point of the table is the passage of time.
    # Everything here is best-effort: a missing, unreadable or malformed
    # ~/.claude.json (the normal case in Docker, where it isn't mounted) leaves
    # the table untouched and the panel reports "unavailable". A config read
    # must never be able to fail a scan of transcripts.
    try:
        from . import account
        config = account.read_config()
        if config is not None:
            record_limit_snapshot(conn, account.snapshot_rows(
                config, datetime.now(timezone.utc).isoformat()))
            conn.commit()
    except Exception:
        pass

    new_files = 0
    updated_files = 0
    skipped_files = 0
    total_turns = 0
    total_sessions = set()

    for filepath in jsonl_files:
        absolute = Path(os.path.abspath(filepath))
        read_root = next(root for root in read_roots if absolute.is_relative_to(root))
        file_key = _processed_file_key(filepath)
        try:
            file_stat = os.stat(filepath)
            mtime = file_stat.st_mtime
            size = file_stat.st_size
            file_identity = (file_stat.st_dev, file_stat.st_ino)
            stored_identity = tuple(
                _processed_identity(value) for value in file_identity)
        except OSError:
            continue

        row = conn.execute(
            "SELECT mtime, lines, size, prefix_hash, st_dev, st_ino "
            "FROM processed_files WHERE path = ?",
            (file_key,)
        ).fetchone()

        # Exactly, not within a tolerance. The stamp at the bottom of this loop
        # is the mtime read just above, *before* the parse, so a writer that
        # appends one more record a few milliseconds later leaves the file's real
        # mtime a hair from the stored one — and the 0.01 s band this used to
        # allow then called the file unchanged for good, because transcripts are
        # append-only and a finished session's mtime never moves again. A writer
        # process appending record pairs ~6 ms apart while scan() looped lost the
        # file's last complete record exactly that way, with no warning and no
        # counter. The band also cancelled `_lines_consumed`'s deliberate
        # one-line step-back, which is paid for by a re-read that only happens
        # when the next scan sees the mtime as changed.
        #
        # Exactness buys that for nothing: os.path.getmtime returns the same
        # double every time for a file nobody wrote to, and SQLite keeps it in a
        # REAL — the same IEEE double — so an untouched file still compares equal
        # and is still skipped. What no mtime comparison can see is an append
        # inside the filesystem's own timestamp granularity, which leaves the
        # mtime identical. That is why stored size participates in the skip.
        # Device/inode identity is part of the same proof: an atomic replacement
        # can restore both mtime and size while naming different bytes.
        previous_mtime = _processed_mtime(row["mtime"]) if row else None
        previous_size = _processed_size(row["size"]) if row else None
        previous_identity = (
            (_processed_identity(row["st_dev"]),
             _processed_identity(row["st_ino"]))
            if row else None
        )
        identity_matches = (
            all(value is not None for value in stored_identity)
            and previous_identity == stored_identity)
        if (previous_mtime is not None and previous_mtime == mtime
                and previous_size is not None and previous_size == size
                and identity_matches):
            skipped_files += 1
            continue

        is_new = row is None
        if verbose:
            status = "NEW" if is_new else "UPD"
            print(f"  [{status}] {terminal_safe(filepath)}")

        if is_new:
            # New file: full parse (single read, returns line count)
            try:
                (session_metas, turns, agents, limit_events,
                 line_count) = parse_transcript(filepath, verbose=verbose, root=read_root)
            except TranscriptReadError as exc:
                # The read STREAM failed partway. Keep what was parsed, and do
                # NOT reach the `processed_files` stamp below: that would write
                # a partial line count beside the file's REAL mtime, and the
                # skip test at the top of this loop compares that mtime exactly
                # -- so the unread tail would be excluded from every later scan.
                # A finished transcript's mtime never moves again, so nothing
                # would ever bring it back. Measured before this existed:
                # force-unmounting the volume mid-scan stored 15,623 of 60,000
                # responses, and three later scans of the healthy, remounted,
                # byte-identical file recovered none of the rest.
                total_turns += _ingest_partial(conn, exc.partial, total_sessions)
                skipped_files += 1
                continue
            if _read_nothing_from(filepath, line_count):
                skipped_files += 1
                continue
            upsert_agents(conn, agents)
            route_limit_records(conn, limit_events)

            if turns or session_metas:
                sessions = aggregate_sessions(session_metas, turns)
                upsert_sessions(conn, sessions)
                insert_turns(conn, turns)
                for s in sessions:
                    total_sessions.add((normalize_source(s.get("source")),
                                        s["session_id"]))
                total_turns += len(turns)
                new_files += 1

        else:
            # Updated file: read once, parse only the lines appended since the
            # last scan (same parser as the first read — see parse_jsonl_file).
            old_lines = _processed_line_count(row["lines"]) if row else 0
            old_prefix = (_processed_prefix_hash(row["prefix_hash"])
                          if row else None)
            current_prefix = _transcript_prefix_hash(filepath, old_lines, root=read_root)
            resume_lines = (old_lines if identity_matches
                            and old_prefix is not None
                            and current_prefix == old_prefix else 0)
            try:
                (new_session_metas, new_turns, agents, limit_events,
                 line_count) = parse_transcript(filepath, skip_lines=resume_lines,
                                                verbose=verbose, root=read_root)
            except TranscriptReadError as exc:
                # Same rule on the append path, and it matters as much: leaving
                # the row untouched keeps the OLD line count, so the next scan
                # resumes from where the last complete read finished.
                total_turns += _ingest_partial(conn, exc.partial, total_sessions)
                skipped_files += 1
                continue

            # Before the "didn't grow" branch below, which would otherwise
            # advance the mtime past an append it never managed to read.
            if _read_nothing_from(filepath, line_count):
                skipped_files += 1
                continue

            if line_count <= resume_lines:
                # File didn't grow (metadata changed but no new content).
                skipped_files += 1
            else:
                upsert_agents(conn, agents)
                route_limit_records(conn, limit_events)

                if new_turns or new_session_metas:
                    sessions = aggregate_sessions(new_session_metas, new_turns)
                    upsert_sessions(conn, sessions)
                    insert_turns(conn, new_turns)
                    for s in sessions:
                        total_sessions.add((normalize_source(s.get("source")),
                                            s["session_id"]))
                    total_turns += len(new_turns)
                updated_files += 1

        # Record file as processed (line_count already known from the single
        # read; only the part of it the file has finished writing is stamped —
        # see _lines_consumed — together with the identity captured before the
        # parse).
        # A file that changed while it was being parsed is deliberately left
        # unstamped. The turns already ingested are idempotent; certifying a
        # digest read from different bytes would let the retry skip unseen data.
        # If an older cursor exists, discard it too: a replacement can restore
        # the old mtime and size, making that cursor look current on the retry.
        if not _same_file_snapshot(filepath, mtime, size, file_identity):
            _discard_processed_cursor(conn, file_key)
            skipped_files += 1
            continue
        consumed = _lines_consumed(filepath, line_count, mtime, root=read_root)
        prefix_hash = _transcript_prefix_hash(filepath, consumed, root=read_root)
        if (prefix_hash is None
                or not _same_file_snapshot(filepath, mtime, size,
                                           file_identity)):
            _discard_processed_cursor(conn, file_key)
            skipped_files += 1
            continue
        conn.execute("""
            INSERT OR REPLACE INTO processed_files
                (path, mtime, lines, size, prefix_hash, st_dev, st_ino)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (file_key, mtime, consumed, size, prefix_hash,
               stored_identity[0], stored_identity[1]))
        conn.commit()

    # Recompute session totals and the primary model from source-qualified turns
    # after every scan, including a warm no-op. This repairs previously interrupted
    # updates without relying on a process-specific dirty flag. Keep aggregate
    # counters in one correlated query; primary-model ranking is a separate query.
    conn.execute(f"""
        UPDATE sessions SET
            (total_input_tokens, total_output_tokens, total_cache_read,
             total_cache_creation, total_cache_creation_1h, turn_count) = (
                SELECT COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(cache_read_tokens), 0),
                       COALESCE(SUM(cache_creation_tokens), 0),
                       COALESCE(SUM(cache_creation_1h_tokens), 0),
                       COUNT(*)
                FROM turns
                WHERE turns.source = sessions.source
                  AND turns.session_id = sessions.session_id
            ),
            model = COALESCE((
                SELECT t.model FROM turns t
                WHERE t.session_id = sessions.session_id
                  AND t.source = sessions.source
                  AND t.model IS NOT NULL AND t.model != ''
                GROUP BY t.model
                ORDER BY {_model_priority_sql("t.model")} DESC,
                         COUNT(*) DESC, t.model ASC
                LIMIT 1
            ), model)
    """)
    conn.commit()

    if verbose:
        print(f"\nScan complete:")
        print(f"  New files:     {new_files}")
        print(f"  Updated files: {updated_files}")
        print(f"  Skipped files: {skipped_files}")
        # "parsed", not "added": total_turns counts the turns this scan read, and
        # insert_turns upserts, so a response already stored gains no row — a
        # Codex rollout replaying its ancestors' history parses thousands that
        # land nowhere. "seen" one line below draws the same distinction.
        print(f"  Turns parsed:  {total_turns}")
        print(f"  Sessions seen: {len(total_sessions)}")

    return {"new": new_files, "updated": updated_files, "skipped": skipped_files,
            "turns": total_turns, "sessions": len(total_sessions)}


if __name__ == "__main__":
    # `python scanner.py` is the third way in, and it is the bare "scan the
    # default roots" form -- nothing more. It used to carry a hand-rolled parser
    # for `--projects-dir`, and that parser was a second, weaker copy of one
    # `cli.validate_flags` and `cli.scan_roots_for` already own: it matched
    # exactly one spelling (`arg == "--projects-dir"`, so `--projects-dir=/x`
    # was dropped), read the value with no check that one was there (a trailing
    # `--projects-dir` fell through to the defaults), used the SINGULAR
    # `projects_dir=` parameter so it could not express the repeatable form
    # `cli.py scan` accepts, and discarded every other token in silence.
    # Measured before this changed: `python scanner.py --porjects-dir /nope
    # --source codex` scanned the default roots, printed an ordinary
    # `Scan complete:` block and exited 0, naming neither the typo nor the
    # unknown flag. All three of those exit 1 with a message under `cli.py`.
    #
    # Rejected rather than parsed, for the same reason `dashboard.py`'s entry
    # point rejects: a second parser is a second thing to keep equal, and the
    # first one to fall behind is the one nobody documents. `cli.py scan` is the
    # supported way to name a root, and it is what this points at.
    if sys.argv[1:]:
        print("python scanner.py takes no arguments; it scans the default "
              "locations. Nothing was scanned.\n"
              f"  Refused: {' '.join(terminal_safe(a) for a in sys.argv[1:])}\n"
              "  For arguments, use:  python cli.py scan --projects-dir PATH",
              file=sys.stderr)
        sys.exit(1)
    scan(include_defaults=True, include_docker=True)
