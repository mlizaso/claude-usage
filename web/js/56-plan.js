// ── Plan limits ────────────────────────────────────────────────────────────
// The subscription/quota panel, for either assistant. Split out of
// 50-render.js.
//
// Both sources project into the same window shape on the server precisely so
// this renders either one — what differs is where the figure came from, which
// the footnote states, not what it means. (A second copy of this header used to
// follow, saying the panel "reads Claude Code's own cached view": true of one
// source, and the same Claude-only claim the card's prose kept making on the
// Codex page.)
//
// Unlike the Rate Limits table below — which reconstructs incidents a source
// wrote down — this is the one place on the page that can honestly show a
// percentage of a quota, and the one place that has to be explicit about how
// old the reading is.

const PLAN_TIER_NAMES = Object.freeze({
  claude_max: 'Max', claude_pro: 'Pro',
  claude_team: 'Team', claude_enterprise: 'Enterprise',
});

// "Session (5-hour)" / "Weekly — Opus". The kind/group vocabulary is an open set
// (new window types ship without warning), so an unrecognised value falls back
// to its own name rather than being dropped.
function durationWindowLabel(minutes) {
  if (!Number.isFinite(minutes) || minutes <= 0) return 'Limit';
  if (minutes === 10080) return 'Weekly';
  if (minutes === 1440) return 'Daily';
  if (minutes === 300) return 'Session (5-hour)';
  if (minutes % 1440 === 0) return (minutes / 1440) + '-day';
  if (minutes % 60 === 0) return (minutes / 60) + '-hour';
  return minutes + '-minute';
}

function planWindowLabel(w) {
  // The SERVER's label wins when it sent one. `limits_core.window_label` is the
  // one definition of what a window is called, shared by this dashboard and the
  // standalone limits server -- so a window named "Weekly (all models)" in the
  // panel is named that in an alert, in the threshold picker and in the other
  // front end. The branches below are the fallback for a payload older than
  // that field, and must not be allowed to disagree with it: this function
  // called a plain weekly window "Weekly" while the server called it "Weekly
  // (all models)", which is two names for one limit on one screen.
  if (w && typeof w.label === 'string' && w.label) return w.label;
  const kind = w.kind || '';
  const group = w.group || '';
  let base;
  if (kind === 'session' || kind === 'five_hour' || group === 'session') base = 'Session (5-hour)';
  else if (group === 'weekly') base = 'Weekly';
  // Codex names a window by its length in minutes ('10080m'). Render the common
  // durations in words and fall back to hours, rather than showing the raw key.
  else if (/^\d+m$/.test(group)) base = durationWindowLabel(parseInt(group, 10));
  else base = kind.replace(/_/g, ' ') || 'Limit';
  return w.scope ? base + ' — ' + w.scope : base;
}

// Pinned en-US, like every other formatted value on the page. Both the date and
// the time are shown: a weekly window resets days away, so a bare clock time
// would be ambiguous.
const PLAN_RESET_FMT = new Intl.DateTimeFormat('en-US', {
  month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
});
// Rounded to the minute — the rule `alertResetKey` already applies to this same
// value, and for the same reason: `resets_at` jitters sub-second between fetches
// of ONE window. Intl truncates seconds rather than rounding, so readings on
// either side of a minute boundary must be normalized before formatting.
// Rounded on the epoch rather than by reusing alertResetKey: its key string
// carries no zone, and JS reads a bare date-time as LOCAL, which would move the
// displayed time by the viewer's whole UTC offset.
//
// `window_start` / `window_end` come through here too. They inherit the same
// jitter when derived from the reset time; where a start instead came from the
// first turn in the window it is a real instant, and the nearest minute is never
// further from it than the truncated one.
function fmtResetAt(iso) {
  if (!iso) return '';
  const at = new Date(Math.round(Date.parse(iso) / 60000) * 60000);
  return Number.isNaN(at.getTime()) ? '' : PLAN_RESET_FMT.format(at);
}

// Which colour a window's gauge gets. The three names are also CSS class
// suffixes (`sev-${sev}` -> app.css), so this is an allow-list before it is
// anything else: `severity` reaches here from ~/.claude.json or from a Codex
// rollout, neither of which this process wrote. Only these literals may ever be
// returned.
//
// The fallback used to be a flat `normal`, which turned "the source published
// no level" into an affirmative "you are fine". Codex publishes none at all —
// its column is `rate_limit_reached_type`, null until a limit has been HIT — so
// its gauge was green at 100%, beside a Claude panel that goes amber at 75 and
// red at 90 through this same function. Claude's own `five_hour` fallback ships
// a blank level too (account.py), so this is not a Codex special case and must
// not become one: the rule reads `percent` and never asks which assistant
// produced it, which is what lets it live in this deliberately source-agnostic
// file.
//
// These are the page's display bands: normal below 75%, warning from 75%, and
// critical from 90%. They are UI choices, not a published upstream contract.
// Alert thresholds in 58-alerts.js are separate user preferences: a threshold
// chooses when to notify, while these bands choose a gauge's display colour.
const PLAN_SEVERITY_BANDS = Object.freeze([[90, 'critical'], [75, 'warning']]);

// No percentage means nothing to derive from, and green would be a claim: the
// empty string emits no `sev-` class at all, leaving the bar on its own track
// colour. That is the neutral fourth state a three-class stylesheet cannot
// otherwise express.
function planSeverity(w) {
  if (['normal', 'warning', 'critical'].includes(w.severity)) return w.severity;
  const pct = w.percent;
  if (!Number.isFinite(pct)) return '';
  for (const [floor, level] of PLAN_SEVERITY_BANDS) if (pct >= floor) return level;
  return 'normal';
}

function fmtAge(seconds) {
  if (seconds == null) return 'unknown';
  if (seconds < 90) return Math.max(0, Math.round(seconds)) + 's';
  if (seconds < 5400) return Math.round(seconds / 60) + ' min';
  return (seconds / 3600).toFixed(1) + ' h';
}

// Has this window's reset time passed, according to the VIEWER's clock?
//
// The server stamps `expired` when it builds the payload, which is right at that
// instant and wrong from the next second onwards. A page left open renders a
// window state that is as old as the page, so the check is re-run here on every
// paint. The two are OR-ed rather than replaced: the server may know a window is
// over for a reason the timestamp does not carry, and a window can only ever
// become expired, never un-expire.
function windowHasEnded(w, nowMs) {
  if (w.expired) return true;
  if (!w.resets_at) return false;
  const at = Date.parse(w.resets_at);
  return Number.isFinite(at) && at <= (nowMs == null ? Date.now() : nowMs);
}

// How old the reading is, counted from when this page received it rather than
// from the number the server computed. Without this the age is frozen: a panel
// that had said "as of 2 min ago" nine hours earlier went on saying it, which
// made a stale reading look like a fresh one.
function planSampleAge(info, nowMs) {
  if (!info || info.age_seconds == null) return null;
  const now = nowMs == null ? Date.now() : nowMs;
  const received = info._received_at == null ? now : info._received_at;
  return info.age_seconds + Math.max(0, (now - received) / 1000);
}

function renderPlanLimits(info) {
  const card = document.getElementById('sec-plan');
  const jump = document.querySelector('.jump-link[data-target="sec-plan"]');
  if (!card) return;
  // No plan to show: an API-key install, an unreadable config, or a container
  // where ~/.claude.json was never mounted. Hide the card AND its jump link, so
  // the nav never points at something that isn't there.
  if (!info || !info.available || !(info.windows || []).length) {
    card.hidden = true;
    if (jump) jump.hidden = true;
    return;
  }
  card.hidden = false;
  if (jump) jump.hidden = false;

  const tier = document.getElementById('plan-tier');
  if (tier) {
    // The chip names the plan the figures belong to, so it has to follow the
    // source: a Codex Pro subscription is not "Claude Pro".
    const raw = (info.plan_type || '');
    if (info.source === 'codex') {
      tier.textContent = raw ? 'Codex ' + raw.charAt(0).toUpperCase() + raw.slice(1) : 'Codex';
    } else {
      const name = PLAN_TIER_NAMES[raw] || raw.replace(/^claude_/, '');
      tier.textContent = name ? 'Claude ' + name : '';
    }
    tier.title = info.rate_limit_tier || '';
  }

  document.getElementById('plan-windows').innerHTML = info.windows.map(w => {
    const label = planWindowLabel(w);
    const reset = fmtResetAt(w.resets_at);
    // A window whose reset time has passed is stale, not full: the cache keeps
    // reporting the old percentage — 100% and "critical" included — until Claude
    // Code next refreshes it. Rendering that verbatim would tell you that you
    // are throttled when the window has already rolled over.
    if (windowHasEnded(w)) {
      // The cache still describes the window that has already rolled over, and
      // will until Claude Code next refreshes — tens of minutes. Rendering its
      // stale 100% would say "you are throttled" when you are not; rendering
      // only "window ended" says nothing at all while the reader watches their
      // cost climb. Its own reset time tells us when the CURRENT window began,
      // and we know what has been recorded in it, so say that instead.
      const startedAt = fmtResetAt(w.window_start);
      const endsAt = fmtResetAt(w.window_end);
      const done = w.recorded
        ? `${esc(fmt(w.recorded.turns))} turns &middot; ${esc(fmt(w.recorded.tokens))} tokens recorded here`
        : 'no usage recorded here yet';
      if (startedAt) {
        return `<div class="plan-window fresh">
          <div class="plan-window-head"><span class="plan-window-label">${esc(label)}</span>
            <span class="plan-window-pct muted">New window</span></div>
          <div class="plan-bar"><div class="plan-bar-fill sev-expired"></div></div>
          <div class="plan-window-foot">Started ${esc(startedAt)}${endsAt ? ' &middot; resets ' + esc(endsAt) : ''}<br>
            ${done} &middot; Claude Code has not published a percentage for it yet</div>
        </div>`;
      }
      return `<div class="plan-window expired">
        <div class="plan-window-head"><span class="plan-window-label">${esc(label)}</span>
          <span class="plan-window-pct">Window ended</span></div>
        <div class="plan-bar"><div class="plan-bar-fill sev-expired"></div></div>
        <div class="plan-window-foot">Reset at ${esc(reset)} &middot; awaiting refresh</div>
      </div>`;
    }
    // An inactive window with nothing used and no reset time is a slot the plan
    // exposes but isn't metering; a 0% gauge would imply it is.
    if (!w.is_active && !w.percent && !w.resets_at) {
      return `<div class="plan-window idle">
        <div class="plan-window-head"><span class="plan-window-label">${esc(label)}</span>
          <span class="plan-window-pct muted">Not in use</span></div>
      </div>`;
    }
    const pct = w.percent == null ? null : w.percent;
    const sev = planSeverity(w);
    const sevClass = sev ? ' sev-' + esc(sev) : '';
    // The one value on this page that lands in a STYLE ATTRIBUTE rather than in
    // text, so `esc` is not enough for it: escaping the HTML metacharacters
    // still leaves `50;background:url(...)` free to close the declaration and
    // add its own. It is a percentage, so the safe form is the arithmetic one —
    // anything that is not a finite number becomes 0, and the bar renders empty
    // instead of rendering an attacker's CSS. `percent` is numeric on both
    // sources today; this holds if that ever stops being true, which
    // `safejson.safe_dashboard_value` does not guarantee since it passes
    // non-strings through untouched.
    const barPct = Number.isFinite(Number(pct)) ? Math.max(0, Math.min(100, Number(pct))) : 0;
    return `<div class="plan-window">
      <div class="plan-window-head"><span class="plan-window-label">${esc(label)}</span>
        <span class="plan-window-pct${sevClass}">${pct == null ? '—' : esc(pct + '%')}</span></div>
      <div class="plan-bar"><div class="plan-bar-fill${sevClass}" style="width:${barPct}%"></div></div>
      <div class="plan-window-foot">${reset ? 'Resets ' + esc(reset) : 'No reset time reported'}</div>
    </div>`;
  }).join('');

  // Same call for either assistant: both project into this window shape, which
  // is what lets one implementation serve both.
  checkQuotaAlerts(info, info.source || selectedSource || 'claude');
  // The shared file is the source of truth, so it is fetched once and then
  // mirrored locally. Fetched HERE rather than at boot because this is the
  // first point at which a token exists and a window list is known.
  if (!alertThresholdsLoaded) loadWindowThresholds();
  renderAlertPicker(info);

  // Offer the "This Weekly Limit" range only once a weekly window is known,
  // since its bounds are that window's. Revealed here rather than at boot
  // because this is where the payload first arrives.
  const weeklyOption = document.getElementById('range-limit-week');
  if (weeklyOption) weeklyOption.hidden = !weeklyLimitBounds(info);

  const note = document.getElementById('plan-note');
  if (note) {
    const age = planSampleAge(info);
    const stale = age != null && age > 3600;
    note.className = 'plan-note' + (stale ? ' stale' : '');
    note.textContent = info.source === 'codex'
      // Codex stamps its quota onto every response in an append-only transcript,
      // so this is a recorded observation rather than a cache reading — which is
      // why the history below exists at all and Claude's cannot.
      ? 'As of ' + fmtAge(age) + ' ago — the newest quota figure Codex recorded in '
        + 'its transcripts, read at the last scan. Rescan to bring it forward.'
      : 'As of ' + fmtAge(age) + ' ago — read from Claude Code’s local cache on '
        + 'this machine, not a live query. This page re-reads that cache every '
        + (PLAN_POLL_SECONDS) + 's, but Claude Code itself only refreshes it every few '
        + 'tens of minutes, so usage since then is not counted here.'
        + (staleEnoughToHideALimit(age)
           ? ' This reading is old enough that the LIST of limits may be '
             + 'incomplete: a limit your plan has gained since then would not '
             + 'appear here at all. Open Claude Code and let it refresh.'
           : '');
  }
}

// A sufficiently old quota cache can omit windows as well as hold stale
// percentages. Explain this separately from the per-window age indicator.
const PLAN_SHAPE_DOUBT_SECONDS = 6 * 3600;

function staleEnoughToHideALimit(age) {
  return Number.isFinite(age) && age > PLAN_SHAPE_DOUBT_SECONDS;
}

// ── Rate limits ────────────────────────────────────────────────────────────
// Shows when a limit was HIT, never a percentage of one: the notice carries the
// reset time it announced, but never the allowance or the headroom, so any
// "% used" figure here would be invented.
//
// Only Claude Code writes that notice. `route_limit_records` files a record as
// one only when it carries an `event_uuid`, and `codex_transcripts` never emits
// one, so on any other assistant this table is structurally empty. The
// summary's "0 / Times limited, 0.0m / Time blocked" printed that absence as a
// measurement, so a Codex reader who really was throttled last week still read
// 0. The tiles print a dash there instead and the copy below says why: the same
// distinction the page already keeps between `n/a` and `$0.00` for a model with
// no published rate.
//
// Gated on the SOURCE, never on `incidents.length`: a Claude reader with a
// clean range genuinely was not limited, and that zero is a measurement they
// must keep.
function renderLimits(incidents) {
  const summary = document.getElementById('limits-summary');
  const body = document.getElementById('limits-body');
  if (!summary || !body) return;
  const recorded = selectedSource === 'claude';

  const blocked = incidents.reduce((sum, i) => sum + (i.blocked_min || 0), 0);
  const last = incidents.length ? incidents[incidents.length - 1] : null;
  summary.innerHTML = [
    ['Times limited', recorded ? String(incidents.length) : '\u2014'],
    ['Time blocked', !recorded ? '\u2014' : blocked >= 60
      ? (blocked / 60).toFixed(1) + 'h'
      : blocked.toFixed(1) + 'm'],
    ['Most recent', recorded && last ? esc(last.started) : '\u2014'],
  ].map(([label, value]) =>
    `<div class="limit-stat"><b>${esc(value)}</b><span>${esc(label)}</span></div>`
  ).join('');

  renderLimitsCopy();

  // `!recorded` and not just `!incidents.length`: the tiles, the copy and this
  // branch all answer from the SOURCE, as the header above says, and the rows
  // were the one place still answering from the array. Handed a Claude array
  // under a Codex heading — which the in-flight source switch can do, since
  // `selectedSource` flips before the fetch lands — it printed Claude's
  // incidents above a note saying this table stays empty for Codex. Not a
  // filter on the array: `limit_incidents` rows carry no `source` key by
  // contract, so gating them on `inSource` would hard-code 'claude' behind a
  // call that looks like it reads the row, and invert if a second assistant
  // ever wrote a limit notice.
  if (!recorded || !incidents.length) {
    body.innerHTML = '<tr><td colspan="5" class="limits-clear">'
      + (recorded
        ? 'No usage limits reached in this range.'
        : 'Not recorded for ' + esc(SOURCE_LABELS[selectedSource] || selectedSource)
          + ' \u2014 only Claude Code writes the notice this table reconstructs.')
      + '</td></tr>';
    labelCells('limits-body');
    return;
  }
  // Most recent first: what a user checks after being throttled.
  body.innerHTML = incidents.slice().reverse().map(i => {
    const resets = i.reset_hint
      ? esc(i.reset_hint) + (i.reset_zone ? ' <span class="muted">(' + esc(i.reset_zone) + ')</span>' : '')
      : '\u2014';
    return `<tr>
      <td>${esc(i.started)}</td>
      <td class="limit-blocked">${(i.blocked_min || 0).toFixed(1)}m</td>
      <td>${resets}</td>
      <td>${esc((i.projects || []).join(', '))}</td>
      <td>${esc(String(i.notices))}</td>
    </tr>`;
  }).join('');
  labelCells('limits-body');
}

// The card's own tooltip and caveat, which change with what is on screen.
//
// They were static markup in `web/index.html`, and one string cannot serve both
// installs: written for Claude it said the figures come from "Claude Code's own
// cache" one paragraph under a `#plan-note` correctly saying they come from
// Codex's transcripts, and rewritten to name Claude Code unconditionally it
// handed the Claude-only reader — the common install — a clause about an
// assistant they do not have. So the renderer owns it, exactly as
// `renderSourceTitle` owns the title and `renderFooterPricingNote` the rate
// card, and it branches the way `renderStopReasonNote` does for the twin card
// rather than inventing a fourth pattern for the same problem. No vendor
// keyword list is involved: `selectedSource` is what the page already knows.
//
// The three phrases `tests/test_rate_limits.py` asserts against the whole
// assembled template live in both branches — it checks the page, not the file,
// so they have to survive the move and every branch of it.
function renderLimitsCopy() {
  const tip = document.getElementById('limits-info');
  const note = document.getElementById('limits-note');
  // What the card is. innerHTML, like renderStopReasonNote beside it: the <em>s
  // are part of the sentence and textContent would flatten them. `whose` is
  // spelled out on the Claude page because a machine holding both histories has
  // two sets of transcripts, and only one of them feeds this table.
  //
  // Each phrase tests/test_rate_limits.py pins stays inside ONE literal: it
  // greps the assembled template, which carries this file's SOURCE, so a phrase
  // wrapped across a `+` is not in the page as far as that test can see.
  const what = whose => 'Shows when a limit was <em>reached</em>, reconstructed '
    + 'from ' + whose
    + ' &mdash; no figure in this table is a percentage of a quota. ';
  const headroom = 'Your remaining headroom is in <em>Plan Limits</em> above, '
    + "and is only as fresh as the reading its 'as of' line dates.";
  if (selectedSource === 'claude') {
    if (tip) tip.title = 'Times Claude Code reported you had hit a usage limit, '
      + 'with the reset time it announced. The remaining allowance is not in '
      + 'the notices, so it cannot be shown here; that figure is in Plan Limits '
      + 'above.';
    if (note) note.innerHTML = what("Claude Code's transcripts") + headroom;
    return;
  }
  const label = SOURCE_LABELS[selectedSource] || selectedSource;
  if (tip) tip.title = 'Only Claude Code writes a usage-limit notice. ' + label
    + ' reports a running percentage instead, so this table stays empty however '
    + 'often it throttled you; that percentage is the figure in Plan Limits '
    + 'above.';
  if (note) note.innerHTML = what('the transcripts')
    + 'Only Claude Code writes those notices, so this table stays empty however '
    + 'often ' + esc(label) + ' throttled you, which is why the counts above are '
    + 'dashes rather than zeros. ' + headroom;
}
