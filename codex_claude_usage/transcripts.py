"""Parsing Claude Code JSONL transcripts into turn/session/agent records.

Pure reading: this module never touches the database. It owns the file-safety
rules (no symlinks, no hard links, no non-regular files, bounded line length),
the record validators, and the streaming dedup contract — Claude Code writes
several records per API response sharing one message.id, and only the last
carries the final usage tally.

`parse_jsonl_file(path, skip_lines=n)` is the single parser for both a file's
first read and every later append; keeping those one implementation is what
stops the incremental scan drifting from the full one.
"""

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

from .safefile import open_regular_file_descriptor
from .safetext import _bounded_text, terminal_safe
from .timestamps import timestamp_max, timestamp_min

MAX_JSONL_LINE_LENGTH = 64 * 1024 * 1024
# Individual transcript counters above one billion are not credible Claude
# usage values. Rejecting them prevents malicious JSON from overflowing later
# SQLite SUM operations while leaving several orders of magnitude of headroom.
MAX_TRANSCRIPT_INTEGER = 1_000_000_000


def _nonnegative_integer(value):
    """Return a SQLite-safe token/count value, rejecting coercible strings."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    if value < 0 or value > MAX_TRANSCRIPT_INTEGER:
        return 0
    return value

def _optional_nonnegative_integer(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_TRANSCRIPT_INTEGER:
        return None
    return value

def _open_transcript(filepath, *, root=None):
    """Open one regular, single-link transcript as a raw byte stream.

    Line splitting owns UTF-8 decoding so the line-size boundary is enforced
    on the bytes read from disk, before multibyte text can expand in memory.
    """
    path = Path(filepath)
    fd = open_regular_file_descriptor(
        path, follow_symlinks=False, single_link=True, root=root,
    )
    if fd is None:
        raise RuntimeError(f"Refusing unsafe transcript path: {path}")
    try:
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise

def ends_with_newline(filepath, *, root=None):
    """True when the file's last byte ends a line — its final record is written.

    A transcript is appended to one record at a time, so the single shape that
    means "read this line again later" is a trailing line with no terminator
    yet. Answering it costs one open and one byte, and it is answered from the
    file rather than from the parser's last line because `_iter_jsonl_lines`
    yields None for an oversized line and the terminator goes with it.

    Opened under the same rules as every other read of this path (no symlink, no
    hard link, regular files only). Anything that goes wrong answers
    "unfinished": re-reading one line is harmless, while calling an unread
    terminator complete can strand that record forever.
    """
    try:
        with _open_transcript(filepath, root=root) as handle:
            if handle.seek(0, os.SEEK_END) == 0:
                return True
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) == b"\n"
    except Exception:
        return False

def _iter_jsonl_lines(stream):
    """Decode byte-bounded logical lines; yield None for an oversized one."""
    while True:
        raw = stream.readline(MAX_JSONL_LINE_LENGTH + 1)
        if not raw:
            return
        oversized = len(raw) > MAX_JSONL_LINE_LENGTH
        while oversized and raw and not raw.endswith(b"\n"):
            raw = stream.readline(MAX_JSONL_LINE_LENGTH + 1)
        yield None if oversized else raw.decode("utf-8", errors="replace")

def _oversized_line_lost():
    """The loss an oversized line is, as an exception both parsers can report.

    `_iter_jsonl_lines` yields None for a line over the cap, having consumed the
    rest of it: that record is gone, exactly as if json.loads had refused it.
    Raised into the per-record handler so it reaches the same "skipped N
    unreadable record(s)" warning rather than a silent `continue`. One
    definition, because the two grammars must not drift into reporting the same
    loss differently — and the message reads the live module global, so a test
    that lowers the cap sees the number it set.
    """
    return ValueError(f"line over {MAX_JSONL_LINE_LENGTH} bytes was skipped")

def _decode_json_object(line):
    """Return (record, undecodable) for one JSONL line.

    Two outcomes hide behind a bare None and only one of them is a loss. A line
    json.loads cannot read costs that API response — the commonest way a record
    disappears, and it disappeared without a word. A line that decodes to
    something other than an object (`[]`, a bare string) is one this grammar
    carries no usage in; the suite's own fixtures contain such a line, so
    counting it as unreadable would claim a clean scan was lossy, which is worse
    than the silence it replaces.

    The exception is carried out rather than a flag because the warning prints
    it: a bare sentinel would report "... : None".
    """
    try:
        value = json.loads(line)
    except (ValueError, RecursionError) as exc:
        return None, exc
    return (value, None) if isinstance(value, dict) else (None, None)

def _load_json_object(line):
    """The decoded object, or None — whether unreadable or simply not one."""
    record, _undecodable = _decode_json_object(line)
    return record

def home_directory_leaf(home=None):
    """The last component of the home directory, or "" if it cannot be read.

    Returns "" rather than raising: `Path.home()` raises for a uid with no
    passwd entry, and `/` has no leaf at all. "" disables the fold below, which
    is the correct degradation -- a label that keeps the username is the status
    quo, and crashing a scan over a cosmetic rename is not.
    """
    if home is None:
        try:
            home = str(Path.home())
        except (OSError, RuntimeError):
            return ""
    leaf = str(home).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return leaf


def folds_to_home(parent, home_leaf):
    """Whether `parent` is the home directory's own name.

    **Deliberately weaker than "the path is inside $HOME", and the weakness is
    the privacy rule rather than a shortcut.** The home directory's leaf is
    normally the account name, and `sessions.project_name` is the one field of
    its kind that LEAVES the machine — /api/data, the CLI `stats` table, three
    CSV exports — while `turns.cwd` is NULL and `processed_files.path` is a
    SHA-256. A project directly in $HOME therefore stored the username as its
    parent (`/Users/victim/proj` -> `victim/proj`), and matching on the leaf
    alone is what folds that to `~/proj` wherever the directory turns out to
    live.

    The cost, stated plainly: a directory named after you somewhere else
    (`/srv/victim/proj`) folds too, and shares a row with `~/proj`. That is a
    mislabel affecting a rare layout, taken deliberately over emitting the
    account name for every user the rule is for.

    Strengthening it to a real "inside $HOME" test would need `turns.cwd`, which
    is NULL by design, so the stored label could never be re-derived or
    re-checked afterwards -- there is nothing left in the database to appeal to.
    A build that shipped the strong rule and then wanted the weak one back would
    have to throw the labels away, which is the whole database (see
    `db.rebuild_database`).

    `normcase` is what makes the two sides agree on case: identity on POSIX,
    lowercasing on Windows, where a cwd differing only in case IS the same
    directory.
    """
    if not home_leaf:
        return False
    return os.path.normcase(parent) == os.path.normcase(home_leaf)


def project_name_from_cwd(cwd, home=None):
    """Derive a friendly project name from cwd path.

    The last two components, EXCEPT that a project sitting directly in the home
    directory folds its parent to `~`. Keeping two components is what makes the
    label useful -- `myproj` alone is ambiguous across checkouts -- but for
    `/Users/victim/myproj` the parent IS the username, and this label is the one
    field that leaves the machine: it reaches /api/data, the CLI `stats` table
    and three CSV exports, so it is what lands in a screenshot attached to a
    public bug report. Everything else in the privacy posture already assumes
    that: `turns.cwd` is NULL and `processed_files.path` is a SHA-256.

    Only the home prefix folds. `/a/b/c/d` stays `c/d` on every machine, because
    changing the shape for non-home paths would relabel every project for no
    privacy gain.

    `home` is injectable, and that is not only for convenience: with it read
    solely from `Path.home()`, the existing `"/home/user/myproject"` cases would
    keep passing on a machine whose home is elsewhere while asserting nothing
    about the fold -- vacuously green, which is the failure mode this repository
    keeps finding in its own tests.

    Four honest limits, all measured rather than reasoned, because a privacy
    control that is believed to cover more than it does is worse than one whose
    edges are written down:

    1. It removes the USERNAME, not every identifying parent. A sandbox
       container id like `27N4MQEA55~pro~writer/Documents` is stored as-is --
       "the parent directory is safe" was never true, only "the parent
       directory is whatever happened to be there".
    2. **It is inert in the shipped Docker image.** The Dockerfile sets
       `HOME=/home/codexclaudeusage` while the bind-mounted transcripts carry the
       HOST user's paths, so no leaf ever matches. Nothing here can fix that:
       the container cannot know the host's username. A scan run on the host
       folds those same sessions correctly.
    3. **A symlinked home defeats it.** `Path.home()` returns `$HOME` verbatim
       while a process's recorded cwd is usually the resolved path, so
       `/home/victim -> /mnt/users/victim` compares two different strings.
       Resolving is not available either: the cwd here is a string out of a
       transcript, from a machine that may not be this one.
    4. **`normcase` is identity on macOS**, whose filesystem is nonetheless
       case- and Unicode-normalization-insensitive, so a cwd differing from
       `$HOME` only in case does not fold. `normcase` is still the right call:
       it is the platform's own answer, and hard-coding a case rule here would
       be wrong on the case-SENSITIVE filesystems macOS also supports.
    """
    cwd = _bounded_text(cwd)
    if not cwd:
        return "unknown"
    # Normalize to forward slashes, take last 2 components
    parts = cwd.replace("\\", "/").rstrip("/").split("/")
    home_leaf = home_directory_leaf(home)
    if len(parts) >= 2:
        # The fold applies when the PARENT component is the home directory's
        # name: `/Users/victim/proj` yes, `/Users/victim/work/proj` no -- there
        # the parent is `work`, which carries nothing and is the label that makes
        # the name useful.
        if folds_to_home(parts[-2], home_leaf):
            return "~/" + parts[-1]
        # And when the LAST component is: `cd ~ && claude` gives a cwd that IS
        # the home directory, which put the username in the label's second half
        # instead of its first (`/Users/victim` -> `Users/victim`) and so slipped
        # past the check above entirely. That is not an exotic layout, and it is
        # the same population this fold exists for -- someone who works directly
        # in their home directory.
        #
        # **This branch is NOT justified by the lossiness argument above**, and
        # an earlier version of this comment implied it was. That argument holds
        # for the parent check, where the stored label `victim/proj` carries one
        # home component and nothing downstream can tell `/Users/victim/proj`
        # from `/srv/victim/proj`. Here the stored label is `Users/victim` and
        # carries BOTH, so a strictly narrower rule -- `last_two(cwd) ==
        # last_two(home)` -- is available.
        #
        # It is rejected because it is a privacy REGRESSION at PARSE time, which
        # is the only time there is. Measured 2026-08-16:
        # `project_name_from_cwd("/home/victim", home="/Users/victim")` returns
        # `~` under the rule shipped here and `home/victim` under the narrow one
        # -- a transcript set written under a DIFFERENT home root (a machine
        # moved from Linux to macOS, a devcontainer or WSL home in the same set)
        # scanned under this one. The cost is a merged display row for a
        # directory named after you that is NOT inside your home; no total moves
        # and no price is wrong.
        #
        # The trade used to be recorded against `privacy_home_project_names_v1`,
        # whose retroactive reach over already-stored rows was called the only
        # mechanism reaching a session whose transcript is gone. That pass went
        # with the rest of the migrations, and under declare-and-rebuild such a
        # session does not come back at all -- so that half of the argument is
        # worth nothing now and only the parse-time half above is load-bearing.
        # Its citation went stale too: `tests/test_migrations.py`'s
        # `("linux", "home/victim")` case no longer exists, and the assertion
        # nearest it today, in `tests/test_scanner.py`, is a same-HOME case the
        # narrow rule would also pass.
        if folds_to_home(parts[-1], home_leaf):
            return "~"
        return "/".join(parts[-2:])
    if parts and folds_to_home(parts[-1], home_leaf):
        return "~"
    return parts[-1] if parts else "unknown"

def _extract_title(record):
    """Extract a session title from a custom-title or ai-title record."""
    rtype = record.get("type")
    if rtype == "custom-title":
        return _bounded_text(record.get("customTitle"), 4096)
    if rtype == "ai-title":
        return _bounded_text(record.get("aiTitle"), 4096)
    return ""

def is_subagent_record(record, source_path=""):
    """True if a record belongs to a dispatched subagent (Task/Agent tool).

    Subagents are detected three ways: an explicit ``isSidechain`` flag, an
    ``agentId`` on the record (or its ``data`` wrapper), or a transcript path
    under a ``subagents`` directory (Claude Code writes one jsonl per subagent).
    """
    if not isinstance(record, dict):
        return False
    if record.get("isSidechain") is True:
        return True
    if _bounded_text(record.get("agentId"), 512):
        return True
    data = record.get("data")
    if isinstance(data, dict) and _bounded_text(data.get("agentId"), 512):
        return True
    sp = str(source_path).replace("\\", "/").lower()
    return "/subagents/" in sp

def record_agent_id(record):
    """Pull the subagent id off a record, if any (top-level or data wrapper)."""
    if not isinstance(record, dict):
        return None
    agent_id = _bounded_text(record.get("agentId"), 512)
    if not agent_id:
        data = record.get("data")
        if isinstance(data, dict):
            agent_id = _bounded_text(data.get("agentId"), 512)
    return agent_id or None

def extract_agent_dispatch(record):
    """Pull subagent identity from a parent's tool_result record.

    Claude Code writes a ``toolUseResult`` dict on the user-side record that
    closes out an Agent/Task tool invocation. It carries ``agentId`` (matching
    the subagent jsonl's records) and ``agentType`` (the human-readable type
    such as 'general-purpose' or 'Explore') plus aggregate stats.
    """
    if not isinstance(record, dict) or record.get("type") != "user":
        return None
    tur = record.get("toolUseResult")
    if not isinstance(tur, dict):
        return None
    agent_id = _bounded_text(tur.get("agentId"), 512)
    agent_type = _bounded_text(tur.get("agentType"), 512)
    if not agent_id or not agent_type:
        return None
    return {
        "source": "claude",
        "agent_id": agent_id,
        "agent_type": agent_type,
        "dispatched_in_session": _bounded_text(record.get("sessionId"), 512),
        "completed_at": _bounded_text(record.get("timestamp"), 128),
        "status": _bounded_text(tur.get("status"), 128),
        "total_tokens": _optional_nonnegative_integer(tur.get("totalTokens")),
        "total_duration_ms": _optional_nonnegative_integer(tur.get("totalDurationMs")),
        "tool_use_count": _optional_nonnegative_integer(tur.get("totalToolUseCount")),
    }

# Claude Code surfaces a hit rate limit as an assistant record carrying
# `error: "rate_limit"`, `apiErrorStatus: 429` and a human-readable notice such
# as "You've hit your session limit · resets 3:20am (Europe/Madrid)". That
# sentence is the only place the reset time appears anywhere in the transcripts;
# the remaining allowance is never recorded, so it cannot be derived.
_RESET_HINT_RE = re.compile(r"resets\s+([^()\n]+?)\s*(?:\(([^)]+)\))?\s*$")


def _message_text(message):
    """Flatten an assistant message's content blocks into plain text."""
    limit = 2048
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return _bounded_text(content, limit)
    if not isinstance(content, list):
        return ""
    parts = []
    used = 0
    for block in content:
        if used >= limit:
            break
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            if parts:
                parts.append(" ")
                used += 1
                if used >= limit:
                    break
            text = block["text"][:limit - used]
            parts.append(text)
            used += len(text)
    return _bounded_text("".join(parts), limit)


def extract_limit_event(record, session_id):
    """Return an API-error notice from an assistant record, or None.

    Only records Claude Code itself flagged are accepted — `isApiErrorMessage`
    and the `error` field, never a substring of the prose. That distinction is
    load-bearing: the transcripts contain whole sessions discussing rate limiting
    in message text and shell commands, and a substring match would turn that
    conversation into fabricated incidents.
    """
    if not isinstance(record, dict):
        return None
    if record.get("isApiErrorMessage") is not True:
        return None
    # Store whatever `error` says rather than whitelisting the values we know
    # ("rate_limit", "server_error", "authentication_failed"). These are Claude
    # Code *client* fields with no server contract behind them, so if a future
    # release renames the value the record still lands in the database and stays
    # diagnosable, instead of the panel silently going empty.
    kind = _bounded_text(record.get("error"), 64)
    if not kind:
        return None
    event_uuid = _bounded_text(record.get("uuid"), 128)
    if not event_uuid:
        return None
    text = _message_text(record.get("message"))
    reset_hint = ""
    reset_zone = ""
    matched = _RESET_HINT_RE.search(text)
    if matched:
        reset_hint = _bounded_text(matched.group(1), 64)
        reset_zone = _bounded_text(matched.group(2) or "", 64)
    return {
        "event_uuid": event_uuid,
        "kind": kind,
        "session_id": session_id,
        "timestamp": _bounded_text(record.get("timestamp"), 128),
        "status": _optional_nonnegative_integer(record.get("apiErrorStatus")),
        "message": text,
        "reset_hint": reset_hint,
        "reset_zone": reset_zone,
    }


# Prefix for the key given to an assistant record that carries no `message.id`.
# Namespaced like Codex's `codex:<thread>:<cumulative>` so a synthesised key can
# never be confused with, or collide with, one Anthropic actually issued.
NO_MESSAGE_ID_PREFIX = "claude-noid:"


def _synthetic_message_id(line_number, session_id, timestamp, model, tool_name):
    """A dedup key for a turn whose record carries no `message.id`.

    Every turn needs a key the conditional unique index can see. Without one the
    row falls outside the source-qualified non-empty message-id conflict, the
    upsert in `insert_turns` never fires for it, and each re-read of the same
    bytes inserts another copy — permanently, with the session totals then
    reconciled from the duplicated rows so nothing downstream can notice. That is
    not hypothetical bookkeeping: a database whose stored columns do not match
    `db.SCHEMA_SQL` is rebuilt, which drops `processed_files`, so the very next
    scan re-reads every transcript on disk and every row it produces arrives at
    a key that already exists. This paragraph attributed that corpus-wide
    re-read to "one-time backfill markers (three so far)" until 2026-08-16; the
    markers, and the `processed_files` clearing they gated, went with the rest
    of the migrations, and a rebuild is what buys the re-read now.

    The key is derived from where the record sits in the file and stable
    attribution fields, not from mutable usage tallies.
    The line number is what stops this trading an over-count for an under-count:
    two genuinely distinct records that happen to agree in every field still
    occupy different lines. It is stable across both scan paths because
    transcripts are append-only and this parser numbers lines absolutely,
    including when it resumes at `skip_lines` — so the full re-read of a file
    reproduces exactly the keys its incremental reads stored.

    Claude Code has not been observed writing an id-less usage record, so this
    closes a latent hole rather than a live over-count. The shape is parsed and
    kept on purpose (`turns_no_id`), and `--projects-dir` is documented as
    pointing at foreign history, so the property is worth holding by construction
    rather than by the corpus's good behaviour.
    """
    # Length-prefix the fields rather than joining on a sentinel: transcript
    # strings may contain every control byte, including any proposed separator.
    # Token tallies are deliberately absent because they are cumulative and
    # mutable while a response streams; excluding them keeps a no-id final-line
    # rewrite on one stable key.
    digest = hashlib.sha256()
    for field in (session_id, timestamp, model, tool_name):
        encoded = (field or "").encode("utf-8", "replace")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"{NO_MESSAGE_ID_PREFIX}{line_number}:{digest.hexdigest()[:40]}"


class TranscriptReadError(Exception):
    """The read STREAM failed partway through a transcript.

    Raised by both parsers so `scanner.scan` can tell "this file is unreadable"
    from "this one record is" -- the distinction the comment inside the parse
    loop says the outer handler could not make, and the half that guard did not
    close. A read error used to be folded into a normal return carrying the
    PARTIAL `line_count`, which `scan()` then stamped into `processed_files`
    beside the file's REAL mtime; the skip test compares that mtime exactly, and
    a finished transcript's mtime never moves again, so the unread tail was lost
    permanently and silently. Reproduced by force-unmounting the volume under a
    running scan: 15,623 of 60,000 responses stored, and three later scans of
    the healthy, remounted, byte-identical file recovered none of the rest.

    `partial` carries the five-tuple parsed before the failure, because those
    records are real and there is no reason to throw them away -- `insert_turns`
    dedupes on `message_id`, so re-reading them when the fault clears is a
    no-op. What the caller must NOT do is record the file as processed.
    """

    def __init__(self, filepath, cause, partial):
        super().__init__(f"{filepath}: {cause}")
        self.filepath = filepath
        self.cause = cause
        self.partial = partial


def parse_jsonl_file(filepath, skip_lines=0, verbose=True, *, root=None):
    """Parse a JSONL file into (session_metas, turns, agents, limit_events, line_count).

    Deduplicates streaming events by message.id — Claude Code logs multiple
    JSONL records per API response, all sharing the same message.id. Only the
    last record per message_id is kept (it has the final usage tallies).

    ``skip_lines`` drives the incremental rescan of a file that has grown since
    the last scan: lines at or before that 1-based line number are still counted
    but are not parsed again. A successful return carries the file's full line
    count; a partial stream failure raises ``TranscriptReadError`` with the count
    and records reached before the error. How much of a successful read the next
    scan may treat as already consumed is `scan()`'s decision rather than this
    one's — a final line with no newline on it yet was read but not finished, and
    `scanner._lines_consumed` is where that is subtracted. This is the single
    parsing implementation for both the first read of a file and every later
    append — keeping the two in one place is what stops the incremental path from
    silently drifting away from the full-parse one.

    ``verbose`` decides whether the malformed-record warning is allowed to NAME
    the file. The warning itself always fires — a lossy scan the user is not
    told about is the defect that counter exists to close — but the path is the
    reader's own project topology, and `cmd_dashboard`'s background scan and
    `/api/rescan` both pass False precisely because nobody asked them for a
    scan log. Both surfaces that capture those streams capture stderr too
    (`ServerManager` appends `[server:err]` lines to the same output channel it
    tells users to share, and `docker logs` takes both), so moving the line to
    stderr does not answer this on its own and gating it is what does.
    """
    seen_messages = {}  # message_id -> turn dict (dedup streaming records)
    prefix_reasoning_effort = {}
    turns_no_id = []    # turns whose record had no message.id (never deduped
                        # against each other; see _synthetic_message_id)
    session_meta = {}   # session_id -> dict
    agents = {}         # agent_id -> dispatch dict
    limit_events = {}   # event uuid -> rate-limit notice
    line_count = 0
    malformed = 0
    first_error = None

    read_error = None
    try:
        with _open_transcript(filepath, root=root) as f:
            for line_count, line in enumerate(_iter_jsonl_lines(f), 1):
                # Contain a bad record to that record. The handler around the
                # whole loop cannot tell 'this file is unreadable' from 'this
                # one line is', so it treated both as end-of-file — and
                # `scan()` then wrote the partial line count to
                # `processed_files.lines` beside the file's real mtime, so no
                # later scan re-read the stranded tail. Nothing in this loop
                # is known to raise today; the guard is here because the cost
                # of being wrong about that is silent, permanent data loss,
                # and the sibling Codex parser has already been wrong about it
                # once (a non-finite `used_percent` reaching `int()`).
                try:
                    in_prefix = line_count <= skip_lines
                    if in_prefix and (line is None or (
                            '"custom-title"' not in line
                            and '"ai-title"' not in line)):
                        # Incremental parsing normally skips the prefix. Keep
                        # the first non-empty effort for an assistant response
                        # whose later streaming record is in the appended tail,
                        # so the parser itself has the same invariant as the
                        # database merge. This is a tiny prefix decode, not a
                        # second full usage parse.
                        if (line is not None and '"assistant"' in line
                                and '"effort"' in line):
                            prefix_record, _ = _decode_json_object(line.strip())
                            if (isinstance(prefix_record, dict)
                                    and prefix_record.get("type") == "assistant"):
                                prefix_message = prefix_record.get("message")
                                if isinstance(prefix_message, dict):
                                    prefix_id = _bounded_text(
                                        prefix_message.get("id"), 512)
                                    prefix_effort = _bounded_text(
                                        prefix_record.get("effort"), 64)
                                    if (prefix_id and prefix_effort
                                            and prefix_id not in prefix_reasoning_effort):
                                        prefix_reasoning_effort[prefix_id] = prefix_effort
                        continue
                    if line is None:
                        raise _oversized_line_lost()
                    line = line.strip()
                    if not line:
                        continue
                    record, undecodable = _decode_json_object(line)
                    if undecodable is not None:
                        # The commonest lossy record there is, and the one the
                        # counter below could not see: a bare `continue` here
                        # meant a scan that lost a response reported a clean run.
                        raise undecodable
                    if record is None:
                        continue        # read fine, holds no record of ours

                    rtype = record.get("type")
                    if rtype not in ("assistant", "user", "custom-title", "ai-title"):
                        continue

                    session_id = _bounded_text(record.get("sessionId"), 512)
                    if not session_id:
                        continue
                    assistant_message = None
                    if rtype == "assistant":
                        assistant_message = record.get("message")
                        if not isinstance(assistant_message, dict):
                            continue

                    # Extract session title from title records
                    title = _extract_title(record)
                    if title:
                        if session_id not in session_meta:
                            session_meta[session_id] = {
                                "session_id": session_id,
                                "project_name": "unknown",
                                "first_timestamp": "",
                                "last_timestamp": "",
                                "git_branch": "",
                                "model": None,
                                "topic": None,
                                "topic_is_custom": False,
                                "source": "claude",
                            }
                        meta = session_meta[session_id]
                        # custom-title always wins; ai-title only if no custom-title set
                        if rtype == "custom-title":
                            meta["topic"] = title
                            # WHICH KIND of record set the topic, carried out of
                            # the parse because `upsert_sessions` cannot tell:
                            # `sessions` stores the title text and nothing else,
                            # so a chunk carrying only a later ai-title had no way
                            # to know it must not overwrite the user's own label.
                            # Chunk-local is enough — the incoming record's kind
                            # alone reproduces a whole-file read's answer, which
                            # is why no column has to be added to store it.
                            meta["topic_is_custom"] = True
                        elif rtype == "ai-title" and not meta.get("topic"):
                            meta["topic"] = title
                        continue

                    if rtype == "user":
                        dispatch = extract_agent_dispatch(record)
                        if dispatch is not None:
                            agents[dispatch["agent_id"]] = dispatch

                    timestamp = _bounded_text(record.get("timestamp"), 128)
                    cwd = _bounded_text(record.get("cwd"))
                    git_branch = _bounded_text(record.get("gitBranch"), 1024)
                    project_name = project_name_from_cwd(cwd)

                    # Update session metadata from any record
                    if session_id not in session_meta:
                        session_meta[session_id] = {
                            "session_id": session_id,
                            "project_name": project_name,
                            "first_timestamp": timestamp,
                            "last_timestamp": timestamp,
                            "git_branch": git_branch,
                            "model": None,
                            "topic": None,
                            "topic_is_custom": False,
                            "source": "claude",
                        }
                    else:
                        meta = session_meta[session_id]
                        if (meta.get("project_name") in (None, "", "unknown")
                                and project_name != "unknown"):
                            meta["project_name"] = project_name
                        if timestamp:
                            meta["first_timestamp"] = timestamp_min(
                                meta["first_timestamp"], timestamp)
                            meta["last_timestamp"] = timestamp_max(
                                meta["last_timestamp"], timestamp)
                        if git_branch and not meta["git_branch"]:
                            meta["git_branch"] = git_branch

                    if rtype == "assistant":
                        # Throttling notices arrive as assistant records with a
                        # "<synthetic>" model and an all-zero usage block, so they
                        # would be dropped by the zero-token guard below. They are
                        # the only place Claude Code records that a limit was hit —
                        # and the only place the reset time is stated — so pull them
                        # out first.
                        limit_event = extract_limit_event(record, session_id)
                        if limit_event is not None:
                            limit_events[limit_event["event_uuid"]] = limit_event
                            continue

                        msg = assistant_message
                        usage = msg.get("usage", {})
                        if not isinstance(usage, dict):
                            usage = {}
                        model = _bounded_text(msg.get("model"), 512)
                        message_id = _bounded_text(msg.get("id"), 512)

                        input_tokens = _nonnegative_integer(usage.get("input_tokens"))
                        output_tokens = _nonnegative_integer(usage.get("output_tokens"))
                        cache_read = _nonnegative_integer(usage.get("cache_read_input_tokens"))
                        cache_creation = _nonnegative_integer(usage.get("cache_creation_input_tokens"))
                        # Cache writes are billed at two rates — 1.25x input for the
                        # 5-minute cache, 2x for the 1-hour one — and the record
                        # carries the split beside the flat total:
                        #   "cache_creation": {"ephemeral_1h_input_tokens": N,
                        #                      "ephemeral_5m_input_tokens": M}
                        # with N + M == cache_creation_input_tokens. Keeping only the
                        # flat number meant every write was priced at the cheaper
                        # tier. Only the 1-hour part is stored; the 5-minute part is
                        # the remainder, so the two can never disagree about the
                        # total the rest of the app already sums.
                        cache_tiers = usage.get("cache_creation")
                        if not isinstance(cache_tiers, dict):
                            cache_tiers = {}
                        cache_creation_1h = min(
                            _nonnegative_integer(cache_tiers.get("ephemeral_1h_input_tokens")),
                            cache_creation,
                        )

                        # Only record turns that have actual token usage
                        if input_tokens + output_tokens + cache_read + cache_creation == 0:
                            continue

                        # Extract tool name from content if present
                        tool_name = None
                        content = msg.get("content", [])
                        if not isinstance(content, list):
                            content = []
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "tool_use":
                                tool_name = _bounded_text(item.get("name"), 512) or None
                                break

                        if model:
                            session_meta[session_id]["model"] = model

                        # Bound enum-like effort and stop_reason fields more
                        # tightly than free-form identifiers. Effort belongs
                        # at record level; an omitted value means not
                        # recorded. A partial streaming record may have no
                        # stop reason, so the completing record must fill it
                        # on conflict.
                        reasoning_effort = _bounded_text(record.get("effort"), 64)
                        stop_reason = _bounded_text(msg.get("stop_reason"), 64)

                        turn = {
                            "session_id": session_id,
                            "timestamp": timestamp,
                            "model": model,
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "cache_read_tokens": cache_read,
                            "cache_creation_tokens": cache_creation,
                            "cache_creation_1h_tokens": cache_creation_1h,
                            "tool_name": tool_name,
                            "cwd": None,
                            "message_id": message_id,
                            "is_subagent": 1 if is_subagent_record(record, filepath) else 0,
                            "agent_id": record_agent_id(record),
                            "reasoning_effort": reasoning_effort,
                            "stop_reason": stop_reason,
                            # Keep the bounded branch from this response for
                            # per-turn cost attribution. A session that
                            # switches branch must not file all its usage
                            # under its session-level label.
                            "git_branch": git_branch,
                        }

                        # The last record supplies final usage, timestamp and
                        # other fields, while git_branch latches onto the
                        # first nonempty value. Replays can resample the
                        # working branch after a response was produced.
                        # Latching keeps full and incremental scans consistent
                        # with fill-when-blank storage. A response that
                        # genuinely spans a checkout is attributed to its
                        # starting branch; one branch label cannot split that
                        # response across both branches.
                        if message_id:
                            previous = seen_messages.get(message_id)
                            if previous is not None and previous["git_branch"]:
                                turn["git_branch"] = previous["git_branch"]
                            if (previous is not None
                                    and previous.get("reasoning_effort")):
                                # Usage/timestamp remain last-record-wins, but
                                # effort is a context label: preserve the first
                                # non-empty value so full and incremental parses
                                # feed the same merge invariant.
                                turn["reasoning_effort"] = previous[
                                    "reasoning_effort"]
                            elif prefix_reasoning_effort.get(message_id):
                                turn["reasoning_effort"] = \
                                    prefix_reasoning_effort[message_id]
                            seen_messages[message_id] = turn
                        else:
                            # Kept as its own turn, exactly as before: the key
                            # carries the line number, so two id-less records in
                            # one file can never collapse into each other. What
                            # it buys is idempotence across READS — the same
                            # bytes yield the same key, so a re-read merges
                            # instead of inserting a second row, and a verbatim
                            # copy of a transcript collapses into the original
                            # the way an id-bearing turn already does.
                            turn["message_id"] = _synthetic_message_id(
                                line_count, session_id, timestamp, model,
                                tool_name)
                            turns_no_id.append(turn)
                except Exception as exc:
                    if not malformed:
                        first_error = exc
                    malformed += 1
                    continue

    except Exception as e:
        # The READ failed, which is not end-of-file however much it looks like
        # one from here. Recorded and re-raised BELOW, once the normal return
        # value has been assembled, so the partial carried out is exactly what
        # a successful parse of the same prefix would have produced rather than
        # a second hand-built copy of it.
        #
        # `sys.stderr`, because the bare `print()` this used to be landed on the
        # dashboard's own stdout under `_background_scan`, where nobody reads it.
        read_error = e
        print(f"  Warning: error reading {terminal_safe(filepath)}: "
              f"{terminal_safe(e)}", file=sys.stderr)

    if malformed:
        # One line, not one per record: a corrupted transcript can hold
        # thousands, and the point is that the user learns the scan was lossy.
        #
        # stderr, like the read-error warning above it. That sibling was moved
        # there and this one was left behind, so a scan that lost records wrote
        # onto the same stdout `cmd_dashboard` puts the authenticated URL on.
        # The path is withheld unless the reader asked for a log -- see the
        # `verbose` note in the docstring. `first_error` is a decode error for
        # one RECORD (json/unicode), which carries no path of its own; a failure
        # to open the file at all is the sibling's, not this counter's.
        print(f"  Warning: skipped {malformed} unreadable record(s) in "
              f"{terminal_safe(filepath) if verbose else 'a transcript'}: "
              f"{terminal_safe(first_error)}", file=sys.stderr)

    turns = turns_no_id + list(seen_messages.values())
    parsed = (list(session_meta.values()), turns, list(agents.values()),
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

def discover_jsonl_files(project_dirs):
    """Return regular JSONL files without crossing symbolic-link boundaries."""
    files = []
    for project_dir in project_dirs:
        root = Path(project_dir)
        # A scan root is explicitly configured (DEFAULT_PROJECTS_DIRS or
        # --projects-dir), so a symlinked root is trusted input and is followed:
        # a symlinked ~/.claude/projects is an ordinary setup and must not
        # silently scan nothing. The privacy boundary is symlinks *inside* the
        # tree, which could redirect traversal to arbitrary locations; those are
        # still excluded below.
        if not root.is_dir():
            continue
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            # os.walk does not descend directory links with followlinks=False,
            # but filter them explicitly so the privacy boundary is obvious
            # and remains true if the traversal is changed later.
            dirnames[:] = [
                name for name in dirnames
                if not (current_path / name).is_symlink()
            ]
            for name in filenames:
                if not name.endswith(".jsonl"):
                    continue
                candidate = current_path / name
                try:
                    info = candidate.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    continue
                files.append(str(candidate))
    # Roots can legitimately overlap now that extra ones can be configured
    # (a parent and its child, a bind mount and its host path). Parsing the
    # same file twice is harmless — insert_turns merges on message_id — but it
    # doubles the work and writes a second processed_files row per file.
    return sorted(set(files))
