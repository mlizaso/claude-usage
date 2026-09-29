"""Parsing Codex rollout transcripts into the same records Claude's parser emits.

Codex writes `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, one file per
*thread*. The grammar shares nothing with Claude Code's — every record is
`{payload, timestamp, type}` and the usage lives on an `event_msg` whose payload
type is `token_count` — so this is a sibling of `transcripts.parse_jsonl_file`
rather than a branch inside it. It returns the identical five-tuple, so
`scanner.scan` does not know or care which source a file came from.

Everything below exists because of a difference that would otherwise be a silent
costing error:

* Cached input and cache writes are subsets of Codex input. Subtract them
before feeding the shared disjoint token buckets to pricing. * Usage events
can repeat without a new API response. Use the cumulative counter to
deduplicate them, with a bound appropriate for a lineage accumulator. * Child
rollouts can replay parent usage and metadata. Key replayed responses by root
lineage so discovering a child cannot count its parent again.

  **Rows an earlier build already stored are repaired, but incidentally rather
  than by name — and the difference is what a future reader needs.** There used
  to be an in-place re-key for exactly this; it went with the rest of the
  migration machinery, and nothing replaced it. `db.init_db` rebuilds on a
  SCHEMA mismatch, and the inflated keying is not one: the ids are wrong, the
  columns are not. What saves it is that no database which *could* be inflated
  has today's column set. Measured 2026-08-15 by rebuilding the pre-fix build's
  schema from `6780f27^:db.py`, running its own `init_db`, seeding one
  thread-keyed row and handing it to this build: `schema_mismatches` reports two
  reasons — `turns.git_branch` is missing, that column having landed *after* the
  keying fix, and the marker table that build declared is not part of this
  schema — `init_db` returns True, and the refilling scan re-keys on the root
  session.

  Only the FIRST of those two generalises, and the distinction is worth keeping
  because the marker table is the more memorable one. Censused across all 21
  released tags on 2026-08-15, the marker table appears only from v1.5.4 onward;
  the 17 tags from v1.0.0 to v1.5.3 have no such table anywhere in their Python,
  so a database from one of those is disqualified by missing columns alone.
  Same conclusion, different reason, and any claim of the form "every build
  declared it" is false.

  The residual, stated rather than implied: this is a side effect of an
  unrelated schema difference, so it costs a full re-read and does not bring
  back rows whose transcript is gone. It would stop working the day a build
  ships with today's exact column set *and* wrong keying, and no test would
  say so.
* **The model is on a different record.** `turn_context.payload.model` is carried
  forward in file order. A session can switch models, so each response must
  retain the model active when its usage was recorded.
* **Cache writes use one OpenAI tier.** Codex stores that tier in
  `cache_write_input_tokens`; it maps to the shared short-lived cache-write
  column while the Anthropic-only one-hour slice remains zero.
"""

import hashlib
import json
import math
import os
import re
import sys

from .safetext import _bounded_text, terminal_safe
from .transcripts import (
    TranscriptReadError,
    _decode_json_object,
    _iter_jsonl_lines,
    _load_json_object,
    _nonnegative_integer,
    _open_transcript,
    _optional_nonnegative_integer,
    _oversized_line_lost,
    project_name_from_cwd,
)
from .timestamps import timestamp_max, timestamp_min

# Skip JSON decoding for lines that cannot contain a supported usage or
# metadata record. Prompt, response and tool-output bodies are not needed by
# the usage parser.
_INTERESTING = ('"token_count"', '"turn_context"', '"session_meta"',
                '"thread_settings_applied"')

# A reset time is a Unix epoch in seconds, so it needs its own sanity bound: the
# transcript-integer cap is 1e9, which is below every timestamp after 2001.
# 4e9 is the year 2096 — generous, and still far from anything that could
# overflow a later date computation.
MAX_EPOCH_SECONDS = 4_000_000_000

# The per-thread cumulative counter is the THIRD field here that is not a
# per-turn token count and must not carry the per-turn bound (see `resets_at`
# above for the second). It re-counts the whole cached context on every
# response, so it grows superlinearly and a long thread runs straight past
# `transcripts.MAX_TRANSCRIPT_INTEGER` (1e9). Bounded instead at the largest
# integer JavaScript represents exactly — the same ceiling `safejson` enforces
# on everything that reaches the browser — so the value stays usable end to end.
MAX_CUMULATIVE_TOKENS = 2 ** 53 - 1


def looks_like_codex(filepath, strict=False, *, root=None):
    """True if this JSONL is a Codex rollout rather than a Claude transcript.

    Sniffed from the content, not from the directory it was found in: a scan root
    is whatever the user pointed at, and `--projects-dir` is explicitly allowed to
    name somewhere unusual (a container's mounted history, a copy from another
    machine). Both formats are line-delimited JSON objects, and they are told
    apart by the keys of the first record that parses.

    The public/default probe remains best-effort for callers asking only a
    question. The scanner passes ``strict=True``: a read failure cannot be
    interpreted as "Claude", because parsing a Codex file with the other
    grammar yields zero turns and would otherwise stamp that loss as complete.
    """
    try:
        with _open_transcript(filepath, root=root) as handle:
            for line in _iter_jsonl_lines(handle):
                if line is None:
                    continue
                line = line.strip()
                if not line:
                    continue
                record = _load_json_object(line)
                if record is None:
                    continue
                # Codex: every record is exactly {payload, timestamp, type}.
                # Claude: records carry sessionId/type at the top level.
                if "payload" in record and "sessionId" not in record:
                    return True
                return False
    except Exception:
        if strict:
            raise
        return False
    return False


def _usage_from(info):
    """Normalise one Codex usage block into the Anthropic column shape.

    `cached_input_tokens` and `cache_write_input_tokens` are detailed subsets of
    `input_tokens`, so only the remainder belongs in the column every cost path
    treats as ordinary input.  The two subsets are clamped in a deterministic
    order to the total: cache reads first, then cache writes to the remaining
    capacity.  Malformed detail fields therefore cannot create negative input
    or make the stored disjoint buckets sum to more than the provider's total.

    Every key returned is a column the caller stores, and only those. The block
    also carries a per-turn `total_tokens`, which is not an Anthropic column,
    which nothing read, and whose name collides with the per-LINEAGE
    accumulator `_cumulative_total` takes out of a different block — three
    reasons for a reader of the one function that owns the subset/superset
    rules to have to go and check.
    """
    if not isinstance(info, dict):
        return None
    raw_input = _nonnegative_integer(info.get("input_tokens"))
    cached = min(_nonnegative_integer(info.get("cached_input_tokens")), raw_input)
    cache_write = min(
        _nonnegative_integer(info.get("cache_write_input_tokens")),
        raw_input - cached,
    )
    return {
        "input_tokens": raw_input - cached - cache_write,
        "cache_read_tokens": cached,
        "output_tokens": _nonnegative_integer(info.get("output_tokens")),
        # OpenAI exposes one cache-write bucket.  The shared schema's total
        # carries it; the one-hour field remains the Anthropic-only subset.
        "cache_creation_tokens": cache_write,
        "cache_creation_1h_tokens": 0,
        # A subset of output_tokens, not an addition to it — stored for display,
        # never summed into cost.
        "reasoning_output_tokens": min(
            _nonnegative_integer(info.get("reasoning_output_tokens")),
            _nonnegative_integer(info.get("output_tokens"))),
    }


def _cumulative_total(block):
    """The per-lineage monotonic counter that doubles as the response's identity.

    Do not apply the per-turn token cap to a cumulative lineage counter.
    Collapsing large counters to zero would merge unrelated responses into one
    row.

    Returns None when there is no usable counter. That is the caller's signal to
    DROP the record: without the counter there is no `message.id` analogue at
    all, and the shared-key fallback is the failure above rather than a
    degradation of it.
    """
    if not isinstance(block, dict):
        return None
    value = block.get("total_tokens")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_CUMULATIVE_TOKENS:
        return None
    return value


_ROLLOUT_NAME_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"(?P<uuid>[0-9a-fA-F-]{8,})\.jsonl$"
)


def thread_id_from_path(filepath):
    """A path-scoped fallback identity for a rollout without a header.

    `session_meta.payload.id` is authoritative and replaces this value whenever
    a normal header exists. Standard rollout names keep their historical UUID
    fallback: that UUID is the logical session identity, and changing it would
    double-count already indexed headerless files after an upgrade. Arbitrary
    names have no such identity and used to collapse by basename across roots;
    those use a canonical-path digest instead.
    """
    name = os.path.basename(str(filepath))
    matched = _ROLLOUT_NAME_RE.match(name)
    if matched:
        return _bounded_text(matched.group("uuid"), 512)
    try:
        canonical = os.path.normcase(os.path.realpath(
            os.path.abspath(os.fspath(filepath))))
        encoded = os.fsencode(canonical)
    except (OSError, TypeError, ValueError):
        encoded = os.fsencode(str(filepath))
    digest = hashlib.sha256(b"codex-fallback\0" + encoded).hexdigest()
    return _bounded_text("path:" + digest, 512)


def _limit_snapshot(rate_limits, timestamp):
    """One observation of the plan's quota window, from a usage record.

    This is the thing Codex does better than Claude Code: the quota state is
    stamped on an append-only transcript instead of living in a single mutable
    cache, so the history is genuinely recoverable rather than being whatever the
    last refresh happened to leave behind.
    """
    if not isinstance(rate_limits, dict):
        return None
    primary = rate_limits.get("primary")
    if not isinstance(primary, dict):
        return None
    percent = primary.get("used_percent")
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return None
    # `json.loads` accepts the bare tokens NaN, Infinity and -Infinity by
    # default, and `int()` refuses every one of them — ValueError for NaN,
    # OverflowError for the infinities. Raised from here the exception escaped
    # into the handler wrapped around the whole parse loop, so one malformed
    # record abandoned the REST OF THE FILE: the partial line count was then
    # written to `processed_files.lines` beside the file's real mtime, and no
    # later scan ever re-read the stranded tail. Measured at 10 of 13 turns lost
    # permanently. This is the only float taken from a transcript; every other
    # number here goes through an int validator that rejects floats outright.
    if isinstance(percent, float) and not math.isfinite(percent):
        return None
    window = _optional_nonnegative_integer(primary.get("window_minutes"))
    # NOT _optional_nonnegative_integer: that caps at MAX_TRANSCRIPT_INTEGER
    # (1e9), which is the right bound for a token count and the wrong one for a
    # Unix epoch — 2026 is ~1.79e9, so every reset time was silently rejected and
    # the whole quota series collapsed into one undated bucket.
    resets_at = primary.get("resets_at")
    if isinstance(resets_at, bool) or not isinstance(resets_at, int) \
            or not (0 < resets_at < MAX_EPOCH_SECONDS):
        resets_at = None
    return {
        # `kind` is the ROUTING key — dashboard_data's projection and history
        # both filter `kind = 'codex'` — and it means the source, which is not
        # the transcript's to name. Taking it from `limit_id` let any other
        # label file the rows under a kind nothing reads, and silently:
        # `route_limit_records` still accepts the record because it has a
        # `percent`, so the "matched no known shape" warning that exists to
        # stop limits vanishing without a word never fires.
        "kind": "codex",
        "group": f"{window}m" if window else "",
        "scope": "",
        "percent": max(0, min(100, int(percent))),
        "severity": _bounded_text(rate_limits.get("rate_limit_reached_type"), 64),
        "is_active": 1,
        "window_minutes": window,
        "resets_at_epoch": resets_at,
        "plan_type": _bounded_text(rate_limits.get("plan_type"), 64),
        "observed_at": timestamp,
    }


def _context_from_record(record):
    """The (model, effort) a record establishes; "" for whichever it establishes none of.

    Use one model-context rule for the main loop and resume recovery. Both
    must honor turn_context and thread_settings_applied.

    Reasoning effort is carried here rather than by a helper of its own for
    exactly that reason: it lives on the *same* `turn_context` record as the
    model and is established by the same events, so a second scan rule would be
    a second chance to drift apart on the incremental path. The two are returned
    together and each is applied independently by the caller — a record that
    sets one and not the other must not blank the other.
    """
    if not isinstance(record, dict):
        return "", ""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return "", ""
    if record.get("type") == "turn_context":
        return (_bounded_text(payload.get("model"), 512),
                _bounded_text(payload.get("effort"), 64))
    if (record.get("type") == "event_msg"
            and payload.get("type") == "thread_settings_applied"):
        settings = payload.get("thread_settings")
        if isinstance(settings, dict):
            # turn_context spells the field effort; thread_settings uses
            # reasoning_effort. Prefer the canonical settings key and keep
            # effort as a compatibility fallback. A model change must not
            # retain the previous model effort accidentally.
            return (_bounded_text(settings.get("model"), 512),
                    _bounded_text(settings.get("reasoning_effort")
                                  or settings.get("effort"), 64))
    return "", ""


def _context_in_force(filepath, up_to_line, response_efforts=None, *, root=None):
    """The (model, effort) last established at or before `up_to_line`.

    Incremental parsing re-reads turn_context records in the prefix to recover
    the model without persisting a second copy of parser state. A substring
    prefilter avoids decoding unrelated records.

    When ``response_efforts`` is supplied, also record the first non-empty effort
    observed for each cumulative response in that prefix. A context change can
    happen between two emissions of one response; recovering only the latest
    context would then make a resumed parse disagree with a full parse even
    though both retain the last emission's tally.
    """
    model = ""
    effort = ""
    if up_to_line <= 0:
        return model, effort
    with _open_transcript(filepath, root=root) as handle:
        for number, line in enumerate(_iter_jsonl_lines(handle), 1):
            if number > up_to_line:
                break
            if line is None:
                continue
            if ('"turn_context"' not in line
                    and '"thread_settings_applied"' not in line
                    and (response_efforts is None
                         or '"token_count"' not in line)):
                continue
            record = _load_json_object(line.strip())
            found_model, found_effort = _context_from_record(record)
            if found_model:
                model = found_model
            if found_effort:
                effort = found_effort
            if response_efforts is None or not isinstance(record, dict):
                continue
            if (record.get("type") != "event_msg"
                    or not isinstance(record.get("payload"), dict)
                    or record["payload"].get("type") != "token_count"):
                continue
            info = record["payload"].get("info")
            if not isinstance(info, dict):
                continue
            usage = _usage_from(info.get("last_token_usage"))
            cumulative = _cumulative_total(info.get("total_token_usage"))
            if (usage is None or cumulative is None or not effort
                    or sum(usage.values()) == 0):
                continue
            response_efforts.setdefault(cumulative, effort)
    return model, effort


def parse_jsonl_file(filepath, skip_lines=0, verbose=True, *, root=None):
    """Parse one Codex rollout into (sessions, turns, agents, limits, line_count).

    The same contract as `transcripts.parse_jsonl_file`: a successful return
    carries the full line count, while a partial stream failure raises
    `TranscriptReadError` with the partial result and must remain unstamped.
    `processed_files.lines` therefore means the same thing for both sources, as
    does what ``verbose`` does to the malformed-record warning documented there.
    Both parsers reach the same scan loop, so a rule that held on only one of
    them would be a hole shaped exactly like whichever source the reader uses.
    """
    session_meta = {}
    turns = {}          # synthetic message id -> turn
    agents = {}
    limit_events = {}
    line_count = 0

    own_thread = thread_id_from_path(filepath)
    seen_own_header = False
    root_session = ""
    project_name = "unknown"
    git_branch = ""
    is_subagent = 0
    agent_type = ""
    agent_label = ""
    first_ts = last_ts = ""
    # An incremental parse resumes with whatever model was in force at the split.
    prefix_reasoning_effort = {}
    try:
        model, effort = _context_in_force(
            filepath, skip_lines, prefix_reasoning_effort, root=root)
    except Exception as exc:
        # A prefix read is part of an incremental parse, not an optional hint:
        # it supplies the model and effort in force at the resume boundary.
        # Blank attribution followed by a successful tail parse would be
        # stamped permanently, so fail exactly like a main-stream read instead.
        raise TranscriptReadError(
            filepath, exc, ([], [], [], [], 0)) from exc
    malformed = 0
    first_error = None

    read_error = None
    try:
        with _open_transcript(filepath, root=root) as handle:
            for line_count, line in enumerate(_iter_jsonl_lines(handle), 1):
                # One malformed record must not cost the rest of the file.
                # The handler around the whole loop cannot distinguish 'this
                # file is unreadable' from 'this one line is', so it treated
                # both as the end of the file — and `scan()` then stamped the
                # partial line count into `processed_files` beside the file's
                # real mtime, which means no later scan re-reads the stranded
                # tail. For a finished rollout that loss is permanent, so the
                # blast radius of a bad record is contained to the record.
                try:
                    if line is None:
                        # A lost record like any other, and one nothing counted.
                        # Not re-counted when resuming: the read that first
                        # passed this line already reported it.
                        if line_count > skip_lines:
                            raise _oversized_line_lost()
                        continue
                    raw = line.strip()
                    if not raw:
                        continue
                    # The header is needed even when resuming mid-file: it carries
                    # the session identity every appended turn is attributed to.
                    interesting = any(token in raw for token in _INTERESTING)
                    if not interesting:
                        continue
                    if line_count <= skip_lines and '"session_meta"' not in raw:
                        continue
                    record, undecodable = _decode_json_object(raw)
                    if undecodable is not None:
                        # Only lines the prefilter above already judged worth
                        # decoding reach this, so a rollout full of prose cannot
                        # warn about lines this parser never reads.
                        raise undecodable
                    if record is None:
                        continue
                    rtype = record.get("type")
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    timestamp = _bounded_text(record.get("timestamp"), 128)

                    if rtype == "session_meta":
                        # A file can carry several: its own header, then an echo of
                        # the parent's when this thread was spawned (and further
                        # ancestors above that). The FIRST one is this thread's own —
                        # verified across the corpus — so it establishes the identity
                        # and every later one is checked against it.
                        declared = _bounded_text(payload.get("id"), 512)
                        first_own_header = not seen_own_header
                        if seen_own_header:
                            if declared != own_thread:
                                continue            # an ancestor's header, not ours
                        else:
                            seen_own_header = True
                            if declared:
                                own_thread = declared
                        # Identity and project belong to the first header for
                        # this rollout. Later same-thread headers may fill
                        # metadata such as a blank branch, but must not move
                        # already-emitted turns to another root or project.
                        if first_own_header:
                            root_session = (_bounded_text(
                                payload.get("session_id"), 512) or own_thread)
                            project_name = project_name_from_cwd(payload.get("cwd"))
                        git = payload.get("git")
                        # The first nonempty branch wins, matching the Claude
                        # parser. Repeated headers must not make a full scan
                        # disagree with an incremental scan.
                        if isinstance(git, dict) and not git_branch:
                            git_branch = _bounded_text(git.get("branch"), 1024)
                        source = payload.get("source")
                        if _bounded_text(payload.get("thread_source"), 64) == "subagent":
                            is_subagent = 1
                        if isinstance(source, dict) and isinstance(source.get("subagent"), dict):
                            is_subagent = 1
                            sub = source["subagent"]
                            agent_type = _bounded_text(sub.get("other"), 128)
                            spawn = sub.get("thread_spawn")
                            if isinstance(spawn, dict):
                                agent_label = _bounded_text(spawn.get("agent_nickname"), 512)
                                agent_type = agent_type or _bounded_text(
                                    spawn.get("agent_path"), 512)
                        continue

                    if rtype == "turn_context":
                        found_model, found_effort = _context_from_record(record)
                        if found_model:
                            model = found_model
                        if found_effort:
                            effort = found_effort
                        continue

                    if rtype != "event_msg":
                        continue
                    ptype = payload.get("type")

                    if ptype == "thread_settings_applied":
                        found_model, found_effort = _context_from_record(record)
                        if found_model:
                            model = found_model
                        if found_effort:
                            effort = found_effort
                        continue

                    if ptype != "token_count":
                        continue

                    info = payload.get("info")
                    if not isinstance(info, dict):
                        continue
                    usage = _usage_from(info.get("last_token_usage"))
                    # The cumulative counter gets its own validator, NOT the
                    # per-turn one — see `_cumulative_total`. A record without a
                    # usable counter is dropped rather than keyed on a fallback:
                    # every fallback is shared, and a shared key deletes the
                    # turns already stored under it.
                    cumulative = _cumulative_total(info.get("total_token_usage"))
                    if usage is None or cumulative is None:
                        continue

                    snapshot = _limit_snapshot(payload.get("rate_limits"), timestamp)
                    if snapshot is not None:
                        # Keep the first observation of each (window, percent) key. Repeated
                        # observations must not move the instant when the level was reached.
                        limit_events.setdefault(
                            (snapshot["kind"], snapshot["group"],
                             snapshot["percent"], snapshot["resets_at_epoch"]), snapshot)

                    billable = (usage["input_tokens"] + usage["output_tokens"]
                                + usage["cache_read_tokens"] + usage["cache_creation_tokens"])
                    if billable == 0:
                        # The all-zero phantom records that accompany a duplicate
                        # emission. Dropping them here keeps them out of the turn
                        # count; MAX() would neutralise them anyway.
                        continue

                    if timestamp:
                        first_ts = timestamp_min(first_ts, timestamp)
                        last_ts = timestamp_max(last_ts, timestamp)

                    # The message.id analogue. Two records sharing a LINEAGE and
                    # a cumulative total are the same API response — keyed on
                    # the root session, not on the thread that wrote the record,
                    # because the accumulator is scoped to the lineage and a
                    # spawned thread replays its parent's history verbatim. See
                    # the module docstring.
                    message_id = f"codex:{root_session or own_thread}:{cumulative}"
                    turn = {
                        "session_id": root_session or own_thread,
                        "timestamp": timestamp,
                        "model": model,
                        "input_tokens": usage["input_tokens"],
                        "output_tokens": usage["output_tokens"],
                        "cache_read_tokens": usage["cache_read_tokens"],
                        "cache_creation_tokens": usage["cache_creation_tokens"],
                        "cache_creation_1h_tokens": usage["cache_creation_1h_tokens"],
                        "reasoning_output_tokens": usage["reasoning_output_tokens"],
                        # Established by the most recent `turn_context`, exactly like
                        # `model` beside it. Codex rollouts carry no stop_reason
                        # analogue, so that column stays '' for this source.
                        "reasoning_effort": effort,
                        "stop_reason": "",
                        "tool_name": None,
                        "cwd": None,
                        "message_id": message_id,
                        "is_subagent": is_subagent,
                        "agent_id": own_thread if is_subagent else None,
                        # Blank ON PURPOSE, and a different kind of blank from
                        # `stop_reason` above: Codex does record a branch, but in
                        # a `session_meta` header rather than on each response,
                        # and the latch below keeps the first non-empty one for
                        # the whole file. Stamping that on every turn would be
                        # file grain wearing turn grain's clothes — no
                        # per-response information at all — while changing stored
                        # attribution for a subagent rollout whose own first
                        # header disagrees with its root's.
                        # `project_by_day_model` COALESCEs the blank onto the
                        # session label, so this source shows the branch it always
                        # showed.
                        "git_branch": "",
                        "source": "codex",
                    }
                    previous = turns.get(message_id)
                    if (previous is not None
                            and previous.get("reasoning_effort")):
                        turn["reasoning_effort"] = previous[
                            "reasoning_effort"]
                    elif prefix_reasoning_effort.get(cumulative):
                        turn["reasoning_effort"] = prefix_reasoning_effort[
                            cumulative]
                    turns[message_id] = turn
                except Exception as exc:
                    if not malformed:
                        first_error = exc
                    malformed += 1
                    continue

    except Exception as exc:
        # See `transcripts.TranscriptReadError`: a failed READ is not
        # end-of-file, and folding it into a normal return stamped the partial
        # line count beside the file's real mtime, so the tail was never read
        # again. Recorded here, raised below once the return value exists.
        read_error = exc
        print(f"  Warning: error reading {terminal_safe(filepath)}: "
              f"{terminal_safe(exc)}", file=sys.stderr)

    if malformed:
        # Counted and reported once rather than per record: a corrupted file
        # can hold thousands, and the point is that the user learns the scan
        # was lossy, not that the terminal is filled.
        #
        # stderr, and the path only when a log was asked for -- the same rule
        # and the same reasons as the Claude parser's copy of this warning.
        print(f"  Warning: skipped {malformed} unreadable record(s) in "
              f"{terminal_safe(filepath) if verbose else 'a transcript'}: "
              f"{terminal_safe(first_error)}", file=sys.stderr)

    sessions = []
    if turns or root_session:
        sessions.append({
            "session_id": root_session or own_thread,
            "project_name": project_name,
            "first_timestamp": first_ts,
            "last_timestamp": last_ts,
            "git_branch": git_branch,
            "model": None,
            "topic": None,
            "source": "codex",
        })
    if is_subagent and turns:
        agents[own_thread] = {
            "source": "codex",
            "agent_id": own_thread,
            "agent_type": agent_type or agent_label or "subagent",
            "dispatched_in_session": root_session or own_thread,
            "completed_at": last_ts,
            "status": "",
            "total_tokens": None,
            "total_duration_ms": None,
            "tool_use_count": None,
        }
    parsed = (sessions, list(turns.values()), list(agents.values()),
              list(limit_events.values()), line_count)
    # Only a PARTIAL read raises. A file that could not be OPENED has read
    # nothing, `line_count` is 0, and `scanner._read_nothing_from` already
    # refuses to stamp it -- that path predates this and is pinned by
    # `test_an_unreadable_file_still_reports_zero`. What was uncovered is the
    # stream that failed after yielding lines, where the partial count looked
    # exactly like a short file.
    if read_error is not None and line_count > 0:
        raise TranscriptReadError(filepath, read_error, parsed)
    return parsed
