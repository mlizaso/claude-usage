// ── Quota threshold alerts ─────────────────────────────────────────────────
// "Tell me when I have used 80% of my window."
//
// ONE implementation for both assistants, and it is not a coincidence that it
// can be: Claude's windows come from its local cache and Codex's from its
// transcript series, but both are projected server-side into the same window
// shape, so everything below takes a `{kind, percent, resets_at}` and never asks
// which assistant produced it. The only source-dependent thing here is the
// notification's title.
//
// Three properties this has to get right, none of them obvious:
//
//  * **Fire once per window, not once per reading.** The panel re-renders every
//    30 seconds, and a percentage that sits at 82 would otherwise notify on
//    every one of them. What counts is the CROSSING.
//  * **Survive a reload.** The fired set is persisted, so refreshing the page
//    does not replay every threshold you have already passed.
//  * **Do not fire on the first sighting of an already-high window.** Opening
//    the dashboard at 91% should not announce 20, 30, 50 and 90 at once — you
//    did not cross them while watching, and four notifications say less than
//    none. The first reading of a window establishes a baseline.

const ALERT_THRESHOLD_KEY = 'cu_alert_thresholds';
const ALERT_FIRED_KEY = 'cu_alert_fired';
// The one the user asked for, and a sensible one: high enough to be worth
// interrupting for, early enough to do something about.
const ALERT_DEFAULT = Object.freeze([80]);
const ALERT_CHOICES = Object.freeze([20, 30, 40, 50, 60, 70, 75, 80, 85, 90, 95]);
// A window is only ever one of a handful, but a long-lived browser profile
// would otherwise accumulate a key per window forever.
const ALERT_FIRED_MAX = 200;

function loadThresholds() {
  try {
    const raw = JSON.parse(localStorage.getItem(ALERT_THRESHOLD_KEY) || 'null');
    if (!Array.isArray(raw)) return [...ALERT_DEFAULT];
    const kept = [...new Set(raw)]
      .filter(n => Number.isFinite(n) && n > 0 && n <= 100)
      .sort((a, b) => a - b);
    // An empty array is a real choice — "never notify me" — and is preserved.
    // Only a missing or malformed value falls back to the default.
    return kept;
  } catch (e) {
    return [...ALERT_DEFAULT];
  }
}

function saveThresholds(list) {
  try {
    localStorage.setItem(ALERT_THRESHOLD_KEY, JSON.stringify(
      [...new Set(list)].filter(n => Number.isFinite(n) && n > 0 && n <= 100)
        .sort((a, b) => a - b)));
  } catch (e) { /* private mode: alerts last for this page only */ }
}

// The reset time, ROUNDED TO THE MINUTE.
//
// Sub-second jitter across a minute boundary must preserve window identity.
// Mirror account._reset_key so a refresh cannot reset the alert baseline and
// repeatedly suppress threshold crossings as if the window had just appeared.
function alertResetKey(resetsAt) {
  const at = Date.parse(resetsAt || '');
  if (!Number.isFinite(at)) return String(resetsAt || '');
  return new Date(Math.round(at / 60000) * 60000).toISOString().slice(0, 16);
}

// Identifies one window across renders and reloads. The reset time is what makes
// two five-hour windows distinct; without it every window of a kind would share
// a key and the second one would never notify.
function alertWindowKey(source, window) {
  const identity = window.key || [window.kind || '', window.group || '',
    window.scope || ''].join('|');
  return [source || 'claude', identity, alertResetKey(window.resets_at)].join('|');
}

// Ticking a threshold you are ALREADY past.
//
// The first sighting of a window is a baseline, so that opening the dashboard at
// 91% does not announce every threshold beneath it. But newly enabling one is
// not a passive first sighting — it is an explicit question about right now, and
// the honest answer is that you are already past it. Setting 30% while sitting
// at 33% otherwise produced nothing at all, for the rest of that window.
//
// Returns how many windows it announced, and records the reading so the ordinary
// path does not then repeat it.
function announceIfAlreadyPast(threshold, windowKey) {
  const info = lastPlanInfo;
  if (!info || !info.available || !Number.isFinite(threshold)) return 0;
  const source = info.source || selectedSource || 'claude';
  const state = loadFired();
  let announced = 0;
  for (const window of info.windows || []) {
    // Only the window this threshold was set ON. Thresholds are per window now,
    // so announcing every window already past the number would report limits
    // the user did not just configure -- setting 4% on the weekly one would
    // announce the 5-hour window at 79%, which is not what was asked.
    if (windowKey && window.key !== windowKey) continue;
    if (windowHasEnded(window) || window.percent == null || window.percent < threshold) continue;
    deliverAlert(
      (SOURCE_LABELS[source] || source || 'Claude Code')
        + ' quota is already at ' + window.percent + '%',
      planWindowLabel(window) + ' is past your new ' + threshold + '% threshold'
        + (window.resets_at ? ' · resets ' + fmtResetAt(window.resets_at) : ''));
    const key = alertWindowKey(source, window);
    const seen = (state[key] || {}).seen;
    state[key] = { seen: Math.max(Number.isFinite(seen) ? seen : -1, window.percent) };
    announced += 1;
  }
  saveFired(state);
  return announced;
}

// Have we recorded any OTHER window of this kind? Distinguishes "the window
// rolled over under us" from "this page has only just opened", which is the
// difference between news and a flood of notifications for crossings nobody saw.
function hasSeenWindowKind(state, source, window) {
  const identity = window.key || [window.kind || '', window.group || '',
    window.scope || ''].join('|');
  const prefix = [source || 'claude', identity, ''].join('|');
  const own = alertWindowKey(source, window);
  return Object.keys(state).some(key => key !== own && key.startsWith(prefix));
}

function loadFired() {
  try {
    const raw = JSON.parse(localStorage.getItem(ALERT_FIRED_KEY) || '{}');
    return (raw && typeof raw === 'object' && !Array.isArray(raw)) ? raw : {};
  } catch (e) { return {}; }
}

function saveFired(state) {
  try {
    const keys = Object.keys(state);
    // Drop the oldest keys rather than growing without bound. Insertion order
    // is preserved for string keys, so the earliest-seen windows go first.
    if (keys.length > ALERT_FIRED_MAX) {
      for (const key of keys.slice(0, keys.length - ALERT_FIRED_MAX)) delete state[key];
    }
    localStorage.setItem(ALERT_FIRED_KEY, JSON.stringify(state));
  } catch (e) { /* nothing to do; alerts simply repeat next session */ }
}

// Which thresholds this reading has newly crossed.
//
// `seen` is the highest percentage previously observed for this window, or null
// on its first sighting. A first sighting deliberately crosses nothing: opening
// the page at 91% must not fire 20, 30, 50 and 90 at once for crossings that
// happened while nobody was looking.
function crossedThresholds(seen, percent, thresholds) {
  if (!Number.isFinite(percent)) return [];
  if (!Number.isFinite(seen)) return [];
  return (thresholds || [])
    .filter(t => Number.isFinite(t) && seen < t && percent >= t)
    .sort((a, b) => a - b);
}

// Show one, on BOTH channels: the browser's notification when it has been
// allowed, and the in-page banner always.
//
// The banner is not a fallback, because there is nothing here to fall back
// from. `Notification.permission === 'granted'` says the user once clicked
// Allow, and a constructor that returns without throwing says the toast was
// created; neither says it was seen. Focus and Do Not Disturb, a backgrounded
// tab, and `tag` collapsing an identical repeat all swallow it silently. So the
// one channel this page can actually see is unconditional — a threshold the
// user asked for arriving at all is the whole point of the feature, and gating
// the banner on the notification looks equivalent while being the single change
// that can deliver nothing.
function deliverAlert(title, body) {
  try {
    if (typeof Notification === 'function' && Notification.permission === 'granted') {
      new Notification(title, { body, tag: title });
    }
  } catch (e) { /* the banner below is the delivery that matters */ }
  showAlertBanner(title + ' — ' + body);
}

function showAlertBanner(text) {
  const banner = document.getElementById('quota-alert');
  if (!banner) return;
  banner.textContent = text;
  banner.hidden = false;
}

function dismissAlertBanner() {
  const banner = document.getElementById('quota-alert');
  if (banner) banner.hidden = true;
}

// Called on every plan-panel render, for whichever source is on screen.
function checkQuotaAlerts(info, source) {
  if (!info || !info.available) return [];
  // The GLOBAL list is now only a fallback. Each window carries its own
  // `thresholds`, resolved server-side by `limits_core` from the shared file
  // both front ends write -- so "notify me at 30% of the 5-hour limit and 4% of
  // the weekly one" is two independent settings rather than one list applied to
  // everything. A window with no `thresholds` array is one from a payload older
  // than this feature (or a source not yet routed through the describer), and
  // falls back to the global list rather than silently alerting on nothing.
  const fallback = loadThresholds();
  const state = loadFired();
  const fired = [];
  for (const window of info.windows || []) {
    // An expired window's percentage describes the one that has already rolled
    // over, so alerting on it would announce a limit you are no longer under.
    // Asked of the VIEWER's clock, exactly as the panel beside this asks it:
    // `expired` was stamped when the payload was built, and a Codex payload is
    // never re-fetched, so trusting the flag had the panel rendering "Window
    // ended" while the alert quoted a reset time already in the past.
    if (windowHasEnded(window) || window.percent == null) continue;
    const key = alertWindowKey(source, window);
    const entry = state[key] || {};
    let seen = Number.isFinite(entry.seen) ? entry.seen : null;
    // A window we have never recorded, but of a kind we HAVE — the previous one
    // rolled over while we were watching. That is a transition we witnessed, not
    // a cold start, and the new window began at 0 whether or not we saw every
    // step of it. Treating it as a first sighting meant coming back to a fresh
    // window already at 55% said nothing at all, for its whole five hours.
    if (seen === null && hasSeenWindowKind(state, source, window)) seen = -1;
    const active = Array.isArray(window.thresholds)
      ? window.thresholds : fallback;
    const crossed = crossedThresholds(seen, window.percent, active);
    for (const threshold of crossed) {
      const label = (SOURCE_LABELS[source] || source || 'Claude Code');
      deliverAlert(
        label + ' quota at ' + window.percent + '%',
        planWindowLabel(window) + ' has passed ' + threshold + '%'
          + (window.resets_at ? ' · resets ' + fmtResetAt(window.resets_at) : ''));
      fired.push(threshold);
    }
    // Record the high-water mark whether or not anything fired: it is what the
    // next reading compares against, and it is what makes the first sighting a
    // baseline rather than a flood.
    state[key] = { seen: Math.max(seen == null ? -1 : seen, window.percent) };
  }
  saveFired(state);
  return fired;
}

// ── The control ────────────────────────────────────────────────────────────
// Any whole percentage from 1 to 100, or null with a reason.
//
// The reason is RETURNED rather than thrown so the caller can put it on screen:
// an entry that vanishes silently is indistinguishable from one that was
// accepted and then ignored.
function parseThreshold(raw) {
  const text = String(raw == null ? '' : raw).trim().replace(/%$/, '');
  if (!text) return { error: 'Enter a number' };
  if (!/^\d+$/.test(text)) return { error: 'Whole numbers only' };
  const value = Number(text);
  if (value < 1 || value > 100) return { error: 'Must be between 1 and 100' };
  return { value: value };
}

function requestNotificationPermission() {
  try {
    if (typeof Notification === 'function' && Notification.permission === 'default') {
      Notification.requestPermission();
    }
  } catch (e) { /* the banner still works */ }
}

// ── The per-window controls ────────────────────────────────────────────────
//
// One "Notify me at" row per quota window, generated from the payload. Nothing
// here names a limit kind: a window this build has never seen arrives with a
// key, a label and its thresholds already resolved, and gets its own row.
//
// Thresholds are held SERVER-side (`limits_core`, shared with the standalone
// limits server), so the same settings govern both front ends and survive
// clearing site data. `windowThresholds` is a local mirror of that file, kept
// so a click can repaint immediately instead of waiting for a round trip.
function emptyThresholdMap() {
  return Object.create(null);
}

function setThresholdMapValue(mapping, key, value) {
  // Assignment to `__proto__` is a prototype mutation on an ordinary object.
  // Define an own data property so externally supplied window keys stay data
  // even when a caller hands us an ordinary object.
  Object.defineProperty(mapping, key, {
    value,
    writable: true,
    enumerable: true,
    configurable: true,
  });
}

function copyThresholdMap(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const copy = emptyThresholdMap();
  for (const [key, thresholds] of Object.entries(value)) {
    setThresholdMapValue(
      copy, key, Array.isArray(thresholds) ? [...thresholds] : thresholds);
  }
  return copy;
}

let windowThresholds = emptyThresholdMap();
let alertThresholdsLoading = null;
// Preserve click order within one page. The server's PATCH operation protects
// unrelated windows across tabs; this queue additionally makes the newest
// rapid edit to the SAME window the last request that reaches the server.
let alertThresholdSaveQueue = Promise.resolve();
let alertThresholdSaveSerial = 0;

function thresholdMapFromBody(body) {
  return copyThresholdMap(body && body.thresholds);
}

function knownQuotaInfos() {
  const infos = new Set([lastPlanInfo, lastClaudeLimits]);
  for (const payload of [rawData, ...loadedSources.values()]) {
    if (payload) {
      infos.add(payload.subscription_limits);
      infos.add(payload.codex_limits);
    }
  }
  return [...infos].filter(Boolean);
}

function updateKnownWindowThreshold(key, thresholds) {
  for (const info of knownQuotaInfos()) {
    for (const window of ((info && info.windows) || [])) {
      if (window && window.key === key) window.thresholds = [...thresholds];
    }
    if (info.orphaned && Object.prototype.hasOwnProperty.call(info.orphaned, key)) {
      setThresholdMapValue(info.orphaned, key, [...thresholds]);
    }
  }
}

function publishThresholdMap(stored) {
  windowThresholds = stored;
  // The response covers all windows, including earlier queued saves and edits
  // from another page. Update every payload before a render can copy its older
  // explicit thresholds back over the stored values or use them for an alert.
  for (const [key, thresholds] of Object.entries(stored)) {
    if (Array.isArray(thresholds)) updateKnownWindowThreshold(key, thresholds);
  }
  const empty = Object.keys(stored).length === 0;
  for (const info of knownQuotaInfos()) {
    // An empty store unambiguously restores defaults. With a nonempty store,
    // a missing canonical key can still have a legacy alias resolved only by
    // the backend; retain that projection until its next quota refresh.
    if (empty) {
      for (const window of info.windows || []) {
        if (window && window.key) window.thresholds = [...ALERT_DEFAULT];
      }
    }
    for (const key of Object.keys(info.orphaned || {})) {
      if (!Object.prototype.hasOwnProperty.call(stored, key)) delete info.orphaned[key];
    }
  }
}

function syncWindowThresholdsFromInfo(info) {
  if (!info) return;
  for (const window of info.windows || []) {
    if (window && window.key && Array.isArray(window.thresholds)
        && (alertThresholdsLoaded
            || !Object.prototype.hasOwnProperty.call(windowThresholds, window.key))) {
      setThresholdMapValue(windowThresholds, window.key, [...window.thresholds]);
    }
  }
  for (const [key, thresholds] of Object.entries(info.orphaned || {})) {
    if (Array.isArray(thresholds)) {
      setThresholdMapValue(windowThresholds, key, [...thresholds]);
    }
  }
}

function storedLegacyThresholds() {
  try {
    if (localStorage.getItem(ALERT_THRESHOLD_KEY) == null) return null;
    const value = JSON.parse(localStorage.getItem(ALERT_THRESHOLD_KEY));
    if (!Array.isArray(value)) return null;
    return [...new Set(value)]
      .filter(n => Number.isFinite(n) && n > 0 && n <= 100)
      .sort((a, b) => a - b);
  } catch (e) { return null; }
}

function knownWindowKeys() {
  const keys = new Set();
  for (const info of knownQuotaInfos()) {
    for (const window of ((info && info.windows) || [])) {
      if (window && window.key) keys.add(window.key);
    }
  }
  return [...keys];
}

function fetchThresholdMap() {
  return apiFetch('/api/limits/thresholds').then(r => {
    if (!r.ok) throw new Error('threshold load failed');
    return r.json();
  }).then(body => {
    const stored = thresholdMapFromBody(body);
    if (stored === null) throw new Error('invalid threshold response');
    return stored;
  });
}

function patchThresholdMap(mapping) {
  return apiFetch('/api/limits/thresholds', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ thresholds: mapping }),
  }).then(r => {
    if (!r.ok) throw new Error('save failed');
    return r.json();
  }).then(body => {
    const stored = thresholdMapFromBody(body);
    if (stored === null) throw new Error('invalid threshold response');
    return stored;
  });
}

function queueThresholdPatch(mapping) {
  const operation = alertThresholdSaveQueue
    .catch(() => undefined)
    .then(() => patchThresholdMap(mapping));
  // A failed save must not poison the queue and prevent later corrections.
  alertThresholdSaveQueue = operation.then(() => undefined, () => undefined);
  return operation;
}

function migrateLegacyThresholds(stored) {
  const legacy = storedLegacyThresholds();
  if (legacy === null) return Promise.resolve(stored);
  const updates = emptyThresholdMap();
  for (const key of knownWindowKeys()) {
    if (!Object.prototype.hasOwnProperty.call(stored, key)) {
      setThresholdMapValue(updates, key, [...legacy]);
    }
  }
  if (!Object.keys(updates).length) return Promise.resolve(stored);
  return queueThresholdPatch(updates).then(saved => {
    try { localStorage.removeItem(ALERT_THRESHOLD_KEY); } catch (e) { /* harmless */ }
    return saved;
  });
}

function thresholdsForWindow(key) {
  const list = windowThresholds[key];
  return Array.isArray(list) ? [...list] : [];
}

// PATCH performs the read-modify-write under the backend's thread/process lock,
// so an edit sends only its own window and cannot erase an unrelated setting.
// The local queue also preserves invocation order for rapid edits in this page.
function saveWindowThresholds(key, list) {
  const previous = copyThresholdMap(windowThresholds) || emptyThresholdMap();
  const next = copyThresholdMap(windowThresholds) || emptyThresholdMap();
  const desired = [...(list || [])].sort((a, b) => a - b);
  const serial = ++alertThresholdSaveSerial;
  // Empty is explicit: it disables the server's 80% default for this window.
  setThresholdMapValue(next, key, desired);
  windowThresholds = next;
  renderAlertPicker(undefined, false);
  // `apiFetch` is the one definition of an authenticated request on this page.
  // Hand-rolling a second `fetch` with its own token header is how the two
  // drift -- and the first draft of this function did exactly that, naming a
  // helper that does not exist.
  if (!API_TOKEN) return Promise.resolve(false);
  const patch = emptyThresholdMap();
  setThresholdMapValue(patch, key, desired);
  const operation = queueThresholdPatch(patch);
  return operation
    .then(stored => {
      // A newer queued edit is already mirrored optimistically. Do not repaint
      // it with this older response while that request waits its turn.
      if (serial === alertThresholdSaveSerial) {
        publishThresholdMap(stored);
        alertThresholdsLoaded = true;
        renderAlertPicker();
        showAlertError('');
      }
      return true;
    }).catch(() => {
      if (serial !== alertThresholdSaveSerial) return false;
      // Earlier queued operations may have succeeded or failed. Re-reading is
      // the only honest rollback baseline once more than one save overlapped.
      return fetchThresholdMap().then(stored => {
        if (serial !== alertThresholdSaveSerial) return;
        publishThresholdMap(stored);
        alertThresholdsLoaded = true;
      }).catch(() => {
        if (serial !== alertThresholdSaveSerial) return;
        windowThresholds = previous;
        alertThresholdsLoaded = false;
      }).then(() => {
        if (serial !== alertThresholdSaveSerial) return false;
        renderAlertPicker(undefined, false);
        showAlertError('Could not save that threshold — it will not survive a reload.');
        return false;
      });
    });
}

function loadWindowThresholds() {
  if (!API_TOKEN) return Promise.resolve({});
  if (alertThresholdsLoaded) return Promise.resolve(windowThresholds);
  if (alertThresholdsLoading) return alertThresholdsLoading;
  const serial = alertThresholdSaveSerial;
  alertThresholdsLoading = fetchThresholdMap()
    .then(stored => serial === alertThresholdSaveSerial
      ? migrateLegacyThresholds(stored) : null)
    .then(stored => {
      if (serial !== alertThresholdSaveSerial) return windowThresholds;
      publishThresholdMap(stored);
      alertThresholdsLoaded = true;
      renderAlertPicker();
      showAlertError('');
      return windowThresholds;
    })
    .catch(() => {
      if (serial !== alertThresholdSaveSerial) return windowThresholds;
      alertThresholdsLoaded = false;
      showAlertError('Could not load alert thresholds. They were not changed; retrying.');
      return null;
    })
    .finally(() => { alertThresholdsLoading = null; });
  return alertThresholdsLoading;
}

// The windows to offer a control for: the ones on screen, plus any window that
// has a threshold configured but is NOT currently reported.
//
// A stale `cachedUsageUtilization` cache can omit a window that still has a
// configured alert. Keep that alert visible and editable even without a
// current quota reading.
function alertWindowRows(info) {
  // The info is PASSED rather than read from `lastPlanInfo`, because the panel
  // renders from its own argument and that global is assigned by the bootstrap
  // AFTER the render. Reading the global here meant the picker described a
  // different payload from the gauges beside it -- and on the first paint,
  // before the bootstrap had assigned anything, described nothing at all.
  info = info || lastPlanInfo;
  const rows = [];
  const seen = new Set();
  for (const w of ((info && info.windows) || [])) {
    if (!w || !w.key) continue;
    seen.add(w.key);
    rows.push({ key: w.key, label: planWindowLabel(w), percent: w.percent,
      thresholds: w.thresholds, live: true });
  }
  const orphaned = (info && info.orphaned) || {};
  for (const key of Object.keys(orphaned)) {
    if (seen.has(key)) continue;
    const parts = key.split(':');
    const kind = parts[1] || '';
    const possibleScope = parts.length > 2 ? parts[parts.length - 1] : '';
    const scope = /^[0-9a-f]{12}$/.test(possibleScope) ? '' : possibleScope;
    const group = (kind === 'session' || kind === 'five_hour') ? 'session'
      : (kind.startsWith('weekly') || kind === 'seven_day') ? 'weekly' : kind;
    rows.push({ key: key, label: planWindowLabel({ kind, group, scope }),
      percent: null, thresholds: orphaned[key], live: false });
  }
  return rows;
}

function renderAlertPicker(info, syncFromPayload = true) {
  const host = document.getElementById('alert-windows');
  if (!host) return;
  if (syncFromPayload) syncWindowThresholdsFromInfo(info || lastPlanInfo);
  const rows = alertWindowRows(info);
  if (!rows.length) {
    host.innerHTML = '<span class="alert-empty">No quota windows reported yet.</span>';
    return;
  }
  host.innerHTML = rows.map(row => {
    const chosen = thresholdsForWindow(row.key);
    const key = esc(row.key);
    // The chips ARE the chosen set, each removable, rather than a fixed row of
    // presets toggled on and off: that is what lets a preset be removed and a
    // custom value be expressed in one control, and it makes the row an honest
    // statement of exactly what will notify you for THIS window.
    const chips = chosen.map(v =>
      '<span class="alert-chip on">' + v + '%'
      + '<button class="alert-x" data-remove="' + v + '" data-window="' + key + '"'
      + ' title="Stop notifying at ' + v + '% of ' + esc(row.label) + '"'
      + ' aria-label="Remove ' + v + ' percent from ' + esc(row.label) + '">&times;</button>'
      + '</span>').join('') || '<span class="alert-empty">none</span>';
    const suggest = ALERT_CHOICES.filter(v => !chosen.includes(v)).map(v =>
      '<button class="alert-chip" data-threshold="' + v + '" data-window="' + key + '">+'
      + v + '%</button>').join('');
    // A window with a threshold but no current reading says so rather than
    // showing a blank percentage, which would read as "0%".
    const state = row.live
      ? (row.percent == null ? '' : '<span class="alert-now">now ' + row.percent + '%</span>')
      : '<span class="alert-stale" title="Configured, but this window is not in'
        + ' the current cache. Your setting is kept and applies when it returns.">'
        + 'not currently reported</span>';
    return '<div class="alert-window" data-window="' + key + '">'
      + '<div class="alert-row">'
      + '<span class="alert-label">' + esc(row.label) + '</span>' + state
      + '<div class="alert-chips">' + chips + '</div></div>'
      + '<div class="alert-row alert-add">'
      + '<input class="alert-input" type="text" inputmode="numeric" maxlength="4"'
      + ' placeholder="1–100" data-window="' + key + '"'
      + ' aria-label="Threshold percentage for ' + esc(row.label) + '">'
      + '<button class="filter-btn" data-add-window="' + key + '">Add</button>'
      + '<div class="alert-chips">' + suggest + '</div></div></div>';
  }).join('');
}

function showAlertError(message) {
  const box = document.getElementById('alert-error');
  if (!box) return;
  box.textContent = message || '';
  box.hidden = !message;
}

function addCustomThreshold(raw, key) {
  const parsed = parseThreshold(raw);
  if (parsed.error) { showAlertError(parsed.error); return false; }
  if (!key) { showAlertError('Pick a limit first.'); return false; }
  showAlertError('');
  const chosen = new Set(thresholdsForWindow(key));
  if (chosen.has(parsed.value)) return true;      // already there; not an error
  chosen.add(parsed.value);
  saveWindowThresholds(key, [...chosen]);
  requestNotificationPermission();
  announceIfAlreadyPast(parsed.value, key);
  return true;
}

function removeThreshold(threshold, key) {
  const chosen = new Set(thresholdsForWindow(key));
  if (!chosen.delete(threshold)) return;
  saveWindowThresholds(key, [...chosen]);
}


// Quick-add: the `+N%` chips, which only ever add. Kept as its own function
// because the delegated handler distinguishes a chip from an `x`, and giving
// both the same entry point is how a click that means "add" starts removing.
function toggleAlertThreshold(threshold, key) {
  if (!Number.isFinite(threshold) || !key) return;
  const chosen = new Set(thresholdsForWindow(key));
  if (chosen.has(threshold)) return;
  chosen.add(threshold);
  saveWindowThresholds(key, [...chosen]);
  requestNotificationPermission();
  announceIfAlreadyPast(threshold, key);
}
