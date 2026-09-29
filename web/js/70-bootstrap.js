// ── Rescan ────────────────────────────────────────────────────────────────
async function triggerRescan() {
  snapshotRefreshFailed = false;
  const btn = document.getElementById('rescan-btn');
  btn.disabled = true;
  btn.textContent = '\u21bb Scanning...';
  showScanLoading();
  try {
    const resp = await apiFetch('/api/rescan', { method: 'POST' });
    const d = await resp.json();
    renderDockerStatus(d.docker);
    // The server answers a refusal with JSON too — 403 (bad token), 409 (a scan
    // is already running), 500 (the scan raised) — precisely so this button can
    // tell a failed scan from a dead server. Reading d.new off one of those
    // bodies rendered "Rescan (undefined new, undefined updated)": the shape of
    // a success, followed by a pointless reload of data nothing had changed.
    //
    // Deliberately not an early `return`: the button is re-enabled by the
    // setTimeout below the try, so bailing out of the function here would leave
    // Rescan greyed out for the life of the page.
    if (!resp.ok) {
      if (resp.status === 409) {
        // Another tab owns the same server-side scan. Its 409 is not a failed
        // outcome and, more importantly, does not make the incrementally
        // committed database final. Follow the shared activity through idle,
        // then perform the same post-scan reload as the request we started.
        btn.textContent = '\u21bb Scan already running...';
        if (await waitForScanToFinish()) {
          invalidateUsageData();
          btn.textContent = '\u21bb Scan complete';
          await loadData();
        }
      } else {
        btn.textContent = '\u21bb Rescan failed';
        showScanFailure();
      }
    } else {
      invalidateUsageData();
      btn.textContent = '\u21bb Rescan (' + d.new + ' new, ' + d.updated + ' updated)';
      await loadData();
    }
  } catch(e) {
    btn.textContent = '\u21bb Rescan failed (no response)';
    showScanFailure();
    console.error(e);
  }
  // No unconditional clear here. loadData owns its terminal overlay state: a
  // successful/render-error response clears it, a scan failure keeps its retry
  // action visible, and an abandoned response leaves a newer source switch's
  // overlay alone. Clearing after the await would overwrite all three choices.
  setTimeout(() => { btn.textContent = '\u21bb Rescan'; btn.disabled = false; }, 3000);
}

// ── Source switching ───────────────────────────────────────────────────────
// Claude's own quota reading, kept aside so switching back does not need another
// fetch, and so Codex's panel can never be left showing Claude's figure.
let lastClaudeLimits = null;

function setSource(source) {
  if (!SOURCES.includes(source) || source === selectedSource) return;
  selectedSource = source;
  renderSourceSwitch();
  updateURL();
  // Each assistant's history is fetched when you first enter it, not up front.
  // A payload covering both takes seconds to build and half of it would be
  // discarded by the source filter; scoped, you wait for the one you asked for.
  // Already-loaded sources are kept, so coming back is instant.
  //
  // Returns the load so a caller can await the switch. Nothing in the page does
  // — a click should not block — but a test that cannot tell when the switch
  // finished can only guess with a timer.
  return loadSource(source);
}

// Payloads already fetched, keyed by source. A switch back does not refetch.
const loadedSources = new Map();
// Saved responses survive server restarts on disk. They are only previews;
// unlike this page's live cache, they always need a completed scan and re-read.
const savedSources = new Set();
const attemptedSnapshots = new Set();
let startupRefresh = null;
let startupInProgress = false;
let hasStartupSnapshots = false;
let snapshotRefreshFailed = false;
// Only the newest request for a source may publish it. Source identity alone is
// not enough: an automatic refresh and a post-rescan reload can ask for the same
// assistant concurrently and complete newest-first. Object identity gives each
// logical load (including its scan-wait retry) one unforgeable ownership token.
const latestDataLoad = new Map();
// The source whose figures are actually painted. `selectedSource` changes as
// soon as a switch is pressed, while `rawData` remains the previous source until
// the new response lands, so neither variable can answer this on its own.
let renderedSource = null;

function invalidateUsageData(preservedSource = null, preservedToken = null) {
  // A successful scan can change every assistant, not only the one visible at
  // the time. Clearing request ownership also prevents a response that was
  // already in flight before the scan from repopulating a stale cache later.
  loadedSources.clear();
  latestDataLoad.clear();
  // A 409/503 proves the caches predate scan activity before its final outcome
  // is known. Preserve only the logical request that is following that scan so
  // a transient status failure can retry without re-admitting any old cache.
  if (preservedSource !== null && preservedToken !== null) {
    latestDataLoad.set(preservedSource, preservedToken);
  }
}
// Which source the model filter currently describes. A payload for a different
// source must REBUILD it, not merge into it: the ids do not overlap, and
// merging would leave the previous assistant's models selected — putting both
// in one view, which is the thing the source split exists to prevent.
let filterBuiltFor = null;

async function loadSource(source) {
  if (hasStartupSnapshots && !loadedSources.has(source)) {
    showLoading(SOURCE_LABELS[source] || source);
    await restoreSnapshot(source);
  }
  if (source !== selectedSource) return;
  const cached = loadedSources.get(source);
  if (cached) {
    rawData = cached;
    renderedSource = source;
    if (filterBuiltFor !== source) {
      buildFilterUI(sourceModels(source), false);
      filterBuiltFor = source;
    }
    // This branch does no fetching, so it is the one place that has to take down
    // an overlay it did not raise: pressing a source that is already here is the
    // reader saying they have stopped waiting for the one still in flight.
    // Nothing else would — that fetch returns on the staleness guard in loadData.
    // updateMetaNote() goes with it, because showLoading() wrote "Loading …
    // usage…" over the header line and clearLoading() does not put it back —
    // dated from THIS payload, because "Updated: …" describes what is on screen
    // and the last thing to set it may well have been the other source.
    clearLoading();
    lastGeneratedAt = cached.generated_at;
    updateMetaNote();
    updateSnapshotStatus();
    applyFilter();
    if (savedSources.has(source)) await loadData(source);
    return;
  }
  showLoading(SOURCE_LABELS[source] || source);
  await loadData(source);
}

// A source switch flips the heading and the control at once — a click has to
// feel answered — but the charts and tables underneath are still the PREVIOUS
// assistant's until the fetch lands, roughly two seconds later. Leaving them
// crisp under the new label would state one assistant's numbers as the other's,
// which is the one thing this page must not do. Dimming says "not current yet"
// without blanking the layout out from under the reader.
// The dim says "what you are looking at is not current". It cannot say "something
// is happening" on a FIRST load, because there is nothing on screen to dim yet —
// which is what made the initial fetch look like a blank, broken page for its
// whole duration. The overlay carries that second message, and both are cleared
// by the same clearLoading() so they can never disagree about whether a fetch is
// still in flight.
function showProgress(message) {
  if (showingSavedData()) {
    clearLoading();
    updateSnapshotStatus();
    return;
  }
  const meta = document.getElementById('meta');
  if (meta) meta.textContent = message;
  const container = document.querySelector('.container');
  if (container) {
    container.classList.add('loading');
    container.setAttribute('aria-busy', 'true');
  }
  const overlay = document.getElementById('load-overlay');
  const spinner = document.getElementById('load-spinner');
  const retry = document.getElementById('scan-retry');
  const text = document.getElementById('load-text');
  if (spinner) spinner.hidden = false;
  if (retry) retry.hidden = true;
  if (text) text.textContent = message;
  if (overlay) overlay.hidden = false;
}

function showLoading(label) {
  // textContent, not innerHTML: `label` reaches here from SOURCE_LABELS, but the
  // fallback at both call sites is the raw source string.
  showProgress('Loading ' + label + ' usage…');
}

function showScanLoading() {
  showProgress('Scanning usage history… This can take a while on first load.');
}

function showScanFailure(blocking = true) {
  if (showingSavedData()) {
    snapshotRefreshFailed = true;
    clearLoading();
    updateSnapshotStatus();
    return;
  }
  const message = blocking
    ? 'Usage scan failed. Totals may be incomplete. Check the terminal, then retry.'
    : 'Automatic scan failed; the existing totals were not replaced. Press Rescan to retry.';
  if (!blocking) {
    const meta = document.getElementById('meta');
    if (meta) meta.textContent = message;
    return;
  }
  showProgress(message);
  const container = document.querySelector('.container');
  if (container) container.removeAttribute('aria-busy');
  const spinner = document.getElementById('load-spinner');
  const retry = document.getElementById('scan-retry');
  if (spinner) spinner.hidden = true;
  if (retry) {
    retry.hidden = false;
    if (typeof retry.focus === 'function') retry.focus();
  }
}

function clearLoading() {
  const container = document.querySelector('.container');
  if (container) {
    container.classList.remove('loading');
    container.removeAttribute('aria-busy');
  }
  const overlay = document.getElementById('load-overlay');
  const spinner = document.getElementById('load-spinner');
  const retry = document.getElementById('scan-retry');
  if (spinner) spinner.hidden = false;
  if (retry) retry.hidden = true;
  if (overlay) overlay.hidden = true;
}

// The models this source actually used. From the daily rollup rather than
// all_models, which spans both assistants.
function sourceModels(source) {
  const seen = new Set();
  for (const r of ((rawData && rawData.daily_by_model) || [])) {
    if ((r.source || 'claude') === source) seen.add(r.model);
  }
  // all_models remains the documented list for the filter, and is the answer
  // whenever the rollup cannot narrow it — a payload without daily rows, or a
  // source whose rows have not been written yet. Narrowing to an empty set would
  // blank every chart and read as "no data" rather than "no filter".
  if (seen.size) return [...seen];
  return [...(((rawData && rawData.all_models) || []))];
}

// The page title names what is on screen. It names the SELECTED source, not the
// number of them: branching on "how many" put "Claude Code Usage" over a
// Codex-only machine's figures, beside a footer citing OpenAI's rate card and
// with the switch hidden because there was nothing to switch to. `dual` still
// decides whether the title is also a CONTROL — with one assistant there is
// nowhere for it to go. A Claude-only machine reads the same as before:
// SOURCE_LABELS.claude is "Claude Code".
function renderSourceTitle() {
  const heading = document.getElementById('app-title');
  if (heading) {
    const dual = availableSources.length > 1;
    heading.textContent = (SOURCE_LABELS[selectedSource] || selectedSource) + ' Usage';
    // With both assistants present the title is also the fastest way to swap
    // between them — it is the thing on screen that names the current one, so
    // it is where people reach first.
    heading.classList.toggle('switchable', dual);
    if (dual) {
      const other = availableSources.find(s => s !== selectedSource) || selectedSource;
      heading.setAttribute('role', 'button');
      heading.setAttribute('tabindex', '0');
      heading.dataset.switchTo = other;
      heading.title = 'Show ' + (SOURCE_LABELS[other] || other) + ' usage instead';
    } else {
      heading.removeAttribute('role');
      heading.removeAttribute('tabindex');
      delete heading.dataset.switchTo;
      heading.title = '';
    }
  }
  document.title = (SOURCE_LABELS[selectedSource] || selectedSource) + ' Usage Dashboard';
}

function renderSourceSwitch() {
  renderSourceTitle();
  // Above the early return below, deliberately: a machine with only one
  // assistant still needs the right rate card named, and if that assistant is
  // Codex the Anthropic wording would be wrong on every count.
  renderFooterPricingNote();
  const wrap = document.getElementById('source-switch');
  if (!wrap) return;
  // One source means no choice to make, so the control stays out of the way —
  // and so do the heading that names it and the divider that closes it. Hiding
  // only the control left "SOURCE |" over an empty gap on the common install,
  // and below 640px, where the bar is a two-column grid, that orphan heading
  // shifted every remaining cell by one: MODELS sat beside it, "All models"
  // beside RANGE, and the refresh dropdown ended up alone.
  //
  // Three flat `hidden` flags rather than one wrapper: a wrapper is a single
  // grid item, so it would move the identical mis-pairing onto the machine that
  // has both assistants. Nothing in the stylesheet backs this — neither
  // .filter-label nor .filter-sep declares `display`, so the UA `[hidden]` rule
  // applies (`.source-switch[hidden]` exists precisely because that one does).
  const dual = availableSources.length >= 2;
  for (const id of ['source-filter-label', 'source-filter-sep']) {
    const part = document.getElementById(id);
    if (part) part.hidden = !dual;
  }
  wrap.hidden = !dual;
  if (!dual) return;
  wrap.innerHTML = availableSources.map(s =>
    '<button class="source-btn' + (s === selectedSource ? ' active' : '')
    + '" data-source="' + esc(s) + '">' + esc(SOURCE_LABELS[s] || s) + '</button>'
  ).join('');
}

// Asked once, when a machine turns out to hold both histories and the reader has
// never chosen. It is a real fork rather than a preference — the two are
// measured in different units — so it is asked rather than guessed.
// Renders the chooser. WHETHER to ask is start()'s decision — it has the only
// view of the stored choice, the URL and what the database holds — so this used
// to re-derive the same condition from two of those three and could disagree
// with its own caller. One decision point.
function askForSource() {
  const chooser = document.getElementById('source-chooser');
  if (!chooser) return;
  const options = availableSources.map(s => {
    const turns = (sourceTurns.get(s) || 0);
    return '<button class="chooser-btn" data-source="' + esc(s) + '">'
      + '<span class="chooser-name">' + esc(SOURCE_LABELS[s] || s) + '</span>'
      + '<span class="chooser-meta">' + esc(fmt(turns)) + ' turns recorded</span></button>';
  }).join('');
  chooser.innerHTML =
    '<div class="chooser-card">'
    + '<div class="chooser-title">Which usage do you want to see?</div>'
    + '<div class="chooser-sub">Both are on this machine. You can switch at any time.</div>'
    + '<div class="chooser-options">' + options + '</div></div>';
  chooser.hidden = false;
}

function chooseSource(source) {
  const chooser = document.getElementById('source-chooser');
  if (chooser) chooser.hidden = true;
  selectedSource = source;
  renderSourceSwitch();
  updateURL();
  return loadSource(source);
}

// ── Boot ───────────────────────────────────────────────────────────────────
// Which assistants exist is a separate, cheap question — one grouped count —
// asked before anything is rendered. Asking it by building the whole dashboard
// meant the reader waited several seconds for BOTH assistants' history to
// render behind the dialog before being allowed to say which one they wanted.
const SCAN_STATUS_POLL_MS = 750;
const SCAN_STATUS_RETRY_MS = 3000;

function showingSavedData() {
  return renderedSource === selectedSource && savedSources.has(renderedSource);
}

function updateSnapshotStatus() {
  const status = document.getElementById('snapshot-status');
  if (!status) return;
  status.hidden = !showingSavedData();
  status.classList.toggle('failed', snapshotRefreshFailed);
  const text = document.getElementById('snapshot-status-text');
  if (text) text.textContent = snapshotRefreshFailed
    ? 'Showing saved data. Update failed — press Rescan to retry.'
    : 'Showing saved data · Updating usage…';
}

async function fetchSnapshot(source = null) {
  const path = '/api/snapshot' + (source ? '?source=' + encodeURIComponent(source) : '');
  const response = await apiFetch(path);
  if (response.status === 403) { showAuthNotice('rejected'); return null; }
  if (!response.ok) return null;
  const snapshot = (await response.json()).snapshot;
  return snapshot && Number.isFinite(snapshot.saved_at) && snapshot.saved_at > 0
    && snapshot.data && !snapshot.data.error ? snapshot : null;
}

async function restoreSnapshot(source) {
  if (attemptedSnapshots.has(source)) return;
  attemptedSnapshots.add(source);
  let snapshot;
  try {
    snapshot = await fetchSnapshot(source);
  } catch (e) { return; }
  try {
    // A slow disk read must never overwrite an already-arrived live response.
    if (!snapshot || loadedSources.has(source)) return;
    const data = snapshot.data;
    if (!Array.isArray(data.all_models) || !Array.isArray(data.daily_by_model)
        || !Array.isArray(data.sessions_all) || data.unscanned) return;
    publishData(source, data, snapshot.saved_at);
  } catch (e) {
    // A disposable cache cannot prevent the normal scan/read path.
    loadedSources.delete(source);
    savedSources.delete(source);
  }
}

async function restoreStartupSnapshot() {
  try {
    const snapshot = await fetchSnapshot();
    const sources = snapshot && snapshot.data.sources;
    if (!Array.isArray(sources)) return;
    hasStartupSnapshots = true;
    sourceTurns = new Map(sources.filter(s => s && SOURCES.includes(s.source))
      .map(s => [s.source, s.turns]));
    availableSources = SOURCES.filter(s => (sourceTurns.get(s) || 0) > 0);
    const fromLink = readURLSource();
    // The saved source list can predate a newly used assistant. An explicit
    // link must never preview the other assistant just because that list is old.
    if (SOURCES.includes(fromLink)) selectedSource = fromLink;
    else if (availableSources.length === 1) selectedSource = availableSources[0];
    else if (availableSources.length > 1) {
      renderSourceSwitch();
      clearLoading();
      askForSource();
      return;
    } else return;
    renderSourceSwitch();
    await restoreSnapshot(selectedSource);
  } catch (e) { /* No usable snapshot: the ordinary startup handles recovery. */ }
}

async function refreshOnEntry() {
  const response = await apiFetch('/api/scan-status');
  if (response.status === 403) { showAuthNotice('rejected'); return null; }
  if (!response.ok) throw new Error('HTTP ' + response.status);
  const status = validatedScanStatus(await response.json());
  // Joining the launcher's scan avoids scanning a cold history twice.
  if (status.state === 'scanning') return waitForScanToFinish(false);
  const rescan = await apiFetch('/api/rescan', { method: 'POST' });
  if (rescan.status === 403) { showAuthNotice('rejected'); return null; }
  if (rescan.status === 409) return waitForScanToFinish(false);
  if (!rescan.ok) return false;
  const result = await rescan.json();
  return !result.error;
}

async function bootDashboard() {
  startupInProgress = true;
  showProgress('Checking usage history…');
  // Start ingestion and reading the saved response independently. A large scan
  // must not delay the preview, and choosing a source waits for this same scan.
  try {
    startupRefresh = refreshOnEntry().catch(() => false);
    await restoreStartupSnapshot();
    const succeeded = await startupRefresh;
    startupRefresh = null;
    if (succeeded === null) return;
    if (!succeeded) { showScanFailure(); return; }
    await start();
  } finally {
    startupRefresh = null;
    startupInProgress = false;
  }
}

function waitFor(milliseconds) {
  return new Promise(resolve => setTimeout(resolve, milliseconds));
}

function validatedScanStatus(value) {
  if (!value || !['scanning', 'idle', 'failed'].includes(value.state) ||
      !Number.isSafeInteger(value.generation) || value.generation < 0) {
    throw new Error('Invalid scan status');
  }
  renderDockerStatus(value.docker);
  return value;
}

function renderDockerStatus(value) {
  const element = document.getElementById('docker-status');
  if (!element || !value || typeof value.state !== 'string') return;
  const messages = {
    scanning: 'Checking Docker usage…',
    unavailable: 'Docker usage could not be refreshed. Stored usage is retained; check Docker and rescan.',
    partial: 'Some Docker usage could not be refreshed. Showing available and previously imported usage.',
    disabled: 'Automatic Docker collection is off.',
    upgrade_required: 'Update Docker Engine to 29.5.1 or newer to collect container usage automatically.',
    ready: 'Docker checked · No usage folders found.',
  };
  let message = Object.hasOwn(messages, value.state) ? messages[value.state] : '';
  if (value.state === 'ready' && Number.isSafeInteger(value.containers)
      && value.containers > 0) {
    message = 'Docker · Usage folders in ' + value.containers
      + (value.containers === 1 ? ' container' : ' containers');
  }
  element.textContent = message;
  element.hidden = !message;
  element.classList.toggle('warning', ['partial', 'unavailable', 'upgrade_required'].includes(value.state));
}

async function waitForScanToFinish(showProgressState = true,
                                   stillRelevant = () => true) {
  while (stillRelevant()) {
    const resp = await apiFetch('/api/scan-status');
    if (resp.status === 403) {
      showAuthNotice('rejected');
      return false;
    }
    if (!stillRelevant()) return false;
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const status = validatedScanStatus(await resp.json());
    if (!stillRelevant()) return false;
    if (status.state === 'idle') return true;
    if (status.state === 'failed') {
      showScanFailure(showProgressState);
      return false;
    }
    if (showProgressState) showScanLoading();
    await waitFor(SCAN_STATUS_POLL_MS);
  }
  return false;
}

async function start() {
  // Registering the background scan happens before its thread starts, so this
  // first cheap request cannot race ahead and accept an incrementally committed
  // zero/partial payload as final. Keep the progress state until a post-scan
  // sources query and payload render finish; saved previews remain interactive.
  showProgress('Checking usage history…');
  try {
    if (!await waitForScanToFinish()) return;
  } catch (e) {
    console.error(e);
    showProgress('Could not check scan progress — retrying…');
    setTimeout(start, SCAN_STATUS_RETRY_MS);
    return;
  }

  let sources = [];
  try {
    const resp = await apiFetch('/api/sources');
    // Same reasoning as in loadData: a rejected token is answered, not retried.
    if (resp.status === 403) { showAuthNotice('rejected'); return; }
    if (resp.status === 409 || resp.status === 503) {
      // The scan may have started after the status probe, or started and
      // finished while this grouped query ran.  Do not accept those counts as
      // final; re-enter bootstrap only after the shared generation is stable.
      if (await waitForScanToFinish()) return start();
      return;
    }
    if (resp.ok) {
      sources = (await resp.json()).sources || [];
    } else {
      // This is the FIRST request the page makes, so an unreadable database is
      // known here — before any chrome is drawn. Answering it now costs the
      // reader one screen instead of a filter bar over twelve empty cards with
      // a retry notice threaded through them. A transient failure still falls
      // through to the single-source path and lets loadData retry, which is
      // what the fresh-install case needs.
      const body = await resp.json().catch(() => ({}));
      if (body.permanent) { showDatabaseNotice(); return; }
    }
  } catch (e) { /* fall through to the single-source path */ }

  sourceTurns = new Map(sources.map(s => [s.source, s.turns]));
  availableSources = SOURCES.filter(s => (sourceTurns.get(s) || 0) > 0);
  if (!availableSources.length) availableSources = ['claude'];
  renderSourceSwitch();

  // With both assistants on the machine, the chooser is the entry view — every
  // time, not just the first. A remembered choice used to skip it, which meant
  // the question was asked exactly once and then silently answered on the
  // reader's behalf forever after. The only thing that skips it now is a link
  // that names a source explicitly, which is an instruction from whoever made
  // the link rather than a preference inferred from a past visit.
  const fromLink = readURLSource();
  if (availableSources.includes(fromLink)) {
    selectedSource = fromLink;
  } else if (availableSources.length === 1) {
    // Only one assistant here: there is nothing to choose between.
    selectedSource = availableSources[0];
  } else {
    // Two. Ask, and load neither until it is answered.
    selectedSource = availableSources[0];
    clearLoading();
    askForSource();
    return;
  }
  // Again, now that the source is settled. The call above runs before this
  // decision because the two-source path renders the chooser and returns from
  // it — so without this the chrome would name the module default (Claude) for
  // the whole of the first fetch on a Codex-only machine, which is the several
  // seconds the overlay below exists to cover, not a flicker.
  const chooser = document.getElementById('source-chooser');
  if (chooser) chooser.hidden = true;
  renderSourceSwitch();
  return loadSource(selectedSource);
}

// ── Plan limits ────────────────────────────────────────────────────────────
// Stamp the arrival time so the rendered age counts from when THIS page got the
// reading, not from a number the server computed and that then stood still.
//
// ARRIVAL, not render: `applyFilter` re-renders this panel from the same object
// on every range button, model toggle and table sort, so stamping at render
// reset the age on any click at all — a three-hour-old reading went back to
// saying "As of 2 min ago" and dropped its `stale` class. A reading is only ever
// received once, so the stamp is written once and then left alone.
function stampReceived(info) {
  if (info && typeof info === 'object' && info._received_at == null) {
    info._received_at = Date.now();
  }
}

function applyPlanLimits(info) {
  // The wire is stamped at each arrival point below; this covers the one object
  // that never came off it — the synthetic "no Codex usage" reading applyFilter
  // builds fresh on every call.
  stampReceived(info);
  lastPlanInfo = info;
  renderPlanLimits(info);
}

async function refreshPlanLimits() {
  try {
    const resp = await apiFetch('/api/limits');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const info = await resp.json();
    stampReceived(info);
    // Claude keeps advancing even while Codex is selected. Alert evaluation is
    // independent of which panel is visible; rendering it below is not.
    checkQuotaAlerts(info, info.source || 'claude');
    // This endpoint reports CLAUDE's quota — a live read of its local cache.
    // Codex's comes from the scanned transcript series instead, so applying
    // this while Codex is on screen would quietly replace its panel with the
    // other assistant's numbers every thirty seconds.
    lastClaudeLimits = info;
    if (selectedSource !== 'claude') {
      if (lastPlanInfo) renderPlanLimits(lastPlanInfo);
      return;
    }
    applyPlanLimits(info);
  } catch (e) {
    // Keep the last reading, but re-render it so its stated age keeps climbing.
    // Freezing the age here would make a dead connection look like a fresh one —
    // the exact failure this whole mechanism exists to end.
    if (lastPlanInfo) renderPlanLimits(lastPlanInfo);
  }
}

function startPlanLimitsPoll() {
  if (planPollTimer) clearInterval(planPollTimer);
  planPollTimer = setInterval(refreshPlanLimits, PLAN_POLL_SECONDS * 1000);
}

// ── Data loading ───────────────────────────────────────────────────────────
// The last `generated_at` the server reported. Kept so the header line can be
// re-rendered without a fetch — flipping the refresh interval has to update the
// note immediately, not at the next poll (which, when the new value is "off",
// would never come).
let lastGeneratedAt = '';

function refreshIntervalLabel(seconds) {
  if (seconds < 60) return seconds + 's';
  if (seconds < 3600) return (seconds / 60) + 'm';
  return (seconds / 3600) + 'h';
}

function updateMetaNote() {
  const meta = document.getElementById('meta');
  if (!meta) return;
  let note = '';
  if (!rangeIncludesToday(selectedRange)) {
    // A historical range cannot gain rows, so polling it would be pure churn.
    note = '';
  } else if (refreshSeconds > 0) {
    note = '<br>Auto-refresh every ' + refreshIntervalLabel(refreshSeconds);
  } else {
    note = '<br>Auto-refresh off';
  }
  // refreshIntervalLabel only ever formats a number from the frozen
  // REFRESH_OPTIONS list, so it carries no untrusted text into innerHTML;
  // generated_at comes from the server and keeps its esc().
  meta.innerHTML = (showingSavedData() ? 'Saved: ' : 'Updated: ') + esc(lastGeneratedAt) + note;
}

async function loadData(source, showScanProgressState = true, existingToken = null) {
  const wanted = source || selectedSource;
  const loadToken = existingToken || {};
  if (existingToken === null) latestDataLoad.set(wanted, loadToken);
  const isLatest = () => latestDataLoad.get(wanted) === loadToken;
  const ownsScreen = () => isLatest() && wanted === selectedSource;
  // Network, status and JSON failures can be transient. Once rendering begins,
  // repeating the same deterministic client exception would only spin forever.
  let retryableFailure = true;
  try {
    if (startupRefresh && !await startupRefresh) return;
    if (!isLatest()) return;
    const resp = await apiFetch('/api/data?source=' + encodeURIComponent(wanted));
    // A rejected token is not a transient failure — retrying it every three
    // seconds forever cannot succeed, and "Forbidden — retrying…" tells the
    // reader neither what is wrong nor what to do. It is the same situation as
    // arriving with no token at all, so it gets the same screen: what happened,
    // and the command that produces a working link.
    if (resp.status === 403) { showAuthNotice('rejected'); return; }
    if (resp.status === 409 || resp.status === 503) {
      // A tracked scan crossed the server's multi-query payload build. The
      // body is intentionally discarded: it can mix incremental commits. A
      // source the reader already left does not own the current overlay.
      if (!ownsScreen()) return;
      invalidateUsageData(wanted, loadToken);
      if (!await waitForScanToFinish(showScanProgressState, ownsScreen)) return;
      if (!ownsScreen()) return;
      // This response proves some scan crossed the read, including a scan
      // started by another tab. It can have changed every assistant, so drop
      // every pre-scan cache and invalidate requests that began before it.
      // Start a new logical load rather than carrying this now-invalid token.
      invalidateUsageData();
      if (wanted !== selectedSource) return;
      return loadData(selectedSource, showScanProgressState);
    }
    const d = await resp.json();
    // A later request for this same source has already taken ownership. Unlike
    // a merely off-screen response, this body is stale even for its own cache.
    if (!isLatest()) return;
    if (d.error) {
      // The same staleness rule as below, for the same reason: everything in
      // here is the screen. An abandoned source's error used to clear the
      // overlay belonging to the fetch the reader is actually waiting for, write
      // its retry notice over that source's header, and — because this error
      // means "no database yet", which is a first load, which is when rawData is
      // null — re-arm itself every three seconds. The source on screen has its
      // own fetch and its own retry.
      if (!ownsScreen()) return;
      // A database the server cannot read is permanent for the life of that
      // process — a foreign file, a damaged one, a refused path, a read-only
      // mount. Retrying it every three seconds cannot succeed, and
      // "Failed to read the usage database — retrying…" tells the reader
      // neither what is wrong nor what to do; the terminal beside them has
      // said both since 2026-08-16. Same judgement as the 403 above, and the
      // same answer: a screen that explains it, not a spinner that lies.
      if (d.permanent) { showDatabaseNotice(); return; }
      // The server binds and serves before the initial scan finishes, so on a
      // fresh start the DB may not exist yet. Show a non-destructive notice and
      // retry instead of nuking the page — once the background scan creates the
      // DB, the next poll renders normally.
      const meta = document.getElementById('meta');
      const recovery = d.recovery === 'scan'
        ? ' — use ' + APP_COMMANDS.scan : '';
      const message = d.error + recovery + ' — retrying…';
      const hasCurrentPayload = rawData !== null && renderedSource === wanted;
      if (hasCurrentPayload) {
        clearLoading();
        if (showingSavedData()) showScanFailure(false);
        if (meta) meta.innerHTML = esc(message);
      } else {
        // The only payload underneath belongs to another assistant (or there is
        // none). Keep it visibly provisional instead of revealing it under the
        // newly selected source's title.
        showProgress(message);
        setTimeout(() => {
          if (ownsScreen()) loadData(wanted, showScanProgressState, loadToken);
        }, SCAN_STATUS_RETRY_MS);
      }
      return;
    }
    retryableFailure = false;
    publishData(wanted, d);
  } catch(e) {
    // Anything thrown while rendering used to vanish into the console, leaving
    // a blank page with no explanation — the reader cannot tell a broken build
    // from an empty database. Say something on screen, and keep the console
    // trace for the detail.
    if (!ownsScreen()) return;
    console.error(e);
    const hasCurrentPayload = rawData !== null && renderedSource === wanted;
    if (retryableFailure && !hasCurrentPayload) {
      showProgress('Could not load usage data — retrying…');
      setTimeout(() => {
        if (ownsScreen()) loadData(wanted, showScanProgressState, loadToken);
      }, SCAN_STATUS_RETRY_MS);
      return;
    }
    clearLoading();
    const meta = document.getElementById('meta');
    if (showingSavedData()) showScanFailure(false);
    // Not gated on the first load: a poll that starts failing is exactly as
    // worth saying out loud, and by then rawData is already set.
    if (meta) {
      meta.textContent = 'Could not render the dashboard: ' + String(e && e.message || e);
    }
  }
}

function publishData(wanted, d, savedAt = null) {
  // Both quota readings ride in on this response, so both are stamped here.
  // Either can be parked for a long time before it is ever rendered — Claude's
  // arrives with a Codex payload too, and Codex's arrives ONLY here, with
  // auto-refresh off by default — and the age the panel states has to count
  // from now regardless.
  if (savedAt !== null) {
    for (const info of [d.subscription_limits, d.codex_limits]) {
      if (info && typeof info === 'object') info._received_at = savedAt * 1000;
    }
    savedSources.add(wanted);
  } else {
    stampReceived(d.subscription_limits);
    stampReceived(d.codex_limits);
    savedSources.delete(wanted);
    snapshotRefreshFailed = false;
  }
  // Both readings are already in this response. Baseline and advance both so
  // a threshold on the assistant off screen is not silently ignored until a
  // source switch, at which point first-sighting suppression would lose it.
  if (savedAt === null) {
    checkQuotaAlerts(d.subscription_limits, 'claude');
    checkQuotaAlerts(d.codex_limits, 'codex');
  }
  // Caching is about the FETCH and is source-independent, so it happens either
  // way: this payload was asked for and is correct for its own source, and a
  // switch to that source must not have to ask again. Same for Claude's own
  // quota reading, which every payload carries whichever source it covers.
  loadedSources.set(wanted, d);
  // Claude's own reading, kept so switching sources does not need a refetch —
  // and so Codex's panel can never be left showing Claude's number.
  if (savedAt === null || lastClaudeLimits === null) {
    lastClaudeLimits = d.subscription_limits;
  }
  // Everything below this line is what is ON SCREEN, so the staleness check
  // has to come first. It used to sit under the whole block: an abandoned
  // response had already replaced rawData and rebuilt the model filter for a
  // source nobody was looking at, and the return then skipped the
  // clearLoading() below — leaving a spinner over a page reading zeros.
  if (wanted !== selectedSource) return;

  lastGeneratedAt = d.generated_at;
  // Below the staleness check on purpose: this is on-screen chrome, and an
  // abandoned source's answer must not write over the source being read. It
  // is also outside `applyFilter`, because the claim is about the database
  // rather than the range — no filter selection can make it true or false.
  renderDatabaseNotice(d.unscanned);

  const isFirstLoad = rawData === null;
  rawData = d;
  renderedSource = wanted;
  updateMetaNote();
  renderSourceSwitch();

  if (filterBuiltFor !== wanted) {
    // Restore range from URL into the dropdown
    selectedRange = readURLRange();
    const rangeSel = document.getElementById('range-select');
    if (rangeSel) rangeSel.value = selectedRange;
    // …and the daily chart's order, which the URL carries for the same reason
    // it carries the range: both describe what is on screen.
    const dailySort = readURLDailySort();
    dailySortKey = dailySort.key;
    dailySortDir = dailySort.dir;
    // Mark default TZ button active
    document.querySelectorAll('.tz-btn').forEach(btn =>
      btn.classList.toggle('active', btn.dataset.tz === hourlyTZ)
    );
    // Build model filter (reads URL for model selection too)
    buildFilterUI(sourceModels(wanted), isFirstLoad);
    filterBuiltFor = wanted;
    updateSortIcons();
    updateModelSortIcons();
    updateProjectSortIcons();
    updateProjectBranchSortIcons();
  } else {
    // Same source, a later poll: fold in any model the server has started
    // reporting, keeping whatever the reader has since unchecked.
    mergeNewlySeenModels(sourceModels(wanted));
  }
  clearLoading();
  updateSnapshotStatus();
  applyFilter();
}

let autoRefreshTimer = null;
let autoRefreshInFlight = false;

// How often to poll, in ms; 0 means don't. Split out from scheduleAutoRefresh so
// the decision can be asserted in a test without arming a real timer (a live
// setInterval keeps the node harness alive past its own timeout).
function refreshIntervalMs() {
  return (refreshSeconds > 0 && rangeIncludesToday(selectedRange))
    ? refreshSeconds * 1000
    : 0;
}

// An auto-refresh tick scans before querying fresh data. Do not overlap ticks
// or treat a successful database read as evidence that transcripts have been
// ingested.
async function autoRefreshTick() {
  // Startup owns the first scan/source/data transaction. A remembered 15-second
  // interval must not race it, fetch the module-default source, and clear the
  // startup overlay before start() has selected and rendered the real source.
  // setInterval also does not await an async callback: one slow manual scan
  // could otherwise accumulate another status poller every 15 seconds, then
  // release all of them into concurrent payload renders when it finished.
  if (rawData === null || startupInProgress || autoRefreshInFlight) return;
  autoRefreshInFlight = true;
  try {
    let scanCompleted = false;
    try {
      const resp = await apiFetch('/api/rescan', { method: 'POST' });
      // 403: the token is bad and no amount of polling will fix it.
      if (resp.status === 403) { showAuthNotice('rejected'); return; }
      if (resp.status === 409) {
        // Keep automatic refresh non-blocking: the already-rendered payload is
        // honest but stale, so covering it every 15 seconds would interrupt the
        // reader for background work. It still must not re-read an incrementally
        // committed database, though; wait silently and render only after idle.
        try {
          if (!await waitForScanToFinish(false)) return;
          scanCompleted = true;
        } catch (e) {
          return;  // the next scheduled tick retries without accepting partial data
        }
      } else if (!resp.ok) {
        showScanFailure(false);
        return;
      } else {
        const result = await resp.json();
        renderDockerStatus(result.docker);
        scanCompleted = true;
      }
    } catch (e) {
      // The scan could not be reached; re-read what is already stored rather than
      // skipping the tick entirely.
    }
    if (scanCompleted) invalidateUsageData();
    await loadData(undefined, false);
  } finally {
    autoRefreshInFlight = false;
  }
}

function scheduleAutoRefresh() {
  if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
  const ms = refreshIntervalMs();
  if (ms) autoRefreshTimer = setInterval(autoRefreshTick, ms);
}

// ── Theme ───────────────────────────────────────────────────────────────────
// Three states, not two. "system" is the default and is handled entirely in CSS
// by @media (prefers-color-scheme), so a reader who has never touched the toggle
// gets their OS theme with no JavaScript and therefore no flash of the wrong one.
// Only an explicit choice sets data-theme, and that attribute has to win in both
// directions — light on a dark OS and dark on a light one.
const THEME_KEY = 'claude-usage-theme';

function systemTheme() {
  return (typeof matchMedia === 'function'
          && matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark';
}

function storedTheme() {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return (v === 'light' || v === 'dark') ? v : null;
  } catch (e) { return null; }   // private mode / disabled storage
}

// What is actually on screen right now, whatever the reason.
function activeTheme() {
  return storedTheme() || systemTheme();
}

function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === 'light' || theme === 'dark') root.setAttribute('data-theme', theme);
  else root.removeAttribute('data-theme');
  renderThemeButton();
  // The charts hold colours copied at construction time, so they have to be
  // re-read AND redrawn; without the redraw the page changes around a chart
  // still painted for the old theme.
  syncChartColors();
  if (rawData) applyFilter();
}

function renderThemeButton() {
  const btn = document.getElementById('theme-btn');
  if (!btn) return;
  const now = activeTheme();
  // The button says what you will GET, not what you are looking at — a control
  // labelled with the current state reads as a status display and gets ignored.
  const next = now === 'light' ? 'dark' : 'light';
  btn.textContent = next === 'light' ? '☀ Light' : '☾ Dark';
  btn.title = 'Switch to the ' + next + ' theme';
}

function toggleTheme() {
  const next = activeTheme() === 'light' ? 'dark' : 'light';
  try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* not fatal */ }
  applyTheme(next);
}

function initTheme() {
  const stored = storedTheme();
  if (stored) applyTheme(stored);
  else { renderThemeButton(); syncChartColors(); }
  // Follow the OS while no explicit choice has been made. Someone whose machine
  // flips at sunset expects the dashboard to follow; someone who picked a theme
  // expects it to stay picked.
  if (typeof matchMedia === 'function') {
    const mq = matchMedia('(prefers-color-scheme: light)');
    const onChange = () => { if (!storedTheme()) applyTheme(null); };
    if (typeof mq.addEventListener === 'function') mq.addEventListener('change', onChange);
    else if (typeof mq.addListener === 'function') mq.addListener(onChange);
  }
}

// ── Footer meta ─────────────────────────────────────────────────────────────
// APP_CONFIG is injected server-side. External links are user-initiated only;
// the dashboard never performs an automatic internet request.
const REPO_URL = 'https://github.com/mlizaso/claude-usage';

function appendFooterLink(container, label, href, needsSeparator) {
  if (needsSeparator) container.appendChild(document.createTextNode(' · '));
  const link = document.createElement('a');
  link.href = href;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.textContent = label;
  container.appendChild(link);
}

// The pricing caveat, for the assistant actually on screen.
//
// This was one static sentence citing Anthropic's rate card and the model
// keywords `fable/mythos/opus/sonnet/haiku`. It stayed on screen under Codex,
// where the vendor is different, the rate card is different, and not one of those
// keywords matches a Codex model — so the page told the reader its Codex figures
// came from a page that does not price Codex.
//
// innerHTML with esc(), matching renderSourceSwitch beside it rather than the
// DOM-node style initFooterMeta uses: every string here is a literal or comes
// from our own frozen ESTIMATED_RATE_MODELS, and the JS tests swap
// getElementById for a plain object that records innerHTML and has no
// appendChild — so node-building here would only be testable by not testing it.
function pricingNoteHTML(source) {
  if (source === 'codex') {
    return 'Cost estimates based on OpenAI API pricing '
      + '(<a href="https://openai.com/api/pricing" target="_blank" rel="noopener noreferrer">'
      + 'openai.com/api/pricing</a>) as of August 2026. A Codex plan is a '
      + 'subscription with a weekly quota and no per-token price anywhere in its '
      + 'data, so these figures are what the same tokens would have cost through '
      + 'the API &mdash; not a bill. Rates for '
      + ESTIMATED_RATE_MODELS.map(m => '<em>' + esc(m) + '</em>').join(' and ')
      + ' are estimates: they appear in the transcripts but on no published price '
      + 'list. Set <code>CLAUDE_USAGE_RATES</code> to override any of them.';
  }
  return 'Cost estimates based on Anthropic API pricing '
    + '(<a href="https://claude.com/pricing#api" target="_blank" rel="noopener noreferrer">'
    + 'claude.com/pricing#api</a>) as of June 2026. Only models containing '
    + '<em>fable</em>, <em>mythos</em>, <em>opus</em>, <em>sonnet</em>, or '
    + '<em>haiku</em> in the name are included in cost calculations. Actual costs '
    + 'for Max/Pro subscribers differ from API pricing.';
}

function renderFooterPricingNote() {
  const el = document.getElementById('footer-pricing');
  if (!el) return;
  el.innerHTML = pricingNoteHTML(selectedSource);
}

function initFooterMeta() {
  const el = document.getElementById('footer-meta');
  if (!el) return;
  const v = APP_CONFIG.version || '';
  if (v) {
    el.appendChild(document.createTextNode('Version '));
    appendFooterLink(el, 'v' + v, REPO_URL + '/releases/tag/v' + encodeURIComponent(v), false);
  }
}

// ── Section nav + collapsible cards ─────────────────────────────────────────
// The dashboard is one long scroll. The sticky jump bar teleports between
// sections; collapsible cards fold away the ones you don't use. Collapse state
// persists per card in localStorage and is independent of in-table Show
// more/less (which only pages rows within a single table).
const COLLAPSE_KEY = 'cu_collapsed_cards';
const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function loadCollapsedSet() {
  try { return new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY) || '[]')); }
  catch (e) { return new Set(); }
}
function saveCollapsedSet(set) {
  try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...set])); } catch (e) {}
}

// Charts created while their card is collapsed (display:none) lay out at zero
// size; resize them once the card is shown again so Chart.js repaints to fit.
function resizeChartsIn(card) {
  card.querySelectorAll('canvas').forEach(cv => {
    const ch = Object.values(charts).find(c => c && c.canvas === cv);
    if (ch) ch.resize();
  });
}

function setCardCollapsed(card, collapsed) {
  card.classList.toggle('collapsed', collapsed);
  const title = card.querySelector('h2, .section-title');
  if (title) title.setAttribute('aria-expanded', String(!collapsed));
}

function toggleCard(card) {
  const collapsed = !card.classList.contains('collapsed');
  setCardCollapsed(card, collapsed);
  const set = loadCollapsedSet();
  if (collapsed) set.add(card.dataset.card); else set.delete(card.dataset.card);
  saveCollapsedSet(set);
  if (!collapsed) requestAnimationFrame(() => resizeChartsIn(card));
}

function jumpToSection(id) {
  const el = document.getElementById(id);
  if (!el) return;
  if (el.dataset.card && el.classList.contains('collapsed')) toggleCard(el);  // expand before scrolling
  el.scrollIntoView({ behavior: prefersReducedMotion ? 'auto' : 'smooth', block: 'start' });
}

function initSectionNav() {
  const bar = document.getElementById('jump-bar');
  const container = document.querySelector('.container');
  if (!container) return;

  // Keep --jump-h synced to the bar's real height so scroll-margin clears it
  // even when the links wrap to a second row on a narrow panel.
  const syncJumpHeight = () => {
    if (bar) document.documentElement.style.setProperty('--jump-h', bar.offsetHeight + 'px');
  };
  syncJumpHeight();
  window.addEventListener('resize', syncJumpHeight);

  // Restore persisted collapse state + make each title an accessible toggle.
  const collapsed = loadCollapsedSet();
  document.querySelectorAll('[data-card]').forEach(card => {
    const title = card.querySelector('h2, .section-title');
    if (title) {
      title.setAttribute('role', 'button');
      title.setAttribute('tabindex', '0');
      title.title = 'Collapse / expand section';
    }
    setCardCollapsed(card, collapsed.has(card.dataset.card));
  });

  // Toggle a card from its title (caret included). Inner controls (CSV, TZ, sort
  // headers) sit outside the title selector, so they keep their own behaviour.
  const TITLE_SEL = '.chart-card > h2, .chart-header > h2, .table-card > .section-title, .section-header > .section-title';
  const onTitleActivate = (e) => {
    if (e.target.closest('.info-icon')) return;  // info tooltip, not a collapse toggle
    if (e.type === 'keydown') { if (e.key !== 'Enter' && e.key !== ' ') return; e.preventDefault(); }
    const title = e.target.closest(TITLE_SEL);
    const card = title && title.closest('[data-card]');
    if (card) toggleCard(card);
  };
  container.addEventListener('click', onTitleActivate);
  container.addEventListener('keydown', onTitleActivate);

  // Jump links teleport to a section (expanding it first if collapsed). Blur the
  // clicked item so the hover/focus dropdown it lives in closes after the jump.
  const closeMenus = (except) => document.querySelectorAll('.jump-menu.open')
    .forEach(m => {
      if (m === except) return;
      m.classList.remove('open');
      const t = m.querySelector('.jump-trigger');
      if (t) t.setAttribute('aria-expanded', 'false');
    });

  if (bar) bar.addEventListener('click', (e) => {
    const link = e.target.closest('.jump-link');
    if (link) { jumpToSection(link.dataset.target); link.blur(); closeMenus(null); }
  });

  // Mirror open/closed state on the menu triggers for assistive tech, and let
  // Escape close an open menu.
  const finePointer = window.matchMedia
    && window.matchMedia('(hover: hover) and (pointer: fine)').matches;
  document.querySelectorAll('.jump-menu').forEach(menu => {
    const trig = menu.querySelector('.jump-trigger');
    const sync = (open) => { if (trig) trig.setAttribute('aria-expanded', String(open)); };
    if (trig) {
      // On a pointing device a mouse click must not focus (and thus pin) the
      // trigger, or the panel stays open after the pointer leaves and fights the
      // next hover. Tab focus still works, since it doesn't go through mousedown.
      // On touch this same preventDefault was what made the menu unopenable —
      // no hover, and no focus either — so it is applied only where hover exists.
      if (finePointer) trig.addEventListener('mousedown', (e) => e.preventDefault());
      trig.addEventListener('click', (e) => {
        e.stopPropagation();
        const open = !menu.classList.contains('open');
        closeMenus(menu);
        menu.classList.toggle('open', open);
        sync(open);
      });
    }
    menu.addEventListener('mouseenter', () => sync(true));
    menu.addEventListener('mouseleave', () => sync(false));
    menu.addEventListener('focusin', () => sync(true));
    menu.addEventListener('focusout', () => sync(false));
    menu.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      menu.classList.remove('open');
      sync(false);
      if (document.activeElement) document.activeElement.blur();
    });
  });
  // A tap anywhere else dismisses an open menu.
  document.addEventListener('click', () => closeMenus(null));

  // Scroll-spy: highlight the link for the topmost section under the bar, and
  // mark the parent Graphs/Tables trigger so the closed menu shows where you are.
  const links = [...document.querySelectorAll('.jump-link')];
  const menus = [...document.querySelectorAll('.jump-menu')];
  const targets = links.map(l => document.getElementById(l.dataset.target)).filter(Boolean)
    .sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);
  let spyScheduled = false;
  const updateActive = () => {
    spyScheduled = false;
    const line = (bar ? bar.offsetHeight : 45) + 16;
    let activeId = targets.length ? targets[0].id : null;
    for (const t of targets) {
      if (t.getBoundingClientRect().top - line <= 1) activeId = t.id; else break;
    }
    // At the very bottom the last (often short) section may never reach the line.
    if (targets.length && (window.innerHeight + window.scrollY) >= document.body.scrollHeight - 4)
      activeId = targets[targets.length - 1].id;
    links.forEach(l => l.classList.toggle('active', l.dataset.target === activeId));
    menus.forEach(menu => {
      const trig = menu.querySelector('.jump-trigger');
      if (trig) trig.classList.toggle('active', !!menu.querySelector('.jump-link.active'));
    });
  };
  window.addEventListener('scroll', () => {
    if (!spyScheduled) { spyScheduled = true; requestAnimationFrame(updateActive); }
  }, { passive: true });
  updateActive();
}

// ── Daily chart panning ────────────────────────────────────────────────────
// Two ways to move the window, because neither alone covers both devices: the
// scrollbar under the chart (precise, and the affordance that says "there is
// more"), and dragging the plot itself (what you actually reach for, and the
// only comfortable option on a phone).
function initDailyPan() {
  const bar = document.getElementById('daily-pan');
  if (bar) bar.addEventListener('scroll', onDailyPanScroll, { passive: true });

  // Keyboard panning, on the focusable wrapper rather than the canvas: a canvas
  // cannot take focus on its own, and without this the window — which now hides
  // most of a 90-day or year-to-date range — was reachable only with a pointer.
  const wrap = document.getElementById('daily-chart-wrap');
  if (wrap) wrap.addEventListener('keydown', (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey) return;   // leave browser shortcuts alone
    if (dailyPanByKey(e.key)) e.preventDefault();
  });

  // Sorting the chart from the per-series panel. Delegated, because the panel is
  // rebuilt on every render.
  const stats = document.getElementById('daily-stats');
  if (stats) stats.addEventListener('click', (e) => {
    const btn = e.target.closest && e.target.closest('[data-daily-sort]');
    if (btn) setDailySort(btn.dataset.dailySort);
  });

  const canvas = document.getElementById('chart-daily');
  const card = document.getElementById('sec-daily');
  if (!canvas || !card) return;

  let pointerId = null, startX = 0, startOffset = 0, moved = false;

  canvas.addEventListener('pointerdown', (e) => {
    if (!dailyWindowLen) return;          // nothing to pan
    if (e.button !== undefined && e.button !== 0) return;
    pointerId = e.pointerId;
    startX = e.clientX;
    startOffset = dailyPanOffset;
    moved = false;
  });

  canvas.addEventListener('pointermove', (e) => {
    if (pointerId === null || e.pointerId !== pointerId) return;
    const dx = e.clientX - startX;
    // A few pixels of slack so a click meant for a tooltip is not read as a
    // drag. Once it IS a drag, capture the pointer so leaving the canvas
    // mid-gesture doesn't strand it.
    if (!moved) {
      if (Math.abs(dx) < 4) return;
      moved = true;
      card.classList.add('dragging');
      try { canvas.setPointerCapture(pointerId); } catch (err) {}
    }
    // Drag right to go back in time, like dragging a sheet of paper.
    setDailyPanOffset(startOffset - dx / dailyColumnWidth());
  });

  const endDrag = (e) => {
    if (pointerId === null || (e && e.pointerId !== undefined && e.pointerId !== pointerId)) return;
    try { canvas.releasePointerCapture(pointerId); } catch (err) {}
    pointerId = null;
    card.classList.remove('dragging');
  };
  canvas.addEventListener('pointerup', endDrag);
  canvas.addEventListener('pointercancel', endDrag);

  // Trackpad horizontal swipe, and shift+wheel — the same gesture a wide table
  // would take. Vertical wheel is left alone so the page still scrolls.
  canvas.addEventListener('wheel', (e) => {
    if (!dailyWindowLen) return;
    const dx = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : (e.shiftKey ? e.deltaY : 0);
    if (!dx) return;
    e.preventDefault();
    setDailyPanOffset(dailyPanOffset + dx / dailyColumnWidth());
  }, { passive: false });

  // The window is sized from the container's width, so a resize changes how
  // many days fit. Re-render from the data already on hand.
  //
  // Re-rendered from dailyRangeRows, NOT from lastDailyRows: the latter is the
  // rows in display order, so feeding it back would bake the current sort into
  // the range and make chronological unreachable.
  let resizeScheduled = false;
  window.addEventListener('resize', () => {
    if (resizeScheduled || !dailyRangeRows.length) return;
    resizeScheduled = true;
    setTimeout(() => {
      resizeScheduled = false;
      if (dailyRangeRows.length) renderDailyChart(dailyRangeRows);
    }, 200);
  });
}

const SORT_ACTIONS = Object.freeze({
  model: setModelSort,
  session: setSessionSort,
  project: setProjectSort,
  'project-branch': setProjectBranchSort,
});
const EXPORT_ACTIONS = Object.freeze({
  dispatches: exportDispatchesCSV,
  sessions: exportSessionsCSV,
  projects: exportProjectsCSV,
  'project-branches': exportProjectBranchCSV,
});
// Every table's footer controls are dispatched through this map by name. A
// table missing from it renders its "Show more" / "Show less" / "Download CSV"
// links as dead text — that is what happened to Cost by Model, whose three
// actions were never registered here.
const TABLE_ACTIONS = Object.freeze({
  lessDispatchRows, moreDispatchRows, exportDispatchesCSV,
  lessSessionRows, moreSessionRows, exportSessionsCSV,
  lessModelRows, moreModelRows, exportModelCSV,
  lessProjectRows, moreProjectRows, exportProjectsCSV,
  lessBranchRows, moreBranchRows, exportProjectBranchCSV,
});

function initControls() {
  document.getElementById('rescan-btn').addEventListener('click', triggerRescan);
  const scanRetry = document.getElementById('scan-retry');
  if (scanRetry) scanRetry.addEventListener('click', triggerRescan);
  const themeBtn = document.getElementById('theme-btn');
  if (themeBtn) themeBtn.addEventListener('click', toggleTheme);
  const alertInput = document.getElementById('alert-input');
  if (alertInput) {
    // Enter submits, because typing a number and pressing return is what people
    // do; and typing clears a stale complaint rather than leaving it accusing
    // the value they have just corrected.
    alertInput.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      if (addCustomThreshold(alertInput.value)) alertInput.value = '';
    });
    alertInput.addEventListener('input', () => showAlertError(''));
  }

  const appTitle = document.getElementById('app-title');
  if (appTitle) appTitle.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    if (!appTitle.dataset.switchTo) return;
    e.preventDefault();
    setSource(appTitle.dataset.switchTo);
  });
  document.getElementById('model-trigger').addEventListener('click', toggleModelPanel);
  document.getElementById('select-all-models').addEventListener('click', selectAllModels);
  document.getElementById('clear-all-models').addEventListener('click', clearAllModels);
  document.getElementById('range-select').addEventListener('change', e => setRange(e.target.value));
  const refreshSel = document.getElementById('refresh-select');
  if (refreshSel) {
    refreshSel.value = String(refreshSeconds);
    refreshSel.addEventListener('change', e => setRefreshSeconds(e.target.value));
  }

  initDailyPan();

  document.querySelectorAll('.tz-btn').forEach(btn =>
    btn.addEventListener('click', () => setHourlyTZ(btn.dataset.tz))
  );
  document.querySelectorAll('[data-sort-group]').forEach(cell =>
    cell.addEventListener('click', () => SORT_ACTIONS[cell.dataset.sortGroup](cell.dataset.sortKey))
  );
  document.querySelectorAll('[data-export]').forEach(btn =>
    btn.addEventListener('click', () => EXPORT_ACTIONS[btn.dataset.export]())
  );

  document.getElementById('model-checkboxes').addEventListener('change', e => {
    const input = e.target.closest('.model-cb-input');
    if (input) onModelToggle(input);
  });
  document.addEventListener('click', e => {
    const picked = e.target.closest('#source-chooser [data-source]');
    if (picked) { chooseSource(picked.dataset.source); return; }
    const switched = e.target.closest('#source-switch [data-source]');
    if (switched) { setSource(switched.dataset.source); return; }
    const title = e.target.closest('#app-title[data-switch-to]');
    if (title) { setSource(title.dataset.switchTo); return; }
    // Every alert control now carries the WINDOW it belongs to: thresholds are
    // per limit, so a click that does not say which limit it meant cannot be
    // acted on. The key comes from the element the server's payload named, not
    // from anything this file derives.
    const chip = e.target.closest('[data-threshold]');
    if (chip) {
      toggleAlertThreshold(Number(chip.dataset.threshold), chip.dataset.window);
      return;
    }
    const drop = e.target.closest('[data-remove]');
    if (drop) {
      removeThreshold(Number(drop.dataset.remove), drop.dataset.window);
      return;
    }
    const add = e.target.closest('[data-add-window]');
    if (add) {
      const key = add.dataset.addWindow;
      const input = document.querySelector(
        '.alert-input[data-window="' + (window.CSS && CSS.escape ? CSS.escape(key) : key) + '"]');
      if (input && addCustomThreshold(input.value, key)) input.value = '';
      return;
    }
    if (e.target.closest('#quota-alert')) { dismissAlertBanner(); return; }
    const control = e.target.closest('[data-table-action]');
    if (!control) return;
    e.preventDefault();
    const action = TABLE_ACTIONS[control.dataset.tableAction];
    if (action) action();
  });
}

// Before initControls, so the button carries its label from the first frame and
// the charts are built with the right palette rather than rebuilt after.
initTheme();
initFooterMeta();
// Paint it before any fetch: the footer is on screen from the first frame, and
// an empty paragraph there reads as a missing disclaimer rather than a pending
// one. Defaults to Claude, which is also what `selectedSource` starts as.
renderFooterPricingNote();
initControls();
initSectionNav();
if (API_TOKEN) {
  bootDashboard();
  scheduleAutoRefresh();
  startPlanLimitsPoll();
} else {
  showAuthNotice();
}

function onAuthHashChange() {
  if (readApiTokenFromFragment()) window.location.reload();
}

function showDatabaseNotice() {
  // A sibling of showAuthNotice rather than another `reason` branch of it, and
  // deliberately so: that function's shared half — the `cli.py url --open`
  // remedy, the hashchange watcher, the retry button — is documented as having
  // to stay identical on every path through it, and none of those three is
  // right here. A different failure with a different remedy gets its own
  // function instead of loosening the rule that keeps that one honest.
  clearLoading();
  // Both polls are futile and both would run for the life of the tab: the data
  // poll re-reads the same unreadable file, and the plan poll's own endpoint
  // survives (it reads ~/.claude.json, not the database) but renders into a
  // panel this overlay covers. Stopped above the element check for the same
  // reason showAuthNotice stops them there — it must not depend on the notice
  // being in the document.
  if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
  if (planPollTimer) { clearInterval(planPollTimer); planPollTimer = null; }
  const meta = document.getElementById('meta');
  if (meta) meta.textContent = 'Database unreadable';
  // Rescan is disabled because it fails the same way: scan() opens the same
  // file through the same init_db that just refused it.
  for (const id of ['rescan-btn', 'refresh-select']) {
    const control = document.getElementById(id);
    if (control) control.disabled = true;
  }
  const notice = document.getElementById('auth-notice');
  if (!notice) return;
  notice.innerHTML =
    '<div class="chooser-card">'
    + '<div class="chooser-title">The usage database cannot be read</div>'
    + '<div class="chooser-sub">The server is running and this link is valid, '
    + 'but the file it reads is not a usable database — it may have been '
    + 'damaged, replaced by something else, or put on a read-only disk. This '
    + 'will not clear on its own, so the page has stopped retrying.</div>'
    // The precise reason — which file, and SQLite's own words — is deliberately
    // NOT sent here. Everything in this body is written into the document, and
    // that diagnosis carries a filesystem path; the terminal already prints it
    // in full, so this points there instead of copying it into the DOM.
    + '<div class="chooser-sub">Use this action — it names the file and the '
    + 'exact reason:</div>'
    + '<pre class="auth-cmd">' + esc(APP_COMMANDS.diagnose) + '</pre>'
    // The remedy names NO path, and that is a correction rather than caution:
    // it used to print `mv ~/.claude/usage.db ...`, which is the wrong file
    // whenever CLAUDE_USAGE_DB is set — the Dockerfile sets it, and AGENTS.md
    // tells users to set it per version to avoid the two-installs rebuild
    // loop. A reader following that line would have moved a database that was
    // working and still had a broken one. The command above prints the real
    // path; this one says to act on what it printed.
    + '<div class="chooser-sub">If it says the file is damaged, move the file '
    + 'it names aside and rebuild from your transcripts — nothing is lost that '
    + 'the transcripts still hold:</div>'
    + '<pre class="auth-cmd">' + esc(APP_COMMANDS.scan) + '</pre>'
    + '<div class="chooser-options"><button class="chooser-btn" id="db-retry">'
    + '<span class="chooser-name">Reload</span>'
    + '<span class="chooser-meta">after fixing it</span></button></div>'
    + '</div>';
  notice.hidden = false;
  const retry = document.getElementById('db-retry');
  if (retry) retry.addEventListener('click', () => window.location.reload());
}

// Refused by the server, for one of the two reasons there are.
//
// This used to write one grey line into the header and disable two buttons,
// which on a page with nothing else on it reads as "the app is broken" — there
// was no statement of what happened and no way out. It cannot simply fetch a
// token: the token is deliberately absent from this page so that another local
// account cannot recover it by requesting `/`. So the page says what happened
// and names the one command that gets it back.
//
// `reason` exists because it then said the WRONG thing three times out of four.
// The three 403 handlers share this screen with the genuine no-token boot, and
// it asserted the boot's diagnosis for all of them — "this tab opened the plain
// address" while the address bar plainly showed `#token=…`. A stale bookmark is
// in fact the likeliest way to get here: the server mints a fresh token on every
// start unless CLAUDE_USAGE_API_TOKEN is set. Only the title, the first
// paragraph and the button's label differ; the remedy below them recovers the
// link either way, so it is written once. The default keeps the bare call at the
// bootstrap — and any future one — meaning what it has always meant.
function showAuthNotice(reason = 'no-token') {
  // This notice replaces the page, and one of its four callers (loadData) is
  // reached with the loading overlay up. The overlay is fixed-position over
  // everything, so leaving it would bury the explanation behind a spinner that
  // never stops — a recoverable problem made to look like a hang. Clearing it
  // here rather than at each call site covers all four (start, loadData,
  // autoRefreshTick, the bootstrap), and any future one.
  clearLoading();
  // A rejected token cannot become valid while this page lives: the server's
  // token is fixed for the life of the process, and this document deliberately
  // cannot fetch a new one. So the two interval polls behind this screen can
  // only produce 403s for as long as the tab is open — one rescan POST every
  // 15s and one limits GET every 30s, forever. Stopped here rather than at each
  // call site, and ABOVE the `if (!notice) return` below, because stopping them
  // must not depend on the notice element being in the document.
  if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
  if (planPollTimer) { clearInterval(planPollTimer); planPollTimer = null; }
  const meta = document.getElementById('meta');
  if (meta) meta.textContent = 'Not connected';
  for (const id of ['rescan-btn', 'refresh-select']) {
    const control = document.getElementById(id);
    if (control) control.disabled = true;
  }
  const notice = document.getElementById('auth-notice');
  if (!notice) return;
  const rejected = reason === 'rejected';
  notice.innerHTML =
    '<div class="chooser-card">'
    + '<div class="chooser-title">'
    + (rejected ? 'The access token in this link is no longer accepted'
                : 'This link has no access token') + '</div>'
    + '<div class="chooser-sub">'
    + (rejected
       ? 'The dashboard mints a new token every time it starts, so a link kept '
         + 'from an earlier run is refused as soon as it restarts. '
       : 'The dashboard is running, but this tab opened the plain address. ')
    + 'The token is kept out of the page on purpose, so that no '
    + 'other account on this machine can read it by loading the same URL — which '
    + 'means this tab cannot fetch one for itself.</div>'
    + '<div class="chooser-sub">Use this action to get the link back:</div>'
    + '<pre class="auth-cmd">' + esc(APP_COMMANDS.reconnect) + '</pre>'
    + '<div class="chooser-sub">Or paste the URL the launcher printed when it '
    + 'started. Already opened it in another tab?</div>'
    // Kept on both branches rather than dropped on this one: it is the manual
    // counterpart to the hashchange watcher below, for an edit to the address
    // bar that does not fire one. But "try again" is a promise it cannot keep
    // against a token the server has already refused — reloading re-reads the
    // same stale link — so on that branch it names its own precondition.
    + '<div class="chooser-options"><button class="chooser-btn" id="auth-retry">'
    + '<span class="chooser-name">'
    + (rejected ? 'Reload with the new link' : 'Reload and try again') + '</span>'
    + '<span class="chooser-meta">re-reads the address bar</span></button></div>'
    + '</div>';
  notice.hidden = false;
  const retry = document.getElementById('auth-retry');
  // Reload rather than re-run the bootstrap: the token is read from the URL
  // fragment at load, so anything that changed it only takes effect on a reload.
  if (retry) retry.addEventListener('click', () => window.location.reload());

  // Pasting the authenticated URL into THIS tab changes only the fragment,
  // which is a same-document navigation — the page does not reload, the token
  // is never re-read, and the screen sits there looking broken while the
  // address bar shows a perfectly good link. Watch for a token arriving and
  // reload so it takes effect.
  //
  // The handler is named and defined once, above, rather than being a fresh
  // arrow here: addEventListener dedupes on (type, callback), so re-registering
  // the same function leaves exactly one live listener however often this
  // notice is re-rendered. A new closure per call accumulated one listener per
  // rejected poll — 240 an hour at the 15s setting.
  window.addEventListener('hashchange', onAuthHashChange);
}
