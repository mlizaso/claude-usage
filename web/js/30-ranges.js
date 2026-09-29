// ── Time range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = { 'today': 'Today', 'week': 'This Week', 'month': 'This Month', 'prev-month': 'Previous Month', '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'ytd': 'Year to Date', 'all': 'All Time', 'limit-week': 'This Weekly Limit' };
const RANGE_TICKS  = { 'today': 1, 'week': 7, 'month': 15, 'prev-month': 15, '7d': 7, '30d': 15, '90d': 13, 'ytd': 12, 'all': 12 };
const VALID_RANGES = Object.keys(RANGE_LABELS);

// Local calendar date as YYYY-MM-DD. NOT toISOString(), which formats in UTC and
// shifts the day back in UTC+ timezones (that was the "This Month" bug, #151).
function localISODate(d) {
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

function rangeIncludesToday(range) {
  if (range === 'all') return true;
  const { start, end } = getRangeBounds(range);
  const today = localISODate(new Date());
  if (start && today < start) return false;
  if (end && today > end) return false;
  return true;
}

// The CURRENT weekly quota window, as local day bounds, or null.
//
// Read from the plan payload rather than computed from the calendar: a quota
// week is not a calendar week. It ends at the window's own `resets_at` and
// begins seven days before that.
// Bucketing it Monday-to-Sunday would report a different set of turns from the
// percentage the gauge shows, which is the whole thing this range exists to
// line up.
//
// `weekly` is chosen by GROUP, not by kind: the live endpoint calls the
// all-models window `weekly_all` and the cache has called it `seven_day`, while
// both report `group: 'weekly'`. Keying on the kind would have this option
// break the next time the upstream name changes -- the same trap the window
// identity work exists to avoid. A model-SCOPED weekly is skipped: it measures
// one model, not the week.
function weeklyLimitBounds(info) {
  const windows = (info && info.windows) || [];
  const weekly = windows.find(w => w && w.group === 'weekly' && !w.scope)
    || windows.find(w => w && w.group === 'weekly');
  if (!weekly || !weekly.resets_at) return null;
  const end = new Date(weekly.window_end || weekly.resets_at);
  if (!Number.isFinite(end.getTime())) return null;
  // The server projects a fresh window when a cached reset has passed. Keep
  // following it if this payload remains open through another reset. A quota
  // week is 168 elapsed hours, matching account.current_window_bounds; local
  // calendar subtraction changes that duration at a daylight-saving boundary.
  const duration = 7 * 24 * 60 * 60 * 1000;
  const now = Date.now();
  if (end.getTime() <= now) {
    const elapsed = Math.floor((now - end.getTime()) / duration) + 1;
    end.setTime(end.getTime() + elapsed * duration);
  }
  const start = new Date(end.getTime() - duration);
  // Local ISO days, because every rollup on this page is bucketed by the
  // viewer's local day. The reset instant is UTC; converting it to a local day
  // is what makes the two comparable.
  return { start: localISODate(start), end: localISODate(end) };
}

function getRangeBounds(range) {
  if (range === 'all') return { start: null, end: null };
  if (range === 'limit-week') {
    const bounds = weeklyLimitBounds(lastPlanInfo);
    // No weekly window reported: fall back to the last 7 days rather than to
    // nothing. An empty dashboard would read as "you used nothing this week",
    // which is a different and much worse claim than "we do not know where your
    // quota week starts".
    if (bounds) return bounds;
    const d = new Date();
    d.setDate(d.getDate() - 6);
    return { start: localISODate(d), end: null };
  }
  const today = new Date();
  const iso = localISODate;
  if (range === 'today') {
    const t = iso(today);
    return { start: t, end: t };
  }
  if (range === 'week') {
    const day = today.getDay();
    const diffToMon = day === 0 ? 6 : day - 1;
    const mon = new Date(today); mon.setDate(today.getDate() - diffToMon);
    const sun = new Date(mon); sun.setDate(mon.getDate() + 6);
    return { start: iso(mon), end: iso(sun) };
  }
  if (range === 'month') {
    const start = new Date(today.getFullYear(), today.getMonth(), 1);
    const end = new Date(today.getFullYear(), today.getMonth() + 1, 0);
    return { start: iso(start), end: iso(end) };
  }
  if (range === 'prev-month') {
    const start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
    const end = new Date(today.getFullYear(), today.getMonth(), 0);
    return { start: iso(start), end: iso(end) };
  }
  if (range === 'ytd') {
    // January 1st of the current LOCAL year through today. The end is left open
    // for the same reason the rolling windows leave theirs open: a turn recorded
    // after these bounds were computed must not be clipped out of "this year".
    return { start: iso(new Date(today.getFullYear(), 0, 1)), end: null };
  }
  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date();
  // days - 1: the window is inclusive of both ends, so subtracting the full N
  // would span N+1 calendar days — "Last 7 Days" drew 8 bars and totalled 8
  // days of spend under a label promising 7.
  d.setDate(d.getDate() - (days - 1));
  return { start: iso(d), end: null };
}

// The concrete days a range covers, as they should be printed.
//
// The rolling windows and YTD carry an OPEN end in getRangeBounds purely so a
// turn recorded mid-render can't be clipped; today IS their definitional last
// day, so that is what the label states. "All Time" has no definition of its
// own, so it borrows the extent of the data actually on screen. Returns null
// when no honest span can be given (All Time with no rows).
// The days that may BOUND a span, sorted. One rule, deliberately expressed as
// `dayToLocalDate(d) !== null` rather than as a second copy of its shape regex:
// every consumer of an endpoint parses it with that function, so a key it rejects
// must not be picked as one. The two used to disagree — the copy here matched on
// shape alone, so the shape-valid, calendar-impossible '2026-13-45' localdays.py
// can emit was chosen as the All Time extent and `dayToLocalDate` then placed it
// on 2027-02-14, giving a 198-day fill running months into the future.
//
// A rejected key is excluded from the EXTENT only. It is never dropped from the
// data: the caller merges the fill into its rows, it does not replace them.
function spanBoundDays(dataDays) {
  return (dataDays || []).filter(d => dayToLocalDate(d) !== null).sort();
}

function rangeSpan(range, dataDays) {
  const { start, end } = getRangeBounds(range);
  if (start === null && end === null) {
    const days = spanBoundDays(dataDays);
    if (!days.length) return null;
    return { start: days[0], end: days[days.length - 1] };
  }
  return { start, end: end || localISODate(new Date()) };
}

// "Last 30 Days (Jul 8 – Aug 6)". Falls back to the bare name whenever the span
// cannot be stated truthfully, so a label is never wrong — only sometimes less
// specific.
//
// Note "This Week"/"This Month"/"Previous Month" print their whole filter
// window, so mid-August "This Month" reads "(Aug 1 – Aug 31)" even though the
// month has not finished. That is the window the figures were filtered by, which
// is what a label on a filtered figure should say.
function rangeLabelWithDates(range, dataDays) {
  const base = RANGE_LABELS[range] || String(range || '');
  const span = rangeSpan(range, dataDays);
  const text = span ? fmtDaySpan(span.start, span.end) : '';
  return text ? base + ' (' + text + ')' : base;
}

// ── The days a chart of a range must draw ──────────────────────────────────
// Not "the days that have rows" — every calendar day in the window.
//
// The daily series was keyed by the days that carried turns, so a quiet day was
// ABSENT from the array rather than zero in it. Two consequences, and the second
// is what got reported: adjacent bars were not adjacent days (on the real
// database "Last 30 Days" drew 9 bars with Aug 6 missing between Aug 5 and
// Aug 7), and because the array was 9 long rather than 30 the pan window's
// `total > windowSize` was false — so no scrollbar was drawn anywhere and most
// of a 90-day or year-to-date range could not be reached at all.
//
// Two rules decide the span:
//
//   * the end never runs past today. Zero means "no usage that day", which is
//     true of a day that has happened and meaningless about one that has not —
//     "This Month" would otherwise draw three weeks of confident zeros into the
//     future.
//   * "All Time" has no bounds of its own, so it borrows the extent of the data
//     exactly as rangeSpan does. That is also what leaves its label alone:
//     rangeLabelWithDates reads the first and last day on screen for that range
//     and for no other.
//
// Returns null when no span can be given (All Time with no rows).
function dailyFillSpan(range, dataDays) {
  const { start, end } = getRangeBounds(range);
  const today = localISODate(new Date());
  if (start === null && end === null) {
    // Malformed keys are excluded from the EXTENT (localdays.py's COALESCE
    // fallback can emit one) but never dropped from the data — the caller
    // merges, it does not replace. `spanBoundDays` is shared with rangeSpan so
    // the label and the fill cannot pick different endpoints.
    const days = spanBoundDays(dataDays);
    if (!days.length) return null;
    return { start: days[0], end: days[days.length - 1] };
  }
  const stop = (end && end < today) ? end : today;
  return stop < start ? null : { start, end: stop };
}

// ~10 years. Only reachable through a corrupt day key far in the past — the
// longest honest span is All Time, and no range of days this project can produce
// comes near it — but a single bad row must not build a million-element array.
const DAILY_FILL_MAX_DAYS = 3700;

// Every local calendar day from start to end inclusive, as 'YYYY-MM-DD'.
//
// Stepped a calendar day at a time from local midnight rather than by adding
// 86,400,000 ms, so a DST change cannot shift or duplicate a day. Walked
// BACKWARDS from the end so the cap above drops the oldest days, never the
// newest — the recent end is the part anyone is looking at.
function eachLocalDay(startISO, endISO) {
  const from = dayToLocalDate(startISO), to = dayToLocalDate(endISO);
  if (!from || !to || to < from) return [];
  const out = [];
  let d = to;
  while (d >= from && out.length < DAILY_FILL_MAX_DAYS) {
    out.push(localISODate(d));
    d = new Date(d.getFullYear(), d.getMonth(), d.getDate() - 1);
  }
  out.reverse();
  return out;
}

function readURLRange() {
  const p = new URLSearchParams(window.location.search).get('range');
  return VALID_RANGES.includes(p) ? p : '30d';
}

function setRange(range) {
  selectedRange = range;
  const sel = document.getElementById('range-select');
  if (sel) sel.value = range;  // keep the dropdown in sync with programmatic calls
  updateURL();
  applyFilter();
  scheduleAutoRefresh();
  // The range is what decides whether the page polls at all — refreshIntervalMs
  // returns 0 for one that cannot gain rows — so the header has to restate what
  // just happened, exactly as setRefreshSeconds does. Without it the note kept
  // whatever the last caller left: "Auto-refresh every 15m" over a timer this
  // call had just cleared, and, the other way round, a live timer under no note
  // at all. Nothing repaired the first case — with polling off, loadData never
  // runs again.
  updateMetaNote();
}

// Change the polling interval (0 = off), remember it, and re-arm the timer.
// scheduleAutoRefresh and updateMetaNote live in 70-bootstrap.js; both are
// hoisted function declarations, so calling them from here is fine at runtime —
// the same thing setRange already does.
function setRefreshSeconds(seconds) {
  const n = Number(seconds);
  refreshSeconds = REFRESH_OPTIONS.includes(n) ? n : REFRESH_DEFAULT;
  const sel = document.getElementById('refresh-select');
  if (sel) sel.value = String(refreshSeconds);  // keep the dropdown in sync with programmatic calls
  try { localStorage.setItem(REFRESH_KEY, String(refreshSeconds)); } catch (e) {}
  scheduleAutoRefresh();
  updateMetaNote();
}

function setHourlyTZ(mode) {
  hourlyTZ = mode;
  document.querySelectorAll('.tz-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.tz === mode)
  );
  applyFilter();
}
