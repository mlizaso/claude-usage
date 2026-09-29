// ── Formatting ─────────────────────────────────────────────────────────────
// Numbers are formatted in a FIXED locale, not the viewer's. The chart axis
// (toFixed) and every CSV column are dot-decimal, so leaving these on
// the browser locale meant a de-DE or fr-FR user read "$1.500,0000" in a table,
// "$1500.00" on the axis beside it, and "1500.0000" in the export of the very
// same figure. Grouping is kept — only the locale is pinned.
const NUM = new Intl.NumberFormat('en-US');
const COST_OPTS = { minimumFractionDigits: 4, maximumFractionDigits: 4 };
const COST = new Intl.NumberFormat('en-US', COST_OPTS);
const COST_BIG = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

// The CSV twin of COST: the same digits and the same rounding, without the
// grouping separator. Two reasons it is a formatter rather than a toFixed call,
// which is what the exports used to be:
//
//   * toFixed rounds an exact half DOWN where Intl rounds it up, so 85,194
//     output tokens at $25/M — 2.129850 exactly — read "$2.1299" in the table
//     and exported as "2.1298". One formatter, one answer.
//   * grouping is dropped here rather than stripped off COST's output
//     afterwards: csvField quotes any field containing a comma, and a quoted
//     "3,183.0286" is a text cell in most spreadsheets — a worse regression
//     than the last decimal it would have repaired. Built from COST's own
//     options so the precision of the two cannot drift.
const CSV_COST = new Intl.NumberFormat('en-US', { ...COST_OPTS, useGrouping: false });

function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(2)+'B';
  if (n >= 1e6) return (n/1e6).toFixed(2)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
  return NUM.format(n);
}
function fmtCost(c)    { return '$' + COST.format(c); }
// Two decimals for a headline figure — but NEVER `$0.00` over nonzero spend.
//
// `$0.00` asserts the work was free, which is a different claim from "small",
// and it is the same claim this page already refuses to make at the other end
// of the scale: `renderModelCostTotals`, `dailySeries()` and `noCostReason` all
// print `n/a` rather than `$0.00` when nothing is priced, because "we have no
// rate for this" is not "this cost nothing".
//
// Sub-cent estimates need more precision to avoid displaying nonzero cost as zero.
//
// Falls back to the four-decimal `COST` rather than to a `< $0.01` form so the
// tile shows the SAME digits as every card under it.
function fmtCostBig(c) {
  const n = Number(c) || 0;
  if (n > 0 && n < 0.005) return '$' + COST.format(n);
  return '$' + COST_BIG.format(n);
}

// A per-million unit price, e.g. "$25.00/M" or "$0.175/M".
//
// Two decimals used to be enough — the cheapest published Anthropic rate is
// $0.10/M. Codex's are not round: cached input at $0.075/M and $0.175/M prints
// as $0.08 and $0.18 at two decimals, 6.7% and 2.9% high beside the
// multiplication the rate exists to let a reader check. THREE decimals cover
// every list price that can actually reach this function.
//
// The FOURTH is for the blend. fmtRate's one caller (tokenCostCell) hands it
// effectiveRate(tokens, cost) — a derived quotient, never a table lookup — and
// a cell mixing the two cache TTL tiers, or a column totalled across models,
// lands on an arbitrary real with no list price behind it at all.
//
// No current table entry needs four decimals. Keep the fourth for blended
// effective rates, whose denominator can produce one regardless of the source
// rates' precision; do not justify it with a dated list-price example.
//
// Every figure above was measured against pricing.PRICING on 2026-08-10. This
// comment previously named $0.125 and $0.025, which 8cd0b81 repriced away
// without opening this file; the executable form of the claim is
// `test_every_rate_in_the_table_prints_without_loss`, which round-trips the
// live table rather than trusting the prose here.
const RATE = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 });
function fmtRate(r) { return '$' + RATE.format(r) + '/M'; }

// A share of a whole, e.g. "90.2%". Pinned to en-US for the same reason every
// other figure on the page is: a viewer-locale "90,2%" beside an en-US token
// count reads as two different numbers.
const PCT = new Intl.NumberFormat('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
function fmtPct(part, whole) {
  if (!(whole > 0)) return '—';
  return PCT.format(part / whole * 100) + '%';
}

// The unit price a bucket of tokens was actually charged at, derived from the
// money rather than looked up. For a single-rate column that reproduces the
// list price exactly; where two rates are in play — cache writes billed partly
// at the 5-minute and partly at the 1-hour tier, or a column totalled across
// models — it yields the effective blend, which is the only number that makes
// `tokens x rate = cost` true on screen.
function effectiveRate(tokens, cost) {
  return tokens > 0 && cost != null ? cost / tokens * 1e6 : null;
}

// Dates are pinned to en-US for the same reason the numbers above are: a
// viewer-locale date would print "6 Aug." or "2026/8/6" on the same card as an
// en-US number.
const DAY_SHORT      = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric' });
const DAY_SHORT_YEAR = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric' });

// 'YYYY-MM-DD' -> a Date at LOCAL midnight. Deliberately NOT new Date(iso):
// that parses a bare ISO date as UTC midnight, which formats as the previous day
// anywhere west of UTC — the same class of bug as the "This Month" one (#151).
//
// The check is on shape AND calendar, and the second half is not decoration.
// `localdays.py`'s COALESCE(substr(...)) fallback emits a raw timestamp prefix
// for anything SQLite's date() cannot parse, so a key with the right shape and an
// impossible calendar — '2026-13-45', '0000-00-00' — really does reach the
// payload. `new Date(y, m, d)` NORMALISES such components instead of returning an
// Invalid Date: (2026, 12, 45) is a perfectly real Sun Feb 14 2027, and (1, 0, 1)
// is 1901 because a two-digit year is remapped into the 1900s. Reading the three
// components back out and requiring them to be the ones that went in is what
// turns "wrong date, confidently formatted" into the null every caller already
// handles. Measured before it: '2026-13-45' beside two August keys made All Time
// a 198-day span ending 2027-02-14.
//
// It subsumes the Number.isNaN check it replaces — an Invalid Date's getFullYear()
// is NaN, and NaN equals nothing — and it costs no real day: across all 418 zones
// Intl lists, every calendar day from 2015 to 2030 survives the round trip
// (verified 2026-08-11; a zone whose DST springs forward AT midnight lands the
// Date on 01:00 of the same day, which the three getters still agree with).
//
// HALF this repair is worse than the defect, so do not make it here alone.
// `dailyFillSpan` (30-ranges.js) is what actually picks the All Time extent, and
// it used to carry its OWN copy of the shape regex — so a calendar-strict
// function here left that copy still selecting '2026-13-45', `eachLocalDay` then
// rejected it and returned [], and the whole All Time fill disappeared: measured,
// ten contiguous days down to zero, the #151-class bug `dailyFillSpan` exists to
// close. Both sites now share one rule (`spanBoundDays`, built on this function),
// and `TestAMalformedDayKeyDoesNotEmptyTheDailyFill` is the assertion that goes
// red if they are ever split again.
function dayToLocalDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
  if (!m) return null;
  const d = new Date(+m[1], +m[2] - 1, +m[3]);
  return (d.getFullYear() === +m[1]
          && d.getMonth() === +m[2] - 1
          && d.getDate() === +m[3]) ? d : null;
}

// "Jul 8 – Aug 6", or just "Aug 6" when both ends are the same day. The year is
// shown whenever the span leaves the current year, so "Previous Month" in
// January reads "Dec 1, 2025 – Dec 31, 2025" rather than an ambiguous "Dec 1".
function fmtDaySpan(startISO, endISO) {
  const a = dayToLocalDate(startISO), b = dayToLocalDate(endISO);
  if (!a || !b) return '';
  const thisYear = new Date().getFullYear();
  const f = (a.getFullYear() !== b.getFullYear() || a.getFullYear() !== thisYear)
    ? DAY_SHORT_YEAR : DAY_SHORT;
  const s = f.format(a), e = f.format(b);
  return s === e ? s : s + ' – ' + e;   // en dash
}

// ── Chart colors ───────────────────────────────────────────────────────────
// Warm/neutral palette kept in sync with the CSS :root variables so charts match
// the Claude Code interface (less blue). Chart legends/axes use C.axis (a touch
// lighter than --muted so small labels stay legible on the dark card); grid uses
// C.border.
const C = {
  text:   '#BFBFBF',
  muted:  '#4F4F50',
  axis:   '#6F6F70',
  border: '#2C2D2E',
  card:   '#1E1F20',
  blue:   '#48A0C7',
  green:  '#74C991',
  red:    '#C74E39',
  accent: '#d97757',
  amber:  '#D9A84E',
  purple: '#9B7EC7',
  teal:   '#5BB8A3',
  mauve:  '#C77E9B',
};

// The values above are the dark defaults; the keys listed below are re-read
// from the CSS variables so the charts follow the theme instead of being
// hand-copied to match it. Only those differ between the palettes — the series
// hues (amber, purple, teal, mauve, and the TOKEN_COLORS below) are saturated
// enough to read on either background and are deliberately left alone, so a
// series keeps its identity when the theme changes.
//
// The list is the count. This comment said "these seven" twice while the array
// held nine, which is where a prose tally beside a literal always ends up — so
// it no longer carries one.
//
// Chart.js reads these at construction time, so a theme change must call this
// and then re-render; mutating C alone repaints nothing.
const THEMED_CHART_COLORS = ['text', 'muted', 'axis', 'border', 'card',
                             'blue', 'green', 'red', 'accent'];

function syncChartColors() {
  if (typeof getComputedStyle !== 'function' || typeof document === 'undefined') return C;
  const style = getComputedStyle(document.documentElement);
  for (const key of THEMED_CHART_COLORS) {
    const value = (style.getPropertyValue('--' + key) || '').trim();
    // An empty read means the variable is missing (or this is a test harness
    // with no real CSS); keep the compiled-in default rather than blanking the
    // colour, which Chart.js would render as transparent.
    if (value) C[key] = value;
  }
  return C;
}
const TOKEN_COLORS = {
  input:          'rgba(72,160,199,0.85)',   // blue
  output:         'rgba(217,119,87,0.85)',    // accent / coral
  cache_read:     'rgba(116,201,145,0.75)',   // green
  cache_creation: 'rgba(217,168,78,0.75)',    // amber
};
// Hover lifts on a dark theme: bars/series go to full opacity (a touch brighter).
const TOKEN_HOVER = {
  input:          'rgba(72,160,199,1)',
  output:         'rgba(217,119,87,1)',
  cache_read:     'rgba(116,201,145,1)',
  cache_creation: 'rgba(217,168,78,1)',
};
// Donut / categorical palette — warm, Anthropic-leaning (clay, tan, sage, dusty
// blue, mauve, ochre, taupe, terracotta) rather than a saturated rainbow.
const MODEL_COLORS = ['#D97757','#C9A26B','#7FA98C','#6E97A8','#B98AA0','#D9A84E','#A88B6A','#C2705A'];

// Subagent type swatches (table tag tint) — warm/neutral, matching the palette.
const AGENT_TYPE_COLORS = {
  'general-purpose':   '#6E97A8',
  'Explore':           '#9B7EC7',
  'Plan':              '#D9A84E',
  'claude-code-guide': '#48A0C7',
  'auto-compact':      '#A88B6A',
  'unknown':           '#4F4F50',
};
function colorForAgentType(t) {
  return Object.prototype.hasOwnProperty.call(AGENT_TYPE_COLORS, t)
    ? AGENT_TYPE_COLORS[t]
    : '#7FA98C';
}
function fmtDuration(ms) {
  if (!ms || ms < 0) return '—';
  const s = Math.round(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return r ? `${m}m${r}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h${m % 60}m`;
}

// Tooltip color swatches: solid fill, no border (Chart.js's default draws a
// bordered box that looked offset/inconsistent). Lines use their solid stroke
// color instead of the translucent area fill.
// Whether the vendored Chart.js runtime actually loaded.
//
// **This is a SUPPORTED state, not a broken install.** `/assets/chart.umd.js`
// answers 404 whenever `dashboard.find_chart_file()` returns None, and that
// refusal is deliberate: it fires when the file is absent OR when its SHA-256
// does not match `dashboard.CHART_JS_SHA256`, which a test exists to pin. A
// partially written vendor file, a stale copy found first by `asset_roots()`,
// or a delivery surface that dropped it all land here.
//
// The three writes below used to be unguarded, and every part of the page is
// concatenated into ONE classic script -- so `Chart is not defined` threw here
// and aborted the whole script. Everything from `30-ranges.js` onward never ran
// its top-level code: `start()` was never called, no data was ever fetched, and
// the page sat at "Loading..." with nothing on screen saying why. Not a
// degraded chart -- a dead dashboard, from a missing decoration.
//
// Guarded, the tables, stat tiles, filters, exports and quota panel all work;
// only the five canvases are lost, and `chartUnavailable()` says so in place.
const CHARTS_AVAILABLE = typeof Chart !== 'undefined' && Chart !== null;

if (CHARTS_AVAILABLE) {
  Chart.defaults.color = C.axis;
  // multiKeyBackground defaults to white and is drawn behind each tooltip swatch,
  // peeking out as a thin white border on plain-box charts — make it transparent.
  Chart.defaults.plugins.tooltip.multiKeyBackground = 'transparent';
  Chart.defaults.plugins.tooltip.callbacks.labelColor = (ctx) => {
    const ds = ctx.dataset || {};
    let col = Array.isArray(ds.backgroundColor) ? ds.backgroundColor[ctx.dataIndex] : ds.backgroundColor;
    if (ds.type === 'line') col = ds.borderColor;
    return { borderColor: col, backgroundColor: col, borderWidth: 0 };
  };
}

// Legend visibility must survive repaints (filter changes, auto-refresh, sort) —
// the charts are destroyed and rebuilt each render, which otherwise resets any
// series the user toggled off. We track hidden series by label per chart and
// reapply on rebuild: dataset charts via `dataset.hidden`, the doughnut via
// per-slice data visibility (see applyModelHidden).
const hiddenSeries = { daily: new Set(), hourly: new Set(), project: new Set(), model: new Set(), subagent: new Set() };
function legendToggle(key) {
  return (e, item, legend) => {
    const ci = legend.chart;
    const ds = ci.data.datasets[item.datasetIndex];
    ds.hidden = !ds.hidden;
    if (ds.hidden) hiddenSeries[key].add(ds.label); else hiddenSeries[key].delete(ds.label);
    ci.update();
  };
}
