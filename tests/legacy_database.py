"""Independent fixtures for the complete schemas emitted by released builds.

Production keeps its own fingerprints in :mod:`claude_usage.db`.  Tests do not
derive these snapshots from that implementation: an accidentally weakened or
edited production fingerprint must disagree with this release census and fail.
Other upgrade tests use this module instead of inventing partial ``turns``
tables that no released claude-usage database ever contained.
"""


LEGACY_BASE_SCHEMA = {
    "sessions": (
        "first_timestamp", "git_branch", "last_timestamp", "model",
        "project_name", "session_id", "total_cache_creation",
        "total_cache_read", "total_input_tokens", "total_output_tokens",
        "turn_count",
    ),
    "turns": (
        "cache_creation_tokens", "cache_read_tokens", "cwd", "id",
        "input_tokens", "message_id", "model", "output_tokens",
        "session_id", "timestamp", "tool_name",
    ),
    "processed_files": ("lines", "mtime", "path"),
}

LEGACY_WITH_AGENTS_SCHEMA = {
    **LEGACY_BASE_SCHEMA,
    "turns": (
        "agent_id", "cache_creation_tokens", "cache_read_tokens", "cwd",
        "id", "input_tokens", "is_subagent", "message_id", "model",
        "output_tokens", "session_id", "timestamp", "tool_name",
    ),
    "agents": (
        "agent_id", "agent_type", "completed_at", "dispatched_in_session",
        "status", "tool_use_count", "total_duration_ms", "total_tokens",
    ),
}

LEGACY_WITH_TOPICS_SCHEMA = {
    **LEGACY_WITH_AGENTS_SCHEMA,
    "sessions": (
        "first_timestamp", "git_branch", "last_timestamp", "model",
        "project_name", "session_id", "topic", "total_cache_creation",
        "total_cache_read", "total_input_tokens", "total_output_tokens",
        "turn_count",
    ),
    "schema_meta": ("key", "value"),
}

LEGACY_WITH_LIMITS_SCHEMA = {
    **LEGACY_WITH_TOPICS_SCHEMA,
    "sessions": (
        "first_timestamp", "git_branch", "last_timestamp", "model",
        "project_name", "session_id", "topic", "total_cache_creation",
        "total_cache_creation_1h", "total_cache_read", "total_input_tokens",
        "total_output_tokens", "turn_count",
    ),
    "turns": (
        "agent_id", "cache_creation_1h_tokens", "cache_creation_tokens",
        "cache_read_tokens", "cwd", "id", "input_tokens", "is_subagent",
        "message_id", "model", "output_tokens", "session_id", "timestamp",
        "tool_name",
    ),
    "limit_events": (
        "event_uuid", "kind", "message", "reset_hint", "reset_zone",
        "session_id", "status", "timestamp",
    ),
    "usage_limits_snapshots": (
        "fetched_at_ms", "grp", "is_active", "kind", "observed_at",
        "percent", "resets_at", "resets_key", "scope", "severity",
    ),
}

RELEASED_SCHEMAS = {
    **{tag: LEGACY_BASE_SCHEMA for tag in (
        "v1.0.0", "v1.1.0", "v1.1.1", "v1.1.2", "v1.2.0", "v1.2.1",
        "v1.2.2", "v1.2.3", "v1.2.4", "v1.2.5", "v1.2.6", "v1.3.0",
        "v1.4.0",
    )},
    **{tag: LEGACY_WITH_AGENTS_SCHEMA for tag in (
        "v1.5.0", "v1.5.1", "v1.5.2", "v1.5.3",
    )},
    **{tag: LEGACY_WITH_TOPICS_SCHEMA for tag in ("v1.5.4", "v1.5.5")},
    **{tag: LEGACY_WITH_LIMITS_SCHEMA for tag in ("v1.6.0", "v1.6.1")},
}


def create_schema(conn, schema=LEGACY_BASE_SCHEMA):
    """Create one complete recorded release shape; types do not identify it."""
    for table, columns in schema.items():
        declared = ", ".join(f'"{column}" TEXT' for column in columns)
        conn.execute(f'CREATE TABLE "{table}" ({declared})')
    conn.commit()
