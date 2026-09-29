"""Reading Claude Code's own config for plan and quota facts.

`~/.claude.json` is not a transcript. It is a client-side cache Claude Code
rewrites in place, and it holds the account's *identity* right next to its quota
state. This module reads exactly the quota fields and drops everything else
before any of it can reach the database or the browser: the email address,
account/organization UUIDs, organization and display names, machine and user
ids, and the `projects` map (whose keys are absolute paths containing the
username) are never returned by anything here.

Two things it is careful about, both learned from the real file:

* **It is a cache, not a live reading.** `fetchedAtMs` advances every few tens of
  minutes, not per request, and the file is rewritten far more often than the
  cache inside it changes. Every projection carries `age_seconds` so the UI can
  say "as of", and can never imply it queried anything.
* **The cache outlives its own window.** A five-hour window that has already
  reset still reads `percent: 100, severity: "critical"` until the next refresh.
  `expired` is computed here, against the clock, so the page shows "window
  ended" instead of telling the user they are throttled when they are not.

No entry point here raises: a missing, unreadable, oversized or malformed
config is simply "unavailable", and a field whose *type or magnitude* is not
one this file can carry reads as an absent one rather than reaching the caller.
That is a promise about a cache nothing here writes, so it is kept by
validating every value on the way through, not by trusting the schema to hold
still. Magnitude belongs in that sentence because `json` parses an integer at
any width: a `fetchedAtMs` of 10**400 has exactly the type the field is
supposed to have and still raised on the first division that touched it.
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .safefile import read_bounded_regular_file
from .safetext import _bounded_text
from .timestamps import parse_instant as _parse_instant

# The file carries a `projects` map that grows with every directory Claude Code
# has been run in, so it is much larger than its quota block. This is a sanity
# bound against reading something pathological, not a real expectation.
MAX_CONFIG_BYTES = 32 * 1024 * 1024

# Plenty for a plan name, a limit kind or a model display name; anything longer
# is not a value this module is meant to be carrying.
MAX_FIELD_LENGTH = 64

# The widest whole number this module will carry out of the cache. The bound is
# SQLite's INTEGER range, because `fetched_at_ms` is stored in one, and it sits
# about five million times above a real epoch-milliseconds reading — so it
# rejects the absurd without touching anything genuine. Some bound is needed:
# `json` parses integers at arbitrary width and a float cannot represent one,
# so `fetchedAtMs / 1000` raised on the way to `age_seconds`.
MAX_WHOLE_NUMBER = 2 ** 63 - 1

# Settings keys that mean "this install authenticates with an API key", checked
# for presence only — the values are credentials and are never read.
_API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _config_dir_override(env=None):
    """`CLAUDE_CONFIG_DIR` as a Path, or None when it is not set.

    Only the OVERRIDE is shared between this module's two readers, and that is
    deliberate: a `config_dir()` supplying the defaults as well cannot serve
    both, because by default `.claude.json` sits BESIDE `~/.claude` while
    `settings.json` sits inside it, so one of the two would resolve to
    `~/.claude/.claude.json`, which nobody has. Sharing the override is the half
    that was missing — `config_path` honoured the variable, `detect_auth_mode`
    did not, and the two therefore described two different installs.

    Reads a mapping rather than `os.environ` directly so `detect_auth_mode`'s own
    `env` argument reaches it. `isinstance` because a caller assembling that
    mapping by hand is exactly who the promise at the top of this file is for,
    and `Path(5)` raises.
    """
    environ = os.environ if env is None else env
    directory = environ.get("CLAUDE_CONFIG_DIR")
    if not isinstance(directory, str) or not directory:
        return None
    return Path(directory)


def config_path():
    """Where Claude Code keeps its config, honouring the usual overrides."""
    override = os.environ.get("CLAUDE_USAGE_CONFIG")
    if override:
        return Path(override)
    config_dir = _config_dir_override()
    if config_dir:
        return config_dir / ".claude.json"
    return Path.home() / ".claude.json"


def _read_json_object(path):
    """Parse a JSON object from `path`, or return None. Never raises.

    Deliberately *less* strict than db.secure_db_permissions: this is a read of a
    file in the user's own home directory, and plenty of people symlink their
    dotfiles out of a repository, so refusing to follow a symlink here would
    break them for no security gain (we only ever read, and only ever emit the
    handful of non-identifying fields below). It still refuses anything that is
    not a regular file the current user owns.
    """
    raw = read_bounded_regular_file(
        path, MAX_CONFIG_BYTES, owner_only=True
    )
    if raw is None:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        # A torn write caught mid-rename lands here; the next scan reads a whole
        # file, so there is nothing to recover.
        return None
    return parsed if isinstance(parsed, dict) else None


def read_config(path=None):
    """Return Claude Code's parsed config, or None when it is unusable."""
    return _read_json_object(path if path is not None else config_path())


def _text(value, limit=MAX_FIELD_LENGTH):
    return _bounded_text(value, limit) if isinstance(value, str) else ""


def _percent(value):
    """A 0-100 utilization figure, or None when there isn't a credible one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return max(0, min(100, int(value)))


def _whole(value, default=0):
    """A whole number as reported, or `default` when there isn't one.

    `_percent`'s rejection rules without its 0-100 clamp, for the integer
    fields that are counts rather than percentages. Anything this cannot make
    sense of — a bool, a string, a dict, NaN, an infinity, a number too wide to
    do arithmetic on — reads as `default`, which is the value an absent field
    already projected to. Deliberately not `int(value or default)`: `int()`
    raises on most of that list, and it is the raise, not the zero, that broke
    the promise at the top of this file.

    The width bound is the same rejection as the infinity above it, one
    operation later. An infinity is refused because `int()` raises on it; an
    integer wider than a float is refused because the *next* thing anything
    does with it raises — `fetchedAtMs / 1000` in `limits_projection`. It
    rejects rather than clamps, so a real reading is never quietly corrupted
    into a wrong one.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return default
    if not -MAX_WHOLE_NUMBER <= value <= MAX_WHOLE_NUMBER:
        return default
    return int(value)


def _settings_declares_api_key(path):
    settings = _read_json_object(path)
    if not settings:
        return False
    if _text(settings.get("apiKeyHelper"), 512):
        return True
    env = settings.get("env")
    if isinstance(env, dict):
        # Presence only. The values are credentials and are never read.
        if any(env.get(name) for name in _API_KEY_ENV_VARS):
            return True
        if env.get("ANTHROPIC_BASE_URL"):
            return True
    return False


def detect_auth_mode(config, env=None, settings_paths=None):
    """'subscription', 'api_key', or 'unknown'.

    Order matters. An explicitly exported key wins over a stale `oauthAccount`
    left behind by an earlier login, because that is the credential the requests
    are actually going out under — and an API-key user has no plan limits to
    show, only per-token billing.
    """
    environ = os.environ if env is None else env
    if any(environ.get(name) for name in _API_KEY_ENV_VARS):
        return "api_key"

    if settings_paths is None:
        # The same relocation `config_path` honours, and for the same reason:
        # `CLAUDE_CONFIG_DIR` moves the whole of `~/.claude` (scripts/run-docker.sh
        # reads it as exactly that), so `settings.json` moves with `.claude.json`.
        # Asking the unrelocated directory whether the relocated config's account
        # is still the credential in use answers about a different install, and it
        # was wrong in both directions: a key declared in the relocated settings
        # left an api-key install reading a stale subscription quota, and a
        # leftover `~/.claude/settings.json` hid a relocated subscriber's panel.
        home = _config_dir_override(environ) or Path.home() / ".claude"
        settings_paths = (home / "settings.json", home / "settings.local.json")
    for candidate in settings_paths:
        if _settings_declares_api_key(candidate):
            return "api_key"

    # `isinstance`, not `config or {}`: a truthy non-dict — a JSON document that
    # is a bare number or string — went straight to `.get`. `limits_projection`
    # already refuses the same thing this way.
    account = config.get("oauthAccount") if isinstance(config, dict) else None
    if isinstance(account, dict):
        billing = _text(account.get("billingType"))
        org_type = _text(account.get("organizationType"))
        # Treat the organizationType family as OPEN: claude_max and claude_pro
        # are what exist today, but claude_team / claude_enterprise / whatever
        # ships next are all subscriptions. An unrecognised claude_* value should
        # still show the panel rather than silently hide it.
        if billing.startswith("stripe_") or org_type.startswith("claude_"):
            return "subscription"
    return "unknown"


# How long each kind of window runs. Used only to work out where the CURRENT
# window starts once the cached one has expired — Claude Code's cache is
# refreshed every few tens of minutes, so after a reset there is a stretch with
# no figure at all for the window you are actually in.
_WINDOW_HOURS = {"session": 5.0, "five_hour": 5.0, "weekly": 24.0 * 7}


def _window_length_hours(kind, group):
    # `current_window_bounds` is public and takes the window dict as its caller
    # built it. Inside this file both of these have been through `_text`, but a
    # caller assembling one by hand is exactly who the promise at the top is
    # for, and a non-string `kind` used to reach `in` (unhashable) or
    # `.startswith` (no such attribute).
    for key in (kind, group):
        if not isinstance(key, str):
            continue
        if key in _WINDOW_HOURS:
            return _WINDOW_HOURS[key]
        if key.startswith("weekly"):
            return _WINDOW_HOURS["weekly"]
    return None


def current_window_bounds(window, now):
    """Where the window you are in NOW starts and ends, or (None, None).

    Only meaningful once the cached window has expired. The cache keeps
    reporting the window that has already rolled over — 100%, "critical" and
    all — until Claude Code next refreshes it, which can be the better part of
    an hour. Rendering that verbatim was the old bug; rendering nothing but
    "window ended" is the opposite one, because the reset time it already
    carries says exactly when the current window began.

    Rolls forward in whole windows rather than assuming exactly one has passed,
    so a machine left idle overnight lands in the right one.
    """
    length = _window_length_hours(window.get("kind"), window.get("group"))
    reset = _parse_instant(window.get("resets_at"))
    if length is None or reset is None:
        return None, None
    from datetime import timedelta
    step = timedelta(hours=length)
    # A pathological resets_at must not spin: a week of 5-hour windows is 34.
    # It must not overflow either, and the first addition is the one that does
    # it — a reset in year 9999 is past datetime.MAX before the loop is
    # entered, so bounding the loop alone was never enough.
    try:
        start, end = reset, reset + step
        for _ in range(10000):
            if end > now:
                break
            start, end = end, end + step
        else:
            return None, None
    except (OverflowError, OSError):
        return None, None
    return start, end


def _window_from_limit(entry, now):
    """One row of `cachedUsageUtilization.utilization.limits[]`, projected."""
    if not isinstance(entry, dict):
        return None
    scope_name = ""
    scope_identity = ""
    scope_suffix = ""
    scope = entry.get("scope")
    if isinstance(scope, dict):
        surface = _text(scope.get("surface"))
        model_id = ""
        model = scope.get("model")
        if isinstance(model, dict):
            model_id = _text(model.get("id"))
            scope_name = _text(model.get("display_name"))
        # A surface is quota identity when no human model name exists. Keep the
        # readable concept, not the raw field name or model id.
        if not scope_name and surface:
            scope_name = surface.replace("_", " ").title()
        elif scope_name and surface:
            scope_suffix = surface.replace("_", " ").title()
        if not scope_name and model_id:
            digest = hashlib.sha256(model_id.encode("utf-8")).hexdigest()[:10]
            scope_name = f"Model {digest}"
        identity_parts = [surface, model_id]
        if any(identity_parts):
            scope_identity = hashlib.sha256(
                "\0".join(identity_parts).encode("utf-8")).hexdigest()[:12]
    resets_at = _text(entry.get("resets_at"), 64)
    reset_instant = _parse_instant(resets_at)
    projected = {
        "kind": _text(entry.get("kind")),
        "group": _text(entry.get("group")),
        "percent": _percent(entry.get("percent")),
        "severity": _text(entry.get("severity")),
        "resets_at": resets_at,
        "scope": scope_name,
        # Removed before the public projection is returned unless two windows
        # need it to avoid sharing one threshold key. It is a one-way digest,
        # never the upstream model id or surface value.
        "_scope_identity": scope_identity,
        "_scope_suffix": scope_suffix,
        "is_active": bool(entry.get("is_active")),
        # Computed here rather than in the browser: the cache demonstrably
        # survives its own reset, and a stale 100% rendered verbatim tells the
        # user they are blocked when the window has already rolled over.
        "expired": bool(reset_instant and reset_instant <= now),
    }
    if projected["expired"]:
        # The cache has no figure for the window you are actually in, but its
        # own reset time says when that window began. Saying so beats saying
        # nothing until Claude Code gets around to refreshing.
        start, end = current_window_bounds(projected, now)
        if start is not None:
            projected["window_start"] = start.isoformat()
            projected["window_end"] = end.isoformat()
    return projected


def limits_projection(config, now=None):
    """The minimal, identity-free view of `cachedUsageUtilization`.

    Returns `{"available": False, "reason": ...}` when there is nothing to show.
    """
    now = now or datetime.now(timezone.utc)
    if not isinstance(config, dict):
        return {"available": False, "reason": "no_config"}
    cached = config.get("cachedUsageUtilization")
    if not isinstance(cached, dict):
        return {"available": False, "reason": "no_cache"}
    utilization = cached.get("utilization")
    if not isinstance(utilization, dict):
        return {"available": False, "reason": "no_cache"}

    windows = []
    raw_limits = utilization.get("limits")
    if isinstance(raw_limits, list):
        for entry in raw_limits:
            window = _window_from_limit(entry, now)
            if window is not None:
                windows.append(window)
    if not windows:
        # limits[] is the canonical list and duplicates five_hour exactly when
        # both are present, so five_hour is only a fallback for a build that
        # doesn't emit limits[] at all.
        five_hour = utilization.get("five_hour")
        if isinstance(five_hour, dict):
            window = _window_from_limit({
                "kind": "five_hour",
                "group": "session",
                "percent": five_hour.get("utilization"),
                "severity": "",
                "resets_at": five_hour.get("resets_at"),
                "is_active": True,
            }, now)
            if window is not None:
                windows.append(window)
    if not windows:
        return {"available": False, "reason": "no_cache"}

    # Usually the display name alone is enough and existing threshold keys stay
    # stable. Only colliding visible identities gain a hashed discriminator;
    # this handles two surfaces or model ids with the same display name without
    # exposing either identifier to the browser.
    visible = {}
    for window in windows:
        key = (window.get("kind") or window.get("group") or "",
               window.get("scope") or "")
        visible.setdefault(key, []).append(window)
    for same_name in visible.values():
        identities = {w.get("_scope_identity") for w in same_name
                      if w.get("_scope_identity")}
        if len(same_name) > 1 and len(identities) > 1:
            for window in same_name:
                if window.get("_scope_identity"):
                    window["scope_discriminator"] = window["_scope_identity"]
                    suffix = (window.get("_scope_suffix")
                              or window["_scope_identity"][:6])
                    window["scope"] = f"{window['scope']} · {suffix}"
    for window in windows:
        window.pop("_scope_identity", None)
        window.pop("_scope_suffix", None)

    account = config.get("oauthAccount")
    account = account if isinstance(account, dict) else {}

    fetched_at_ms = max(0, _whole(cached.get("fetchedAtMs")))
    age_seconds = None
    if fetched_at_ms:
        age_seconds = max(0, int(now.timestamp() - fetched_at_ms / 1000))

    extra = utilization.get("extra_usage")
    extra = extra if isinstance(extra, dict) else {}
    spend = utilization.get("spend")
    spend = spend if isinstance(spend, dict) else {}
    spend_used = spend.get("used")
    spend_used = spend_used if isinstance(spend_used, dict) else {}

    return {
        "available": True,
        # Plan facts only. Not the organization's name, uuid, or the user's.
        "plan_type": _text(account.get("organizationType")),
        "rate_limit_tier": _text(account.get("organizationRateLimitTier")),
        "fetched_at_ms": fetched_at_ms,
        "age_seconds": age_seconds,
        "windows": windows,
        "extra_usage": {
            "is_enabled": bool(extra.get("is_enabled")),
            "utilization": _percent(extra.get("utilization")),
            "spend_limit_reached": bool(extra.get("spend_limit_reached")),
        },
        "spend": {
            # Minor units as reported, so no float rounding happens on the way
            # through. `disclaimer` is dropped: it is marketing copy carrying an
            # external URL, and this page never renders one it did not author.
            "used_minor": max(0, _whole(spend_used.get("amount_minor"))),
            "currency": _text(spend_used.get("currency"), 8),
            "exponent": max(0, min(6, _whole(spend_used.get("exponent")))),
            "percent": _percent(spend.get("percent")),
            "enabled": bool(spend.get("enabled")),
        },
    }


def current_limits_from_config(config, env=None, now=None):
    """Apply the shared auth-mode gate to an already-read config. Never raises.

    The optional live-limits path replaces only the quota block in a copied
    config. Both HTTP surfaces still need this gate afterward so an API-key
    install cannot accidentally display stale subscription windows.
    """
    try:
        mode = detect_auth_mode(config, env)
        if mode != "subscription":
            return {"available": False, "reason": mode}
        return limits_projection(config, now=now)
    except Exception:
        return {"available": False, "reason": "unavailable"}


def current_limits(env=None, now=None):
    """The plan-limits payload the dashboard shows, or why there isn't one.

    The single definition of "read the config, decide whether this install even
    has plan windows, project them" — `/api/data` embeds the result and
    `/api/limits` returns it on its own, and the two must not be able to
    disagree about what an API-key install looks like.

    Never raises, like everything else here: an unreadable or malformed config
    is `available: False`, which is exactly what Docker sees (run-docker.sh
    mounts ~/.claude/projects, never ~/.claude.json).
    """
    try:
        config = read_config()
        return current_limits_from_config(config, env=env, now=now)
    except Exception:
        return {"available": False, "reason": "unavailable"}


def _reset_key(resets_at):
    """A stable identity for one reset window.

    Sub-second jitter on either side of a minute boundary must not make
    repeated observations of the same window look like new windows.
    Round to the nearest minute.
    """
    instant = _parse_instant(resets_at)
    if instant is None:
        return _text(resets_at, 32)
    rounded = instant.replace(second=0, microsecond=0)
    if instant.second >= 30:
        try:
            rounded = rounded.fromtimestamp(rounded.timestamp() + 60,
                                            tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            # The last minute of year 9999 has nowhere to round up to. The
            # un-rounded minute is still a stable key for that window, which is
            # all this is for.
            pass
    return rounded.strftime("%Y-%m-%dT%H:%M")


def snapshot_rows(config, observed_at, now=None):
    """Rows to persist for one observation of the plan's limit windows."""
    projection = limits_projection(config, now=now)
    if not projection.get("available"):
        return []
    fetched_at_ms = projection["fetched_at_ms"]
    rows = []
    for window in projection["windows"]:
        percent = window["percent"]
        rows.append((
            window["kind"],
            window["group"],
            window["scope"],
            _reset_key(window["resets_at"]),
            -1 if percent is None else percent,
            window["severity"],
            1 if window["is_active"] else 0,
            window["resets_at"],
            fetched_at_ms,
            observed_at,
        ))
    return rows
