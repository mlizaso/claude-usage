"""The per-section queries that make up the dashboard payload.

Split out of dashboard_data.py, where all ten were inline in one 437-line
function. That shape made each one unreadable in isolation and untestable in
isolation — you could not exercise the project rollup without running the other
nine — and it hid the single rule they all obey:

**Every cost-bearing rollup is grouped by (…, source, local day, model), so the
client can filter by range and price per row.** A row that carries one model for
several models' tokens cannot be priced, which is the defect this shape exists
to prevent; see the `project_by_day_model` note below and AGENTS.md.

Each function here takes an open connection and returns the list it owns.
Nothing here opens, closes or migrates a database — that stays in
dashboard_data.get_dashboard_data.

**Nothing here reads the quota tables either, and that is the line to hold.**
Three of the payload's sections — `subscription_limits`, `codex_limits` and
`codex_limit_history` — stay in dashboard_data.py, so "one function per payload
section" describes this module's ten usage sections rather than every key.
Take the roster and its size off the four constants in
tests/test_payload_surface.py, never off a number written here: this sentence
said "fourteen" while the payload had fifteen, having been falsified by the key
that added the fifteenth. The discriminator is the `source` parameter every function below carries:
a quota surface cannot take one, because each **is** one assistant —
`claude_limits` reads `~/.claude.json`, and the Codex pair is
`WHERE kind = 'codex'`. Bringing them here would also bring `account` and
`os.environ` into a module whose whole contract is "hand me an open connection".
tests/test_payload_surface.py::TestEveryPayloadSectionHasADeclaredOwner pins
both halves.
"""

from .localdays import local_day_expr as _local_day
from .localdays import local_minute_expr as _local_minute
from .pricing import (
    LONG_CONTEXT_MODELS,
    LONG_CONTEXT_THRESHOLD,
    calc_cost_parts_tiered,
)
from .safejson import (
    dashboard_number as _dashboard_number,
    dashboard_text as _dashboard_text,
    optional_dashboard_number as _optional_dashboard_number,
)
from .timestamps import (
    parse_instant, register_timestamp_order, timestamp_compare, timestamp_order,
)

# Subagent type, with Claude Code's auto-compaction agent named rather than left
# as 'unknown'. Shared by the two subagent rollups so they cannot classify the
# same dispatch differently.
AGENT_TYPE_EXPR = (
    "COALESCE(a.agent_type, "
    "CASE WHEN t.agent_id LIKE 'acompact-%' THEN 'auto-compact' "
    "ELSE 'unknown' END)"
)

# One throttling incident can produce notices for multiple retries and
# subagents. Merge notices that share a reset target and are separated by less
# than this gap.
INCIDENT_GAP_SECONDS = 30 * 60

# The counted fields of a session's splits, in the order the payload writes
# them. Named once because `sessions_all` builds two views of the same rows
# (per day and per model) and a key present in one but not the other is a column
# that silently reads zero in the browser.
_SESSION_TOKEN_KEYS = ("input", "output", "cache_read", "cache_creation",
                       "cache_creation_1h", "turns")

_COST_PART_KEYS = ("input", "output", "cache_read", "cache_creation")
_LONG_TOKEN_KEYS = ("long_input", "long_output", "long_cache_read",
                    "long_cache_creation", "long_cache_creation_1h")


def _long_context_predicate(model_expr, input_expr):
    """SQLite predicate matching the exact long-context model families."""
    names = ", ".join("'" + name.replace("'", "''") + "'"
                       for name in sorted(LONG_CONTEXT_MODELS))
    snapshots = " OR ".join(
        f"{model_expr} GLOB '{name}-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
        f" OR {model_expr} GLOB '{name}-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'"
        for name in sorted(LONG_CONTEXT_MODELS)
    )
    # Snapshot patterns are date-shaped, so gpt-5.4-mini/nano cannot inherit
    # the parent tier merely because their ids share a textual prefix.
    return (f"(({model_expr} IN ({names})) OR ({snapshots})) AND ({input_expr}) > "
            f"{LONG_CONTEXT_THRESHOLD}")


def _long_token_sum(column, model_expr, input_expr="input_tokens"):
    """SQL SUM for the portion contributed by long-context turns."""
    if input_expr == "input_tokens":
        prefix = model_expr.rsplit(".", 1)[0] + "." if "." in model_expr else ""
        input_expr = " + ".join(
            f"COALESCE({prefix}{field}, 0)"
            for field in ("input_tokens", "cache_read_tokens", "cache_creation_tokens"))
    predicate = _long_context_predicate(model_expr, input_expr)
    return f"SUM(CASE WHEN {predicate} THEN COALESCE({column}, 0) ELSE 0 END)"


def _row_value(row, key, default=0):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


_PRICING_SUM_KEYS = (
    "input", "output", "cache_read", "cache_creation", "cache_creation_1h",
    "long_input", "long_output", "long_cache_read", "long_cache_creation",
    "long_cache_creation_1h", "reasoning", "turns", "dispatches",
)


def _merge_pricing_rows(rows, key_fields):
    """Merge rate-period subgroups while retaining exact cost components.

    A local-day bucket can contain turns on both sides of an effective-date
    change. SQL groups by ``pricing_day`` as a hidden dimension, then this
    combines those rows after each period has been priced independently.
    """
    merged = {}
    for source_row in rows:
        row = dict(source_row)
        key = tuple(row.get(field) for field in key_fields)
        cost = _cost_fields(row, day_key="pricing_day")
        target = merged.get(key)
        if target is None:
            target = dict(row)
            target["_cost"] = cost["cost"]
            target["_cost_parts"] = (dict(cost["cost_parts"])
                                      if cost["cost_parts"] else None)
            merged[key] = target
            continue
        # A blank start is "not recorded", never "earliest". `timestamp_compare`
        # sorts a blank below every real instant — correct for a sort, wrong for
        # this selection: one turn with no timestamp put a NULL `pricing_day`
        # subgroup beside the real one, and merging blanked `start`, `start_local`
        # and `start_day` for the whole dispatch while its tokens stayed in the
        # row. This is the rule `timestamp_min` already applies, and it repairs
        # the mirror case too: a real start now replaces a blank target.
        candidate = row.get("start_ts") if "start_ts" in row else None
        current = target.get("start_ts")
        if candidate and (not current
                          or timestamp_compare(candidate, current) < 0):
            for field in ("start_ts", "start_local", "start_day"):
                if field in row:
                    target[field] = row[field]
        for field in _PRICING_SUM_KEYS:
            if field in row:
                target[field] = (target.get(field) or 0) + (row[field] or 0)
        target["_cost"] += cost["cost"]
        if cost["cost_parts"]:
            if target["_cost_parts"] is None:
                target["_cost_parts"] = {name: 0.0 for name in _COST_PART_KEYS}
            for name in _COST_PART_KEYS:
                target["_cost_parts"][name] += cost["cost_parts"][name]
    return list(merged.values())


def _timestamp_duration_minutes(first, last, first_order=None, last_order=None):
    """Return a non-negative duration for valid mixed-offset timestamps."""
    try:
        first_order = first_order or timestamp_order(first)
        last_order = last_order or timestamp_order(last)
        # The persisted order keys are the canonical chronology used by SQL
        # ordering. Keep this guard in front of datetime subtraction so a
        # malformed or hand-written row cannot turn a session into negative
        # time; direct sqlite3 fixtures without the new columns derive them
        # through the same helper.
        if first_order > last_order:
            return 0.0
        start, end = parse_instant(first), parse_instant(last)
        if start is None or end is None:
            return 0.0
        return max(0.0, round((end - start).total_seconds() / 60, 1))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 0.0


def _cost_fields(row, model_key="model", day_key="day"):
    """Return exact aggregate cost/parts from a rollup row.

    SQL retains the long-context portion before grouping. The portion is removed
    from the outward row after this helper runs, so the public payload continues
    to expose the original token columns plus a single breakdown object.
    """
    if "_cost" in row:
        return {"cost": row["_cost"], "cost_parts": row.get("_cost_parts")}
    model = _dashboard_text(_row_value(row, model_key), "unknown") or "unknown"
    values = {key: _dashboard_number(_row_value(row, key))
              for key in ("input", "output", "cache_read", "cache_creation",
                          "cache_creation_1h")}
    long_values = {key: _dashboard_number(_row_value(row, key))
                   for key in _LONG_TOKEN_KEYS}
    parts = calc_cost_parts_tiered(
        model, values["input"], values["output"], values["cache_read"],
        values["cache_creation"], values["cache_creation_1h"],
        long_values["long_input"], long_values["long_output"],
        long_values["long_cache_read"], long_values["long_cache_creation"],
        long_values["long_cache_creation_1h"],
        timestamp=_row_value(row, day_key) if day_key else None)
    if parts is None:
        return {"cost": 0.0, "cost_parts": None}
    return {"cost": sum(parts.values()), "cost_parts": {
        key: _dashboard_number(parts[key]) for key in _COST_PART_KEYS}}


def long_token_sum(column, model_expr, input_expr="input_tokens"):
    """Public SQL helper for rollups and terminal report aggregation."""
    return _long_token_sum(column, model_expr, input_expr)


def cost_fields(row, model_key="model", day_key="day"):
    """Public cost helper shared by payload and terminal report rows."""
    return _cost_fields(row, model_key, day_key)


def source_clause(source, column="source", existing_where=False):
    """Public source predicate shared by payload and terminal reports."""
    return _source_clause(source, column, existing_where)


def normalized_source(column="source"):
    """SQL expression folding legacy blank/NULL source values into Claude."""
    return f"COALESCE(NULLIF({column}, ''), 'claude')"


# ── Source scoping ─────────────────────────────────────────────────────────
# The dashboard shows one assistant at a time, so it asks for one. Scoping in
# SQL rather than filtering the payload in the browser is what makes the first
# paint fast: on a database holding both, building the full payload costs
# seconds and half of it was always discarded by the client's own source filter.
#
# `None` means "everything", which is what the CLI and any older client get —
# the parameter is additive and the unscoped behaviour is unchanged.
def _source_clause(source, column="source", existing_where=False):
    """(sql_fragment, params) restricting a query to one source."""
    if not source:
        return "", ()
    keyword = "AND" if existing_where else "WHERE"
    # A row whose `source` is NULL or '' is Claude's — the column defaults to
    # 'claude' and the Codex parser always sets it explicitly — which is the
    # same defaulting the client applies.
    return f" {keyword} {normalized_source(column)} = ?", (source,)


def all_models(conn, source=None):
    """Every model id in the database, most-used first (drives the filter).
    """
    _scope, _params = _source_clause(source, "source", False)
    # ── All models (for filter UI) ────────────────────────────────────────────
    # GROUP BY uses the normalised expression too so NULL and '' don't end up
    # as two separate "unknown" rows.
    model_rows = conn.execute("""
        SELECT COALESCE(NULLIF(model, ''), 'unknown') as model
        FROM turns
{_scope}        GROUP BY COALESCE(NULLIF(model, ''), 'unknown')
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """.format(_scope=_scope), _params).fetchall()
    all_models = list(dict.fromkeys(
        _dashboard_text(r["model"], "unknown") or "unknown" for r in model_rows
    ))
    return all_models


def daily_by_model(conn, source=None):
    """Tokens per (local day, source, model) across ALL history.

    The client filters by range rather than the server, so one payload serves
    every range the page offers without a refetch.
    """
    _scope, _params = _source_clause(source, "source", False)
    # ── Daily per-model, ALL history (client filters by range) ────────────────
    daily_rows = conn.execute(f"""
        SELECT
            {_local_day('timestamp')} as day,
            date(timestamp) as pricing_day,
            COALESCE(NULLIF(source, ''), 'claude') as source,
            COALESCE(NULLIF(model, ''), 'unknown') as model,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            SUM(cache_creation_1h_tokens) as cache_creation_1h,
            SUM(reasoning_output_tokens)  as reasoning,
            {_long_token_sum('input_tokens', 'model')} as long_input,
            {_long_token_sum('output_tokens', 'model')} as long_output,
            {_long_token_sum('cache_read_tokens', 'model')} as long_cache_read,
            {_long_token_sum('cache_creation_tokens', 'model')} as long_cache_creation,
            {_long_token_sum('cache_creation_1h_tokens', 'model')} as long_cache_creation_1h,
            COUNT(*)                   as turns
        FROM turns
{_scope}        GROUP BY day, pricing_day, COALESCE(NULLIF(source, ''), 'claude'),
                 COALESCE(NULLIF(model, ''), 'unknown')
        ORDER BY day, model, pricing_day
    """.format(_scope=_scope), _params).fetchall()
    daily_rows = _merge_pricing_rows(
        daily_rows, ("day", "source", "model"))

    daily_by_model = [{
        "day":            _dashboard_text(r["day"]),
        "source":         _dashboard_text(r["source"], "claude") or "claude",
        "model":          _dashboard_text(r["model"], "unknown"),
        "input":          _dashboard_number(r["input"]),
        "output":         _dashboard_number(r["output"]),
        "cache_read":     _dashboard_number(r["cache_read"]),
        "cache_creation": _dashboard_number(r["cache_creation"]),
        "cache_creation_1h": _dashboard_number(r["cache_creation_1h"]),
        # A SUBSET of `output`, never added to it and never priced separately —
        # the output figure every cost path multiplies already contains these.
        # Carried so the tables can show how much of the output was thinking.
        "reasoning":      _dashboard_number(r["reasoning"]),
        "turns":          _dashboard_number(r["turns"]),
        **_cost_fields(r),
    } for r in daily_rows]
    return daily_by_model


def effort_by_day_model(conn, source=None):
    """Tokens per (local day, source, model, reasoning effort) across ALL history.

    Effort is ADDED to the (day, source, model) key rather than substituted for
    part of it. That is not stylistic: SQLite accepts a non-aggregated column
    outside the GROUP BY and returns an arbitrary row's value for it, so a
    rollup grouped by (day, effort) alone would still emit a `model` — one
    arbitrary model's name attached to a bucket that mixed several — and the
    client would price the whole bucket at that model's rate, up to 5x out, with
    no error and no failing test. Every row here knows its own model, which is
    the same rule `daily_by_model` and `project_by_day_model` follow.

    Rows whose effort was never recorded are kept and reported as '' rather than
    dropped or folded into a named level: a turn scanned before the column
    existed is *unknown*, not `medium`, and a breakdown that silently omitted
    them would not sum to the totals shown everywhere else.
    """
    _scope, _params = _source_clause(source, "source", False)
    rows = conn.execute(f"""
        SELECT
            {_local_day('timestamp')} as day,
            date(timestamp) as pricing_day,
            COALESCE(NULLIF(source, ''), 'claude') as source,
            COALESCE(NULLIF(model, ''), 'unknown') as model,
            COALESCE(reasoning_effort, '') as effort,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            SUM(cache_creation_1h_tokens) as cache_creation_1h,
            SUM(reasoning_output_tokens)  as reasoning,
            {_long_token_sum('input_tokens', 'model')} as long_input,
            {_long_token_sum('output_tokens', 'model')} as long_output,
            {_long_token_sum('cache_read_tokens', 'model')} as long_cache_read,
            {_long_token_sum('cache_creation_tokens', 'model')} as long_cache_creation,
            {_long_token_sum('cache_creation_1h_tokens', 'model')} as long_cache_creation_1h,
            COUNT(*)                   as turns
        FROM turns
{_scope}        GROUP BY day, pricing_day, COALESCE(NULLIF(source, ''), 'claude'),
                 COALESCE(NULLIF(model, ''), 'unknown'),
                 COALESCE(reasoning_effort, '')
        ORDER BY day, model, effort, pricing_day
    """.format(_scope=_scope), _params).fetchall()
    rows = _merge_pricing_rows(rows, ("day", "source", "model", "effort"))

    return [{
        "day":            _dashboard_text(r["day"]),
        "source":         _dashboard_text(r["source"], "claude") or "claude",
        "model":          _dashboard_text(r["model"], "unknown"),
        "effort":         _dashboard_text(r["effort"], ""),
        "input":          _dashboard_number(r["input"]),
        "output":         _dashboard_number(r["output"]),
        "cache_read":     _dashboard_number(r["cache_read"]),
        "cache_creation": _dashboard_number(r["cache_creation"]),
        "cache_creation_1h": _dashboard_number(r["cache_creation_1h"]),
        "reasoning":      _dashboard_number(r["reasoning"]),
        "turns":          _dashboard_number(r["turns"]),
        **_cost_fields(r),
    } for r in rows]


def stop_reason_by_day_model(conn, source=None):
    """Turn counts per (local day, source, model, stop reason) across ALL history.

    Only Claude records a stop reason; Codex rollouts carry no equivalent, so
    every Codex row lands in the '' bucket and the page reports the breakdown as
    Claude-only rather than claiming Codex never truncates.

    A max_tokens response can be truncated while contributing to the same
    usage totals as a complete response. Keep the reason visible whether the
    bucket is empty, contains one turn, or contains many.

    Group recorded stop reasons without a closed enum. All-zero synthetic
    responses are excluded from turns, so their stop_sequence notices must not
    dilute usage-turn shares.

    Carries `model` for the same anti-mispricing reason as `effort_by_day_model`,
    and so the existing model filter can gate it with no special case.
    """
    _scope, _params = _source_clause(source, "source", False)
    rows = conn.execute(f"""
        SELECT
            {_local_day('timestamp')} as day,
            date(timestamp) as pricing_day,
            COALESCE(NULLIF(source, ''), 'claude') as source,
            COALESCE(NULLIF(model, ''), 'unknown') as model,
            COALESCE(stop_reason, '')  as stop_reason,
            SUM(output_tokens)         as output,
            {_long_token_sum('output_tokens', 'model')} as long_output,
            COUNT(*)                   as turns
        FROM turns
{_scope}        GROUP BY day, pricing_day, COALESCE(NULLIF(source, ''), 'claude'),
                 COALESCE(NULLIF(model, ''), 'unknown'),
                 COALESCE(stop_reason, '')
        ORDER BY day, model, stop_reason, pricing_day
    """.format(_scope=_scope), _params).fetchall()
    rows = _merge_pricing_rows(rows, ("day", "source", "model", "stop_reason"))

    return [{
        "day":         _dashboard_text(r["day"]),
        "source":      _dashboard_text(r["source"], "claude") or "claude",
        "model":       _dashboard_text(r["model"], "unknown"),
        "stop_reason": _dashboard_text(r["stop_reason"], ""),
        "output":      _dashboard_number(r["output"]),
        "turns":       _dashboard_number(r["turns"]),
        **_cost_fields(r),
    } for r in rows]


def hourly_by_model(conn, source=None):
    """Tokens per (UTC day, hour, local day, source, model).

    The `day`/`hour` pair is still UTC and is still what the browser shifts
    behind its local/UTC toggle: converting only the day key would desynchronise
    the pair across midnight, which is why this query is invariant 4's one
    deliberate exemption.

    `local_day` is carried BESIDE that pair, not instead of it, and it exists so
    range membership can be exact. The client used to derive membership by
    resolving the bucket's UTC hour START into a local day, which is off by a
    whole bucket wherever a local-day boundary falls mid-hour -- every sub-hour
    UTC offset (`Asia/Kolkata` +05:30 puts local midnight at 18:30Z, so the
    18:00Z bucket straddles two local days). The tiles bucket by
    `date(timestamp,'localtime')` per TURN, so the card covered a different set
    of turns from the tiles above it while the range label was unchanged.

    Grouping by it splits a straddling bucket into two rows sharing one
    (day, hour) -- which is the point, since that is genuinely two local days'
    worth of turns -- and `aggregateHourly` sums them back into one hour bucket,
    so no total moves. This is the payload-side rule the project states for
    exactly this shape: add the rollup rather than approximate it client-side.
    """
    register_timestamp_order(conn)
    _scope, _params = _source_clause(source, "source", True)
    # ── Hourly per-day per-model (client filters by range + TZ-shifts) ────────
    # Raw timestamps retain their offset spelling. The ordering key already
    # carries the validated UTC instant, so use it for both coordinates. Keep
    # malformed dates in their raw bucket rather than letting SQLite normalize
    # an impossible calendar date. Direct SQL inserts may need the same shared
    # ordering function that normally stamps these keys during ingestion.
    hourly_rows = conn.execute("""
        WITH hourly_turns AS (
            SELECT timestamp, source, model, output_tokens,
                   COALESCE(NULLIF(timestamp_order, ''),
                            timestamp_order(timestamp)) as utc_order
            FROM turns
            WHERE timestamp IS NOT NULL AND length(timestamp) >= 13
{_scope}        )
        SELECT
            CASE WHEN substr(utc_order, 1, 2) = '2|'
                 THEN substr(utc_order, 3, 10)
                 ELSE substr(timestamp, 1, 10) END    as day,
            {local_day}                               as local_day,
            CAST(CASE WHEN substr(utc_order, 1, 2) = '2|'
                      THEN substr(utc_order, 14, 2)
                      ELSE substr(timestamp, 12, 2) END AS INTEGER) as hour,
            COALESCE(NULLIF(source, ''), 'claude')    as source,
            COALESCE(NULLIF(model, ''), 'unknown')    as model,
            SUM(output_tokens)                        as output,
            COUNT(*)                                  as turns
        FROM hourly_turns
        GROUP BY day, local_day, hour,
                 COALESCE(NULLIF(source, ''), 'claude'),
                 COALESCE(NULLIF(model, ''), 'unknown')
        ORDER BY day, hour, model
    """.format(_scope=_scope, local_day=_local_day('timestamp')), _params).fetchall()

    hourly_by_model = [{
        "day":    _dashboard_text(r["day"]),
        # The same expression every other rollup keys on, so a turn is in range
        # here exactly when it is in range there. Its COALESCE fallback is the
        # raw UTC prefix, so a timestamp `date()` cannot read still lands in the
        # bucket it always did rather than dropping out.
        "local_day": _dashboard_text(r["local_day"]),
        "source": _dashboard_text(r["source"], "claude") or "claude",
        "hour":   min(_dashboard_number(r["hour"]), 23),
        "model":  _dashboard_text(r["model"], "unknown"),
        "output": _dashboard_number(r["output"]),
        "turns":  _dashboard_number(r["turns"]),
    } for r in hourly_rows]
    return hourly_by_model


def sessions_all(conn, source=None):
    """One row per session, plus its per-(local day, model) split.

    A session carries one 'primary' model, so filtering on that alone made a
    session vanish when its primary was unchecked even though a model it also
    used was still selected — and priced its whole lifetime at that one model.
    `by_model` is what lets the client keep the session and sum only the
    selected models.

    `by_day_model` adds the day to that key, for the same reason
    `project_by_day_model` exists: the row totals here are a session's LIFETIME,
    so selecting sessions on `last_date` and printing those totals credited
    every token a session ever used to whatever range its last-active day fell
    in — and dropped, entirely, every session still running past the end of the
    range. It is nested inside the session rather than shipped as its own
    payload array because the per-day rows cannot carry the identity the table
    prints (id, project, title, Last Active, Duration); the client would have to
    join them back to these rows anyway.

    `by_model` stays: it is `by_day_model` summed over days, it is the contract
    AGENTS.md records, and it is what a payload with no day split still gets.
    """
    register_timestamp_order(conn)
    _scope, _params = _source_clause(source, "source", False)
    _tscope, _tparams = _source_clause(source, "source", False)
    # ── All sessions (client filters by range and model) ──────────────────────
    # last_local / last_day are the displayed time and the day the row is
    # filtered by. Both are local so a session lands on the same day the charts
    # put its turns on; first_timestamp/last_timestamp stay raw because the
    # duration below is a difference, which no timezone can change.
    session_rows = conn.execute(f"""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            COALESCE(NULLIF(first_timestamp_order, ''),
                     timestamp_order(first_timestamp)) as first_timestamp_order,
            COALESCE(NULLIF(last_timestamp_order, ''),
                     timestamp_order(last_timestamp)) as last_timestamp_order,
            {_local_minute('last_timestamp')} as last_local,
            {_local_day('last_timestamp')}    as last_day,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, total_cache_creation_1h,
            model, turn_count,
            git_branch, topic, COALESCE(NULLIF(source, ''), 'claude') as source
        FROM sessions{_scope}
    """.format(_scope=_scope), _params).fetchall()
    session_rows.sort(key=lambda row: row["last_timestamp_order"], reverse=True)

    # Per-session, per-LOCAL-DAY, per-model split — the finest grain the
    # sessions view needs, and the one both splits below are built from.
    #
    # The model half of the key: a session row carries one "primary" model, so
    # filtering by model could only ever match that one — unchecking it made
    # whole sessions (and their projects) disappear even though they had used a
    # model that was still selected, and the row's cost priced every token in
    # the session at the primary model.
    #
    # The day half: the row totals are lifetime, so a range selected on
    # `last_date` charged one day for a whole session's history and hid every
    # session that was still running after the range ended. Same defect, same
    # remedy and the same local-day expression as `project_by_day_model`.
    #
    # The branch half is not for the sessions table, which prints none: it is
    # what lets the client count the Sessions column of `Cost by Project &
    # Branch` at the same grain that table's money is now summed at. Keyed on
    # the session's single label instead, a branch row the label does not name
    # counted zero sessions beside real money — `feature | 0 sessions | $10.00`
    # next to `main | 1 session | $0.03`. It is the raw turn value, blank and
    # all: the client resolves a blank against the session label exactly as the
    # COALESCE in `project_by_day_model` does, so the two keys agree row for row
    # (a Codex turn, which never carries a branch, resolves to the session label
    # on both sides).
    session_day_rows = conn.execute(f"""
        SELECT
            session_id,
            COALESCE(NULLIF(source, ''), 'claude') as source,
            {_local_day('timestamp')}  as day,
            date(timestamp) as pricing_day,
            COALESCE(NULLIF(model, ''), 'unknown') as model,
            COALESCE(git_branch, '')   as branch,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            SUM(cache_creation_1h_tokens) as cache_creation_1h,
            {_long_token_sum('input_tokens', 'model')} as long_input,
            {_long_token_sum('output_tokens', 'model')} as long_output,
            {_long_token_sum('cache_read_tokens', 'model')} as long_cache_read,
            {_long_token_sum('cache_creation_tokens', 'model')} as long_cache_creation,
            {_long_token_sum('cache_creation_1h_tokens', 'model')} as long_cache_creation_1h,
            COUNT(*)                   as turns
FROM turns
{_tscope}        GROUP BY session_id, COALESCE(NULLIF(source, ''), 'claude'),
                 day, pricing_day,
                 COALESCE(NULLIF(model, ''), 'unknown'),
                 COALESCE(git_branch, '')
        ORDER BY session_id, day, model, branch, pricing_day
    """.format(_tscope=_tscope), _tparams).fetchall()
    session_day_rows = _merge_pricing_rows(
        session_day_rows, ("session_id", "source", "day", "model", "branch"))

    days_by_session = {}
    models_by_session = {}
    costs_by_session_model = {}
    for r in session_day_rows:
        session_id = _dashboard_text(r["session_id"])
        source_name = _dashboard_text(r["source"], "claude") or "claude"
        session_key = (source_name, session_id)
        model = _dashboard_text(r["model"], "unknown")
        # Sanitised once, per row, before anything is summed: the totals below
        # are sums of these, so a value the range check would have rejected must
        # not reach them.
        counts = {k: _dashboard_number(r[k]) for k in _SESSION_TOKEN_KEYS}
        # `by_model` is these rows summed, so the finer key changes how many
        # rows it sums and not what they add up to — the contract AGENTS.md
        # records for it is unchanged, and
        # `TestTheSessionDaySplitAccountsForEveryTurn` checks both ways round.
        days_by_session.setdefault(session_key, []).append(
            dict(day=_dashboard_text(r["day"]), model=model,
                 branch=_dashboard_text(r["branch"]), **counts,
                 **_cost_fields(r)))
        parts = models_by_session.setdefault(session_key, {})
        totals = parts.setdefault(model, {k: 0 for k in _SESSION_TOKEN_KEYS})
        for k in _SESSION_TOKEN_KEYS:
            totals[k] += counts[k]
        model_key = (source_name, session_id, model)
        model_parts = costs_by_session_model.setdefault(model_key, {
            "cost": 0.0, "cost_parts": None})
        row_cost = _cost_fields(r)
        model_parts["cost"] += row_cost["cost"]
        if row_cost["cost_parts"]:
            if model_parts["cost_parts"] is None:
                model_parts["cost_parts"] = {
                    key: 0.0 for key in _COST_PART_KEYS}
            for key in _COST_PART_KEYS:
                model_parts["cost_parts"][key] += row_cost["cost_parts"][key]

    sessions_all = []
    for r in session_rows:
        source_name = _dashboard_text(r["source"], "claude") or "claude"
        session_id = _dashboard_text(r["session_id"])
        session_key = (source_name, session_id)
        sessions_all.append({
            # Full id: the table truncates for display, but the CSV export
            # needs the whole thing (an 8-char prefix isn't uniquely useful).
            "session_id":    session_id,
            "source":        source_name,
            "project":       _dashboard_text(r["project_name"], "unknown") or "unknown",
            "branch":        _dashboard_text(r["git_branch"]),
            "topic":         _dashboard_text(r["topic"]),
            "last":          _dashboard_text(r["last_local"]),
            "last_date":     _dashboard_text(r["last_day"]),
            "duration_min":  _timestamp_duration_minutes(
                r["first_timestamp"], r["last_timestamp"],
                r["first_timestamp_order"], r["last_timestamp_order"]),
            "model":         _dashboard_text(r["model"], "unknown") or "unknown",
            "turns":         _dashboard_number(r["turn_count"]),
            "input":         _dashboard_number(r["total_input_tokens"]),
            "output":        _dashboard_number(r["total_output_tokens"]),
            "cache_read":    _dashboard_number(r["total_cache_read"]),
            "cache_creation": _dashboard_number(r["total_cache_creation"]),
            "cache_creation_1h": _dashboard_number(r["total_cache_creation_1h"]),
            # Two views of the same rows: the day split is what the table and
            # its CSV are range-scoped from, the model split is that summed over
            # days. Both are clamped again on the way out — a sum of in-range
            # values can leave the range the client can represent exactly.
            "by_day_model":  days_by_session.get(session_key, []),
            "by_model":      [
                dict(model=model, **{k: _dashboard_number(v[k])
                                     for k in _SESSION_TOKEN_KEYS},
                     **costs_by_session_model.get(
                         (source_name, session_id, model),
                         {"cost": 0.0, "cost_parts": None}))
                for model, v in models_by_session.get(session_key, {}).items()],
        })
    return sessions_all


def project_by_day_model(conn, source=None):
    """Tokens per (project, branch, local day, source, model).

    The project tables are built from THIS, never from `sessions_all`. A session
    row carries lifetime totals beside a single primary model and a single date,
    so aggregating it priced a mixed-model session at one model (up to 5x out)
    and credited its whole history to whatever range that date fell in (101x on
    a session crossing local midnight).
    """
    _scope, _params = _source_clause(source, "t.source", False)
    # Group project and branch usage by local day and model for precise cost
    # and range attribution. Do not sum distinct-session counts across those
    # rows: one session can appear in several buckets. The client counts
    # sessions from its range-filtered session list.
    project_rows = conn.execute(f"""
        SELECT
            COALESCE(NULLIF(s.project_name, ''), 'unknown') as project,
            -- Attribute each turn to its own branch when available. Codex and
            -- missing per-turn metadata fall back to the session's branch.
            COALESCE(NULLIF(t.git_branch, ''),
                     NULLIF(s.git_branch, ''), '')          as branch,
            {_local_day('t.timestamp')}                     as day,
            date(t.timestamp)                               as pricing_day,
            COALESCE(NULLIF(t.source, ''), 'claude')        as source,
            COALESCE(NULLIF(t.model, ''), 'unknown')        as model,
            SUM(t.input_tokens)                             as input,
            SUM(t.output_tokens)                            as output,
            SUM(t.cache_read_tokens)                        as cache_read,
            SUM(t.cache_creation_tokens)                    as cache_creation,
            SUM(t.cache_creation_1h_tokens)                 as cache_creation_1h,
            {_long_token_sum('t.input_tokens', 't.model')} as long_input,
            {_long_token_sum('t.output_tokens', 't.model')} as long_output,
            {_long_token_sum('t.cache_read_tokens', 't.model')} as long_cache_read,
            {_long_token_sum('t.cache_creation_tokens', 't.model')} as long_cache_creation,
            {_long_token_sum('t.cache_creation_1h_tokens', 't.model')} as long_cache_creation_1h,
            COUNT(*)                                        as turns
        FROM turns t
        LEFT JOIN sessions s ON t.session_id = s.session_id
            AND {normalized_source('t.source')} = {normalized_source('s.source')}
        -- Positional: `sessions` also has a `model` column, so a bare
        -- GROUP BY/ORDER BY `model` is ambiguous across the join.
{_scope}        GROUP BY 1, 2, 3, 4, 5, 6
        ORDER BY 3, 1, 2, 6, 4
    """.format(_scope=_scope), _params).fetchall()
    project_rows = _merge_pricing_rows(
        project_rows, ("project", "source", "branch", "day", "model"))

    project_by_day_model = [{
        "project":        _dashboard_text(r["project"], "unknown") or "unknown",
        "source":         _dashboard_text(r["source"], "claude") or "claude",
        "branch":         _dashboard_text(r["branch"]),
        "day":            _dashboard_text(r["day"]),
        "model":          _dashboard_text(r["model"], "unknown"),
        "input":          _dashboard_number(r["input"]),
        "output":         _dashboard_number(r["output"]),
        "cache_read":     _dashboard_number(r["cache_read"]),
        "cache_creation": _dashboard_number(r["cache_creation"]),
        "cache_creation_1h": _dashboard_number(r["cache_creation_1h"]),
        "turns":          _dashboard_number(r["turns"]),
        **_cost_fields(r),
    } for r in project_rows]
    return project_by_day_model


def subagent_by_type(conn, source=None):
    """Subagent tokens per (local day, type, source, model).

    Grouped on the three *expressions*, never on their SELECT aliases. A bare
    name in GROUP BY resolves against the input columns first, so
    `GROUP BY agent_type` bound to `agents.agent_type` — NULL for every dispatch
    with no parent record — and merged auto-compaction turns with unrecognised
    ones into a single group that the non-aggregated CASE then labelled from one
    arbitrary row. `source` and `model` are real `turns` columns too: grouping on
    those split a NULL source from an '' source into two rows carrying the
    identical 'claude' label, double-counting the dispatch.
    """
    _scope, _params = _source_clause(source, "t.source", True)
    # ── Subagent breakdown by type, by day & model ────────────────────────────
    # JOIN turns to agents (parent tool_result metadata captured by the scanner).
    # acompact-* ids are Claude Code's auto-compaction subagent (no parent
    # dispatch record); anything else without a match is shown as 'unknown'.
    # The classifier is the module-level AGENT_TYPE_EXPR, shared with
    # top_dispatches so the two cannot label the same dispatch differently.
    subagent_daily_rows = conn.execute(f"""
        SELECT
            {_local_day('t.timestamp')}              as day,
            date(t.timestamp)                        as pricing_day,
            {AGENT_TYPE_EXPR}                        as agent_type,
            COALESCE(NULLIF(t.source, ''), 'claude') as source,
            COALESCE(NULLIF(t.model, ''), 'unknown') as model,
            SUM(t.input_tokens)                      as input,
            SUM(t.output_tokens)                     as output,
            SUM(t.cache_read_tokens)                 as cache_read,
            SUM(t.cache_creation_tokens)             as cache_creation,
            SUM(t.cache_creation_1h_tokens)          as cache_creation_1h,
            {_long_token_sum('t.input_tokens', 't.model')} as long_input,
            {_long_token_sum('t.output_tokens', 't.model')} as long_output,
            {_long_token_sum('t.cache_read_tokens', 't.model')} as long_cache_read,
            {_long_token_sum('t.cache_creation_tokens', 't.model')} as long_cache_creation,
            {_long_token_sum('t.cache_creation_1h_tokens', 't.model')} as long_cache_creation_1h,
            COUNT(DISTINCT t.agent_id)               as dispatches,
            COUNT(*)                                 as turns
        FROM turns t
        LEFT JOIN agents a ON t.agent_id = a.agent_id
            AND {normalized_source('t.source')} = {normalized_source('a.source')}
        WHERE t.is_subagent = 1
{_scope}        GROUP BY day, pricing_day, {AGENT_TYPE_EXPR},
                 COALESCE(NULLIF(t.source, ''), 'claude'),
                 COALESCE(NULLIF(t.model, ''), 'unknown')
        ORDER BY day, {AGENT_TYPE_EXPR}, pricing_day
    """.format(_scope=_scope), _params).fetchall()
    subagent_daily_rows = _merge_pricing_rows(
        subagent_daily_rows, ("day", "source", "agent_type", "model"))

    subagent_by_type = [{
        "day":            _dashboard_text(r["day"]),
        "source":         _dashboard_text(r["source"], "claude") or "claude",
        "agent_type":     _dashboard_text(r["agent_type"], "unknown"),
        "model":          _dashboard_text(r["model"], "unknown"),
        "input":          _dashboard_number(r["input"]),
        "output":         _dashboard_number(r["output"]),
        "cache_read":     _dashboard_number(r["cache_read"]),
        "cache_creation": _dashboard_number(r["cache_creation"]),
        "cache_creation_1h": _dashboard_number(r["cache_creation_1h"]),
        "dispatches":     _dashboard_number(r["dispatches"]),
        "turns":          _dashboard_number(r["turns"]),
        **_cost_fields(r),
    } for r in subagent_daily_rows]
    return subagent_by_type


def top_dispatches(conn, source=None):
    """One row per (dispatch, source, model), plus its per-local-day split.

    Grouped per model, not per dispatch: with a bare `model` column SQLite
    returns one arbitrary row's value for the group, and the client then priced
    every token in the dispatch at it. The client collapses these back per
    agent_id, costing each row at its own model.

    `by_day` adds the day to that key, for the same reason `by_day_model` exists
    on a session: the row totals here are a dispatch's LIFETIME, so selecting
    dispatches on `start_date` charged one day for a dispatch's whole history —
    4.0x on a two-day fixture — and dropped, entirely, every dispatch still
    running after its start day. `start`, Duration, Tool Uses and Status stay
    whole-dispatch values, exactly as `Last Active` and `Duration` do in the
    sessions table; a per-day row cannot carry them.

    Send daily splits only for rows spanning multiple local days. A single-day
    row already carries the correct date and totals, so an empty split is
    exact and avoids redundant payload bytes.

    The lifetime aggregate uses a canonical earliest instant for `start`: valid
    ISO timestamps go through SQLite's Julian-day parser and are rendered back
    as UTC, while malformed values use a deterministic raw-text fallback. This
    keeps mixed-offset starts chronological without a second full turn scan.
    The separate day-grained query remains intentional: it supplies the sparse
    `by_day` payload without changing the lifetime row's identity or metadata.
    """
    _scope, _params = _source_clause(source, "t.source", True)
    # ── Top dispatches (one row per source / agent id / model) ───────────────
    # SQLite's MIN(text) is lexical, while transcript timestamps may carry
    # different offsets. `julianday` compares valid ISO instants; malformed
    # values are ignored by it and fall back to the deterministic text MIN.
    # The resulting UTC spelling is deliberately a canonical display value for
    # this payload rather than a promise to retain one raw source spelling.
    dispatch_start = (
        "COALESCE(datetime(MIN(julianday(t.timestamp))), MIN(t.timestamp))")
    top_dispatch_rows = conn.execute(f"""
        SELECT
            t.agent_id                               as agent_id,
            {AGENT_TYPE_EXPR}                        as agent_type,
            COALESCE(NULLIF(t.source, ''), 'claude') as source,
            COALESCE(NULLIF(t.model, ''), 'unknown') as model,
            {dispatch_start}                         as start_ts,
            {_local_minute(dispatch_start)}          as start_local,
            {_local_day(dispatch_start)}             as start_day,
            date(t.timestamp)                          as pricing_day,
            SUM(t.input_tokens)                      as input,
            SUM(t.output_tokens)                     as output,
            SUM(t.cache_read_tokens)                 as cache_read,
            SUM(t.cache_creation_tokens)             as cache_creation,
            SUM(t.cache_creation_1h_tokens)          as cache_creation_1h,
            {_long_token_sum('t.input_tokens', 't.model')} as long_input,
            {_long_token_sum('t.output_tokens', 't.model')} as long_output,
            {_long_token_sum('t.cache_read_tokens', 't.model')} as long_cache_read,
            {_long_token_sum('t.cache_creation_tokens', 't.model')} as long_cache_creation,
            {_long_token_sum('t.cache_creation_1h_tokens', 't.model')} as long_cache_creation_1h,
            COUNT(*)                                 as turns,
            a.dispatched_in_session                  as parent_session,
            a.total_duration_ms                      as duration_ms,
            a.tool_use_count                         as tool_uses,
            a.status                                 as status
        FROM turns t
        LEFT JOIN agents a ON t.agent_id = a.agent_id
            AND {normalized_source('t.source')} = {normalized_source('a.source')}
        WHERE t.is_subagent = 1 AND t.agent_id IS NOT NULL
        -- Grouped per (dispatch, model), not per dispatch: with a bare
        -- `model` column SQLite returns one arbitrary row's model for the whole
        -- group, and the client then priced every token in the dispatch at it —
        -- 5x out either way for a dispatch that spanned two models. The client
        -- collapses these back per source-qualified agent id, costing each row
        -- separately.
{_scope}        GROUP BY t.agent_id, pricing_day,
                 COALESCE(NULLIF(t.source, ''), 'claude'),
                 COALESCE(NULLIF(t.model, ''), 'unknown')
        ORDER BY (SUM(t.input_tokens) + SUM(t.output_tokens)
                  + SUM(t.cache_read_tokens) + SUM(t.cache_creation_tokens)) DESC
    """.format(_scope=_scope), _params).fetchall()
    top_dispatch_rows = _merge_pricing_rows(
        top_dispatch_rows, ("agent_id", "source", "model"))

    # The same turns with the local day added to the key. Grouped on the same
    # three expressions as the query above, so a row and its split cannot
    # disagree about which dispatch, source or model they describe.
    dispatch_day_rows = conn.execute(f"""
        SELECT
            t.agent_id                               as agent_id,
            COALESCE(NULLIF(t.source, ''), 'claude') as source,
            COALESCE(NULLIF(t.model, ''), 'unknown') as model,
            {_local_day('t.timestamp')}              as day,
            date(t.timestamp)                        as pricing_day,
            SUM(t.input_tokens)                      as input,
            SUM(t.output_tokens)                     as output,
            SUM(t.cache_read_tokens)                 as cache_read,
            SUM(t.cache_creation_tokens)             as cache_creation,
            SUM(t.cache_creation_1h_tokens)          as cache_creation_1h,
            {_long_token_sum('t.input_tokens', 't.model')} as long_input,
            {_long_token_sum('t.output_tokens', 't.model')} as long_output,
            {_long_token_sum('t.cache_read_tokens', 't.model')} as long_cache_read,
            {_long_token_sum('t.cache_creation_tokens', 't.model')} as long_cache_creation,
            {_long_token_sum('t.cache_creation_1h_tokens', 't.model')} as long_cache_creation_1h,
            COUNT(*)                                 as turns
        FROM turns t
        WHERE t.is_subagent = 1 AND t.agent_id IS NOT NULL
{_scope}        GROUP BY t.agent_id, COALESCE(NULLIF(t.source, ''), 'claude'),
                 COALESCE(NULLIF(t.model, ''), 'unknown'),
                 {_local_day('t.timestamp')}, date(t.timestamp)
        ORDER BY t.agent_id, {_local_day('t.timestamp')}, date(t.timestamp)
    """.format(_scope=_scope), _params).fetchall()
    dispatch_day_rows = _merge_pricing_rows(
        dispatch_day_rows, ("agent_id", "source", "model", "day"))

    def _key(r):
        return (_dashboard_text(r["agent_id"]),
                _dashboard_text(r["source"], "claude") or "claude",
                _dashboard_text(r["model"], "unknown"))

    days_by_dispatch = {}
    for r in dispatch_day_rows:
        # Sanitised once, per row, before anything is summed: the client adds
        # these up, so a value the range check would have rejected must not
        # reach it.
        days_by_dispatch.setdefault(_key(r), []).append(dict(
            day=_dashboard_text(r["day"]),
            **{k: _dashboard_number(r[k]) for k in _SESSION_TOKEN_KEYS},
            **_cost_fields(r)))

    top_dispatches = []
    for r in top_dispatch_rows:
        days = days_by_dispatch.get(_key(r), [])
        top_dispatches.append({
            "agent_id":       _dashboard_text(r["agent_id"]),
            "source":         _dashboard_text(r["source"], "claude") or "claude",
            "agent_type":     _dashboard_text(r["agent_type"], "unknown"),
            "model":          _dashboard_text(r["model"], "unknown"),
            "start":          _dashboard_text(r["start_local"]),
            "start_date":     _dashboard_text(r["start_day"]),
            "input":          _dashboard_number(r["input"]),
            "output":         _dashboard_number(r["output"]),
            "cache_read":     _dashboard_number(r["cache_read"]),
            "cache_creation": _dashboard_number(r["cache_creation"]),
            "cache_creation_1h": _dashboard_number(r["cache_creation_1h"]),
            "turns":          _dashboard_number(r["turns"]),
            **_cost_fields(r, day_key="start_day"),
            "duration_ms":    _optional_dashboard_number(r["duration_ms"]),
            "tool_uses":      _optional_dashboard_number(r["tool_uses"]),
            "status":         _dashboard_text(r["status"]),
            # What the table's token and cost columns are range-scoped from,
            # and `[]` for rows that lived on a single day — see the
            # docstring for what that empty array means and why it is not a
            # missing split. The identity beside it (start, duration, tool uses,
            # status) is the whole dispatch's, which is why those cells say so.
            "by_day":         days if len(days) > 1 else [],
        })
    return top_dispatches


def limit_incidents(conn, source=None):
    """Throttling notices collapsed into incidents.

    Collapsed here rather than in the browser because one incident produces a
    notice per retry and per subagent.
    """
    register_timestamp_order(conn)
    # Throttling notices come only from Claude transcripts; Codex has never
    # recorded one. Scoped anyway so the Codex view cannot show them.
    _scope, _params = _source_clause(source, "s.source", True)
    # Collapse retry and subagent notices into incidents before sending them
    # to the browser. Notices sharing a reset target within
    # INCIDENT_GAP_SECONDS belong to the same incident. Keep the module
    # constant authoritative.
    limit_rows = conn.execute(f"""
        SELECT
            l.timestamp                                  as timestamp,
            COALESCE(NULLIF(l.timestamp_order, ''),
                     timestamp_order(l.timestamp))         as timestamp_order,
            {_local_day('l.timestamp')}                  as day,
            {_local_minute('l.timestamp')}               as local_time,
            COALESCE(l.reset_hint, '')                   as reset_hint,
            COALESCE(l.reset_zone, '')                   as reset_zone,
            COALESCE(l.status, 0)                        as status,
            COALESCE(NULLIF(s.project_name, ''), 'unknown') as project
        FROM limit_events l
        LEFT JOIN sessions s ON l.session_id = s.session_id
            AND COALESCE(NULLIF(s.source, ''), 'claude') = 'claude'
        -- Only usage limits: server_error / authentication_failed are captured
        -- too (they are real friction) but they are not quota events and would
        -- make the panel mean two different things at once.
        WHERE l.timestamp <> '' AND COALESCE(l.kind, '') = 'rate_limit'{_scope}
        ORDER BY timestamp_order
    """.format(_scope=_scope), _params).fetchall()

    incidents = []
    for row in limit_rows:
        at = parse_instant(_dashboard_text(row["timestamp"]))
        if at is None:
            continue
        hint = _dashboard_text(row["reset_hint"])
        current = incidents[-1] if incidents else None
        same = (current is not None
                and current["reset_hint"] == hint
                and (at - current["_last"]).total_seconds() <= INCIDENT_GAP_SECONDS)
        if same:
            current["_last"] = at
            current["notices"] += 1
            current["projects"].add(_dashboard_text(row["project"], "unknown"))
        else:
            incidents.append({
                "_first": at, "_last": at, "notices": 1,
                "projects": {_dashboard_text(row["project"], "unknown")},
                "day": _dashboard_text(row["day"]),
                "started": _dashboard_text(row["local_time"]),
                "reset_hint": hint,
                "reset_zone": _dashboard_text(row["reset_zone"]),
                "status": _dashboard_number(row["status"]),
            })

    limit_incidents = [{
        "day":          i["day"],
        "started":      i["started"],
        "blocked_min":  max(0.0, round((i["_last"] - i["_first"]).total_seconds() / 60, 1)),
        "notices":      i["notices"],
        "projects":     sorted(i["projects"]),
        "reset_hint":   i["reset_hint"],
        "reset_zone":   i["reset_zone"],
        "status":       i["status"],
    } for i in incidents]
    return limit_incidents
