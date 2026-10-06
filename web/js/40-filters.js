// ── Model filter ───────────────────────────────────────────────────────────
function modelPriority(m) {
  const ml = m.toLowerCase();
  if (ml.includes('fable') || ml.includes('mythos')) return 0;
  if (ml.includes('opus'))   return 1;
  if (ml.includes('sonnet')) return 2;
  if (ml.includes('haiku'))  return 3;
  return 4;
}

function sortedModels(models) {
  return [...models].sort((a, b) => {
    const pa = modelPriority(a), pb = modelPriority(b);
    return pa !== pb ? pa - pb : a.localeCompare(b);
  });
}

// Compact display name for the collapsed trigger, e.g. "claude-opus-4-8" ->
// "Opus 4.8", "claude-fable-5" -> "Fable 5". Non-Anthropic ids fall back to the
// basename with any provider prefix and trailing date suffix stripped.
function shortModelName(m) {
  const ml = m.toLowerCase();
  let family = null;
  if (ml.includes('fable'))       family = 'Fable';
  else if (ml.includes('mythos')) family = 'Mythos';
  else if (ml.includes('opus'))   family = 'Opus';
  else if (ml.includes('sonnet')) family = 'Sonnet';
  else if (ml.includes('haiku'))  family = 'Haiku';
  if (family) {
    // Strip a trailing date stamp first. Anthropic ids carry one
    // (claude-opus-4-20250514), and matching the version against the raw id let
    // the date supply the digits: an id whose version is a single component
    // rendered as "Opus 4.20250514" instead of "Opus 4".
    const core = m.replace(/[-_]\d{6,}$/, '');
    const two = core.match(/(\d+)[._-](\d+)/);
    if (two) return family + ' ' + two[1] + '.' + two[2];
    const one = core.match(/(\d+)/);
    return one ? family + ' ' + one[1] : family;
  }
  let base = m.split('/').pop().split(':')[0];
  base = base.replace(/[-_]?\d{6,}.*$/, '');
  return base || m;
}

function readURLModels(allModels) {
  const param = new URLSearchParams(window.location.search).get('models');
  if (!param) return defaultModelSelection(allModels);
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  const kept = allModels.filter(m => fromURL.has(m));
  // A list that matches nothing here is a list for a different source (or a
  // stale link). Fall back rather than render an empty dashboard.
  return kept.length ? new Set(kept) : defaultModelSelection(allModels);
}

// Whether the current selection IS the default — which decides whether the URL
// needs a `models` parameter at all. Asks defaultModelSelection rather than
// restating its rule: two copies of "what does this source start with" would let
// a link stop round-tripping the moment one of them changed.
function isDefaultModelSelection(allModels) {
  const expected = defaultModelSelection(allModels);
  if (selectedModels.size !== expected.size) return false;
  for (const model of expected) if (!selectedModels.has(model)) return false;
  return true;
}

// `useURL` is false when SWITCHING source. The URL's `models` list belongs to
// the source that was on screen when it was written, and model ids do not
// overlap between assistants — so honouring it across a switch intersects to
// the empty set and every chart, table and total comes up blank. That reads as
// "switching is broken" rather than "your filter excluded everything", which is
// exactly how it was reported.
function buildFilterUI(allModels, useURL = true) {
  allModelsList = [...allModels];
  selectedModels = useURL ? readURLModels(allModels) : defaultModelSelection(allModels);
  renderModelCheckboxes();
}

// The selection a source starts with: everything priced, or everything when
// nothing is priced (so a source with no rate table is not shown as empty).
function defaultModelSelection(allModels) {
  const billable = allModels.filter(m => isBillable(m));
  return new Set(billable.length ? billable : allModels);
}

// Fold models the server has started reporting into the existing selection.
// /api/data is polled every 30s and re-read after a rescan, but the filter was
// only ever built on first load — so a model id that appeared later was absent
// from selectedModels, and every table, chart and total silently dropped its
// turns until the page was reloaded. Merging (rather than rebuilding) keeps
// whatever the user has since unchecked.
function mergeNewlySeenModels(allModels) {
  const known = new Set(allModelsList);
  const fresh = (allModels || []).filter(m => !known.has(m));
  if (!fresh.length) return false;
  const wasDefault = isDefaultModelSelection(allModelsList);
  allModelsList = [...allModelsList, ...fresh];
  // Keep the all-unpriced default useful as new model IDs arrive, while
  // retaining any models the user deliberately unchecked.
  const selectAll = wasDefault && !allModelsList.some(m => isBillable(m));
  for (const m of fresh) if (selectAll || isBillable(m)) selectedModels.add(m);
  renderModelCheckboxes();
  return true;
}

// The two groups are PRICED and UNPRICED, and the headings say so.
//
// They read "Anthropic" and "Other providers" while the split was
// `isBillable`, which is `getPricing(model) !== null` — a proxy for "is
// Anthropic" only until Codex support gave OpenAI ids published rates. After
// that a Codex dashboard filed `gpt-5.6-sol` under a heading reading
// "Anthropic", beneath a title reading "Codex Usage", and an unpriced
// Anthropic id landed under "Other providers" on a Claude one. Naming the
// vendor instead is not available: a classifier keyed on the pricing families
// could not classify the very ids that trigger this (an unpriced
// `gpt-oss-120b` IS an OpenAI model), and it would be the second source of
// truth isBillable exists to forbid. So the strings say what the predicate
// tests, which is true for both sources and both directions — and keeps the
// page's own distinction that "no published rate" is not "$0.00".
function renderModelCheckboxes() {
  const allModels = allModelsList;
  const sorted = sortedModels(allModels);
  const priced   = sorted.filter(m => isBillable(m));
  const unpriced = sorted.filter(m => !isBillable(m));
  const rowHTML = m => {
    const checked = selectedModels.has(m);
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${esc(m)}" title="${esc(m)}">
      <input class="model-cb-input" type="checkbox" value="${esc(m)}" ${checked ? 'checked' : ''}>
      <span class="model-cb-box">&#10003;</span>
      <span class="model-cb-text">${esc(m)}</span>
    </label>`;
  };
  let html = '';
  // Only show a group heading when both groups are present — a single-group
  // list doesn't need a label.
  const labelled = priced.length && unpriced.length;
  if (priced.length) {
    if (labelled) html += '<div class="model-group-label">Priced</div>';
    html += priced.map(rowHTML).join('');
  }
  if (unpriced.length) {
    if (labelled) html += '<div class="model-group-label">No published rate</div>';
    html += unpriced.map(rowHTML).join('');
  }
  document.getElementById('model-checkboxes').innerHTML = html;
  updateModelTriggerLabel();
}

// Collapsed trigger text, in priority order:
//   "All models"     — everything selected
//   "No models"      — nothing selected
//   "All priced"     — every model with a published rate selected and nothing
//                      else; "+N" if some unpriced ones are on too. That set is
//                      exactly what defaultModelSelection starts with, so this
//                      is the label for "the default, plus N".
//   "Fable 5, Opus 4.7 +5" — otherwise, first two names + overflow count
function updateModelTriggerLabel() {
  const labelEl = document.getElementById('model-trigger-label');
  if (!labelEl) return;
  const n = selectedModels.size;
  if (n === 0)                    { labelEl.textContent = 'No models';  return; }
  if (n === allModelsList.length) { labelEl.textContent = 'All models'; return; }
  const priced   = allModelsList.filter(m => isBillable(m));
  const unpriced = allModelsList.filter(m => !isBillable(m));
  if (priced.length && priced.every(m => selectedModels.has(m))) {
    // n < total (handled above), so when unpriced ones exist at least one is
    // unselected.
    const alsoOn = unpriced.filter(m => selectedModels.has(m)).length;
    labelEl.textContent = alsoOn ? 'All priced +' + alsoOn : 'All priced';
    return;
  }
  const chosen = sortedModels(allModelsList).filter(m => selectedModels.has(m));
  const shown = chosen.slice(0, 2).map(shortModelName);
  const extra = chosen.length - shown.length;
  labelEl.textContent = shown.join(', ') + (extra > 0 ? ' +' + extra : '');
}

function toggleModelPanel(event) {
  if (event) event.stopPropagation();
  const panel = document.getElementById('model-panel');
  const trigger = document.getElementById('model-trigger');
  const open = panel.hidden;
  panel.hidden = !open;
  trigger.classList.toggle('open', open);
  trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function closeModelPanel() {
  const panel = document.getElementById('model-panel');
  if (!panel || panel.hidden) return;
  panel.hidden = true;
  const trigger = document.getElementById('model-trigger');
  trigger.classList.remove('open');
  trigger.setAttribute('aria-expanded', 'false');
}

// Close the panel on outside click or Escape. Clicks inside #model-select
// (including the checkboxes and All/None) keep it open so multiple models can
// be toggled in one pass.
document.addEventListener('click', (e) => {
  const sel = document.getElementById('model-select');
  if (sel && !sel.contains(e.target)) closeModelPanel();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModelPanel(); });

function onModelToggle(cb) {
  const label = cb.closest('label');
  if (cb.checked) { selectedModels.add(cb.value);    label.classList.add('checked'); }
  else            { selectedModels.delete(cb.value); label.classList.remove('checked'); }
  updateModelTriggerLabel();
  updateURL();
  applyFilter();
}

function selectAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = true; selectedModels.add(cb.value); cb.closest('label').classList.add('checked');
  });
  updateModelTriggerLabel(); updateURL(); applyFilter();
}

function clearAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = false; selectedModels.delete(cb.value); cb.closest('label').classList.remove('checked');
  });
  updateModelTriggerLabel(); updateURL(); applyFilter();
}

// ── URL persistence ────────────────────────────────────────────────────────
// Stamped onto every history entry updateURL replaces, so a later boot can tell
// an address bar the page wrote itself from one the reader arrived on. It
// survives a reload — same entry — and is absent from a link opened in a fresh
// tab or pasted over this one, which is the distinction start() needs and could
// not make: `?source=` has to keep meaning "an instruction from whoever made
// the link", never a preference inferred from a past visit.
const SELF_WRITTEN_URL = Object.freeze({ cuSelfWritten: true });

function urlWasWrittenByThisPage() {
  // `typeof` because the JavaScript test harness runs the page without a
  // History object; every browser has one.
  return typeof history !== 'undefined'
    && !!(history.state && history.state.cuSelfWritten);
}

function readURLSource() {
  // Only a source the READER named. The page writes `?source=` into its own bar
  // so the view can be copied out of it, and reading that back on a reload
  // answered the chooser on their behalf — for Codex only, because `claude` was
  // omitted as the default, so the same action (choose, then reload) asked
  // again for one assistant and never again for the other.
  if (urlWasWrittenByThisPage()) return null;
  const p = new URLSearchParams(window.location.search).get('source');
  return SOURCES.includes(p) ? p : null;
}

function updateURL() {
  const allModels = Array.from(document.querySelectorAll('#model-checkboxes input')).map(cb => cb.value);
  const params = new URLSearchParams();
  // Written for BOTH assistants once the machine holds both. `source` is a
  // default only while there is nothing to choose between; with two histories
  // present, which one is on screen was chosen, and a link that omits it
  // reproduces the chooser rather than the view it was copied from. Omitting it
  // also destroyed an explicitly authored `?source=claude` on the reader's
  // first filter click while `?source=codex` survived, so a shared link was
  // durable for one of the two sources only.
  if (availableSources.length > 1 || selectedSource !== 'claude') {
    params.set('source', selectedSource);
  }
  if (selectedRange !== '30d') params.set('range', selectedRange);
  if (!isDefaultModelSelection(allModels)) params.set('models', Array.from(selectedModels).join(','));
  // The daily chart's order belongs here for the same reason the range and the
  // model list do: this URL describes WHAT IS SHOWN, and a chart sorted by
  // Output is not showing the same thing as one in date order. Omitted at the
  // default so an unshuffled link stays clean.
  if (dailySortKey !== 'day') params.set('sort', dailySortKey + '.' + dailySortDir);
  const search = params.toString() ? '?' + params.toString() : '';
  history.replaceState(SELF_WRITTEN_URL, '', window.location.pathname + search + AUTH_FRAGMENT);
}

// ── Session sort ───────────────────────────────────────────────────────────
function setSessionSort(col) {
  if (sessionSortCol === col) {
    sessionSortDir = sessionSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    sessionSortCol = col;
    sessionSortDir = 'desc';
  }
  updateSortIcons();
  applyFilter();
}

function updateSortIcons() {
  // Scope the clear to this table's own icons. `.sort-icon` matches every
  // sortable header on the page, so sorting Recent Sessions used to wipe the
  // direction arrows off the Model, Project and Project+Branch tables — which
  // stayed sorted, just with no indication of how.
  document.querySelectorAll('[id^="sort-icon-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('sort-icon-' + sessionSortCol);
  if (icon) icon.textContent = sessionSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

// `cost` is read off the row like every other column, never recomputed. A
// session row carries ONE "primary" model beside the totals of every model it
// used, so `calcCost(a.model, ...)` priced the whole row at its top-token one —
// the "anything holding one model per row cannot be priced" error, this time
// inside a comparator. The cell printed `sessionForSelection`'s honest
// per-model figure while the sort ranked by that other number, so a $13.50
// session sat below a $9.00 one with every printed figure correct. Reading
// `a[sessionSortCol]` makes this the same comparator sortProjects uses, and it
// is the same list `exportSessionsCSV` writes out.
function sortSessions(sessions) {
  return [...sessions].sort((a, b) => {
    let av, bv;
    if (sessionSortCol === 'duration_min') {
      av = parseFloat(a.duration_min) || 0;
      bv = parseFloat(b.duration_min) || 0;
    } else {
      av = a[sessionSortCol] ?? 0;
      bv = b[sessionSortCol] ?? 0;
    }
    if (av < bv) return sessionSortDir === 'desc' ? 1 : -1;
    if (av > bv) return sessionSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

// ── Aggregation & filtering ────────────────────────────────────────────────
// Sum a session's own rows into the row the table prints, costing each one at
// ITS OWN model. Returns null when nothing survived the filters, so the session
// drops out of the table entirely.
//
// `Last Active` and `Duration` are deliberately left as the SESSION's, not the
// slice's: they are properties of the session, and clipping them would make
// "Duration" mean something different in every range. The row therefore mixes
// two semantics on purpose, which is why those two cells say so — see
// renderSessionsTable.
//
// The displayed Model IS recomputed, over the models actually in the slice.
// Keeping the session's primary model on a narrowed row would name a model
// whose tokens are not in it.
//
// `out.branches` is the set of branches the SLICE ran on, and it is what the
// Sessions column of `Cost by Project & Branch` is counted from. `out.branch`
// stays the session's own label, which is what the sessions table and its CSV
// print. A part with no branch of its own resolves to that label, the same
// COALESCE `project_by_day_model` applies in SQL, so the money rows and this
// set key identically.
//
// There is exactly ONE live case for a blank part, and the comment beside that
// COALESCE in rollups.py is the authority on it: a Codex turn, which stores no
// branch at all. Two others look live and are not, so do not re-add them. A
// Claude record carrying no `gitBranch` is refuted by this repository's own
// measurement, recorded beside the fill-when-blank merge in
// `scanner.insert_turns`. A row stored before the column existed cannot reach
// this build, because such a database does not match the declared schema and is
// rebuilt and rescanned rather than backfilled. An older payload SHAPE is
// unreachable too, for the reason the `inSource` note below gives.
function sessionFromParts(s, parts) {
  if (!parts.length) return null;
  const out = { ...s, input: 0, output: 0, cache_read: 0, cache_creation: 0,
                cache_creation_1h: 0, turns: 0, cost: 0,
                cost_parts: { input: 0, output: 0, cache_read: 0, cache_creation: 0 },
                billable: false };
  const tokensByModel = new Map();
  const branches = new Set();
  for (const b of parts) {
    out.input += b.input; out.output += b.output;
    out.cache_read += b.cache_read; out.cache_creation += b.cache_creation;
    out.cache_creation_1h += b.cache_creation_1h || 0;
    out.turns += b.turns;
    const rowParts = rowCostParts(b);
    if (rowParts) {
      for (const key of COST_COLUMNS) out.cost_parts[key] += rowParts[key];
      out.cost += COST_COLUMNS.reduce((sum, key) => sum + rowParts[key], 0);
    }
    out.billable = out.billable || isBillable(b.model);
    // Accumulated per MODEL, not compared per row: there is one row per (day,
    // model), so the largest single row is not the largest model — picking the
    // top row would name whichever day happened to be busiest.
    const tokens = b.input + b.output + b.cache_read + b.cache_creation;
    tokensByModel.set(b.model, (tokensByModel.get(b.model) || 0) + tokens);
    branches.add(b.branch || s.branch || '');
  }
  out.branches = [...branches];
  let topTokens = -1;
  for (const [model, tokens] of tokensByModel) {
    if (tokens > topTokens) { topTokens = tokens; out.model = model; }
  }
  return out;
}

// Narrow a session row to the models currently selected AND the days in range.
//
// The range test is per DAY, not on the session's last-active day. These rows
// carry LIFETIME totals, so selecting on `last_date` printed every token a
// session had ever used under whatever range that one day fell in — and hid a
// session still running after the range ended, taking every in-range turn it
// had with it. Same defect and now the same remedy as the project tables: sum
// the rows of the days in range, priced per model.
//
// This also decides the Sessions tile and the Sessions column of both project
// tables, whose meaning is therefore "sessions with turns in range" rather than
// "sessions last active in range".
//
// The two fallbacks are older payload shapes, kept because a degraded payload
// should render a degraded table rather than an empty one: given only a
// per-model split, or neither, the session is selected on its last-active day
// exactly as it always was.
function sessionForSelection(s, start, end) {
  if (s.by_day_model && s.by_day_model.length) {
    return sessionFromParts(s, s.by_day_model.filter(b =>
      selectedModels.has(b.model)
      && (!start || b.day >= start) && (!end || b.day <= end)));
  }
  if ((start && s.last_date < start) || (end && s.last_date > end)) return null;
  if (s.by_model && s.by_model.length) {
    return sessionFromParts(s, s.by_model.filter(b => selectedModels.has(b.model)));
  }
  return selectedModels.has(s.model) ? { ...s, cost: rowCost(s), cost_parts: rowCostParts(s),
    billable: isBillable(s.model) } : null;
}

// Narrow one (dispatch, model) row to the days in range, the same way
// `sessionForSelection` narrows a session. Returns null when nothing survived,
// so the dispatch drops out of the table entirely.
//
// No model filter here: there is one row per (dispatch, source, model), so the
// model is the row's and its caller has already tested it — the split carries
// only days.
//
// **What an empty `by_day` means.** The server ships the split only for a row
// whose turns fall on more than one local day. `[]` therefore says "this row
// lived on exactly one local day, and that day is `start_date`" — so the
// fallback below is EXACT for it, not a degradation: selecting on `start_date`
// selects that one day, and the row's own totals are already that day's totals.
//
// A row with no `by_day` key at all is an older payload, from before the split
// existed, and deliberately takes the same path — for the same reason
// `sessionForSelection`'s fallbacks exist: a degraded payload should render a
// degraded table rather than an empty one. There it is the old, wrong-for-a-
// multi-day-dispatch behaviour, which is the price of rendering at all.
function dispatchPartsInRange(r, start, end) {
  if (r.by_day && r.by_day.length) {
    const out = { input: 0, output: 0, cache_read: 0, cache_creation: 0,
                  cache_creation_1h: 0, turns: 0,
                  cost_parts: { input: 0, output: 0, cache_read: 0, cache_creation: 0 } };
    let any = false;
    for (const b of r.by_day) {
      if (start && b.day < start) continue;
      if (end && b.day > end) continue;
      any = true;
      out.input += b.input; out.output += b.output;
      out.cache_read += b.cache_read; out.cache_creation += b.cache_creation;
      out.cache_creation_1h += b.cache_creation_1h || 0;
      out.turns += b.turns;
      const bParts = rowCostParts(b);
      if (bParts) for (const key of COST_COLUMNS) out.cost_parts[key] += bParts[key];
    }
    return any ? out : null;
  }
  if ((start && r.start_date < start) || (end && r.start_date > end)) return null;
  return r;
}

// A payload row belongs to the view when it came from the selected assistant.
// Rows written before the source column existed carry no `source` and are
// Claude's by definition — the same defaulting `db.SCHEMA_SQL` declares, as
// `source TEXT DEFAULT 'claude'` on both `turns` and `sessions`, rather than
// anything a migration applies.
//
// A database missing the column cannot reach this code at all any more: it does
// not match the declared schema, so `init_db` rebuilds it and the refilling
// scan writes the value explicitly. The fallback is kept anyway — for its price,
// not for a live case, and the reason once given here was not a reachable one.
// **A payload this build assembles never omits the key**: every array filtered
// through here takes its `source` from a `COALESCE(NULLIF(source, ''),
// 'claude')` in SQL and then through a defaulting `_dashboard_text(...,
// "claude") or "claude"` in rollups.py — eight arrays, counted 2026-08-15.
// Nor does an outliving snapshot supply one: the page and
// every payload it polls come from one process, whose API token is minted per
// process unless `CODEX_CLAUDE_USAGE_API_TOKEN` pins it, so a server swapped
// underneath a live page answers 403 rather than with an older row shape. The
// `||` is therefore one operator's worth of insurance against a payload shape
// nobody has re-derived, and nothing rests on it.
function inSource(r) {
  return (r.source || 'claude') === selectedSource;
}

// ── Cost buckets ───────────────────────────────────────────────────────────
// A bucket groups payload rows that each carry their own model — by effort
// level, by stop reason, by anything. It keeps the money broken down per column
// (`parts`) rather than only as a total, so each cell can print the
// multiplication that produced it, and it remembers WHICH rates fed each column
// so a derived per-million figure that is really an average says so.
function newCostBucket(extra) {
  const parts = {}, rates = {};
  for (const k of COST_COLUMNS) { parts[k] = 0; rates[k] = new Set(); }
  const bucket = { turns: 0, cost: 0, billable: false, parts, rates };
  for (const k of TOKEN_COLUMNS) bucket[k] = 0;
  return Object.assign(bucket, extra);
}

// Which rate field a column is charged at. `cache_creation` is absent because it
// has no single one — see columnRate.
const RATE_FIELD = Object.freeze({
  input: 'input', output: 'output', cache_read: 'cache_read',
});

// The unit price a row's column was charged at, as the NUMBER rather than as the
// rate OBJECT getPricing hands back. Keying the set on the object asked "were
// these the same table entry?" when the question the cell asks is "is the figure
// I am about to print on the price list?" — and PRICING lists claude-opus-5,
// -4-8, -4-7, -4-6 and -4-5 as five separate literals holding identical numbers.
// A bucket fed by two of them printed the published $5.00/M labelled `avg`: the
// mistake mixedTiers refuses to make three lines away, pointed the other way,
// and one model-filter click from the default view. An unpriced model still
// contributes `null`, which is a genuinely different rate from any published one.
function columnRate(row, column) {
  // Use the same effective date as rowCostParts. Otherwise the money can be
  // charged at a historical policy while the `avg` marker keys the row on the
  // current table, labelling a genuine list price as a blend (or vice versa).
  const p = getPricingAt(row.model, row.pricing_day || row.day);
  if (!p) return null;
  if (column !== 'cache_creation') return p[RATE_FIELD[column]];
  // Cache writes bill at TWO rates and the row carries the split (invariant 6),
  // so this column has no single published rate and both of the obvious keys are
  // wrong in opposite directions: on the 5-minute rate alone, two all-1-hour
  // models charged $10.00/M and $6.25/M would go unmarked; on both fields at
  // once, two all-5-minute models sharing $6.25/M would be called an average.
  // What the row actually contributed is the rate its own mix came to.
  const total = Math.max(row.cache_creation || 0, 0);
  const long = Math.min(Math.max(row.cache_creation_1h || 0, 0), total);
  // A row at ONE tier returns that tier's rate as a literal rather than as a
  // quotient. `(total * rate) / total` is not the identity in binary floating
  // point — for a non-dyadic rate it lands one ulp away for some totals and not
  // others, so two rows of the same model at the same tier keyed as two
  // different rates and the cell printed that model's own list price labelled
  // `avg`: the claim mixedTiers refuses to make three lines below, made here by
  // arithmetic. Every rate PRICING ships is dyadic, so this is reachable only
  // through a CODEX_CLAUDE_USAGE_RATES override.
  if (total <= 0 || long === 0) return p.cache_write;
  if (long === total) return p.cache_write_1h;
  // The genuine mix, still a quotient and still float-unstable — and that is
  // fine, not an unfinished fix. A bucket reaching this branch has
  // 0 < cache_creation_1h < cache_creation, so mixedTiers is already true and
  // the cell is marked whatever the set's size. Do NOT "complete" this by
  // rounding the quotient: rounding collapses two genuinely different blends
  // onto one key and can drop a real `avg`, which is the direction that
  // actually misleads a reader.
  return ((total - long) * p.cache_write + long * p.cache_write_1h) / total;
}

// Fold one payload row into a bucket, priced AT THAT ROW'S OWN MODEL.
//
// This is the whole reason `effort_by_day_model` carries a model per row.
// Summing the tokens of a level across models and pricing the sum once charges
// everything at whichever model was picked — up to 5x out, with no error and
// nothing on screen to suggest the figure is wrong.
function accumulateCostRow(bucket, row) {
  bucket.turns += row.turns || 0;
  for (const k of TOKEN_COLUMNS) bucket[k] += row[k] || 0;
  const parts = rowCostParts(row);
  for (const k of COST_COLUMNS) {
    // Which unit prices this column was charged at. An unpriced model
    // contributes `null` — its tokens land in the count but not in the money,
    // pulling the derived rate below every published one, which is still an
    // average and is marked as one rather than passing itself off as a price.
    if ((row[k] || 0) > 0) bucket.rates[k].add(columnRate(row, k));
  }
  // No FLAG for the cache-write tier split is recorded here on purpose. Whether
  // the accumulated cell blends the two tiers is a property of the totals being
  // printed, not of the rows — see mixedTiers, which the cell asks about them.
  if (!parts) return;
  bucket.billable = true;
  for (const k of COST_COLUMNS) { bucket.parts[k] += parts[k]; bucket.cost += parts[k]; }
}

// Whether the rate shown for a column is an effective average rather than a
// list price. THE one answer — every card that prints a money cell asks this,
// including renderModelCostTotals, which used to carry its own copy and
// disagreed with this one on the second disjunct below.
//
// Two ways a column stops being a list price, and the second is not a special
// case of the first:
//
//   * more than one price fed it — two models, or a cache-write column mixing
//     the two TTL tiers. A priced model sitting BESIDE an unpriced one is this
//     case, not the next: the set is {rate, null}, size 2.
//   * NO price fed it. Every model with tokens in this column is unpriced, so
//     the set is exactly {null} — size ONE — while the bucket is still billable
//     from a column beside it. The cell then derives $0.00/M, and `$0.00`
//     asserts the usage was free, which is a different claim from "not priced".
//     Size alone let that pass itself off as a published rate.
//
// Tolerates a bucket that has been through JSON (a Set does not survive the
// round trip) by answering "not a blend" rather than throwing.
function blendedRate(bucket, column) {
  const rates = bucket && bucket.rates && bucket.rates[column];
  return !!(rates && (rates.size > 1 || rates.has(null)));
}

// Sum whole buckets into one. Used for a totals row, where the union of the
// contributing rate sets is what decides the "avg" marker — exactly as it would
// be if the rows had been accumulated into a single bucket in the first place.
function mergeCostBuckets(buckets, extra) {
  const total = newCostBucket(extra);
  for (const b of buckets) {
    total.turns += b.turns || 0;
    total.cost += b.cost || 0;
    for (const k of TOKEN_COLUMNS) total[k] += b[k] || 0;
    for (const k of COST_COLUMNS) {
      total.parts[k] += (b.parts && b.parts[k]) || 0;
      if (b.rates && b.rates[k]) for (const rate of b.rates[k]) total.rates[k].add(rate);
    }
    total.billable = total.billable || !!b.billable;
  }
  return total;
}

function applyFilter() {
  if (!rawData) return;

  const { start, end } = getRangeBounds(selectedRange);

  // Filter daily rows by model + date range
  const filteredDaily = (rawData.daily_by_model || []).filter(r =>
    inSource(r) && selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );

  // Daily chart: aggregate by day
  const dailyMap = Object.create(null);
  for (const r of filteredDaily) {
    if (!dailyMap[r.day]) dailyMap[r.day] = { day: r.day, input: 0, output: 0, cache_read: 0, cache_creation: 0, cache_creation_1h: 0, cost: 0 };
    const d = dailyMap[r.day];
    d.input          += r.input;
    d.output         += r.output;
    d.cache_read     += r.cache_read;
    d.cache_creation += r.cache_creation;
    d.cache_creation_1h += r.cache_creation_1h || 0;
    d.cost           += rowCost(r);
  }
  // Give every calendar day in the range a column, whether or not it has turns.
  // dailyMap is keyed by the days that DO, so a quiet day was missing from the
  // series rather than zero in it — see dailyFillSpan for what that cost.
  //
  // A merge, never a replacement: a key already present keeps its figures, and a
  // key the fill does not produce (a day outside the span, or one localdays.py's
  // COALESCE fallback emitted unparseably) survives untouched.
  const fillSpan = dailyFillSpan(selectedRange, Object.keys(dailyMap));
  if (fillSpan) {
    for (const day of eachLocalDay(fillSpan.start, fillSpan.end)) {
      if (!dailyMap[day]) {
        dailyMap[day] = { day, input: 0, output: 0, cache_read: 0,
                          cache_creation: 0, cache_creation_1h: 0, cost: 0 };
      }
    }
  }
  // Nothing downstream counts these rows as usage: the stat tiles, the session
  // count and every table are built from filteredDaily / filteredSessions, not
  // from here, and rangeLabelWithDates only reads this list for "All Time",
  // whose fill is exactly the extent of the data it already read.
  const daily = Object.values(dailyMap).sort((a, b) => a.day.localeCompare(b.day));

  // By model: aggregate tokens + turns from daily data
  const modelMap = Object.create(null);
  for (const r of filteredDaily) {
    if (!modelMap[r.model]) modelMap[r.model] = { model: r.model, input: 0, output: 0, cache_read: 0, cache_creation: 0, cache_creation_1h: 0, reasoning: 0, turns: 0,
      cost: 0, cost_parts: { input: 0, output: 0, cache_read: 0, cache_creation: 0 }, billable: false };
    const m = modelMap[r.model];
    m.input          += r.input;
    m.output         += r.output;
    m.cache_read     += r.cache_read;
    m.cache_creation += r.cache_creation;
    m.cache_creation_1h += r.cache_creation_1h || 0;
    // A SUBSET of `output`, carried so the table can show how much of it was
    // thinking. Never added to output and never priced — the accumulator has to
    // name it explicitly or the column silently renders zero.
    m.reasoning      += r.reasoning || 0;
    m.turns          += r.turns;
    const rowParts = rowCostParts(r);
    if (rowParts) {
      for (const key of COST_COLUMNS) m.cost_parts[key] += rowParts[key];
      m.cost += COST_COLUMNS.reduce((sum, key) => sum + rowParts[key], 0);
      m.billable = true;
    }
  }

  // Filter sessions by model + date range. A session carries ONE "primary"
  // model and ONE last-active day beside the totals of every model it used on
  // every day it ran, so both filters have to be applied to its per-(day,
  // model) rows rather than to the row itself: matching on the primary model
  // made a session vanish when that model was unchecked even though a model it
  // also used was still selected, and matching on `last_date` billed the range
  // for the session's whole history. Keep the session when any of its rows is
  // in view, and report only those rows' figures — see sessionForSelection.
  const filteredSessions = (rawData.sessions_all || [])
    .filter(inSource)
    .map(s => sessionForSelection(s, start, end))
    .filter(Boolean);

  // No per-model session count is kept here. Nothing read one — the Cost by
  // Model card has no Sessions column, the model chart plots input+output and
  // the model CSV has no such field — and the count that used to be
  // accumulated was wrong anyway: it credited a whole session to its top-token
  // model and silently dropped any session whose top model had no daily row in
  // range. If that column is ever wanted, derive it the way the project tables
  // below do, from the range-filtered session list.
  const byModel = Object.values(modelMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By project / project+branch: aggregate from the per-(project, branch, day,
  // model) rollup, NOT from sessions. A session row carries lifetime totals, one
  // primary model and one date, so aggregating it would price a mixed-model
  // session entirely at its primary model and would credit its whole history to
  // whatever range its last-active day falls in. These rows are per day and per
  // model, so they filter by range and price per model exactly like the daily
  // chart — which is why these tables now agree with the stat tiles.
  const filteredProjectRows = (rawData.project_by_day_model || []).filter(r =>
    inSource(r) && selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );

  const accumulate = (bucket, r) => {
    bucket.input          += r.input;
    bucket.output         += r.output;
    bucket.cache_read     += r.cache_read;
    bucket.cache_creation += r.cache_creation;
    bucket.cache_creation_1h += r.cache_creation_1h || 0;
    bucket.turns          += r.turns;
    const rowParts = rowCostParts(r);
    if (rowParts) {
      for (const key of COST_COLUMNS) bucket.cost_parts[key] += rowParts[key];
      bucket.cost += COST_COLUMNS.reduce((sum, key) => sum + rowParts[key], 0);
    }
    // A project is billable if ANY of its models is, so a project running only
    // a local model still reads "n/a" rather than asserting it was free.
    bucket.billable = bucket.billable || isBillable(r.model);
  };

  // Count distinct sessions after the active filters. A session can occur in
  // several day/model buckets, so summing their independent counts would
  // overcount.
  const projSessions = Object.create(null);
  const branchSessions = Object.create(null);
  for (const s of filteredSessions) {
    projSessions[s.project] = (projSessions[s.project] || 0) + 1;
    for (const branch of (s.branches && s.branches.length
                          ? s.branches : [s.branch || ''])) {
      const bk = s.project + '\x00' + branch;
      branchSessions[bk] = (branchSessions[bk] || 0) + 1;
    }
  }

  const newBucket = (extra) => Object.assign(
    { input: 0, output: 0, cache_read: 0, cache_creation: 0, cache_creation_1h: 0,
      turns: 0, sessions: 0, cost: 0,
      cost_parts: { input: 0, output: 0, cache_read: 0, cache_creation: 0 },
      billable: false }, extra);

  const projMap = Object.create(null);
  for (const r of filteredProjectRows) {
    if (!projMap[r.project]) projMap[r.project] = newBucket({ project: r.project });
    accumulate(projMap[r.project], r);
  }
  for (const p of Object.values(projMap)) p.sessions = projSessions[p.project] || 0;
  const byProject = Object.values(projMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  const projBranchMap = Object.create(null);
  for (const r of filteredProjectRows) {
    const key = r.project + '\x00' + (r.branch || '');
    if (!projBranchMap[key]) projBranchMap[key] = newBucket({ project: r.project, branch: r.branch || '' });
    accumulate(projBranchMap[key], r);
  }
  for (const [key, pb] of Object.entries(projBranchMap)) pb.sessions = branchSessions[key] || 0;
  const byProjectBranch = Object.values(projBranchMap).sort((a, b) => b.cost - a.cost);

  // Totals
  const totals = {
    sessions:       filteredSessions.length,
    turns:          byModel.reduce((s, m) => s + m.turns, 0),
    input:          byModel.reduce((s, m) => s + m.input, 0),
    output:         byModel.reduce((s, m) => s + m.output, 0),
    cache_read:     byModel.reduce((s, m) => s + m.cache_read, 0),
    cache_creation: byModel.reduce((s, m) => s + m.cache_creation, 0),
    cache_creation_1h: byModel.reduce((s, m) => s + m.cache_creation_1h, 0),
    cost:           byModel.reduce((s, m) => s + (m.cost || 0), 0),
    // Whether ANY model in view has a published rate. Without this the cost tile
    // renders $0.00 for a source we have no prices for, which reads as "this was
    // free" rather than "this is not priced" — the difference between a fact and
    // a fabrication.
    billable:       byModel.some(m => isBillable(m.model)),
    // ...which is vacuously false when NOTHING is in view, and the tile then
    // said "No published per-token rate for these models" about models it had
    // priced one selection earlier. Two ways in, and they are not the same
    // claim: an empty range is not an empty filter. `billable` cannot tell any
    // of the three apart on its own, so the other two are stated here.
    empty:          byModel.length === 0,
    noModelsSelected: selectedModels.size === 0,
    // True when any priced model in view is priced from an estimate.
    estimated:      byModel.some(m => isBillable(m.model) && isEstimatedRate(m.model)),
    subagent_tokens: (rawData.subagent_by_type || [])
      .filter(r => inSource(r) && selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end))
      .reduce((s, r) => s + r.input + r.output + r.cache_read + r.cache_creation, 0),
  };

  // Hourly aggregation (filtered by model + range, then bucketed by UTC hour)
  // Resolve each row into the display frame FIRST, then range-filter on the
  // resulting day — the bounds are local calendar dates, so filtering the raw
  // UTC day here made this panel cover a different window than the daily chart.
  const hourlySrc = (rawData.hourly_by_model || [])
    .filter(r => inSource(r) && selectedModels.has(r.model))
    // RANGE MEMBERSHIP IS DECIDED BEFORE THE FRAME, AND ALWAYS ON THE LOCAL
    // DAY. The Local/UTC toggle re-buckets the hours; it must not change WHICH
    // turns the card is about.
    //
    // This filter used to run on the already-framed day, so in UTC mode it
    // compared a raw UTC day key against `start`/`end`, which are LOCAL
    // calendar dates -- every other card in the range uses the local day, and
    // `localdays.local_day_expr` is the one definition of it. Turns near a
    // range boundary therefore moved in or out of the set when the toggle was
    // flipped, so the hourly averages stopped describing the same turns as the
    // stat tiles above them while the range label was unchanged. Sharpest at a
    // large offset: `TZ=Pacific/Midway` with range `today`.
    //
    // `r.local_day` -- the payload's OWN local day, from the same
    // `localdays.local_day_expr` the tiles and every other rollup key on, and
    // grouped per turn rather than per bucket.
    //
    // Deriving it here instead, from `hourlyInFrame(r, 'local')`, was off by a
    // whole bucket wherever a local-day boundary falls MID-HOUR: that resolves
    // the bucket's UTC hour START, so in `Asia/Kolkata` (+05:30, local midnight
    // at 18:30Z) the whole 18:00Z bucket took the local day of 23:30, and the
    // turn at 18:40Z -- local 00:10 the next day, and counted by the tiles --
    // went with it. Every sub-hour offset has the same boundary, in both toggle
    // modes, so the fix for the range-vs-toggle defect above still left the card
    // and the tiles describing different turns. The rollup now splits such a
    // bucket into one row per local day, which is what makes an exact answer
    // available here at all.
    //
    // The fallback is the degraded path for a row with no `local_day` and
    // nothing else: it is the previous behaviour, wrong only in those zones,
    // and strictly better than `undefined` comparing false and dropping the row
    // out of the card entirely.
    .filter(r => {
      const localDay = r.local_day || hourlyInFrame(r, 'local').day;
      return (!start || localDay >= start) && (!end || localDay <= end);
    })
    .map(r => {
      const framed = hourlyInFrame(r, hourlyTZ);
      return { ...r, day: framed.day, hour: framed.hour };
    });
  const hourlyAgg = aggregateHourly(hourlySrc, hourlyTZ);

  // Subagent breakdown by type (filtered by range + selected models)
  const subagentTypeMap = Object.create(null);
  for (const r of (rawData.subagent_by_type || [])) {
    if (!inSource(r) || !selectedModels.has(r.model)) continue;
    if (start && r.day < start) continue;
    if (end && r.day > end) continue;
    const k = r.agent_type;
    if (!subagentTypeMap[k]) subagentTypeMap[k] = { agent_type: k, input: 0, output: 0, cache_read: 0, cache_creation: 0, cache_creation_1h: 0, turns: 0,
      cost: 0, cost_parts: { input: 0, output: 0, cache_read: 0, cache_creation: 0 }, billable: false };
    const m = subagentTypeMap[k];
    m.input += r.input; m.output += r.output;
    m.cache_read += r.cache_read; m.cache_creation += r.cache_creation;
    m.cache_creation_1h += r.cache_creation_1h || 0;
    m.turns += r.turns;
    const rowParts = rowCostParts(r);
    if (rowParts) {
      for (const key of COST_COLUMNS) m.cost_parts[key] += rowParts[key];
      m.cost += COST_COLUMNS.reduce((sum, key) => sum + rowParts[key], 0);
      m.billable = true;
    }
  }
  const byAgentType = Object.values(subagentTypeMap).sort((a, b) =>
    (b.input + b.output + b.cache_read + b.cache_creation) -
    (a.input + a.output + a.cache_read + a.cache_creation));

  // Top dispatches: the server now emits one row per (dispatch, model), so
  // filter those rows first and then collapse them per agent_id — costing each
  // row at its own model. Pricing a whole dispatch at one model was 5x out
  // either way whenever a dispatch spanned two. The displayed model is the one
  // that used the most tokens, rather than whichever row SQLite happened to
  // return for the group.
  //
  // The range test is per DAY, in `dispatchPartsInRange`, not on the dispatch's
  // start day. These rows carry LIFETIME totals, so selecting on `start_date`
  // printed every token a dispatch had ever used under whatever range that one
  // day fell in — and hid a dispatch still running after its start day, taking
  // every in-range turn it had with it. Same defect and the same remedy as the
  // sessions table.
  const dispatchRows = (rawData.top_dispatches || []).filter(d =>
    inSource(d) && selectedModels.has(d.model)
  );
  const dispatchMap = new Map();
  for (const r of dispatchRows) {
    const part = dispatchPartsInRange(r, start, end);
    if (!part) continue;
    let d = dispatchMap.get(r.agent_id);
    if (!d) {
      // Zero the accumulators: the spread copies this row's own figures, which
      // the loop below is about to add. `by_day` goes with them — the spread
      // would leave ONE model's split on an object that is the whole dispatch.
      d = { ...r, input: 0, output: 0, cache_read: 0, cache_creation: 0,
            cache_creation_1h: 0, turns: 0, cost: 0,
            cost_parts: { input: 0, output: 0, cache_read: 0, cache_creation: 0 },
            billable: false,
            by_day: null, _topModelTokens: -1 };
      dispatchMap.set(r.agent_id, d);
    }
    const rowTokens = part.input + part.output + part.cache_read + part.cache_creation;
    d.input += part.input; d.output += part.output;
    d.cache_read += part.cache_read; d.cache_creation += part.cache_creation;
    d.cache_creation_1h += part.cache_creation_1h || 0;
    d.turns += part.turns;
    // A blank start_date is "not recorded", never "earliest": it sorts below
    // every real day under `<`, so one model-row carrying no timestamp erased
    // the whole dispatch's start while all of its tokens stayed in the row.
    // Same rule as rollups._merge_pricing_rows applies server-side.
    if (r.start_date && (!d.start_date || r.start_date < d.start_date)) {
      d.start_date = r.start_date; d.start = r.start;
    }
    if (rowTokens > d._topModelTokens) { d._topModelTokens = rowTokens; d.model = r.model; }
    const partCost = part.cost_parts || rowCostParts({ ...part, model: r.model });
    if (partCost) {
      if (!d.cost_parts) d.cost_parts = { input: 0, output: 0, cache_read: 0, cache_creation: 0 };
      for (const key of COST_COLUMNS) d.cost_parts[key] += partCost[key];
      d.cost += COST_COLUMNS.reduce((sum, key) => sum + partCost[key], 0);
    }
    d.billable = d.billable || isBillable(r.model);
  }
  const filteredDispatches = [...dispatchMap.values()].sort((a, b) =>
    (b.input + b.output + b.cache_read + b.cache_creation) -
    (a.input + a.output + a.cache_read + a.cache_creation));

  // Cost by reasoning effort, and why responses ended. Both are the same turns
  // the tables above already show, grouped by something other than the model —
  // so both go through the SAME predicate (source, model selection, range). A
  // breakdown filtered differently would not sum to the totals beside it, and
  // the reader has no way to tell which of the two is the honest one.
  const inRange = (r) => inSource(r) && selectedModels.has(r.model)
    && (!start || r.day >= start) && (!end || r.day <= end);

  const effortMap = Object.create(null);
  for (const r of (rawData.effort_by_day_model || [])) {
    if (!inRange(r)) continue;
    const key = r.effort || '';
    if (!effortMap[key]) effortMap[key] = newCostBucket({ effort: key });
    accumulateCostRow(effortMap[key], r);
  }
  // Most expensive first; turns break a tie so an unpriced source (every cost 0)
  // still orders by something meaningful rather than by hash order.
  const byEffort = Object.values(effortMap)
    .sort((a, b) => (b.cost - a.cost) || (b.turns - a.turns));

  const stopMap = Object.create(null);
  for (const r of (rawData.stop_reason_by_day_model || [])) {
    if (!inRange(r)) continue;
    const key = r.stop_reason || '';
    if (!stopMap[key]) {
      stopMap[key] = { stop_reason: key, turns: 0, output: 0, cost: 0,
                       billable: false, rates: { output: new Set() } };
    }
    const b = stopMap[key];
    b.turns += r.turns || 0;
    b.output += r.output || 0;
    // Only the output column is costed here: the rollup carries turns and output
    // and nothing else, and inventing the other three would be arithmetic on
    // numbers the payload does not contain.
    const parts = rowCostParts(r);
    if ((r.output || 0) > 0) b.rates.output.add(columnRate(r, 'output'));
    if (!parts) continue;
    b.billable = true;
    b.cost += parts.output;
  }
  const byStopReason = Object.values(stopMap)
    .sort((a, b) => (b.turns - a.turns) || (b.output - a.output));

  // One label for the stat tiles and all three chart titles, built once so they
  // can never disagree about which window the page is showing. "All Time" needs
  // the days actually on screen to state a span, hence `daily`.
  const rangeLabelFull = rangeLabelWithDates(selectedRange, daily.map(d => d.day));

  document.getElementById('daily-chart-title').textContent = 'Daily Token Usage \u2014 ' + rangeLabelFull;
  document.getElementById('hourly-chart-title').textContent = 'Average Hourly Distribution \u2014 ' + rangeLabelFull;
  document.getElementById('subagent-chart-title').textContent = 'Subagent Tokens by Type \u2014 ' + rangeLabelFull;

  // Rate-limit incidents, range-filtered on their local day like everything else.
  const limitIncidents = (rawData.limit_incidents || []).filter(i =>
    (!start || i.day >= start) && (!end || i.day <= end));

  // Each assistant reports its own quota, from a different place: Claude's from
  // its local cache, Codex's from the transcript series. One renderer, because
  // they mean the same thing to the reader.
  applyPlanLimits(selectedSource === 'codex'
    ? (rawData.codex_limits || { available: false, reason: 'no_codex_usage' })
    : lastClaudeLimits);
  sourceIsPriced = totals.billable;
  renderStats(totals, rangeLabelFull);
  renderLimits(limitIncidents);
  renderDailyChart(daily);
  renderHourlyChart(hourlyAgg);
  renderModelChart(byModel);
  renderProjectChart(byProject);
  renderSubagentChart(byAgentType);
  lastFilteredDispatches = filteredDispatches;
  renderTopDispatches(lastFilteredDispatches);
  lastFilteredSessions = sortSessions(filteredSessions);
  lastByModel = byModel;
  lastByProject = sortProjects(byProject);
  lastByProjectBranch = sortProjectBranch(byProjectBranch);
  lastByEffort = byEffort;
  lastByStopReason = byStopReason;
  renderSessionsTable(lastFilteredSessions);
  renderModelCostTable(lastByModel);
  renderEffortCostTable(lastByEffort);
  renderStopReasonTable(lastByStopReason);
  renderProjectCostTable(lastByProject);
  renderProjectBranchCostTable(lastByProjectBranch);
  // Set after the first full render, so only the very first paint animates.
  chartsHaveDrawn = true;
}
