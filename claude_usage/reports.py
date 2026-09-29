"""Rendering the `today`, `week` and `stats` terminal reports.

The presentation layer for the CLI: it receives an open connection, queries it
and prints. Kept apart from cli.py so that file is only argument parsing and
dispatch, and so the report bodies can be read without scrolling past pricing
tables and connection handling.

Costs are summed per row, never by pricing an aggregate — each row carries its
own model. Days are the viewer's local calendar day (localdays.LOCAL_DAY),
bracketed by an indexed raw-timestamp window so the query does not scan the
whole table.

**Every report covers exactly one assistant**, the same rule the dashboard
obeys. Claude is billed per token against published rates; a Codex plan is a
subscription. A blended total adds a real number to a differently-derived one
and matches nothing the dashboard can show, so the scope is stated in the header
and `--source all` has to be asked for by name.
"""

from datetime import date, timedelta

from .localdays import LOCAL_DAY, local_day_expr, utc_window
from .pricing import fmt, fmt_cost
# One definition of the source predicate, shared with the dashboard rollups.
# Written twice it would be free to drift, which is exactly why localdays.py
# exists — see AGENTS.md on the two hand-synced local-day copies.
from .rollups import cost_fields, long_token_sum, normalized_source, source_clause
from .safetext import terminal_safe
from .timestamps import timestamp_max, timestamp_min

# Reports default to Claude, matching the dashboard's own default, unless the
# database holds only one assistant — then there is nothing to choose between
# and the reports cover it without a flag. A Codex-only machine must not be
# handed an empty report because of a default it was never asked about.
DEFAULT_SOURCE = "claude"
SOURCE_LABELS = {"claude": "Claude Code", "codex": "Codex"}
# `None` scopes nothing, so it is what `--source all` resolves to.
ALL_SOURCES_LABEL = "all sources"
BLEND_NOTE = (
    "  Note: this spans two billing regimes — Claude is billed per token at\n"
    "        published rates, Codex is a subscription quota. The total below is\n"
    "        a sum of unlike figures and matches no dashboard view."
)


_REPORT_SUM_KEYS = ("inp", "out", "cr", "cc", "cc1h", "long_inp",
                    "long_out", "long_cr", "long_cc", "long_cc1h", "turns")


def _report_cost_fields(row):
    """Price a report subgroup using its canonical token aliases."""
    if "_cost" in row:
        return {"cost": row["_cost"], "cost_parts": row.get("_cost_parts")}
    canonical = {
        "model": row.get("model"),
        "input": row.get("inp", 0), "output": row.get("out", 0),
        "cache_read": row.get("cr", 0),
        "cache_creation": row.get("cc", 0),
        "cache_creation_1h": row.get("cc1h", 0),
        "long_input": row.get("long_inp", 0),
        "long_output": row.get("long_out", 0),
        "long_cache_read": row.get("long_cr", 0),
        "long_cache_creation": row.get("long_cc", 0),
        "long_cache_creation_1h": row.get("long_cc1h", 0),
        "pricing_day": row.get("pricing_day"),
    }
    return cost_fields(canonical, day_key="pricing_day")


def _merge_report_rows(rows, key_fields):
    """Merge hidden pricing-day groups after independently pricing each."""
    merged = {}
    for source_row in rows:
        row = dict(source_row)
        cost = _report_cost_fields(row)
        key = tuple(row.get(field) for field in key_fields)
        target = merged.get(key)
        if target is None:
            target = dict(row)
            target["_cost"] = cost["cost"]
            target["_cost_parts"] = (dict(cost["cost_parts"])
                                      if cost["cost_parts"] else None)
            merged[key] = target
            continue
        for field in _REPORT_SUM_KEYS:
            if field in row:
                target[field] = (target.get(field) or 0) + (row[field] or 0)
        target["_cost"] += cost["cost"]
        if cost["cost_parts"]:
            if target["_cost_parts"] is None:
                target["_cost_parts"] = {name: 0.0 for name in (
                    "input", "output", "cache_read", "cache_creation")}
            for name in target["_cost_parts"]:
                target["_cost_parts"][name] += cost["cost_parts"][name]
    return list(merged.values())


def _report_cost(row):
    return _report_cost_fields(row)["cost"]


def hr(char="-", width=60):
    print(char * width)


def source_label(source):
    """Human name for a resolved scope (`None` meaning every source)."""
    if source is None:
        return ALL_SOURCES_LABEL
    return SOURCE_LABELS.get(source, source)


def sources_present(conn):
    """Which assistants this database holds, in name order.

    NULL or '' is Claude's, the same fold `dashboard_data.available_sources`
    applies in SQL and `inSource` applies on the page. That is the defaulting
    `db.SCHEMA_SQL` declares -- `source TEXT DEFAULT 'claude'` -- and not
    anything a migration applies; this docstring named a migration as the third
    copy, and "a row written before the column existed" as what carries the
    blank, until 2026-08-16. Neither survives: a database without `turns.source`
    does not match the declared schema, so `init_db` rebuilds it. What does
    reach here is a Python-API caller passing the key explicitly -- measured
    2026-08-16, `insert_turns` with `source=None` stores NULL and `source=""`
    stores '', and both come back from this function as `claude`.

    That normalisation happens in Python rather than in the SQL, because
    `COALESCE(NULLIF(source, ''), …)` is a function of the column and so cannot
    use `idx_turns_source` for the grouping. Selecting the bare column permits
    an index-only scan; normalize the small result after the query.

    Only the *set* matters to every caller, so dropping the turn counts the
    dashboard's equivalent query carries costs nothing here.
    """
    rows = conn.execute("SELECT DISTINCT source FROM turns").fetchall()
    return sorted({(r["source"] or "") or "claude" for r in rows})


def _has_claude_turns(conn):
    """Whether any turn is Claude's, in index seeks rather than a scan.

    Three separate probes rather than one `IN`/`OR`/`COALESCE` predicate: each
    of these is a point lookup SQLite can answer from `idx_turns_source` in
    O(log n), and on any machine that has ever run Claude Code the first one
    hits immediately.
    """
    for sql, params in (("SELECT 1 FROM turns WHERE source = ? LIMIT 1", ("claude",)),
                        ("SELECT 1 FROM turns WHERE source = ? LIMIT 1", ("",)),
                        ("SELECT 1 FROM turns WHERE source IS NULL LIMIT 1", ())):
        if conn.execute(sql, params).fetchone():
            return True
    return False


def resolve_source(conn, requested=None):
    """The scope a report should use: a source name, or `None` for all of them.

    An explicit request is honoured even when the database holds nothing for it
    — an empty report is the truthful answer to "show me Codex" on a machine
    that has never run it, and inventing a fallback would put Claude's money
    under a Codex heading.

    The Claude probe comes first so the default path stays a pair of index
    seeks. Enumerating the sources is only needed to rescue a database that has
    no Claude data at all, and that is the one case where the scan is affordable
    because it is also the case where the default would otherwise be wrong.
    """
    if requested == "all":
        return None
    if requested:
        return requested
    if _has_claude_turns(conn):
        return DEFAULT_SOURCE
    present = sources_present(conn)
    return present[0] if len(present) == 1 else DEFAULT_SOURCE


def _stats_title(source):
    """`stats` carries its scope in the title rather than a separate line.

    "Claude Code Usage - All-Time Statistics" printed above Codex figures would
    be the very ambiguity this scoping exists to remove.
    """
    if source is None:
        return "Combined Usage - All-Time Statistics"
    return f"{source_label(source)} Usage - All-Time Statistics"


def _print_scope(source, conn):
    """The one line that stops a total from being ambiguous."""
    print(f"  Source: {source_label(source)}")
    if source is None and len(sources_present(conn)) > 1:
        print(BLEND_NOTE)


def _cmd_today(conn, source=DEFAULT_SOURCE):
    today = date.today().isoformat()
    lo, hi = utc_window(today, today)
    scope, sparams = source_clause(source, "source", existing_where=True)

    rows = conn.execute(f"""
        SELECT
            COALESCE(NULLIF(model, ''), 'unknown') as model,
            date(timestamp)            as pricing_day,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            SUM(cache_creation_1h_tokens) as cc1h,
            {long_token_sum('input_tokens', 'model')} as long_inp,
            {long_token_sum('output_tokens', 'model')} as long_out,
            {long_token_sum('cache_read_tokens', 'model')} as long_cr,
            {long_token_sum('cache_creation_tokens', 'model')} as long_cc,
            {long_token_sum('cache_creation_1h_tokens', 'model')} as long_cc1h,
            COUNT(*)                   as turns
        FROM turns
        WHERE timestamp >= ? AND timestamp < ? AND {LOCAL_DAY} = ?{scope}
        GROUP BY model, pricing_day
        ORDER BY inp + out DESC, pricing_day
    """, (lo, hi, today, *sparams)).fetchall()
    rows = _merge_report_rows(rows, ("model",))

    sessions = conn.execute(f"""
        SELECT COUNT(DISTINCT COALESCE(NULLIF(source, ''), 'claude') || char(31) || session_id) as cnt
        FROM turns
        WHERE timestamp >= ? AND timestamp < ? AND {LOCAL_DAY} = ?{scope}
    """, (lo, hi, today, *sparams)).fetchone()

    subagent = conn.execute(f"""
        SELECT
            COUNT(*) as turns,
            SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) as tokens
        FROM turns
        WHERE timestamp >= ? AND timestamp < ? AND {LOCAL_DAY} = ?
          AND COALESCE(is_subagent, 0) = 1{scope}
    """, (lo, hi, today, *sparams)).fetchone()

    print()
    hr()
    print(f"  Today's Usage  ({today})")
    _print_scope(source, conn)
    hr()

    if not rows:
        print("  No usage recorded today.")
        print()
        return

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0

    for r in rows:
        cost = _report_cost(r)
        total_cost += cost
        total_inp += r["inp"] or 0
        total_out += r["out"] or 0
        total_cr  += r["cr"]  or 0
        total_cc  += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"  {terminal_safe(r['model']):<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"  {'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  Sessions today:   {sessions['cnt']}")
    print(f"  Subagent tokens:  {fmt(subagent['tokens'] or 0)}  ({fmt(subagent['turns'] or 0)} turns)")
    print(f"  Cache read:       {fmt(total_cr)}")
    print(f"  Cache creation:   {fmt(total_cc)}")
    hr()
    print()

def _cmd_week(conn, source=DEFAULT_SOURCE):

    today_d = date.today()
    start_d = today_d - timedelta(days=6)
    start = start_d.isoformat()
    end = today_d.isoformat()
    lo, hi = utc_window(start, end)
    scope, sparams = source_clause(source, "source", existing_where=True)

    by_day_model = conn.execute(f"""
        SELECT
            {LOCAL_DAY}                as day,
            date(timestamp)            as pricing_day,
            COALESCE(NULLIF(model, ''), 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            SUM(cache_creation_1h_tokens) as cc1h,
            {long_token_sum('input_tokens', 'model')} as long_inp,
            {long_token_sum('output_tokens', 'model')} as long_out,
            {long_token_sum('cache_read_tokens', 'model')} as long_cr,
            {long_token_sum('cache_creation_tokens', 'model')} as long_cc,
            {long_token_sum('cache_creation_1h_tokens', 'model')} as long_cc1h,
            COUNT(*)                   as turns
        FROM turns
        WHERE timestamp >= ? AND timestamp < ?
          AND {LOCAL_DAY} BETWEEN ? AND ?{scope}
        GROUP BY day, pricing_day, model
    """, (lo, hi, start, end, *sparams)).fetchall()
    by_day_model = _merge_report_rows(by_day_model, ("day", "model"))

    by_model = conn.execute(f"""
        SELECT
            date(timestamp)            as pricing_day,
            COALESCE(NULLIF(model, ''), 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            SUM(cache_creation_1h_tokens) as cc1h,
            {long_token_sum('input_tokens', 'model')} as long_inp,
            {long_token_sum('output_tokens', 'model')} as long_out,
            {long_token_sum('cache_read_tokens', 'model')} as long_cr,
            {long_token_sum('cache_creation_tokens', 'model')} as long_cc,
            {long_token_sum('cache_creation_1h_tokens', 'model')} as long_cc1h,
            COUNT(*)                   as turns
        FROM turns
        WHERE timestamp >= ? AND timestamp < ?
          AND {LOCAL_DAY} BETWEEN ? AND ?{scope}
        GROUP BY pricing_day, model
        ORDER BY inp + out DESC
    """, (lo, hi, start, end, *sparams)).fetchall()
    by_model = _merge_report_rows(by_model, ("model",))

    sessions = conn.execute(f"""
        SELECT COUNT(DISTINCT COALESCE(NULLIF(source, ''), 'claude') || char(31) || session_id) as cnt
        FROM turns
        WHERE timestamp >= ? AND timestamp < ?
          AND {LOCAL_DAY} BETWEEN ? AND ?{scope}
    """, (lo, hi, start, end, *sparams)).fetchone()

    print()
    hr()
    print(f"  Weekly Usage  ({start} to {end})")
    _print_scope(source, conn)
    hr()

    if not by_model:
        print("  No usage recorded in the last 7 days.")
        print()
        return

    # Aggregate per-day across models (with per-turn cost attribution)
    per_day = {}
    for r in by_day_model:
        d = r["day"]
        bucket = per_day.setdefault(d, {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        bucket["turns"] += r["turns"]
        bucket["inp"]   += r["inp"] or 0
        bucket["out"]   += r["out"] or 0
        bucket["cost"]  += _report_cost(r)

    print("  By Day:")
    for i in range(7):
        d = (start_d + timedelta(days=i)).isoformat()
        b = per_day.get(d, {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        print(f"    {d}  turns={b['turns']:<4}  in={fmt(b['inp']):<8}  out={fmt(b['out']):<8}  cost={fmt_cost(b['cost'])}")

    hr()
    print("  By Model:")

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0
    for r in by_model:
        cost = _report_cost(r)
        total_cost  += cost
        total_inp   += r["inp"] or 0
        total_out   += r["out"] or 0
        total_cr    += r["cr"]  or 0
        total_cc    += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"    {terminal_safe(r['model']):<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"    {'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  Sessions this week:  {sessions['cnt']}")
    print(f"  Cache read:          {fmt(total_cr)}")
    print(f"  Cache creation:      {fmt(total_cc)}")
    hr()
    print()

def _cmd_stats(conn, source=DEFAULT_SOURCE):
    # Two forms of the same predicate: the `sessions`/`turns` queries below have
    # no WHERE of their own, the subagent and daily-average ones do, and the
    # project rollup reads the column through a join alias.
    scope, sparams = source_clause(source, "source", existing_where=False)
    tscope, tparams = source_clause(source, "t.source", existing_where=False)
    wscope, wparams = source_clause(source, "source", existing_where=True)

    # Session-level info (count, date range). SQL MIN/MAX is lexical, not
    # chronological, once valid timestamps carry different offsets. Reduce
    # the raw endpoints with the neutral timestamp helper, then render the
    # selected raw values with the same local-day expression as every report.
    session_bounds = conn.execute(f"""
        SELECT first_timestamp, last_timestamp
        FROM sessions{scope}
    """, sparams).fetchall()
    first_timestamp = ""
    last_timestamp = ""
    for bound in session_bounds:
        first_timestamp = timestamp_min(first_timestamp,
                                        bound["first_timestamp"])
        last_timestamp = timestamp_max(last_timestamp,
                                       bound["last_timestamp"])
    rendered_bounds = conn.execute(f"""
        SELECT {local_day_expr("?")} as first,
               {local_day_expr("?")} as last
    """, (first_timestamp,) * 4 + (last_timestamp,) * 4).fetchone()
    session_info = {
        "sessions": len(session_bounds),
        "first": rendered_bounds["first"],
        "last": rendered_bounds["last"],
    }

    # All-time totals from turns (more accurate — per-turn model attribution)
    totals = conn.execute(f"""
        SELECT
            SUM(input_tokens)             as inp,
            SUM(output_tokens)            as out,
            SUM(cache_read_tokens)        as cr,
            SUM(cache_creation_tokens)    as cc,
            SUM(cache_creation_1h_tokens) as cc1h,
            COUNT(*)                      as turns
        FROM turns{scope}
    """, sparams).fetchone()

    # By model from turns (each turn has the actual model used)
    by_model = conn.execute(f"""
        SELECT
            date(timestamp)            as pricing_day,
            COALESCE(NULLIF(model, ''), 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            SUM(cache_creation_1h_tokens) as cc1h,
            {long_token_sum('input_tokens', 'model')} as long_inp,
            {long_token_sum('output_tokens', 'model')} as long_out,
            {long_token_sum('cache_read_tokens', 'model')} as long_cr,
            {long_token_sum('cache_creation_tokens', 'model')} as long_cc,
            {long_token_sum('cache_creation_1h_tokens', 'model')} as long_cc1h,
            COUNT(*)                   as turns
        FROM turns{scope}
        GROUP BY pricing_day, model
        ORDER BY inp + out DESC
    """, sparams).fetchall()
    by_model = _merge_report_rows(by_model, ("model",))
    # Pricing-day is intentionally a hidden cost boundary, not a session
    # boundary. Counting inside it and keeping the first group under-counts a
    # model used by different sessions on different rate days; summing those
    # counts over-counts one session that spans a rate day. Derive the distinct
    # source/session identity once at the visible model grain instead.
    model_sessions = {
        row["model"]: row["sessions"]
        for row in conn.execute(f"""
            SELECT model, COUNT(*) AS sessions
            FROM (
                SELECT DISTINCT
                    COALESCE(NULLIF(model, ''), 'unknown') AS model,
                    {normalized_source('source')} AS source_key,
                    session_id
                FROM turns{scope}
            )
            GROUP BY model
        """, sparams).fetchall()
    }
    for row in by_model:
        row["sessions"] = model_sessions.get(row["model"], 0)

    # Top 5 projects from turns (join with sessions for project name)
    top_projects = conn.execute(f"""
        SELECT
            COALESCE(s.project_name, 'unknown') as project_name,
            SUM(t.input_tokens)  as inp,
            SUM(t.output_tokens) as out,
            COUNT(*)             as turns,
            COUNT(DISTINCT COALESCE(NULLIF(t.source, ''), 'claude') || char(31) || t.session_id) as sessions
        FROM turns t
        LEFT JOIN sessions s ON t.session_id = s.session_id
            AND {normalized_source('t.source')} = {normalized_source('s.source')}{tscope}
        GROUP BY s.project_name
        ORDER BY inp + out DESC
        LIMIT 5
    """, tparams).fetchall()

    # Subagent totals (subagent tokens are included in the all-time totals above)
    subagent = conn.execute(f"""
        SELECT
            COUNT(*) as turns,
            SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) as tokens
        FROM turns
        WHERE COALESCE(is_subagent, 0) = 1{wscope}
    """, wparams).fetchone()

    # Daily average (last 30 days), bounded at BOTH ends like `today` and
    # `week` above. It used to be open at the top -- `timestamp >= ?` with no
    # upper bound and `LOCAL_DAY >= date('now','localtime','-30 days')` with
    # none either -- and an average is the one figure in this report where an
    # extra GROUP BY row moves the answer rather than adding to it. Measured
    # 2026-08-16 on a three-day fixture of 3,000 input tokens a day: one
    # well-formed turn dated two days in the FUTURE at 300,000 input took the
    # printed average from 3.0K to 77.3K, a 25.75x error, and so did the same
    # turn 200 days out. Nothing needs to be corrupt for that: a clock-skewed
    # machine, a VM restored from a snapshot, a UTC/local RTC mismatch, or
    # transcripts scanned from another host all produce future-dated records.
    # The mirror image was already correct -- the same turn 200 days in the
    # PAST left the figure at 3.0K -- which is what showed the bound was
    # missing on one side only.
    #
    # The same hole admitted the raw-prefix buckets `local_day_expr`'s gate
    # deliberately creates: one turn whose stored timestamp is the literal text
    # `now` gave 77.3K, and so did one stored as the Julian number `2460000.5`.
    #
    # Both bounds exclude those by SORT ORDER, which was half a closure, and
    # this comment recorded the residue as one barely reachable case. Measured
    # 2026-08-16 on the same fixture: `2026-08-00`, `2026-07-32` and
    # `2026-07-99` all sort INSIDE the range, pass both lexicographic tests and
    # print 77.3K, while `2026-02-31` -- the only member this comment named --
    # did not reach the range at all. The third conjunct excludes by KIND
    # instead, asking the question an average has and a total does not: is this
    # row's day key a real calendar day? A key date() produced round-trips
    # through it; a raw-prefix key survives only when that prefix is itself a
    # real day, so `2026-08-10x...` still counts, on 2026-08-10, rather than
    # being dropped from a day it plainly belongs to.
    #
    # Written on the day key rather than copied from the gate, so it cannot
    # drift from whatever `local_day_expr` comes to gate on. The two forms
    # agreed on every one of 5,911 adversarial values (2026-08-16), and the
    # conjunct excluded none of the 155,960 well-formed ones that docstring
    # already rests on. `_cmd_week` is deliberately left as it is: there a
    # gated-out row is added to a TOTAL, which is not wrong, where here it
    # MOVES a mean.
    #
    # One `date.today()`, not two: the boundary and the window have to come
    # from the same reading of the clock. This also retires the split brain
    # between Python's `date.today() - 30` and SQLite's
    # `date('now','localtime','-30 days')`, which were two independent
    # computations of one boundary.
    today_local = date.today()
    # Both SQL bounds are inclusive, so today through 29 days ago is exactly
    # the 30 local calendar days promised by the report label.
    first_average_day = (today_local - timedelta(days=29)).isoformat()
    last_local_day = today_local.isoformat()
    avg_lo, avg_hi = utc_window(first_average_day, last_local_day)
    daily_avg = conn.execute(f"""
        SELECT
            AVG(daily_inp) as avg_inp,
            AVG(daily_out) as avg_out
        FROM (
            SELECT
                {LOCAL_DAY} as day,
                SUM(input_tokens) as daily_inp,
                SUM(output_tokens) as daily_out
            FROM turns
            WHERE timestamp >= ? AND timestamp < ?
              AND {LOCAL_DAY} BETWEEN ? AND ?
              AND date({LOCAL_DAY}) = {LOCAL_DAY}{wscope}
            GROUP BY day
        )
    """, (avg_lo, avg_hi, first_average_day, last_local_day, *wparams)).fetchone()

    # Build total cost across all models
    total_cost = sum(
        _report_cost(r)
        for r in by_model
    )

    print()
    hr("=")
    print(f"  {_stats_title(source)}")
    hr("=")
    if source is None and len(sources_present(conn)) > 1:
        print(BLEND_NOTE)
        hr()

    # Wrapped like every other transcript-derived value this file prints. The
    # local-day expression above falls back to a raw `substr` of the stored
    # timestamp for text SQLite cannot parse, so both slots still carry
    # whatever a transcript put there — `\x1b[2J\x1b[H` fits in ten characters
    # and cleared the reader's screen. Escaped AFTER the slice, because
    # `terminal_safe` never emits a raw ESC and so cannot be cut in half by it.
    first_date = terminal_safe((session_info["first"] or "")[:10])
    last_date = terminal_safe((session_info["last"] or "")[:10])
    print(f"  Period:           {first_date} to {last_date}")
    print(f"  Total sessions:   {session_info['sessions'] or 0:,}")
    print(f"  Total turns:      {fmt(totals['turns'] or 0)}")
    print(f"  Subagent turns:   {fmt(subagent['turns'] or 0)}")
    print()
    print(f"  Input tokens:     {fmt(totals['inp'] or 0):<12}  (raw prompt tokens)")
    print(f"  Output tokens:    {fmt(totals['out'] or 0):<12}  (generated tokens)")
    print(f"  Cache read:       {fmt(totals['cr'] or 0):<12}  (90% cheaper than input)")
    print(f"  Cache creation:   {fmt(totals['cc'] or 0):<12}  (25% premium on input, 100% for 1-hour writes)")
    print(f"  Subagent tokens:  {fmt(subagent['tokens'] or 0):<12}  (included in totals)")
    print()
    print(f"  Est. total cost:  ${total_cost:.4f}")
    hr()

    print("  By Model:")
    for r in by_model:
        cost = _report_cost(r)
        print(f"    {terminal_safe(r['model']):<30}  sessions={r['sessions']:<4}  turns={fmt(r['turns'] or 0):<6}  "
              f"in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print("  Top Projects:")
    for r in top_projects:
        print(f"    {terminal_safe(r['project_name'] or 'unknown'):<40}  sessions={r['sessions']:<3}  "
              f"turns={fmt(r['turns'] or 0):<6}  tokens={fmt((r['inp'] or 0)+(r['out'] or 0))}")

    # `is not None`, not truthiness: AVG() is NULL only when the window holds no
    # days at all, while a day whose uncached input happens to be zero averages
    # to 0.0 — also falsy, and suppressing the section on that hid a perfectly
    # good output average along with it.
    if daily_avg["avg_inp"] is not None:
        hr()
        print("  Daily Average (last 30 days):")
        print(f"    Input:   {fmt(int(daily_avg['avg_inp'] or 0))}")
        print(f"    Output:  {fmt(int(daily_avg['avg_out'] or 0))}")

    hr("=")
    print()
