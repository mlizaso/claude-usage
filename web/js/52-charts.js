// ── Chart rendering ────────────────────────────────────────────────────────
// Split out of 50-render.js, which had grown to cover four unrelated jobs:
// the overview tiles, the charts, the tables, and the plan panel. The numeric
// prefix keeps the concatenation order — this is a classic script, so a part
// that moved after its dependants would break at load.
function hourlyInFrame(r, tzMode) {
  if (tzMode === 'utc') return { day: r.day, hour: r.hour };
  // `dayToLocalDate` (20-format.js), NOT a shape regex. The regex that stood
  // here rejected '' and 'not-a-date' — which is what its comment was about,
  // and that part was never wrong — but it ACCEPTED every calendar-impossible
  // day key, and `Date.UTC` normalises one rather than refusing it. This is
  // the one query in the payload `local_day_expr`'s round-trip gate does not
  // cover: `hourly_by_model` ships the raw `substr(timestamp, 1, 10)` prefix
  // on purpose, so `2026-02-31` reaches here verbatim.
  //
  // What that cost was a bucket decided by the TZ TOGGLE. In 'utc' mode the
  // key stays `2026-02-31`, which sorts above every February day and below
  // every March one, so it joins neither month; flip to Local and it became
  // `2026-03-03` and joined March. Measured under node on a two-row fixture
  // (2026-08-16): "This Month (Mar)" totalled 100 output / 1 turn in UTC mode
  // and 5,100 / 10 in Local — 51x, from a control that only re-frames.
  // `2026-02-29` -> `2026-03-01` and `2026-04-31` -> `2026-05-01` did the
  // same, and `0000-01-01` — a key this build really can emit — became
  // `1900-01-01`, because `new Date(0, 0, 1)` remaps years 0-99 into the
  // 1900s.
  //
  // Used as a GATE, not as the instant: this frame needs a UTC instant and
  // `dayToLocalDate` answers with a local midnight. Its components are the
  // key's own three by definition — that is what surviving the round trip
  // means — so reading them back off it is exact for every real day and is
  // the one way to avoid a fourth copy of the regex.
  const valid = dayToLocalDate(r.day);
  if (!valid) return { day: r.day, hour: r.hour };
  const at = new Date(Date.UTC(valid.getFullYear(), valid.getMonth(),
                               valid.getDate(), r.hour));
  if (Number.isNaN(at.getTime())) return { day: r.day, hour: r.hour };
  return { day: localISODate(at), hour: at.getHours() };
}

// The INSTANT a framed local (day, hour) pair denotes — the inverse of the
// resolution `hourlyInFrame` just did, run against the SAME date.
//
// Recovered rather than carried, and that is not just because `applyFilter`
// (40-filters.js) spreads `{...r, day: framed.day, hour: framed.hour}` and keeps
// nothing else `hourlyInFrame` returns. The OTHER caller — `hourlyBucketIsPeak`,
// the shading of a bucket that holds no rows — has no row to carry anything on,
// so an exact inverse has to exist whatever the row path does. Both callers go
// through this one function precisely so that the bars with data and the bars
// without cannot draw two different bands on one screen; they did, in the 15
// zones below, and that was half the defect.
//
// A LOCAL HOUR IS NOT A LOCAL :00, and forgetting that was the other half.
// `hourlyInFrame` buckets with `Date.getHours()`, which TRUNCATES: in a
// fractional-offset zone the instant a bucket holds sits at :30 (:45 in
// Pacific/Chatham) past the hour, not on it. Rebuilding it at :00 asks about the
// UTC hour BEFORE the one the bar actually holds, so the whole band came out one
// bar late — Asia/Kolkata (+5:30) put 12:00Z, the first throttled hour, in bucket
// 17 and then asked about 11:00Z, painting that bar off-peak while reddening
// bucket 23, which holds 18:00Z and is throttled in no season. So the minute is
// recovered from the offset in force at that wall clock and put back. For an
// offset of k hours + f minutes the bucket is (utcHour + k) mod 24 with the
// remainder f left over, which is exactly what `getHours()` returns and exactly
// what this undoes; on a whole-hour zone f is 0 and nothing moves.
//
// Measured over all 418 zones `Intl.supportedValuesOf('timeZone')` lists, every
// day of 2026, both toggle positions (2026-08-11): 15 zones mis-shaded 522 bars
// each (261 Pacific weekdays x the 2 bars at the ends of the band; 260 for
// Australia/Lord_Howe, whose 30-minute DST shift makes it whole-hour for part of
// the year), and 0 zones after. 'utc' mode was clean throughout and is untouched.
//
// The alternative — carry the row's own UTC day and hour through `applyFilter`
// and drop the round trip for rows — was rejected on measurement, not taste: it
// cannot serve the empty-bucket caller at all, and against this inverse it
// changes no answer anywhere. Over those same 418 zones and 365 days the carried
// truth and this reconstruction agree on 3,661,680 of 3,661,680 rows and
// 3,661,424 of 3,661,424 bars, including every one of the 129 fall-back
// collisions of 2026 (one per affected zone) where two UTC hours genuinely share
// one bucket. Those 129 remain the one known inexactness: JS resolves a repeated
// wall clock to the earlier of the two, so both rows are asked about the earlier
// UTC hour. No pair disagreed about the window in 2026; a pair that did would be
// the reason to carry the hour, and nothing else is.
//
// An instant rather than a UTC hour, because the hour alone cannot say which
// day's window to measure it against: a local hour near either end of the day
// belongs to the UTC day on the other side of midnight, and reading the window
// off the LOCAL day would place the band by the wrong date whenever those two
// fall on opposite sides of a US DST change.
function localHourInstant(day, hour) {
  // Same gate as `hourlyInFrame`, and it has to be the same one: these two are
  // inverses, so a key one of them accepts and the other refuses would draw a
  // band on the bars with rows and a different band on the bars without —
  // which is exactly the split the sub-hour repair above was written to end.
  const valid = dayToLocalDate(day);
  if (!valid) return null;
  const at = new Date(valid.getFullYear(), valid.getMonth(), valid.getDate(),
                      hour);
  if (Number.isNaN(at.getTime())) return null;
  // The sub-hour remainder of the offset in force at that wall clock. Read off
  // the :00 probe rather than off `new Date()`, for the same reason the shading
  // is per row at all: a zone whose fractional offset differs by season (there
  // is exactly one — Australia/Lord_Howe, +10:30 and +11:00) must be measured on
  // the row's own date. Both `%` results are normalised into 0..59 because a
  // negative offset (Pacific/Marquesas, -9:30) yields a negative remainder.
  //
  // Note honestly that only the second of those two is currently observable.
  // Swapping this for `new Date()` was measured over all 418 zones and all of
  // 2026 (2026-08-11) and changed nothing, because substituting a remainder of
  // 30 where the truth is 0 stays inside the same UTC hour — and Lord Howe's
  // whole-hour season is the winter one, so today's clock over-adds rather than
  // under-adds. It bites the other way round between October and April, which no
  // test can reach without controlling the run date; the per-row read is correct
  // by construction rather than by assertion.
  const minute = ((-at.getTimezoneOffset() % 60) + 60) % 60;
  if (minute === 0) return at;
  const exact = new Date(valid.getFullYear(), valid.getMonth(),
                         valid.getDate(), hour, minute);
  return Number.isNaN(exact.getTime()) ? at : exact;
}

// Is this already-framed row's own UTC hour inside the throttled window?
//
// The failure this replaces: the colour used to come from `isPeakHour(bucket)`,
// which converts with ONE offset read off `new Date()` — the offset in force the
// day the page is opened — while the bucket beside it came from the row's own
// date. Measured in Europe/Madrid on 2026-08-11 (CEST, +2) against rows dated
// 2026-01-15 (CET, +1): the bars at either end of the band were shaded by the
// wrong one of the two offsets, in both directions at once, for every "All
// Time" and "Year to Date" view in the 128 of the 418 zones Intl lists whose
// offset differs between January and August. (As first written that
// measurement read "12:00Z, the first throttled hour, painted off-peak, and
// 18:00Z, throttled in no season, painted red" — which took the window's UTC
// placement off the fixed PDT set. In January it is the other way round:
// 12:00Z is 04:00 PST and 18:00Z is 10:00 PST. That was the second defect, and
// it is fixed in 00-core.js.)
//
// In 'utc' mode `hourlyInFrame` left the pair alone, so r.hour IS the UTC hour;
// so it is for a row whose day did not parse, which that function passes through
// unframed.
//
// The window itself is resolved on the row's own UTC DAY (see peakHoursUTCOn in
// 00-core.js), so a January row is measured against PST and a July one against
// PDT, and a Saturday row against an empty window. All three halves of this
// function are therefore per-row: which UTC hour the bar holds, where the
// throttled window sat that day, and whether it was open at all — Mon–Fri, on
// the Pacific calendar, which is not the row's UTC day and not the viewer's.
function hourlyRowIsPeak(r, tzMode) {
  if (tzMode === 'utc') return isPeakUTCHour(r.hour, r.day);
  const at = localHourInstant(r.day, r.hour);
  return at === null ? isPeakUTCHour(r.hour, r.day) : instantIsPeak(at);
}

// The shading of a bucket that holds no rows, and so has nothing of its own to
// decide from. It borrows the offset of the most recent day in view rather than
// today's, so the shaded band stays continuous and stays in the data's frame; a
// view with no parseable day at all falls back to today's, which is what the
// whole chart used to use. Both are invisible — a bucket with no rows draws a
// zero-height bar — but the tooltip still names the hour, so it must not name
// the wrong one.
//
// It reaches the window through the SAME `localHourInstant` the row path uses,
// and that shared call is what makes the two agree rather than a test watching
// them. When the inverse was wrong they were wrong together on the bars borrowing
// a day, and right on a chart with no data at all — which is how one screen came
// to show a band at 17–22 with nothing in view and 18–23 the moment a row
// arrived, in Asia/Kolkata and 14 other zones.
function hourlyBucketIsPeak(hour, refDay, tzMode) {
  if (tzMode === 'utc') {
    return refDay ? isPeakUTCHour(hour, refDay) : isPeakHour(hour, tzMode);
  }
  const at = refDay ? localHourInstant(refDay, hour) : null;
  return at === null ? isPeakHour(hour, tzMode) : instantIsPeak(at);
}

function aggregateHourly(rows, tzMode) {
  const byHour = {};
  for (let h = 0; h < 24; h++) byHour[h] = { turns: 0, output: 0, rows: 0, peak: false };
  const days = new Set();
  for (const r of rows) {
    // Rows arrive already resolved into the display frame by hourlyInFrame.
    const displayHour = r.hour;
    byHour[displayHour].turns  += r.turns  || 0;
    byHour[displayHour].output += r.output || 0;
    byHour[displayHour].rows   += 1;
    // A bucket is a WALL-CLOCK hour, so over a range spanning a DST change it
    // legitimately holds rows from two different UTC hours — one throttled, one
    // not. Shaded when ANY of them is: the marker warns a reader off a window,
    // and hiding half a throttled hour is the worse failure of the two. It widens
    // the band by one bar at each end for such a range, which is true of what
    // those bars contain.
    //
    // The SAME rule carries the Mon–Fri narrowing, and it has to: any range
    // wider than a week collapses weekday and weekend rows into one 24-bucket
    // average, so a window bucket over "Last 30 days" holds ~22 weekday rows
    // that were throttled beside ~8 weekend ones that were not. Shading it is
    // the accurate answer — that
    // hour of the average day genuinely does contain throttled usage — and the
    // alternatives are both wrong: "shade only if ALL rows are throttled"
    // unshades the entire band on every multi-week range, which is the common
    // case and would make the legend a lie; "shade if MOST are" invents a
    // threshold the vendor never published and would flip on the composition
    // of the filter rather than on anything about the window. A weekend-only
    // range is unshaded outright, and a weekday-only range is fully shaded,
    // because then every row in the bucket agrees.
    if (!byHour[displayHour].peak && hourlyRowIsPeak(r, tzMode)) {
      byHour[displayHour].peak = true;
    }
    if (r.day) days.add(r.day);
  }
  const dayCount = days.size;
  const refDay = Array.from(days)
    .filter(d => /^\d{4}-\d{2}-\d{2}$/.test(String(d)))
    .sort()
    .pop();
  const hours = [];
  for (let h = 0; h < 24; h++) {
    hours.push({
      hour:       h,
      avgTurns:   dayCount ? byHour[h].turns  / dayCount : 0,
      avgOutput:  dayCount ? byHour[h].output / dayCount : 0,
      totalTurns: byHour[h].turns,
      peak:       byHour[h].rows
        ? byHour[h].peak
        : hourlyBucketIsPeak(h, refDay, tzMode),
    });
  }
  return { hours, dayCount };
}

// The peak-hour red at a given alpha. Chart.js needs a concrete colour string,
// so the token has to be resolved rather than referenced — C.red is whatever
// the current theme's --red is, re-read from the CSS by syncChartColors (which
// initTheme runs before anything renders). A token that is not a 6-digit hex is
// handed over as given: losing the alpha is better than painting nothing.
function peakBarColor(alpha) {
  const hex = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(String(C.red).trim());
  if (!hex) return C.red;
  const [r, g, b] = hex.slice(1).map(part => parseInt(part, 16));
  return `rgba(${r},${g},${b},${alpha})`;
}

// Charts cannot be drawn without the vendored Chart.js runtime, which is a
// SUPPORTED state -- `/assets/chart.umd.js` answers 404 whenever
// `dashboard.find_chart_file()` refuses the file, including on a SHA-256
// mismatch, which is a deliberate refusal with a test pinning it.
//
// Says so in the canvas's own place rather than leaving an empty box, and
// returns true so each render function can bail before `new Chart`. The rest of
// the page -- tables, tiles, filters, exports, quota -- is unaffected and still
// carries every figure the charts would have drawn.
function chartUnavailable(canvasId) {
  if (CHARTS_AVAILABLE) return false;
  const canvas = document.getElementById(canvasId);
  const wrap = canvas && canvas.parentElement;
  if (wrap && !wrap.querySelector('.chart-missing')) {
    if (canvas) canvas.style.display = 'none';
    const note = document.createElement('div');
    note.className = 'chart-missing muted';
    note.textContent = 'Chart unavailable — the bundled chart library did not '
      + 'load. Every figure it would show is in the tables below.';
    wrap.appendChild(note);
  }
  return true;
}

function renderHourlyChart(agg) {
  const dayCountEl = document.getElementById('hourly-day-count');
  dayCountEl.textContent = agg.dayCount
    ? agg.dayCount + ' day' + (agg.dayCount === 1 ? '' : 's') + ' averaged · ' + tzDisplayName(hourlyTZ)
    : 'No data · ' + tzDisplayName(hourlyTZ);

  // Same rule as the daily chart: the "N days averaged · <zone>" line above is
  // text and survives a missing runtime, so the guard sits after it.
  if (chartUnavailable('chart-hourly')) return;
  const ctx = document.getElementById('chart-hourly').getContext('2d');
  if (charts.hourly) charts.hourly.destroy();

  const labels = agg.hours.map(h => formatHourLabel(h.hour));
  const turns  = agg.hours.map(h => h.avgTurns);
  const output = agg.hours.map(h => h.avgOutput);
  // A PAIR, not one colour: 0.9 at rest and 1.0 on hover is the lift
  // TOKEN_COLORS/TOKEN_HOVER give every other bar on the page, so painting
  // these with a single opaque C.red would theme them and silently drop the
  // lift from this chart alone. .peak-swatch carries the same 0.9 over --card,
  // so the legend swatch and the bars it explains are the same pixel colour.
  const barColors      = agg.hours.map(h => h.peak ? peakBarColor(0.9) : TOKEN_COLORS.input);
  const barHoverColors = agg.hours.map(h => h.peak ? peakBarColor(1)   : TOKEN_HOVER.input);

  charts.hourly = new Chart(ctx, {
    data: {
      labels: labels,
      datasets: [
        {
          type: 'bar',
          label: 'Avg turns / hour',
          hidden: hiddenSeries.hourly.has('Avg turns / hour'),
          data: turns,
          backgroundColor: barColors,
          hoverBackgroundColor: barHoverColors,
          pointStyle: 'rect',
          yAxisID: 'y',
          order: 2,
        },
        {
          type: 'line',
          label: 'Avg output tokens / hour',
          hidden: hiddenSeries.hourly.has('Avg output tokens / hour'),
          data: output,
          borderColor: TOKEN_COLORS.output,
          backgroundColor: 'rgba(217,119,87,0.15)',
          borderWidth: 2,
          pointRadius: 2,
          pointHoverRadius: 4,
          pointHoverBackgroundColor: TOKEN_HOVER.output,
          pointStyle: 'circle',
          pointBackgroundColor: TOKEN_COLORS.output,
          pointBorderColor: TOKEN_COLORS.output,
          tension: 0.3,
          yAxisID: 'y1',
          order: 1,
        },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150, animation: chartAnimation(),
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { onClick: legendToggle('hourly'), labels: { color: C.axis, usePointStyle: true, boxWidth: 8, boxHeight: 8 } },
        tooltip: {
          usePointStyle: true,
          callbacks: {
            title: (items) => {
              if (!items.length) return '';
              const idx = items[0].dataIndex;
              const h = agg.hours[idx];
              const base = formatHourLabel(h.hour) + ' ' + tzDisplayName(hourlyTZ);
              // The hours, not a vendor. One window — Mon–Fri 05:00–11:00 PT,
              // resolved per day by peakHoursUTCOn in 00-core.js — is shaded on
              // whichever source is on screen, so "Anthropic US hours" labelled
              // a Codex chart, under a title reading "Codex Usage" and a footer
              // citing OpenAI's rate card, with a window OpenAI never
              // published. Naming OpenAI there instead would only move the
              // false claim: the shaded hours are still Anthropic's published
              // window, and no equivalent is known for any other provider.
              // What IS true of both is the hours.
              return h.peak ? base + ' · Peak — US business hours' : base;
            },
            label: (item) => {
              if (item.dataset.label && item.dataset.label.indexOf('turns') !== -1) {
                return ' Avg turns: ' + item.parsed.y.toFixed(2);
              }
              return ' Avg output: ' + fmt(item.parsed.y);
            },
          }
        },
      },
      scales: {
        x: { ticks: { color: C.axis, maxRotation: 0, autoSkip: isNarrowViewport(), maxTicksLimit: isNarrowViewport() ? 6 : undefined, font: { size: 10 } }, grid: { color: C.border } },
        y:  { position: 'left',  beginAtZero: true, ticks: { color: C.axis, callback: v => v.toFixed(1) },     grid: { color: C.border }, title: { display: !isNarrowViewport(), text: 'Avg turns / hour',         color: C.axis, font: { size: 11 } } },
        y1: { position: 'right', beginAtZero: true, ticks: { color: C.axis, callback: v => fmt(v) }, grid: { drawOnChartArea: false },   title: { display: !isNarrowViewport(), text: 'Avg output tokens / hour', color: C.axis, font: { size: 11 } } },
      }
    }
  });
}

// One description of the five daily series, used to build the datasets AND to
// refill them while panning. Two lists would be two things to keep in the same
// order, and a mismatch would silently plot Output's numbers under Input's name.
// The cost series is dropped for a source with no published rates, rather than
// drawn as a flat zero line under a $0.00 axis — which would assert that the
// usage was free instead of unpriced.
function dailySeries() {
  return DAILY_SERIES.filter(s => !s.money || sourceIsPriced);
}

// `key` is the series' stable identity — it names the field, keys the sort, and
// is what goes in the URL, so renaming a label cannot break a shared link.
// `money` is what makes the Est. Cost series disappear on an unpriced source
// rather than render a flat zero line: `$0.00` asserts the usage was free, which
// is a different claim from "not priced".
const DAILY_SERIES = Object.freeze([
  { key: 'input',          label: 'Input',          pick: d => d.input,          axis: 'y1', stack: 'io',    color: TOKEN_COLORS.input,          hover: TOKEN_HOVER.input },
  { key: 'output',         label: 'Output',         pick: d => d.output,         axis: 'y1', stack: 'io',    color: TOKEN_COLORS.output,         hover: TOKEN_HOVER.output },
  { key: 'cache_read',     label: 'Cache Read',     pick: d => d.cache_read,     axis: 'y',  stack: 'cache', color: TOKEN_COLORS.cache_read,     hover: TOKEN_HOVER.cache_read },
  { key: 'cache_creation', label: 'Cache Creation', pick: d => d.cache_creation, axis: 'y',  stack: 'cache', color: TOKEN_COLORS.cache_creation, hover: TOKEN_HOVER.cache_creation },
  { key: 'cost',           label: 'Est. Cost',      pick: d => d.cost,           axis: 'y2', line: true, money: true },
]);

// ── Ordering the daily chart ───────────────────────────────────────────────
// Chronological is the default and the reset. Any other order is one series
// read in one direction: 'output.desc' is the biggest Output days first, which
// is what the panel's Max column offers, and 'output.asc' is the same series
// from the other end, which is what its Min column offers. There is no second
// per-day value to sort by — a day has ONE Output figure — and saying so in the
// UI is what stops "Min" reading like a number each day separately carries.
function sortDailyRows(rows) {
  if (dailySortKey === 'day') return rows;
  const series = DAILY_SERIES.find(s => s.key === dailySortKey);
  if (!series) return rows;
  const dir = dailySortDir === 'asc' ? 1 : -1;
  // Ties break by date, so two equal days (very common once the range is
  // zero-filled) keep a stable, readable order instead of an arbitrary one.
  return [...rows].sort((a, b) => {
    const delta = ((series.pick(a) || 0) - (series.pick(b) || 0)) * dir;
    return delta || String(a.day).localeCompare(String(b.day));
  });
}

// Min / mean / max per series, over whatever rows it is given — which is always
// the WHOLE range, never the pan window. The panel exists to show values the
// window is hiding, so measuring the window would defeat it.
//
// Order-independent by construction, so a sort cannot move a figure here.
function dailyStats(rows) {
  return dailySeries().map(s => {
    let min = null, max = null, sum = 0;
    for (const row of rows) {
      const v = s.pick(row) || 0;
      if (min === null || v < min) min = v;
      if (max === null || v > max) max = v;
      sum += v;
    }
    return {
      key: s.key, label: s.label, money: !!s.money, days: rows.length,
      min: min === null ? 0 : min,
      max: max === null ? 0 : max,
      mean: rows.length ? sum / rows.length : 0,
    };
  });
}

// How many of those days actually carried usage. Printed beside the day count
// so a `Min` of 0 reads as "there were quiet days" rather than as a puzzle.
function dailyActiveDays(rows) {
  return rows.filter(r => (r.input || 0) || (r.output || 0)
                       || (r.cache_read || 0) || (r.cache_creation || 0)).length;
}

function renderDailyStats(rows) {
  const host = document.getElementById('daily-stats');
  if (!host) return;
  if (!rows.length) {
    host.innerHTML = '<div class="daily-stats-empty">No usage in this range.</div>';
    return;
  }
  const stats = dailyStats(rows);
  const value = (s, v) => s.money ? fmtCost(v) : fmt(v);
  const active = (key, dir) =>
    dailySortKey === key && dailySortDir === dir ? ' active' : '';
  const cell = (s, dir, v) =>
    `<td><button type="button" class="daily-sort-cell${active(s.key, dir)}"` +
    ` data-daily-sort="${s.key}.${dir}"` +
    ` title="Sort the chart by ${esc(s.label)}, ${dir === 'asc' ? 'lowest' : 'highest'} first">` +
    `${esc(value(s, v))}</button></td>`;
  const rowHTML = s => `<tr>
      <th scope="row"><button type="button" class="daily-sort-name${dailySortKey === s.key ? ' active' : ''}" data-daily-sort="${s.key}" title="Sort the chart by ${esc(s.label)} — click again to reverse">${esc(s.label)}</button></th>
      ${cell(s, 'asc', s.min)}
      <td><button type="button" class="daily-sort-cell${dailySortKey === s.key ? ' active' : ''}" data-daily-sort="${s.key}" title="Sort the chart by ${esc(s.label)} — click again to reverse">${esc(value(s, s.mean))}</button></td>
      ${cell(s, 'desc', s.max)}
    </tr>`;
  const days = rows.length;
  const used = dailyActiveDays(rows);
  host.innerHTML = `
    <div class="daily-stats-head">
      <span class="daily-stats-title">Per day</span>
      <button type="button" class="daily-sort-reset${dailySortKey === 'day' ? ' active' : ''}" data-daily-sort="day">Chronological</button>
    </div>
    <table class="daily-stats-table">
      <thead><tr>
        <th scope="col">Series</th>
        <th scope="col" title="Lowest day first">Min &#9650;</th>
        <th scope="col">Mean</th>
        <th scope="col" title="Highest day first">Max &#9660;</th>
      </tr></thead>
      <tbody>${stats.map(rowHTML).join('')}</tbody>
    </table>
    <p class="daily-stats-note">Across all ${days} day${days === 1 ? '' : 's'} in range (${used} with usage) &mdash; the whole range, not only the days on screen.</p>
    <p class="daily-stats-note">Click a figure to sort the chart by that series. A day has one value per series, so Min and Max are the same series read from opposite ends.</p>`;
}

// Apply a sort spec: 'day' resets to chronological, 'output.desc' / 'output.asc'
// set a direction outright, and a bare 'output' toggles — which is what the
// series name and the Mean cell offer, so "click again to reverse" exists
// without pretending Min and Max are two different per-day numbers.
function setDailySort(spec) {
  const [key, mode] = String(spec == null ? '' : spec).split('.');
  if (key === 'day') {
    dailySortKey = 'day';
    dailySortDir = 'asc';
  } else {
    if (!DAILY_SERIES.some(s => s.key === key)) return;
    dailySortDir = (mode === 'asc' || mode === 'desc')
      ? mode
      : (dailySortKey === key && dailySortDir === 'desc' ? 'asc' : 'desc');
    dailySortKey = key;
  }
  updateURL();
  // Re-read the RANGE, not the rows currently on screen — those are already in
  // the previous order, and sorting a sorted array would make the old order
  // permanent.
  renderDailyChart(dailyRangeRows);
}

function readURLDailySort() {
  const raw = new URLSearchParams(window.location.search).get('sort');
  const m = /^([a-z_]+)\.(asc|desc)$/.exec(String(raw || ''));
  const fallback = { key: 'day', dir: 'asc' };
  if (!m || !DAILY_SERIES.some(s => s.key === m[1])) return fallback;
  return { key: m[1], dir: m[2] };
}

// The largest value an axis has to show across the WHOLE range, so a pinned
// scale covers every day you can pan to. Stacked series are summed, because
// that is what the bar's height actually is; series the user has toggled off
// are excluded, so hiding a big one still lets the rest fill the chart.
// Returns null when the axis has nothing visible — then it is left to autoscale.
function dailyAxisMax(rows, axis) {
  const series = dailySeries().filter(s => s.axis === axis && !hiddenSeries.daily.has(s.label));
  if (!series.length || !rows.length) return null;
  let peak = 0;
  for (const row of rows) {
    const stacks = Object.create(null);
    for (const s of series) {
      const key = s.stack || s.label;
      stacks[key] = (stacks[key] || 0) + (s.pick(row) || 0);
    }
    for (const key in stacks) if (stacks[key] > peak) peak = stacks[key];
  }
  // A touch of headroom so the tallest bar isn't flush with the top gridline.
  return peak > 0 ? peak * 1.05 : null;
}

// The daily chart's own legend handler: what `legendToggle` does, and then the
// re-pin. This is the one chart whose axes carry an explicit `max` (see `pin`
// below), and the shared helper only sets `dataset.hidden` and calls `update()`
// — so hiding the biggest series left the axis at a maximum nothing remaining
// reaches and flattened the survivors to a sliver, which is the opposite of what
// dailyAxisMax's comment above promises. Measured in Chrome on the default
// 30-day view: 4.5px of a 210.7px plot instead of 200.4px.
//
// Recomputing all three axes covers UNhiding too, so a restored series is not
// clipped by a maximum computed while it was gone; `dailyAxisMax` returns null
// where an axis has nothing left, which restores autoscale rather than pinning
// it to nothing. It reads dailyRangeRows — the WHOLE range, not the window — or
// the pin would stop covering the days you can pan to.
//
// Deliberately not a `key === 'daily'` branch inside legendToggle: that helper
// is shared with three other charts — hourly, project and subagent — none of
// which pin, and 20-format.js loads before this file, so the generic layer
// would have to reach forward into daily-only state. Three, not five: the five
// keys of `hiddenSeries` are a different set, counting this chart itself and
// the model doughnut, which toggles slices rather than datasets and so carries
// its own inline onClick. Deliberately not a re-render either — destroying the
// chart from inside Chart.js's own event dispatch leaves core running on the dead
// instance (`notifyPlugins('afterEvent', …)` and `if (changed) this.render()`).
function dailyLegendToggle(e, item, legend) {
  const ci = legend.chart;
  const ds = ci.data.datasets[item.datasetIndex];
  ds.hidden = !ds.hidden;
  if (ds.hidden) hiddenSeries.daily.add(ds.label); else hiddenSeries.daily.delete(ds.label);
  // Only while panning. A range that fits carries `max: null` on every axis and
  // autoscales itself, so assigning a number here would start pinning a chart
  // that never was.
  if (dailyWindowLen) {
    for (const axis of ['y', 'y1', 'y2']) {
      ci.options.scales[axis].max = dailyAxisMax(dailyRangeRows, axis);
    }
  }
  ci.update();
}

function renderDailyChart(daily) {
  // `daily` is always the range in calendar order. A sort key whose series is
  // not on screen — Est. Cost on a source with no published rates — cannot be
  // honoured or shown, so it degrades here rather than ordering the chart by a
  // number nothing on it displays. Done before anything reads the state, so the
  // next updateURL drops the stale parameter too.
  if (dailySortKey !== 'day' && !dailySeries().some(s => s.key === dailySortKey)) {
    dailySortKey = 'day';
    dailySortDir = 'asc';
  }
  dailyRangeRows = daily;
  lastDailyRows = sortDailyRows(daily);
  const rows = lastDailyRows;
  const sorted = dailySortKey !== 'day';
  const total = rows.length;
  const windowSize = dailyWindowSize(total);
  const panning = total > windowSize;
  dailyWindowLen = panning ? windowSize : 0;

  // Re-anchor when the dataset itself changes (a new range, a new model filter,
  // a scan that added a day) — that is what you want to see first. An
  // auto-refresh of the same data keeps where you had panned to.
  //
  // The SORT is part of that identity. It changes the order but not the range,
  // the row count, or the first and last day of the underlying data, so without
  // it here a re-sort left the window parked where it was and the days the
  // reader just asked to rank were off-screen.
  const key = [selectedRange, total, rows[0] && rows[0].day,
               rows[total - 1] && rows[total - 1].day, windowSize,
               dailySortKey, dailySortDir].join('|');
  if (key !== dailyPanKey) {
    dailyPanKey = key;
    // Anchor to the interesting end: the most recent days in calendar order,
    // and the top of the ranking in a value order.
    dailyPanOffset = sorted ? 0 : Math.max(0, total - windowSize);
  }
  dailyPanOffset = Math.max(0, Math.min(dailyPanOffset, Math.max(0, total - windowSize)));

  const shown = panning ? rows.slice(dailyPanOffset, dailyPanOffset + windowSize) : rows;
  // Pin the scales only while panning; a range that fits keeps autoscaling
  // exactly as before. dailyAxisMax is order-independent, so a sorted view pins
  // the same maxima as the calendar one.
  const pin = axis => (panning ? dailyAxisMax(rows, axis) : null);

  // Guarded HERE, not at the top of the function: the Min/Mean/Max panel below
  // is plain text and does not need Chart.js, and returning early cost it as
  // well -- a missing chart runtime took a panel that had every figure the
  // chart would have drawn. Only the canvas is genuinely lost.
  if (chartUnavailable('chart-daily')) { renderDailyStats(dailyRangeRows); return; }
  const ctx = document.getElementById('chart-daily').getContext('2d');
  if (charts.daily) charts.daily.destroy();
  charts.daily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: shown.map(d => d.day),
      datasets: dailySeries().map(s => ({
        label: s.label,
        hidden: hiddenSeries.daily.has(s.label),
        data: shown.map(s.pick),
        yAxisID: s.axis,
        ...(s.line
          ? { type: 'line', borderColor: C.accent, backgroundColor: 'transparent',
              pointBackgroundColor: C.accent, pointRadius: 3, tension: 0.3 }
          : { backgroundColor: s.color, hoverBackgroundColor: s.hover, stack: s.stack }),
      })),
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150, animation: chartAnimation(),
      plugins: {
        legend: { onClick: dailyLegendToggle, labels: { color: C.axis, boxWidth: 12 } },
        tooltip: { callbacks: {
          label: item => item.dataset.label === 'Est. Cost'
            ? ` Est. Cost: ${fmtCost(item.raw)}`
            : ` ${item.dataset.label}: ${fmt(item.raw)}`
        }}
      },
      scales: {
        // While panning the window is already sized so every label fits, so the
        // tick limit only has a job when the whole range is on screen. It is
        // also dropped in a value order: RANGE_TICKS thins EVENLY SPACED dates,
        // and in a ranking the bars are no longer in date order — thinning them
        // leaves bars whose day nothing on the axis states.
        x:  { ticks: { color: C.axis, maxTicksLimit: (panning || sorted) ? undefined : (isNarrowViewport() ? 5 : RANGE_TICKS[selectedRange]),
              // '2026-08-05' -> '08-05'; the full date stays in the tooltip.
              callback(v) { const d = String(this.getLabelForValue(v)); return isNarrowViewport() ? d.slice(5) : d; } }, grid: { color: C.border } },
        y:  { position: 'left',  beginAtZero: true, max: pin('y'),  ticks: { color: C.green,  callback: v => fmt(v) },         grid: { color: C.border },          title: { display: !isNarrowViewport(), text: 'Cache',         color: C.green } },
        y1: { position: 'right', beginAtZero: true, max: pin('y1'), ticks: { color: C.blue,   callback: v => fmt(v) },         grid: { drawOnChartArea: false },    title: { display: !isNarrowViewport(), text: 'Input / Output', color: C.blue } },
        // A third axis has nowhere to go on a phone; the cost line and its tooltip
        // survive without it. It also goes where there is no cost series at all:
        // dailySeries() drops that series on a source with no published rates,
        // and an axis with nothing bound to it autoscales 0..1 and prints a
        // fabricated $0.00–$1.00 ladder — the same "this was free" claim the
        // series is dropped to avoid, beside an Est. Cost tile reading n/a.
        //
        // An explicit boolean rather than Chart.js's `display: 'auto'`, which
        // looks like the idiomatic answer to "only when a series uses me": 'auto'
        // also retires the axis when the reader legend-hides Est. Cost on a
        // PRICED source, reflowing the plot 73px on every toggle. That case keeps
        // its (empty) axis, knowingly.
        y2: { display: !isNarrowViewport() && sourceIsPriced, position: 'right', beginAtZero: true, max: pin('y2'), ticks: { color: C.accent, callback: v => '$' + v.toFixed(2) }, grid: { drawOnChartArea: false }, title: { display: true, text: 'Est. Cost', color: C.accent }, offset: true },
      }
    }
  });

  syncDailyPan(total, windowSize, panning);
  // The panel measures the RANGE, not the window — see dailyStats.
  renderDailyStats(dailyRangeRows);
}

// Move the window without rebuilding the chart: only the labels and the five
// data arrays change, so the axes — which are pinned across the whole range —
// are not even recomputed. `update('none')` skips the animation, so panning
// tracks the pointer instead of easing after it.
function setDailyPanOffset(offset) {
  const total = lastDailyRows.length;
  if (!dailyWindowLen || !charts.daily) return;
  const clamped = Math.max(0, Math.min(Math.round(offset), total - dailyWindowLen));
  if (clamped === dailyPanOffset) return;
  dailyPanOffset = clamped;
  const shown = lastDailyRows.slice(dailyPanOffset, dailyPanOffset + dailyWindowLen);
  charts.daily.data.labels = shown.map(d => d.day);
  // Must be the SAME list the chart was built from: this indexes datasets
  // positionally, so panning an unpriced source against the full spec would
  // work only by the accident of Est. Cost being last.
  dailySeries().forEach((s, i) => {
    const ds = charts.daily.data.datasets[i];
    if (ds) ds.data = shown.map(s.pick);
  });
  charts.daily.update('none');
  syncDailyPanPosition();
  updateDailyPanHint();
}

// The scrollbar under the chart is a proxy for the window: its track is as wide
// as the whole range would be at one column per day, so the thumb's size and
// position mean what they look like they mean.
let dailyPanSyncing = false;

function syncDailyPan(total, windowSize, panning) {
  const bar = document.getElementById('daily-pan');
  const track = document.getElementById('daily-pan-track');
  const wrap = document.getElementById('sec-daily');
  if (!bar || !track) return;
  bar.hidden = !panning;
  if (wrap) wrap.classList.toggle('chart-pannable', panning);
  if (!panning) { updateDailyPanHint(); return; }
  // The scrollable DISTANCE has to be one column per day you can pan past, so
  // the track is that distance plus one bar-width of visible track. Sizing it
  // as `total * column` instead looks right but is short by the width of the
  // axis gutters — the strip spans the card while the plot does not — and the
  // last few days become unreachable from the scrollbar.
  //
  // Stated as `calc(100% + Npx)` rather than measured: a percentage resolves
  // against the bar's own content box every time it is laid out, so
  // scrollWidth - clientWidth is N whatever the bar's width turns out to be.
  // Reading `bar.clientWidth` here instead sampled it ONCE, before the panel
  // beside the plot had rendered — measured at 390px the track was sized from
  // 336 while the settled bar was 357, max scrollLeft came to 20.19 columns,
  // and the scrollbar stopped one day short of the newest one on first paint.
  //
  // It is also the whole of what stops a RESIZE rewinding the window, which is
  // the second thing a "simplification" back to a measured width would break.
  // The strip is laid out at its new width ~200ms before the debounced
  // re-render re-sizes the track, so a sampled track is suddenly too short: the
  // browser clamps scrollLeft, the clamp arrives as a scroll event, and
  // onDailyPanScroll cannot tell it from a drag. Measured in Chrome with the
  // sampled form restored — 390→640px on a 90-day range moved the window from
  // Aug 1 – Aug 9 to Jun 23 – Jul 1 and left it there, and 900→940px did the
  // same with the column width unchanged. With `calc` the scroll distance is N
  // at either width, so there is nothing to clamp.
  const maxOffset = Math.max(0, total - windowSize);
  track.style.width = 'calc(100% + ' + (maxOffset * dailyColumnWidth()) + 'px)';
  syncDailyPanPosition();
  updateDailyPanHint();
}

function syncDailyPanPosition() {
  const bar = document.getElementById('daily-pan');
  if (!bar || bar.hidden) return;
  dailyPanSyncing = true;
  bar.scrollLeft = dailyPanOffset * dailyColumnWidth();
  // Cleared on the next frame: assigning scrollLeft fires a scroll event
  // asynchronously, and reading it back as user input would fight the pointer.
  requestAnimationFrame(() => { dailyPanSyncing = false; });
}

function onDailyPanScroll() {
  if (dailyPanSyncing) return;
  const bar = document.getElementById('daily-pan');
  if (!bar) return;
  setDailyPanOffset(bar.scrollLeft / dailyColumnWidth());
}

// Move the window from the keyboard. Panning was pointer-only, and once the
// range is gap-filled the window hides most of a 90-day or year-to-date range —
// so "pointer-only" meant "unreachable without a mouse". Returns whether the key
// was one of ours, which is what lets the caller preventDefault exactly then and
// leave every other key to the page.
function dailyPanByKey(key) {
  if (!dailyWindowLen) return false;
  const max = Math.max(0, lastDailyRows.length - dailyWindowLen);
  let next;
  if (key === 'ArrowLeft')       next = dailyPanOffset - 1;
  else if (key === 'ArrowRight') next = dailyPanOffset + 1;
  else if (key === 'PageUp')     next = dailyPanOffset - dailyWindowLen;
  else if (key === 'PageDown')   next = dailyPanOffset + dailyWindowLen;
  else if (key === 'Home')       next = 0;
  else if (key === 'End')        next = max;
  else return false;
  setDailyPanOffset(next);   // clamps
  return true;
}

// What the sort is, in words, for the hint under the chart.
function dailySortNote() {
  const series = DAILY_SERIES.find(s => s.key === dailySortKey);
  if (!series) return '';
  return 'sorted by ' + series.label
       + (dailySortDir === 'desc' ? ', highest first' : ', lowest first');
}

// The one line that tells the reader there is more chart than they can see.
// Deliberately always present rather than a hover tooltip: this was reported as
// "there is no scrollbar", by someone who had looked above and below the chart
// for one.
function updateDailyPanHint() {
  const hint = document.getElementById('daily-pan-hint');
  if (!hint) return;
  const total = lastDailyRows.length;
  if (!total) { hint.textContent = ''; return; }
  const bits = [];
  if (dailyWindowLen) {
    bits.push(`Showing ${dailyWindowLen} of ${total} days`);
    // Only in calendar order: in a ranking the window is not a date span, and
    // printing one would claim a contiguity the bars do not have.
    if (dailySortKey === 'day') {
      const first = lastDailyRows[dailyPanOffset];
      const last = lastDailyRows[Math.min(total - 1, dailyPanOffset + dailyWindowLen - 1)];
      const span = first && last ? fmtDaySpan(first.day, last.day) : '';
      if (span) bits.push(span);
    }
    bits.push('drag the chart, use the scrollbar below, or focus it and press ← → Home End');
  } else {
    bits.push(`Showing all ${total} day${total === 1 ? '' : 's'}`);
  }
  if (dailySortKey !== 'day') bits.push(dailySortNote());
  hint.textContent = bits.join(' · ');
}

function renderModelChart(byModel) {
  if (chartUnavailable('chart-model')) return;
  const ctx = document.getElementById('chart-model').getContext('2d');
  if (charts.model) charts.model.destroy();
  if (!byModel.length) { charts.model = null; return; }
  charts.model = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: byModel.map(m => m.model),
      datasets: [{ data: byModel.map(m => m.input + m.output), backgroundColor: MODEL_COLORS, hoverBackgroundColor: MODEL_COLORS, hoverOffset: 8, borderWidth: 2, borderColor: C.card, hoverBorderColor: C.card }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150, animation: chartAnimation(),
      plugins: {
        legend: {
          position: 'bottom',
          // Model ids are long and this legend sits under the doughnut, so on a
          // phone it gets a smaller box and type rather than a truncation rule
          // that could disagree with the tooltip.
          labels: { color: C.axis, boxWidth: isNarrowViewport() ? 9 : 12,
                    font: { size: isNarrowViewport() ? 10 : 11 } },
          onClick: (e, item, legend) => {
            const ci = legend.chart;
            ci.toggleDataVisibility(item.index);
            const label = ci.data.labels[item.index];
            if (!ci.getDataVisibility(item.index)) hiddenSeries.model.add(label); else hiddenSeries.model.delete(label);
            ci.update();
          },
        },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${fmt(ctx.raw)} tokens` } }
      }
    }
  });
  // Reapply any slices the user toggled off in a previous render.
  byModel.forEach((m, i) => {
    if (hiddenSeries.model.has(m.model) && charts.model.getDataVisibility(i)) charts.model.toggleDataVisibility(i);
  });
  charts.model.update();
}

function renderProjectChart(byProject) {
  if (chartUnavailable('chart-project')) return;
  const top = byProject.slice(0, 10);
  const ctx = document.getElementById('chart-project').getContext('2d');
  if (charts.project) charts.project.destroy();
  if (!top.length) { charts.project = null; return; }
  charts.project = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: top.map(p => { const cut = isNarrowViewport() ? 16 : 22; return p.project.length > cut ? '\u2026' + p.project.slice(-(cut - 2)) : p.project; }),
      datasets: [
        { label: 'Input',  hidden: hiddenSeries.project.has('Input'),  data: top.map(p => p.input),  backgroundColor: TOKEN_COLORS.input,  hoverBackgroundColor: TOKEN_HOVER.input },
        { label: 'Output', hidden: hiddenSeries.project.has('Output'), data: top.map(p => p.output), backgroundColor: TOKEN_COLORS.output, hoverBackgroundColor: TOKEN_HOVER.output },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false, resizeDelay: 150, animation: chartAnimation(),
      plugins: { legend: { onClick: legendToggle('project'), labels: { color: C.axis, boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: C.axis, callback: v => fmt(v) }, grid: { color: C.border } },
        y: { ticks: { color: C.axis, font: { size: 11 } }, grid: { color: C.border } },
      }
    }
  });
}

function renderSubagentChart(byType) {
  if (chartUnavailable('chart-subagent')) return;
  const ctx = document.getElementById('chart-subagent').getContext('2d');
  if (charts.subagent) charts.subagent.destroy();
  if (!byType.length) { charts.subagent = null; return; }
  charts.subagent = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: byType.map(t => t.agent_type),
      datasets: [
        { label: 'Input',          hidden: hiddenSeries.subagent.has('Input'),          data: byType.map(t => t.input),          backgroundColor: TOKEN_COLORS.input,          hoverBackgroundColor: TOKEN_HOVER.input,          stack: 'tokens' },
        { label: 'Output',         hidden: hiddenSeries.subagent.has('Output'),         data: byType.map(t => t.output),         backgroundColor: TOKEN_COLORS.output,         hoverBackgroundColor: TOKEN_HOVER.output,         stack: 'tokens' },
        { label: 'Cache Read',     hidden: hiddenSeries.subagent.has('Cache Read'),     data: byType.map(t => t.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     hoverBackgroundColor: TOKEN_HOVER.cache_read,     stack: 'tokens' },
        { label: 'Cache Creation', hidden: hiddenSeries.subagent.has('Cache Creation'), data: byType.map(t => t.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, hoverBackgroundColor: TOKEN_HOVER.cache_creation, stack: 'tokens' },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false, resizeDelay: 150, animation: chartAnimation(),
      plugins: {
        legend: { onClick: legendToggle('subagent'), labels: { color: C.axis, boxWidth: 12 } },
        tooltip: { callbacks: {
          label: ctx => ` ${ctx.dataset.label}: ${fmt(ctx.raw)}`,
          footer: items => {
            const total = items.reduce((s, it) => s + it.raw, 0);
            const row = byType[items[0].dataIndex];
            // Both figures in one line, so both go through a formatter — the
            // raw count read "1.23M · 45678 turns".
            return ` Total: ${fmt(total)} · ${fmt(row.turns)} turns`;
          }
        } }
      },
      scales: {
        x: { stacked: true, ticks: { color: C.axis, callback: v => fmt(v) }, grid: { color: C.border } },
        y: { stacked: true, ticks: { color: C.axis, font: { size: 11 } }, grid: { color: C.border } },
      }
    }
  });
}

