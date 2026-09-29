
// ── Helpers ────────────────────────────────────────────────────────────────
const APP_CONFIG = Object.freeze(window.APP_CONFIG || { version: '', surface: 'web' });
const HTML_ESCAPES = Object.freeze({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
});

const API_TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;
function readApiTokenFromFragment() {
  const fragment = window.location.hash.startsWith('#')
    ? window.location.hash.slice(1)
    : window.location.hash;
  if (!fragment || fragment.length > 256) return '';
  try {
    const values = new URLSearchParams(fragment).getAll('token');
    return values.length === 1 && API_TOKEN_PATTERN.test(values[0]) ? values[0] : '';
  } catch (e) {
    return '';
  }
}
const API_TOKEN = readApiTokenFromFragment();
const AUTH_FRAGMENT = API_TOKEN ? '#token=' + encodeURIComponent(API_TOKEN) : '';

function esc(s) {
  return String(s).replace(/[&<>"']/g, ch => HTML_ESCAPES[ch]);
}

function configuredCommand(name, fallback) {
  const commands = APP_CONFIG && APP_CONFIG.commands;
  const value = commands && commands[name];
  return typeof value === 'string' && value.length > 0 && value.length <= 256
    ? value : fallback;
}
const APP_COMMANDS = Object.freeze({
  scan: configuredCommand('scan', 'python cli.py scan'),
  diagnose: configuredCommand('diagnose', 'python cli.py stats'),
  reconnect: configuredCommand('reconnect', 'python cli.py url --open'),
});

function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (API_TOKEN) headers.set('X-Claude-Usage-Token', API_TOKEN);
  return fetch(path, {
    ...options,
    headers,
    credentials: 'same-origin',
    cache: 'no-store',
  });
}

// ── State ──────────────────────────────────────────────────────────────────
let rawData = null;
let selectedModels = new Set();
let allModelsList = [];
let selectedRange = '30d';
// ── Source ─────────────────────────────────────────────────────────────────
// Which assistant's usage is on screen. One database holds both, discriminated
// by turns.source, and every rollup in the payload carries it — so filtering
// here is the same mechanism the model filter already uses, not a second one.
//
// Exactly one source is shown at a time, on purpose. Claude is billed per token
// against published rates; Codex here is a subscription with a weekly quota and
// no price at all. A combined "Est. Cost" would be adding a real number to an
// imaginary one.
const SOURCES = Object.freeze(['claude', 'codex']);
const SOURCE_LABELS = Object.freeze({ claude: 'Claude Code', codex: 'Codex' });
let selectedSource = 'claude';
let availableSources = [];
// Turn counts per source, from the cheap /api/sources probe. Used by the
// chooser, which is answered before any payload has been fetched.
let sourceTurns = new Map();
// Whether the models currently in view have published rates. Drives whether the
// page shows money at all — see the Est. Cost tile and the daily chart.
let sourceIsPriced = true;

let charts = {};
let sessionSortCol = 'last';
let modelSortCol = 'cost';
let modelSortDir = 'desc';
let projectSortCol = 'cost';
let projectSortDir = 'desc';
let branchSortCol = 'cost';
let branchSortDir = 'desc';
let lastFilteredSessions = [];
let lastByModel = [];
let lastByProject = [];
let lastByProjectBranch = [];
let lastFilteredDispatches = [];
// The two breakdowns that group the SAME turns as lastByModel by something other
// than the model: how hard the assistant was asked to think, and why each
// response stopped. Kept beside the others so a renderer can be re-run (sort,
// pagination, a later poll) without re-deriving them.
let lastByEffort = [];
let lastByStopReason = [];
let sessionSortDir = 'desc';

// Tables reveal rows in steps: 10 -> 25 -> 50, capped at 50 because rendering
// more than that visibly hurts performance. Past 50 the footer offers a
// "Download CSV to see more" link instead of another in-table step, plus a
// Show less button that resets straight back to 10. Limits persist across
// re-renders so sorting/filtering keeps the user's chosen depth (visible rows
// always reflect the active sort).
const TABLE_STEPS = [10, 25, 50];
const TABLE_MAX = TABLE_STEPS[TABLE_STEPS.length - 1];  // hard cap on in-table rows
// Don't paginate a table that barely exceeds the first step — paging away one or
// two rows just to show a "Show more" button is more annoying than helpful. Below
// this many rows a table always renders in full (no toggle).
const PAGINATE_THRESHOLD = 12;
function nextTableLimit(current, total) {
  for (const s of TABLE_STEPS) {
    if (s > current && s < total) return s;
  }
  return Math.min(total, TABLE_MAX);  // reveal everything, but never past the cap
}
// Rows to actually show: everything when the table is small enough to skip
// paging, otherwise the user's current step.
function shownCount(limit, total) {
  return total <= PAGINATE_THRESHOLD ? total : limit;
}
let modelLimit = TABLE_STEPS[0];
let sessionsLimit = TABLE_STEPS[0];
let projectLimit = TABLE_STEPS[0];
let branchLimit = TABLE_STEPS[0];
let dispatchesLimit = TABLE_STEPS[0];
let hourlyTZ = 'local';  // 'local' or 'utc'

// Charts are destroyed and rebuilt on every render, so Chart.js replays its
// 1000ms entry animation each time — including on every auto-refresh poll and
// every filter tweak. That replay is what reads as "the page keeps resetting".
// Animate the first paint, then never again for the life of the page.
let chartsHaveDrawn = false;
function chartAnimation() { return chartsHaveDrawn ? false : undefined; }

// Chart.js's own `responsive` only resizes the canvas — it never re-evaluates
// tick limits or axis visibility — so the narrow-screen decisions below are
// read fresh each time the option objects are rebuilt, which is every render.
function isNarrowViewport() { return window.innerWidth < 640; }

// ── Daily chart panning ────────────────────────────────────────────────────
// A bar has a floor width. Ninety of them squeezed into one screen are slivers,
// and "All Time" is worse — so past the point where they would fit, the chart
// shows a WINDOW of days and you pan through it.
//
// Panning the data rather than scrolling a wide canvas is what keeps the axes
// still: there is only ever one chart, at the container's width, so the three
// y-axes are drawn in the same place on every frame. Their maxima are pinned to
// the whole range (not the visible slice) while panning, so the scale doesn't
// jump under you either — the bars move, nothing else does.
const DAILY_COL_PX = 44;          // comfortable bar + gap on a pointer device
const DAILY_COL_PX_NARROW = 26;   // a phone gets narrower bars before it pans
// Rough width of the axis gutters, used only to pick a column count before the
// chart exists to measure. Being a few pixels out just shifts that count by one.
const DAILY_GUTTER_PX = 150;
const DAILY_GUTTER_PX_NARROW = 86;

// Two arrays, and the difference matters. `dailyRangeRows` is the range in
// CALENDAR order, every day present — it is what the per-series panel measures
// and what a re-sort re-reads. `lastDailyRows` is the same rows in DISPLAY
// order, which is the calendar order until the reader sorts by a series; the
// window is a slice of that one. Feeding a sorted array back in as the range
// would make the sort permanent.
let dailyRangeRows = [];    // chronological, gap-free; the range itself
let lastDailyRows = [];     // display order; the window is a slice of this
let dailyPanOffset = 0;     // index of the leftmost visible day
let dailyWindowLen = 0;     // days on screen; 0 when everything fits
let dailyPanKey = '';       // identifies the dataset, to know when to re-anchor

// How the daily chart is ordered. 'day' is chronological — the default, and
// always one click away. Any other key is a DAILY_SERIES key, and the direction
// is the whole of the difference between "sort by Output Max" and "…Min": a day
// has one Output value, not two, so the two cells are the same series read from
// opposite ends.
let dailySortKey = 'day';
let dailySortDir = 'asc';

function dailyColumnWidth() {
  return isNarrowViewport() ? DAILY_COL_PX_NARROW : DAILY_COL_PX;
}

// How many days fit at that column width.
function dailyWindowSize(total) {
  const canvas = document.getElementById('chart-daily');
  const wrap = canvas && canvas.parentElement;
  const width = (wrap && wrap.clientWidth) || 900;
  const gutter = isNarrowViewport() ? DAILY_GUTTER_PX_NARROW : DAILY_GUTTER_PX;
  const plot = Math.max(120, width - gutter);
  return Math.max(2, Math.min(total, Math.floor(plot / dailyColumnWidth())));
}

// ── Auto-refresh preference ────────────────────────────────────────────────
// Seconds between polls; 0 means off. Persisted in localStorage rather than the
// URL because the URL describes *what data is shown* (range, models) and is
// meant to be bookmarked and shared — baking a polling interval into a shared
// link would impose the sharer's choice on the reader. It also has to be
// localStorage for the VS Code panel: the extension rebuilds its iframe from a
// bare http://127.0.0.1:<port>/ on every reload, discarding anything
// history.replaceState wrote, while localStorage survives because the extension
// reuses the remembered port.
const REFRESH_KEY = 'cu_refresh_seconds';
const REFRESH_OPTIONS = Object.freeze([0, 15, 30, 60, 300, 900]);
// Off by default: a poll rebuilds every chart and table, and having that happen
// unbidden every 30s while you are reading is the behaviour this control exists
// to end. Turning it on is one click and the choice is remembered.
const REFRESH_DEFAULT = 0;

function loadRefreshSeconds() {
  try {
    const raw = localStorage.getItem(REFRESH_KEY);
    // getItem returns null when the key was never set, and Number(null) is 0 —
    // which is also a meaningful stored value ("off"). Check for the absent key
    // before coercing so the two stay distinguishable.
    if (raw === null) return REFRESH_DEFAULT;
    const seconds = Number(raw);
    return REFRESH_OPTIONS.includes(seconds) ? seconds : REFRESH_DEFAULT;
  } catch (e) {
    return REFRESH_DEFAULT;   // private mode / storage disabled
  }
}
let refreshSeconds = loadRefreshSeconds();

// ── Plan-limit polling ─────────────────────────────────────────────────────
// The plan panel is the one card that answers "right now", so it re-reads the
// quota cache on its own short interval REGARDLESS of the auto-refresh setting
// above. Those two are not in conflict: auto-refresh exists to stop the charts
// and tables rebuilding under the reader, and this panel rebuilds neither — it
// writes into #plan-windows and #plan-note and nothing else.
//
// Refresh quota observations even while usage auto-refresh is disabled, so
// an expired displayed window can be replaced when the cache rolls over.
const PLAN_POLL_SECONDS = 30;
let lastPlanInfo = null;
let planPollTimer = null;

// ── Peak-hour config ───────────────────────────────────────────────────────
// Anthropic throttles Mon–Fri 05:00–11:00 PT. The window is published in
// PACIFIC wall-clock time, and Pacific is UTC-7 for most of the year (PDT) and
// UTC-8 for the rest (PST), so it does NOT sit still in UTC: 12:00–17:59Z in
// summer, 13:00–18:59Z in winter.
//
// It used to be approximated by the fixed set below, which is the PDT
// placement. That was wrong by an hour at each end for every day of the PST
// season — 127 of the 365 days of 2026 — and wrong in BOTH directions at once:
// on 2026-01-15, 12:00Z (04:00 PST, not throttled) was painted red while 18:00Z
// (10:00 PST, throttled) was painted off-peak. It is now resolved per day, by
// asking Intl what the Pacific clock read at that instant, so the band follows
// US DST rather than being pinned to one season — including the historical
// rules, which last moved in 2007 and can move again.
//
// The band was also shaded on all seven days while the published window is
// Mon–Fri, which is the last of the three approximations here and is closed
// below: the weekday is resolved in Pacific, per day, in the same memo.
const PEAK_PT_ZONE = 'America/Los_Angeles';
// 05:00–11:00 is half-open: 05,06,…,10 are inside it and 11 is not, which is
// what the six-hour set below has meant since it was written.
const PEAK_PT_FIRST_HOUR = 5;
const PEAK_PT_LAST_HOUR = 10;

// Mon–Fri, and the weekday is a PACIFIC one — the same calendar the 05:00–11:00
// is quoted in. It is not the viewer's and it is not UTC: 2026-08-10T00:30Z is
// Monday in UTC and in Tokyo, and Sunday 17:30 in Los Angeles, so a UTC or
// viewer weekday would call a Sunday instant a Monday. It is read off the SAME
// instant the hour is read off, in the same zone, which is the only reading that
// cannot disagree with itself.
//
// Written as the five names rather than as a numeric range because the formatter
// below hands back text: mapping 'Mon' to 1 to compare it against 1..5 would add
// a table that can drift from the probes and buy nothing.
const PEAK_PT_WORKDAYS = new Set(['Mon', 'Tue', 'Wed', 'Thu', 'Fri']);

// The PDT placement, kept as the fallback for the two cases that have no date
// to resolve against: a row whose day does not parse, and a runtime whose Intl
// cannot resolve PEAK_PT_ZONE (a small-ICU build, or no Intl at all). Being the
// summer placement it is right for the larger part of the year, and it is
// exactly what the whole chart did before the per-day resolution existed — so
// the degraded path is the old behaviour, not a new one.
//
// It is a set of HOURS and carries no weekday, so the degraded path shades all
// seven days exactly as the whole chart used to. That is deliberate for the same
// reason: a runtime that cannot tell you the Pacific hour cannot tell you the
// Pacific weekday either, and "the old behaviour" is the one already described
// in the legend and tested.
const PEAK_HOURS_UTC = new Set([12, 13, 14, 15, 16, 17]);

// [UTC instant, the Pacific hour it must format to]. Three of them, because one
// cannot separate the three ways this can go wrong: tz data the engine does not
// have (`format` throws, or answers in UTC), an engine that ignores `hourCycle`
// and hands back a 12-hour clock (17:00 PT would read as 5 and paint the
// evening red), and tz data too old to know the zone observes DST at all.
//
// There is deliberately NO probe at midnight. A build that answers on the h24
// cycle spells it "24" rather than "00", and such a probe would reject an
// otherwise perfect formatter unless `ptHourOf` folded 24 back to 0 — a pair of
// lines that exist only to satisfy each other, since 24 and 0 are both outside
// 05:00–11:00 anyway and the window is unaffected either way. Add both, or
// neither: a midnight probe without the fold silently costs the whole per-day
// resolution on an h24 engine.
const PEAK_PT_PROBES = Object.freeze([
  [Date.UTC(2026, 6, 15, 12), 5],    // 05:00 PDT — the window's first hour
  [Date.UTC(2026, 6, 15, 0), 17],    // 17:00 PDT — a 12-hour clock says 5 here
  [Date.UTC(2026, 0, 15, 12), 4],    // 04:00 PST — only true if DST is known
]);

// A 12-hour clock's "5 AM" becomes NaN here and fails the probes rather than
// silently mis-shading, which is the whole reason this is not inlined.
function ptHourOf(fmt, at) {
  return Number(fmt.format(at));
}

// The weekday half, probed the same way and for the same reasons. Three probes,
// because three different things can go wrong and no one instant separates them:
// tz data the engine does not have (`format` throws, or answers in UTC), a
// locale it ignored (Spanish 'dom' matches none of PEAK_PT_WORKDAYS and would
// silently unshade the whole chart), and an off-by-a-day reading.
//
// The middle probe is the boundary one and the reason this is not derived from
// the UTC day index: 2026-08-10T00:30Z is Monday in UTC and Sunday in Pacific.
// A formatter that answers 'Mon' there is reading the wrong calendar, and would
// shade Sunday evenings — the exact defect this whole weekday test closes,
// reintroduced one layer down.
const PEAK_PT_WEEKDAY_PROBES = Object.freeze([
  [Date.UTC(2026, 6, 15, 12), 'Wed'],      // 05:00 PDT — a window instant
  [Date.UTC(2026, 7, 10, 0, 30), 'Sun'],   // Monday in UTC, Sunday 17:30 PT
  [Date.UTC(2026, 7, 8, 12), 'Sat'],       // 05:00 PDT on the excluded day
]);

let ptHourFormat;   // undefined until first use; null once known unavailable
function ptHourFormatter() {
  if (ptHourFormat !== undefined) return ptHourFormat;
  ptHourFormat = null;
  try {
    const fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: PEAK_PT_ZONE, hourCycle: 'h23', hour: '2-digit' });
    if (PEAK_PT_PROBES.every(p => ptHourOf(fmt, new Date(p[0])) === p[1])) {
      ptHourFormat = fmt;
    }
  } catch (e) {
    ptHourFormat = null;
  }
  return ptHourFormat;
}

let ptWeekdayFormat;   // undefined until first use; null once known unavailable
function ptWeekdayFormatter() {
  if (ptWeekdayFormat !== undefined) return ptWeekdayFormat;
  ptWeekdayFormat = null;
  try {
    const fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: PEAK_PT_ZONE, weekday: 'short' });
    if (PEAK_PT_WEEKDAY_PROBES.every(
          p => fmt.format(new Date(p[0])) === p[1])) {
      ptWeekdayFormat = fmt;
    }
  } catch (e) {
    ptWeekdayFormat = null;
  }
  return ptWeekdayFormat;
}

// Resolve the Pacific weekday and window once per UTC day. Reuse the result
// across hourly rows and models; keep an empty set for weekends.
const PEAK_DAY_MS = 86400000;
const peakHoursByDay = new Map();
const PEAK_DAY_CACHE_MAX = 4096;   // ~11 years of days; a bound, not a policy

function peakHoursUTCForDayIndex(dayIndex) {
  const hit = peakHoursByDay.get(dayIndex);
  if (hit) return hit;
  const fmt = ptHourFormatter();
  if (!fmt) return PEAK_HOURS_UTC;
  // null where the engine cannot be trusted with a Pacific weekday, in which
  // case the window keeps its (verified) hours and simply is not narrowed to
  // Mon–Fri — degrading to the seven-day band the chart drew before, not to a
  // day with no band at all.
  const wfmt = ptWeekdayFormatter();
  const hours = new Set();
  try {
    const midnight = dayIndex * PEAK_DAY_MS;
    for (let h = 0; h < 24; h++) {
      const at = new Date(midnight + h * 3600000);
      const pt = ptHourOf(fmt, at);
      if (pt < PEAK_PT_FIRST_HOUR || pt > PEAK_PT_LAST_HOUR) continue;
      // Asked per HOUR rather than once per day, which costs the six formats
      // the window is wide (24 -> 30 for a day the memo has not seen) and buys
      // the one thing a once-per-day answer cannot give: it holds even if a
      // day's window instants were ever to straddle a Pacific midnight. They
      // do not today — the earliest UTC hour any window can start at is 12:00Z
      // and Pacific is never east of UTC, so all six share one Pacific date —
      // but that is a fact about the current tz rules, not about this code, and
      // it is the kind of fact this file has been wrong about before.
      if (wfmt && !PEAK_PT_WORKDAYS.has(wfmt.format(at))) continue;
      hours.add(h);
    }
  } catch (e) {
    return PEAK_HOURS_UTC;   // a bar's colour must never throw the render
  }
  if (peakHoursByDay.size >= PEAK_DAY_CACHE_MAX) peakHoursByDay.clear();
  peakHoursByDay.set(dayIndex, hours);
  return hours;
}

// The window at an INSTANT — what the local-mode shading has in hand, and the
// path that must not allocate.
function peakHoursUTCAt(at) {
  return peakHoursUTCForDayIndex(Math.floor(at.getTime() / PEAK_DAY_MS));
}

// The window on a 'YYYY-MM-DD' UTC day. The one-slot memo in front of the parse
// is not premature: the hourly rollup arrives ordered by day, so consecutive
// rows ask about the same one, and without it every row in UTC mode pays a
// regex it does not need.
//
// `null`, not '': the sentinel has to be a value `String(utcDay)` can never
// produce, and '' is exactly what a row with no day at all stringifies to — it
// matched the sentinel, skipped the parse, and resolved the window against a
// stale index (day 0, 1970-01-01) instead of falling back.
let lastPeakDayKey = null;
let lastPeakDayIndex = 0;
function peakHoursUTCOn(utcDay) {
  const key = String(utcDay);
  if (key !== lastPeakDayKey) {
    // Match the shape rather than coercing: '' and 'not-a-date' slice down to
    // values Number() turns into 0, which would resolve the window against a
    // long-dead offset in year 0 instead of saying "no idea".
    //
    // This used to add "for the reason hourlyInFrame does", and that pointer
    // went stale on 2026-08-16: `hourlyInFrame` and `localHourInstant` now gate
    // on `dayToLocalDate`'s round trip, because a shape-valid but
    // calendar-impossible key was NORMALISED into a real day and moved a row
    // between months depending on the TZ toggle.
    //
    // This one is deliberately left on the shape, and what that costs is
    // measured rather than assumed. What it derives is a UTC DAY INDEX used
    // only to pick which DST window to shade, so an impossible key resolves to
    // the window of the day it normalises onto instead of falling back to
    // `PEAK_HOURS_UTC`. Measured 2026-08-16: `2026-02-31` answers {13..18},
    // byte for byte what `2026-03-03` answers, where the fallback is {12..17} —
    // one bar's difference, PST's placement against PDT's. No row moves bucket
    // and no total changes: this feeds shading and nothing else. Tightening it
    // is a behaviour change nothing has measured a need for, so it is a known
    // limit rather than an oversight.
    const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(key);
    if (!parts) return PEAK_HOURS_UTC;
    lastPeakDayIndex = Math.floor(
      Date.UTC(+parts[1], +parts[2] - 1, +parts[3]) / PEAK_DAY_MS);
    lastPeakDayKey = key;
  }
  return peakHoursUTCForDayIndex(lastPeakDayIndex);
}

// Was `utcHour` on `utcDay` inside the throttled window? Both arguments are
// UTC: the window is a property of the instant, not of the viewer. A day whose
// Pacific weekday is Saturday or Sunday answers false for all 24 of them.
function isPeakUTCHour(utcHour, utcDay) {
  return peakHoursUTCOn(utcDay).has(utcHour);
}

// The same question asked of an instant, which is what a local-mode row is once
// its (day, hour) pair has been resolved back through the viewer's clock.
function instantIsPeak(at) {
  return peakHoursUTCAt(at).has(at.getUTCHours());
}

// Local-timezone offset in hours (signed). Fractional offsets (e.g. India
// UTC+5:30) are FLOORED, not rounded, and that is not a taste: the bars this
// number colours are bucketed by `hourlyInFrame`, which reads `Date.getHours()`
// — and getHours() truncates the fractional part rather than rounding it. In
// Asia/Kolkata 12:00Z (the first peak hour) lands in bucket 17, so rounding to
// +6 asked whether 11:00Z was peak and painted the peak bar off-peak while
// reddening an off-peak one. Flooring reproduces getHours() exactly: for an
// offset k+f with 0 <= f < 1 the bucket is (utcHour + k) mod 24, whatever f is.
// Identical to Math.round on every whole-hour zone, so nothing else moves.
//
// This offset is TODAY'S, which is why it no longer decides which bars are
// shaded. It used to, and that was a defect in its own right: `hourlyInFrame`
// buckets a row through a Date built on the row's OWN date, so it applies the
// offset that was in force *then*, while this one applies the offset in force
// the day the page is opened. The two disagree by an hour for every row on the
// far side of a DST change — in Europe/Madrid on 2026-08-11 (CEST, +2) against
// rows dated 2026-01-15 (CET, +1), the bars at either end of the band swapped
// colours with their neighbours. (That measurement used to be written out as
// "12:00Z, the first throttled hour, painted off-peak, and 18:00Z, throttled in
// no season, painted red", which took the window's UTC placement from the fixed
// PDT set above; in January it is the other way round.) Peak-ness is now decided
// per row, from the row's own date, by `aggregateHourly` in 52-charts.js.
//
// What remains here is the last-resort fallback for a bucket with no rows and no
// parseable day to borrow an offset from, which is why these two functions still
// exist and still floor.
//
// The second waiver that used to be recorded here — that the PT window itself
// was pinned to its PDT placement — is gone: peakHoursUTCOn resolves it per day.
function localOffsetHours() {
  return Math.floor(-new Date().getTimezoneOffset() / 60);
}

// Return the UTC hour (0–23) corresponding to a displayed-hour bucket. One
// direction only: the inverse used to live here too and had no caller once
// `hourlyInFrame` took over resolving rows — and the round-trip test built on
// the pair could not fail, because two functions differing only in the sign of
// the same offset are inverses whatever that offset is. What guards this one is
// the bucketing it has to agree with (TestPeakShadingUsesTheBucketingOffset).
function displayHourToUTC(displayHour, tzMode) {
  if (tzMode === 'utc') return displayHour;
  return ((displayHour - localOffsetHours()) % 24 + 24) % 24;
}

// The shading of a bucket with no date of its own anywhere in view — the last
// resort, and now the only place today's clock decides anything. The window is
// resolved for today rather than pinned to a season, so this degrades to "the
// right window, today's offset" instead of "one season's window, always".
//
// Today's weekday comes with it: opened on a Saturday with no data at all, the
// chart draws no band. That is the honest answer for a chart showing nothing —
// the alternative is a band asserting a throttled window that is not open — and
// it is invisible in practice, since every bucket here is empty by definition.
function isPeakHour(displayHour, tzMode) {
  return isPeakUTCHour(displayHourToUTC(displayHour, tzMode),
                       new Date().toISOString().slice(0, 10));
}

function formatHourLabel(h) {
  return String(h).padStart(2, '0') + ':00';
}

function tzDisplayName(tzMode) {
  if (tzMode === 'utc') return 'UTC';
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'Local';
  } catch(e) {
    return 'Local';
  }
}


// Set once the shared per-window thresholds have been fetched, so the plan
// panel's repaint does not re-request them on every poll.
let alertThresholdsLoaded = false;
