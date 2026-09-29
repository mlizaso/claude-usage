"""Pricing and cost arithmetic for Claude and Codex models.

The single source of truth for what a turn costs. Kept apart from the CLI that
prints it and the scanner that records it, because this is the one piece of
domain logic the dashboard reimplements in its assembled JavaScript.
`scripts/generate-pricing-assets.py` renders the browser's rates, dated
policies, long-context contract, provenance metadata, and documentation tables
from the data below; parity tests keep the remaining arithmetic aligned.

Resolution is three-tier (exact id, then `startswith` for date-suffixed ids like
claude-opus-4-7-20260215, then a family substring). A model matching no tier
returns None and is billed at nothing rather than guessed at, so local and
third-party models (gemma, glm, ...) are never charged at Anthropic rates.

Anthropic cache writes have separate five-minute and one-hour TTL rates.
Preserve cache_write and cache_write_1h independently. OpenAI uses one write
category, so both schema projections share its rate.
"""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
import re

from .safefile import read_bounded_regular_file
from .timestamps import parse_instant

PRICING = {
    # Standard API rates verified 2026-09-29 against the provider price lists:
    # https://platform.claude.com/docs/en/about-claude/pricing
    # https://developers.openai.com/api/docs/pricing
    # New model IDs retain their own rates so older usage is not repriced.
    "claude-fable-5-1":  {"input": 10.00, "output": 50.00, "cache_read": 0.25, "cache_write": 12.50, "cache_write_1h": 20.00},
    "claude-mythos-5-1": {"input": 10.00, "output": 50.00, "cache_read": 0.25, "cache_write": 12.50, "cache_write_1h": 20.00},
    "claude-fable-5":    {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50, "cache_write_1h": 20.00},
    "claude-mythos-5":   {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50, "cache_write_1h": 20.00},
    # Keep an explicit model entry so a future fallback-rate change cannot
    # silently reprice a model whose bundled estimate should stay fixed.
    "claude-opus-5-5":   {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00, "cache_write_1h": 8.00},
    "claude-opus-5":     {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25, "cache_write_1h": 10.00},
    "claude-opus-4-8":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25, "cache_write_1h": 10.00},
    "claude-opus-4-7":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25, "cache_write_1h": 10.00},
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25, "cache_write_1h": 10.00},
    "claude-opus-4-5":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25, "cache_write_1h": 10.00},
    # Keep each generation explicit instead of relying on the family fallback.
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50, "cache_write_1h": 4.00},
    "claude-sonnet-5":   {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50, "cache_write_1h": 4.00},
    "claude-sonnet-4-7": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75, "cache_write_1h": 6.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75, "cache_write_1h": 6.00},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75, "cache_write_1h": 6.00},
    "claude-haiku-4-7":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25, "cache_write_1h": 2.00},
    "claude-haiku-4-6":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25, "cache_write_1h": 2.00},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25, "cache_write_1h": 2.00},

    # Codex/OpenAI rates are maintained from vendor pricing sources. Cached
    # input and cache writes have separate rates; OpenAI uses one write
    # category for both shared-schema write fields. RATE_POLICIES preserves
    # dated transitions. Sol uses the verification date as its estimate
    # boundary; Astra was added from its model pricing page. See the generated
    # README tables and dated policy entries.
    "gpt-6-astra":       {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50, "cache_write_1h": 12.50},
    "gpt-6-sol":         {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50, "cache_write_1h": 2.50},
    "gpt-6-luna":        {"input": 0.10, "output":  0.50, "cache_read": 0.01, "cache_write": 0.125, "cache_write_1h": 0.125},
    "gpt-5.6-sol":       {"input": 4.00, "output": 20.00, "cache_read": 0.40,  "cache_write": 5.00,   "cache_write_1h": 5.00},
    "gpt-5.6-terra":     {"input": 2.00, "output": 12.00, "cache_read": 0.20,  "cache_write": 2.50,   "cache_write_1h": 2.50},
    "gpt-5.6-luna":      {"input": 0.20, "output":  1.20, "cache_read": 0.02,  "cache_write": 0.25,   "cache_write_1h": 0.25},
    "gpt-5.5":           {"input": 5.00, "output": 30.00, "cache_read": 0.50,  "cache_write": 5.00,   "cache_write_1h": 5.00},
    "gpt-5.4":           {"input": 2.50, "output": 15.00, "cache_read": 0.25,  "cache_write": 2.50,   "cache_write_1h": 2.50},
    "gpt-5.4-mini":      {"input": 0.75, "output":  4.50, "cache_read": 0.075, "cache_write": 0.75,   "cache_write_1h": 0.75},
    "gpt-5.4-nano":      {"input": 0.20, "output":  1.25, "cache_read": 0.02,  "cache_write": 0.20,   "cache_write_1h": 0.20},
    "gpt-5.3-codex":     {"input": 1.75, "output": 14.00, "cache_read": 0.175, "cache_write": 1.75,   "cache_write_1h": 1.75},
    # NOT on the price list. Both are Codex-internal ids that appear in
    # ~/.codex/models_cache.json and in the transcripts but nowhere in OpenAI's
    # published pricing, so these two — and only these two — are estimates,
    # placed at the nearest published Codex tier. They are flagged as such in the
    # UI; everything above is a published rate and is not.
    "gpt-5.3-codex-spark": {"input": 1.75, "output": 14.00, "cache_read": 0.175, "cache_write": 1.75, "cache_write_1h": 1.75},
    "codex-auto-review": {"input": 1.75, "output": 14.00, "cache_read": 0.175, "cache_write": 1.75, "cache_write_1h": 1.75},
}

# Keep transitions as data so historical reports can select the rate effective
# when a turn ran. A policy may use ``start``/``before`` for a verified current
# change, or ``until``/``after`` when a vendor publishes a bounded transition.
# Do not invent an end date when the vendor only promises availability through
# a checked date.
RATE_POLICIES = {
    "gpt-5.6-sol": {
        # This is the first date we verified the promotion, not a claim about
        # when OpenAI launched it. Rows before it retain the prior estimate.
        "start": "2026-08-22",
        "before": {"input": 5.00, "output": 30.00, "cache_read": 0.50,
                   "cache_write": 6.25, "cache_write_1h": 6.25},
        "rates": {"input": 4.00, "output": 20.00, "cache_read": 0.40,
                   "cache_write": 5.00, "cache_write_1h": 5.00},
    },
    "gpt-5.6-terra": {
        "start": "2026-07-30",
        "before": {"input": 2.50, "output": 15.00, "cache_read": 0.25,
                   "cache_write": 3.125, "cache_write_1h": 3.125},
        "rates": {"input": 2.00, "output": 12.00, "cache_read": 0.20,
                  "cache_write": 2.50, "cache_write_1h": 2.50},
    },
    "gpt-5.6-luna": {
        "start": "2026-07-30",
        "before": {"input": 1.00, "output": 6.00, "cache_read": 0.10,
                   "cache_write": 1.25, "cache_write_1h": 1.25},
        "rates": {"input": 0.20, "output": 1.20, "cache_read": 0.02,
                  "cache_write": 0.25, "cache_write_1h": 0.25},
    },
}

# OpenAI's long-context surcharge applies to the complete request when its
# prompt is above 272K tokens. Keep the family list exact: gpt-5.4-mini and
# gpt-5.4-nano are separate products and must not inherit gpt-5.4's tier.
LONG_CONTEXT_THRESHOLD = 272_000
LONG_CONTEXT_MODELS = frozenset({
    "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4",
})
_LONG_CONTEXT_SNAPSHOT = re.compile(
    r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$"
)

# A copy of the initial built-in table lets overrides remain authoritative even
# when a caller asks for a dated rate. The public table remains mutable for the
# established override API.
_CANONICAL_CURRENT_RATES = {model: dict(rates) for model, rates in PRICING.items()}
# ``PRICING`` is a public mutable table, so equal numeric values do not prove
# that a user did not override a policy model. Keep object identity for the
# supported loader path: test fixtures and callers that restore the original
# table objects naturally stop matching a stale marker.
_OVERRIDE_RATE_OBJECTS = {}

# Models whose rates are estimated rather than published. The dashboard labels
# any figure derived from these, so an estimate is never shown as a fact.
ESTIMATED_RATE_MODELS = set({
    # Only the two ids OpenAI does not publish a rate for. Everything else in
    # this table comes from a vendor price list.
    "gpt-5.3-codex-spark", "codex-auto-review",
})
RATE_OVERRIDE_ENV = "CLAUDE_USAGE_RATES"
RATE_FIELDS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")


def load_rate_overrides(path=None, env=None):
    """Replace or add per-model rates from a JSON file. Never raises.

    Replace built-in rates with your own contract prices or provide rates for
    additional model IDs. Point `CLAUDE_USAGE_RATES` at a file like

        {"gpt-5.6-sol": {"input": 1.25, "output": 10.0, "cache_read": 0.125}}

    and those models bill at your rates instead, with the "estimated" label
    dropped — you supplied them, so they are not our guess any more.

    Missing fields keep the built-in value, so a file that only corrects `output`
    does exactly that — "built-in" meaning whatever `get_pricing` resolves the id
    to, not only an exact table key, so correcting one field of a dated id leaves
    its own tier's other four alone. A model with no rates at any tier starts
    from zero, because the module's rule is that an unrecognised model is billed
    at nothing rather than guessed at; supplying one field must not promote it to
    some other vendor's price list.

    Anything malformed is ignored rather than half-applied: a partially-read
    price table is a silently wrong bill.
    """
    import json as _json
    import os as _os
    import sys as _sys

    environ = _os.environ if env is None else env
    source = path if path is not None else environ.get(RATE_OVERRIDE_ENV, "")
    if not source:
        return set()
    raw = read_bounded_regular_file(source, 1 << 20)
    if raw is None:
        return set()
    try:
        parsed = _json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return set()
    if not isinstance(parsed, dict):
        return set()

    applied = set()
    for model, rates in parsed.items():
        if not isinstance(model, str) or not isinstance(rates, dict):
            continue
        # Through the tier chain, not a bare key lookup, and copied before it is
        # mutated (several ids share one dict). `PRICING.get(model) or
        # PRICING["gpt-5.6-sol"]` meant a lone `{"my-local-llama": {"input": 0}}`
        # billed that model's output at $30.00/M, and correcting one field of
        # `claude-opus-4-8-20260215` silently raised its output from $25 to $30.
        resolved = get_pricing(model)
        base = dict(resolved) if resolved else {f: 0.0 for f in RATE_FIELDS}
        changed = False
        for field in RATE_FIELDS:
            value = rates.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if value < 0 or value != value or value in (float("inf"), float("-inf")):
                continue
            # The same rejection as the infinity above it, one operation later:
            # a JSON integer has no width, and `float()` on one wider than a
            # double raises. That broke the promise on the first line of this
            # docstring — `cli.main` calls the loader unguarded, so every
            # command died on the traceback — and it cut the file at an
            # arbitrary point, leaving the models the loop had already reached
            # overridden while the ones behind it silently kept the built-ins.
            # account._whole refuses an over-wide integer for the same reason.
            if abs(value) > _sys.float_info.max:
                continue
            base[field] = float(value)
            changed = True
        if not changed:
            continue
        PRICING[model] = base
        _OVERRIDE_RATE_OBJECTS[model] = base
        # A rate the user supplied is theirs, not our estimate.
        ESTIMATED_RATE_MODELS.discard(model)
        applied.add(model)
    return applied


def resolved_rates(models):
    """The current rates for `models`, for a client that bills from its own copy.

    The dashboard computes every figure in the browser from a second copy of
    this table, so a rate the user supplied has to travel: while it stopped
    here, `cli.py stats` printed $1.0000 and the page's Est. Cost tile showed
    $5.0000 for the identical turns — under a footer telling the reader to set
    the variable. Hand this the set `load_rate_overrides` returned and it yields
    exactly what the page's `applyRateOverrides` consumes.

    Fully resolved five-field dicts rather than the user's own file,
    deliberately: "a missing field keeps the built-in value" is decided above,
    and a second implementation of that rule in JavaScript is the drift these
    two copies already cost enough to avoid. Copies, because `is_estimated`
    answers by object identity and the live dicts must not leave this module.
    """
    return {model: dict(PRICING[model])
            for model in sorted(models) if model in PRICING}


def _pricing_key(model):
    """Resolve an id to the table key without selecting a dated rate."""
    if not model:
        return None
    if model in PRICING:
        return model
    # Longest key first, not table order. This tier exists to absorb dated
    # snapshot ids, and the table lists `gpt-5.4` before `gpt-5.4-mini` and
    # `gpt-5.3-codex` before `gpt-5.3-codex-spark` — so a first-match loop billed
    # a dated `gpt-5.4-mini-*` at the parent tier (3.3x over), a dated
    # `gpt-5.4-nano-*` at 12.5x, and answered "published" for a dated
    # `gpt-5.3-codex-spark-*` whose rate is still an estimate. Sorted per call
    # rather than cached at import because load_rate_overrides adds keys later.
    # web/js/10-pricing.js's getPricing must resolve the same way.
    for key in sorted(PRICING, key=len, reverse=True):
        if model.startswith(key):
            return key
    # Substring fallback: match model family by keyword
    m = model.lower()
    if "fable" in m or "mythos" in m:
        return "claude-fable-5"
    if "opus" in m:
        return "claude-opus-4-8"
    if "sonnet" in m:
        return "claude-sonnet-4-6"
    if "haiku" in m:
        return "claude-haiku-4-5"
    # Codex families. `codex` first: "codex-auto-review" also contains no gpt,
    # and a future "gpt-...-codex-..." should still land on the review tier only
    # when it is actually the review model.
    if "codex-auto-review" in m:
        return "codex-auto-review"
    # Deliberately NOT a bare "gpt" match. The rule this table has always
    # followed is that an unrecognised model is billed at nothing rather than
    # guessed at, so local and third-party models are never charged at someone
    # else's rates — and "gpt" is the single most common substring in that
    # population. Only the families Codex actually ships fall back here.
    if "gpt-5" in m or "codex" in m:
        return "gpt-5.6-sol"
    return None


def _as_pricing_date(value):
    """Return an ISO value's UTC date, or ``None`` when it is not usable."""
    if isinstance(value, datetime):
        value = value.isoformat()
    if isinstance(value, date):
        return value
    parsed = parse_instant(value)
    return parsed.date() if parsed is not None else None


def get_pricing(model, at=None):
    """Return the rates for ``model`` at an optional request instant.

    With no ``at`` argument this preserves the long-standing current-table API.
    A dated lookup applies only a known effective policy; a ``start`` boundary
    can preserve a prior estimate before a verified current change, while an
    ``until`` boundary is used only when a vendor publishes a real end. An
    override that changed a built-in entry remains authoritative for every
    date.

    The fallback family keywords are deliberately kept visible here for the
    parity tests: ``"fable" in m``, ``"mythos" in m``, ``"opus" in m``,
    ``"sonnet" in m``, ``"haiku" in m``, ``"codex-auto-review" in m``, and
    ``"codex" in m`` plus ``"gpt-5" in m``. Resolution itself lives in ``_pricing_key`` so the
    date-aware and current-table callers cannot diverge.
    """
    key = _pricing_key(model)
    if key is None:
        return None
    rates = PRICING.get(key)
    policy = RATE_POLICIES.get(key)
    when = _as_pricing_date(at)
    if (when is None or not policy or key not in _CANONICAL_CURRENT_RATES
            or rates is _OVERRIDE_RATE_OBJECTS.get(key)
            or rates != _CANONICAL_CURRENT_RATES[key]):
        return rates
    start = policy.get("start")
    if start and when < date.fromisoformat(start):
        return policy["before"]
    until = policy.get("until")
    if until and when > date.fromisoformat(until):
        return policy["after"]
    return policy["rates"]


def pricing_for(model, at=None):
    """Explicit alias for callers that want to document date-aware pricing."""
    return get_pricing(model, at)


def is_long_context_model(model):
    """Whether ``model`` is an eligible long-context family or snapshot.

    A plain prefix check is intentionally not enough: ``gpt-5.4-mini`` and
    ``gpt-5.4-nano`` share the parent spelling but are separate products. Only
    an exact family id or a date-shaped snapshot suffix inherits the tier.
    """
    if not isinstance(model, str):
        return False
    if model in LONG_CONTEXT_MODELS:
        return True
    for name in LONG_CONTEXT_MODELS:
        prefix = f"{name}-"
        if (model.startswith(prefix)
                and _LONG_CONTEXT_SNAPSHOT.fullmatch(model[len(name):])):
            return True
    return False


def is_estimated(model):
    """True when this model's rates are an estimate rather than a price list.

    Kept beside the table so the two cannot drift: a rate added above without a
    decision about its provenance shows up here as "published", which is the
    claim that needs justifying.
    """
    resolved = get_pricing(model)
    if resolved is None:
        return False
    return any(resolved is PRICING[name] for name in ESTIMATED_RATE_MODELS)

def is_long_context(model, inp, cache_read=0, cache_creation=0):
    """Whether one request's complete input side receives the surcharge."""
    prompt_tokens = sum(
        value if isinstance(value, (int, float)) and value > 0 else 0
        for value in (inp, cache_read, cache_creation))
    if prompt_tokens <= LONG_CONTEXT_THRESHOLD:
        return False
    return is_long_context_model(model)


def _calc_cost_parts_at_rate(p, inp, out, cache_read, cache_creation,
                             cache_creation_1h=0, long_context=False):
    """Price one already-classified normal or long-context token bucket."""
    if not p:
        return None
    # Clamp instead of trusting the split: the two columns are summed
    # independently by SQL, and a negative 5-minute remainder would credit money
    # back if a 1-hour figure ever exceeded its own total.
    long_lived = min(max(cache_creation_1h, 0), max(cache_creation, 0))
    short_lived = max(cache_creation, 0) - long_lived
    input_multiplier = 2.0 if long_context else 1.0
    output_multiplier = 1.5 if long_context else 1.0
    return {
        "input":          inp        * p["input"]      * input_multiplier / 1_000_000,
        "output":         out        * p["output"]     * output_multiplier / 1_000_000,
        "cache_read":     cache_read * p["cache_read"] * input_multiplier / 1_000_000,
        "cache_creation": (short_lived * p["cache_write"]    * input_multiplier / 1_000_000 +
                           long_lived  * p["cache_write_1h"] * input_multiplier / 1_000_000),
    }


def calc_cost_parts(model, inp, out, cache_read, cache_creation,
                    cache_creation_1h=0, timestamp=None, long_context=None,
                    at=None):
    """What each kind of token cost, in dollars, or None for an unpriced model.

    A direct call prices one request and therefore can classify its own prompt.
    Aggregate callers should use :func:`calc_cost_parts_tiered`, because the
    long-context threshold is per request rather than per aggregate bucket.
    """
    if at is not None and timestamp is None:
        timestamp = at
    p = get_pricing(model, timestamp)
    if not p:
        return None
    if long_context is None:
        long_context = is_long_context(model, inp, cache_read, cache_creation)
    return _calc_cost_parts_at_rate(
        p, inp, out, cache_read, cache_creation, cache_creation_1h,
        bool(long_context))


def calc_cost_parts_tiered(model, inp, out, cache_read, cache_creation,
                           cache_creation_1h=0, long_input=0, long_output=0,
                           long_cache_read=0, long_cache_creation=0,
                           long_cache_creation_1h=0, timestamp=None, at=None):
    """Price aggregate totals while retaining each turn's long-context tier.

    ``long_*`` values are the portions contributed by turns whose prompt was
    above :data:`LONG_CONTEXT_THRESHOLD`; the remainder is priced normally.
    This is the rollup boundary: once SQL has summed turns, the original
    per-request threshold cannot be reconstructed from ``inp`` alone.
    """
    if at is not None and timestamp is None:
        timestamp = at
    p = get_pricing(model, timestamp)
    if not p:
        return None
    totals = {
        "input": max(inp, 0), "output": max(out, 0),
        "cache_read": max(cache_read, 0),
        "cache_creation": max(cache_creation, 0),
        "cache_creation_1h": max(cache_creation_1h, 0),
    }
    long = {
        "input": min(max(long_input, 0), totals["input"]),
        "output": min(max(long_output, 0), totals["output"]),
        "cache_read": min(max(long_cache_read, 0), totals["cache_read"]),
        "cache_creation": min(max(long_cache_creation, 0), totals["cache_creation"]),
        "cache_creation_1h": min(max(long_cache_creation_1h, 0), totals["cache_creation_1h"]),
    }
    normal = {key: totals[key] - long[key] for key in totals}
    normal_parts = _calc_cost_parts_at_rate(
        p, normal["input"], normal["output"], normal["cache_read"],
        normal["cache_creation"], normal["cache_creation_1h"], False)
    long_parts = _calc_cost_parts_at_rate(
        p, long["input"], long["output"], long["cache_read"],
        long["cache_creation"], long["cache_creation_1h"], True)
    return {key: normal_parts[key] + long_parts[key]
            for key in ("input", "output", "cache_read", "cache_creation")}


def calc_cost(model, inp, out, cache_read, cache_creation, cache_creation_1h=0,
              timestamp=None, long_context=None, at=None):
    """Cost of one bucket of tokens, in dollars.

    The optional 1-hour argument defaults to 0 so a caller with no split — a
    pre-migration row, or a caller that never had one — bills exactly as before
    rather than silently dropping the write.
    """
    parts = calc_cost_parts(model, inp, out, cache_read, cache_creation,
                            cache_creation_1h, timestamp, long_context, at)
    if parts is None:
        return 0.0
    return (parts["input"] + parts["output"]
            + parts["cache_read"] + parts["cache_creation"])

def _fixed(value, places):
    """Round `value` to `places` decimals the way JavaScript's toFixed does.

    Python's f-string rounds an exact half to EVEN and `toFixed` takes the
    larger, so `f"{1250/1000:.1f}"` is "1.2" where `(1.25).toFixed(1)` is "1.3"
    — the same one-formatter-two-answers split that produced `CSV_COST` on the
    page. `Decimal(float)` is the double's exact value, which is the value
    `toFixed` is specified to round, so this reproduces it rather than
    approximating it. Only ever reached with a non-negative quotient, where
    half-up and half-away-from-zero are the same rule.
    """
    return str(Decimal(value).quantize(Decimal(1).scaleb(-places),
                                       rounding=ROUND_HALF_UP))

def fmt(n):
    # Match the browser formatter, including the billions tier, so terminal
    # and dashboard totals use the same units. Formatter parity tests cover both.
    if n >= 1_000_000_000:
        return _fixed(n / 1_000_000_000, 2) + "B"
    if n >= 1_000_000:
        return _fixed(n / 1_000_000, 2) + "M"
    if n >= 1_000:
        return _fixed(n / 1_000, 1) + "K"
    return str(n)

def fmt_cost(c):
    return f"${c:.4f}"
