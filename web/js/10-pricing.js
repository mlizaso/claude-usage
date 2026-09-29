// ── Pricing (generated from claude_usage/pricing.py) ───────────────────────
// cache_write is the short-lived write bucket; cache_write_1h is its one-hour
// subset. Anthropic publishes distinct 5-minute/1-hour rates. OpenAI publishes
// one write bucket, so its two schema fields intentionally share a rate.
// BEGIN GENERATED PRICING DATA. Run scripts/generate-pricing-assets.py.
const PRICING = {
  // Generated from claude_usage.pricing.PRICING; do not edit by hand.
  'claude-fable-5-1': { input: 10, output: 50, cache_write: 12.5, cache_read: 0.25, cache_write_1h: 20 },
  'claude-mythos-5-1': { input: 10, output: 50, cache_write: 12.5, cache_read: 0.25, cache_write_1h: 20 },
  'claude-fable-5': { input: 10, output: 50, cache_write: 12.5, cache_read: 1, cache_write_1h: 20 },
  'claude-mythos-5': { input: 10, output: 50, cache_write: 12.5, cache_read: 1, cache_write_1h: 20 },
  'claude-opus-5-5': { input: 4, output: 20, cache_write: 5, cache_read: 0.2, cache_write_1h: 8 },
  'claude-opus-5': { input: 5, output: 25, cache_write: 6.25, cache_read: 0.5, cache_write_1h: 10 },
  'claude-opus-4-8': { input: 5, output: 25, cache_write: 6.25, cache_read: 0.5, cache_write_1h: 10 },
  'claude-opus-4-7': { input: 5, output: 25, cache_write: 6.25, cache_read: 0.5, cache_write_1h: 10 },
  'claude-opus-4-6': { input: 5, output: 25, cache_write: 6.25, cache_read: 0.5, cache_write_1h: 10 },
  'claude-opus-4-5': { input: 5, output: 25, cache_write: 6.25, cache_read: 0.5, cache_write_1h: 10 },
  'claude-sonnet-5-5': { input: 2, output: 10, cache_write: 2.5, cache_read: 0.2, cache_write_1h: 4 },
  'claude-sonnet-5': { input: 2, output: 10, cache_write: 2.5, cache_read: 0.2, cache_write_1h: 4 },
  'claude-sonnet-4-7': { input: 3, output: 15, cache_write: 3.75, cache_read: 0.3, cache_write_1h: 6 },
  'claude-sonnet-4-6': { input: 3, output: 15, cache_write: 3.75, cache_read: 0.3, cache_write_1h: 6 },
  'claude-sonnet-4-5': { input: 3, output: 15, cache_write: 3.75, cache_read: 0.3, cache_write_1h: 6 },
  'claude-haiku-4-7': { input: 1, output: 5, cache_write: 1.25, cache_read: 0.1, cache_write_1h: 2 },
  'claude-haiku-4-6': { input: 1, output: 5, cache_write: 1.25, cache_read: 0.1, cache_write_1h: 2 },
  'claude-haiku-4-5': { input: 1, output: 5, cache_write: 1.25, cache_read: 0.1, cache_write_1h: 2 },
  'gpt-6-astra': { input: 10, output: 50, cache_write: 12.5, cache_read: 1, cache_write_1h: 12.5 },
  'gpt-6-sol': { input: 2, output: 10, cache_write: 2.5, cache_read: 0.2, cache_write_1h: 2.5 },
  'gpt-6-luna': { input: 0.1, output: 0.5, cache_write: 0.125, cache_read: 0.01, cache_write_1h: 0.125 },
  'gpt-5.6-sol': { input: 4, output: 20, cache_write: 5, cache_read: 0.4, cache_write_1h: 5 },
  'gpt-5.6-terra': { input: 2, output: 12, cache_write: 2.5, cache_read: 0.2, cache_write_1h: 2.5 },
  'gpt-5.6-luna': { input: 0.2, output: 1.2, cache_write: 0.25, cache_read: 0.02, cache_write_1h: 0.25 },
  'gpt-5.5': { input: 5, output: 30, cache_write: 5, cache_read: 0.5, cache_write_1h: 5 },
  'gpt-5.4': { input: 2.5, output: 15, cache_write: 2.5, cache_read: 0.25, cache_write_1h: 2.5 },
  'gpt-5.4-mini': { input: 0.75, output: 4.5, cache_write: 0.75, cache_read: 0.075, cache_write_1h: 0.75 },
  'gpt-5.4-nano': { input: 0.2, output: 1.25, cache_write: 0.2, cache_read: 0.02, cache_write_1h: 0.2 },
  'gpt-5.3-codex': { input: 1.75, output: 14, cache_write: 1.75, cache_read: 0.175, cache_write_1h: 1.75 },
  'gpt-5.3-codex-spark': { input: 1.75, output: 14, cache_write: 1.75, cache_read: 0.175, cache_write_1h: 1.75 },
  'codex-auto-review': { input: 1.75, output: 14, cache_write: 1.75, cache_read: 0.175, cache_write_1h: 1.75 },
};
// Model ids are data, including JavaScript's magic object names. With an
// ordinary prototype, assigning a user-supplied `__proto__` override changes
// the table's prototype instead of adding that model. A null-prototype table
// makes every string an ordinary own key.
Object.setPrototypeOf(PRICING, null);

// Generated from claude_usage.pricing.RATE_POLICIES; do not edit by hand.
const RATE_POLICIES = Object.freeze({
  "gpt-5.6-sol": Object.freeze({
    start: "2026-08-22",
    before: Object.freeze({ input: 5, output: 30, cache_write: 6.25, cache_read: 0.5, cache_write_1h: 6.25 }),
    rates: Object.freeze({ input: 4, output: 20, cache_write: 5, cache_read: 0.4, cache_write_1h: 5 }),
  }),
  "gpt-5.6-terra": Object.freeze({
    start: "2026-07-30",
    before: Object.freeze({ input: 2.5, output: 15, cache_write: 3.125, cache_read: 0.25, cache_write_1h: 3.125 }),
    rates: Object.freeze({ input: 2, output: 12, cache_write: 2.5, cache_read: 0.2, cache_write_1h: 2.5 }),
  }),
  "gpt-5.6-luna": Object.freeze({
    start: "2026-07-30",
    before: Object.freeze({ input: 1, output: 6, cache_write: 1.25, cache_read: 0.1, cache_write_1h: 1.25 }),
    rates: Object.freeze({ input: 0.2, output: 1.2, cache_write: 0.25, cache_read: 0.02, cache_write_1h: 0.25 }),
  }),
});

// Generated from the canonical long-context policy.
const LONG_CONTEXT_THRESHOLD = 272000;
const LONG_CONTEXT_MODELS = Object.freeze([
  "gpt-5.4",
  "gpt-5.5",
  "gpt-5.6-luna",
  "gpt-5.6-sol",
  "gpt-5.6-terra",
  "gpt-6-astra",
  "gpt-6-luna",
  "gpt-6-sol",
]);

// Generated provenance and override-field contracts.
let ESTIMATED_RATE_MODELS = Object.freeze([
  "codex-auto-review",
  "gpt-5.3-codex-spark",
]);
const RATE_FIELDS = Object.freeze([
  "input",
  "output",
  "cache_read",
  "cache_write",
  "cache_write_1h",
]);
// END GENERATED PRICING DATA.
const CANONICAL_CURRENT_RATES = Object.fromEntries(
  Object.entries(PRICING).map(([model, rates]) => [model, { ...rates }])
);
const OVERRIDDEN_RATE_MODELS = new Set();

function isLongContextModel(model) {
  if (typeof model !== 'string') return false;
  if (LONG_CONTEXT_MODELS.includes(model)) return true;
  return LONG_CONTEXT_MODELS.some(name => {
    if (!model.startsWith(`${name}-`)) return false;
    const suffix = model.slice(name.length);
    return /^(?:-\d{8}|-\d{4}-\d{2}-\d{2})$/.test(suffix);
  });
}

function isLongContext(model, inp, cacheRead, cacheCreation) {
  const prompt = [inp, cacheRead, cacheCreation]
    .reduce((sum, value) => sum + (Number.isFinite(value) && value > 0 ? value : 0), 0);
  return prompt > LONG_CONTEXT_THRESHOLD && isLongContextModel(model);
}

// Models whose rates are estimated rather than published, so the page can say so
// beside any figure derived from them.
//
// Resolved through getPricing rather than by matching the id, for the same
// reason isBillable is: the question "is this rate a guess" must have exactly
// one answer, and a second keyword list beside the table could disagree with it.
// Mirrors pricing.is_estimated; tests/test_pricing_parity.py fails if the two
// sets drift.
//
// `ESTIMATED_RATE_MODELS` is generated as `let` for exactly one reason: a rate
// the user supplied through CLAUDE_USAGE_RATES is no longer our estimate, and
// dropping the label means rebuilding this list. The generated array itself
// stays frozen; see applyRateOverrides at the foot of this file. RATE_FIELDS is
// generated from the Python override contract for the same reason.

function isEstimatedRate(model) {
  const resolved = getPricing(model);
  if (!resolved) return false;
  return ESTIMATED_RATE_MODELS.some(name => PRICING[name] === resolved);
}

// Whether a model is one we have rates for. Derived from getPricing rather than
// from its own keyword list: calcCost is gated on this function, so the two used
// to be able to disagree — a priced model whose id carried a new family name
// would have been charged by cli.py and shown as free here. Now that cannot
// happen, because there is only one answer to "do we know this model's price?".
function isBillable(model) {
  return getPricing(model) !== null;
}

function getPricing(model) {
  if (!model) return null;
  if (Object.prototype.hasOwnProperty.call(PRICING, model)) return PRICING[model];
  // Longest key first, not table order — the same rule as pricing.get_pricing,
  // which names this function in its own comment. This tier exists to absorb
  // dated snapshot ids, and the table lists `gpt-5.4` before `gpt-5.4-mini` and
  // `gpt-5.3-codex` before `gpt-5.3-codex-spark`, so a first-match loop billed a
  // dated `gpt-5.4-mini-*` at the parent tier (3.3x over), a dated
  // `gpt-5.4-nano-*` at 12.1x, and reported "published" for a dated
  // `gpt-5.3-codex-spark-*` whose rate is only an estimate. Sorted per call, as
  // in Python, rather than cached at import: the cheap thing to get wrong later
  // is a cache that goes stale when a key is added, and the sort is ~24 elements
  // reached only by ids that miss the exact-match return above.
  for (const key of Object.keys(PRICING).sort((a, b) => b.length - a.length)) {
    if (model.startsWith(key)) return PRICING[key];
  }
  const m = model.toLowerCase();
  if (m.includes('fable') || m.includes('mythos')) return PRICING['claude-fable-5'];
  if (m.includes('opus'))   return PRICING['claude-opus-4-8'];
  if (m.includes('sonnet')) return PRICING['claude-sonnet-4-6'];
  if (m.includes('haiku'))  return PRICING['claude-haiku-4-5'];
  // Codex families — mirrors pricing.get_pricing, same order.
  if (m.includes('codex-auto-review')) return PRICING['codex-auto-review'];
  // Not a bare 'gpt': an unrecognised model must stay unpriced rather than be
  // charged at a family it merely resembles. Mirrors pricing.get_pricing.
  if (m.includes('gpt-5') || m.includes('codex')) return PRICING['gpt-5.6-sol'];
  return null;
}

function getPricingAt(model, at) {
  const current = getPricing(model);
  if (!current || !at) return current;
  const key = Object.keys(PRICING).find(name => PRICING[name] === current);
  const policy = key && RATE_POLICIES[key];
  if (!policy || OVERRIDDEN_RATE_MODELS.has(key) || !CANONICAL_CURRENT_RATES[key]
      || JSON.stringify(current) !== JSON.stringify(CANONICAL_CURRENT_RATES[key])) {
    return current;
  }
  const parsed = at instanceof Date ? at : new Date(at);
  if (Number.isNaN(parsed.getTime())) return current;
  const day = parsed.toISOString().slice(0, 10);
  if (policy.start && day < policy.start) return policy.before;
  if (policy.until && day > policy.until) return policy.after;
  return policy.rates;
}

// `cacheCreation` is the FULL write total; `cacheCreation1h` is the part of it
// written to the 1-hour cache (the transcripts report
// cache_creation_input_tokens == ephemeral_1h + ephemeral_5m). The 1-hour slice
// is subtracted out and re-billed at its own higher rate. The last argument is
// optional and defaults to 0, so a caller without a split bills exactly as
// before instead of losing the write. Mirrors pricing.calc_cost exactly —
// tests/test_dashboard_js.py runs both against the same inputs.
// What each kind of token cost, or null for an unpriced model. Split out so the
// Cost by Model table can show where the money went rather than only the total;
// calcCost is the sum of exactly these four, so a breakdown can never disagree
// with the total it belongs to. Mirrors pricing.calc_cost_parts.
function costParts(model, inp, out, cacheRead, cacheCreation, cacheCreation1h,
                   timestamp, longContext) {
  if (!isBillable(model)) return null;
  const p = getPricingAt(model, timestamp);
  if (!p) return null;
  const total = Math.max(cacheCreation || 0, 0);
  const longLived = Math.min(Math.max(cacheCreation1h || 0, 0), total);
  const shortLived = total - longLived;
  const isLong = longContext == null
    ? isLongContext(model, inp, cacheRead, cacheCreation)
    : !!longContext;
  const inputMultiplier = isLong ? 2 : 1;
  const outputMultiplier = isLong ? 1.5 : 1;
  return {
    input:      inp       * p.input      * inputMultiplier / 1e6,
    output:     out       * p.output     * outputMultiplier / 1e6,
    cache_read: cacheRead * p.cache_read * inputMultiplier / 1e6,
    cache_creation: shortLived * p.cache_write    * inputMultiplier / 1e6 +
                    longLived  * p.cache_write_1h * inputMultiplier / 1e6,
  };
}

function costPartsTiered(model, inp, out, cacheRead, cacheCreation,
                         cacheCreation1h, longInput, longOutput,
                         longCacheRead, longCacheCreation,
                         longCacheCreation1h, timestamp) {
  const p = getPricingAt(model, timestamp);
  if (!p) return null;
  const totals = {
    input: Math.max(inp || 0, 0), output: Math.max(out || 0, 0),
    cacheRead: Math.max(cacheRead || 0, 0),
    cacheCreation: Math.max(cacheCreation || 0, 0),
    cacheCreation1h: Math.max(cacheCreation1h || 0, 0),
  };
  const long = {
    input: Math.min(Math.max(longInput || 0, 0), totals.input),
    output: Math.min(Math.max(longOutput || 0, 0), totals.output),
    cacheRead: Math.min(Math.max(longCacheRead || 0, 0), totals.cacheRead),
    cacheCreation: Math.min(Math.max(longCacheCreation || 0, 0), totals.cacheCreation),
    cacheCreation1h: Math.min(Math.max(longCacheCreation1h || 0, 0), totals.cacheCreation1h),
  };
  const normal = costParts(model,
    totals.input - long.input, totals.output - long.output,
    totals.cacheRead - long.cacheRead,
    totals.cacheCreation - long.cacheCreation,
    totals.cacheCreation1h - long.cacheCreation1h,
    timestamp, false);
  const extended = costParts(model, long.input, long.output, long.cacheRead,
    long.cacheCreation, long.cacheCreation1h, timestamp, true);
  if (!normal || !extended) return null;
  return Object.fromEntries(COST_COLUMNS.map(key => [key, normal[key] + extended[key]]));
}

// The four token columns that carry money, in the order the cost tables print
// them, and every token column a cost bucket accumulates. Named once: a rollup
// that sums a column its renderer does not print (or the reverse) shows a total
// that silently disagrees with the cells above it.
//
// `reasoning` is deliberately absent from COST_COLUMNS. It is a SUBSET of
// `output` — the output figure calcCost already multiplies contains it — so
// pricing it as a fifth column would bill the same tokens twice.
const COST_COLUMNS = Object.freeze(['input', 'output', 'cache_read', 'cache_creation']);
const TOKEN_COLUMNS = Object.freeze([...COST_COLUMNS, 'cache_creation_1h', 'reasoning']);

function calcCost(model, inp, out, cacheRead, cacheCreation, cacheCreation1h,
                  timestamp, longContext) {
  const parts = costParts(model, inp, out, cacheRead, cacheCreation, cacheCreation1h,
                          timestamp, longContext);
  if (!parts) return 0;
  return parts.input + parts.output + parts.cache_read + parts.cache_creation;
}

function rowCostParts(row) {
  // Aggregate rows use an all-zero placeholder while they accumulate. An
  // explicitly unbillable row has no price breakdown; treating that placeholder
  // as real cost turns entirely unpriced usage into $0.00.
  if (row && row.billable === false) return null;
  const supplied = row && row.cost_parts;
  if (supplied && COST_COLUMNS.every(key => typeof supplied[key] === 'number'
                                           && Number.isFinite(supplied[key]))) {
    return supplied;
  }
  return costParts(row.model, row.input || 0, row.output || 0,
                   row.cache_read || 0, row.cache_creation || 0,
                   row.cache_creation_1h || 0, row.pricing_day || row.day);
}

function rowCost(row) {
  const parts = rowCostParts(row);
  return parts ? COST_COLUMNS.reduce((sum, key) => sum + parts[key], 0) : 0;
}

// ── Rates the user supplied ────────────────────────────────────────────────
// CLAUDE_USAGE_RATES points at a JSON file of per-model rates. The server reads
// and resolves it once (pricing.load_rate_overrides) and injects the models it
// applied, already resolved to five fields, on APP_CONFIG. This page bills from
// its own copy of the table, so while the override stopped at the Python side
// `cli.py stats` printed $1.0000 and the Est. Cost tile, the Cost by Model table
// and the CSV export showed $5.0000 for the identical turns — beneath a footer
// telling the reader to set the variable that had just failed to work.
//
// Mirrors the last two lines of pricing.load_rate_overrides: replace the rates,
// then drop the "estimated" label, because a rate the user supplied is theirs
// and not our guess. Dropping it means taking the name out of
// ESTIMATED_RATE_MODELS rather than only replacing the object — isEstimatedRate
// matches by identity, so the replacement would still answer "estimated".
function applyRateOverrides(overrides) {
  if (!overrides || typeof overrides !== 'object') return;
  // The server uses entries rather than an object literal so `__proto__`
  // survives embedding in APP_CONFIG as data. Accept the old object shape as
  // well for compatibility with saved/test documents and third-party callers.
  const entries = Array.isArray(overrides)
    ? overrides
    : Object.entries(overrides);
  const applied = new Set();
  for (const entry of entries) {
    if (!Array.isArray(entry) || entry.length !== 2) continue;
    const [model, rates] = entry;
    if (typeof model !== 'string') continue;
    if (!rates || typeof rates !== 'object') continue;
    // All five fields or none. A half-applied price table is a silently wrong
    // bill — the same rule pricing.load_rate_overrides applies to the file.
    if (!RATE_FIELDS.every(f => typeof rates[f] === 'number'
                                && isFinite(rates[f]) && rates[f] >= 0)) continue;
    const table = {};
    for (const field of RATE_FIELDS) table[field] = rates[field];
    PRICING[model] = table;
    applied.add(model);
    OVERRIDDEN_RATE_MODELS.add(model);
  }
  if (!applied.size) return;
  ESTIMATED_RATE_MODELS = Object.freeze(
    ESTIMATED_RATE_MODELS.filter(name => !applied.has(name)));
}

applyRateOverrides(APP_CONFIG.rate_overrides);
