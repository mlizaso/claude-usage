// The empty state four of the seven data tables did not have.
//
// `renderTopDispatches`, the effort table and the stop-reason table each print
// a centred "nothing in selected range" row; the sessions, model-cost,
// project-cost and project/branch tables rendered an absolutely EMPTY <tbody>
// -- a header row over blank space, with nothing saying whether that means "no
// rows matched" or "the render failed". The INCONSISTENCY is the argument: a
// reader who learns from one card that a blank table means "nothing here"
// reads a blank one as broken.
//
// `colspan` is the table's real column count, taken from its own <thead>.
function emptyTableRow(columns, text) {
  return '<tr><td colspan="' + columns + '" class="muted" '
    + 'style="text-align:center;padding:24px">' + text + '</td></tr>';
}

// ── Data tables ────────────────────────────────────────────────────────────
// Every table on the page: its sort state, its sort indicators, its comparator,
// its pagination controls and its rows. Split out of 50-render.js.
//
// The five tables deliberately keep five sets of sort state rather than sharing
// one: they sort by different columns, and a shared cursor would make sorting
// one table silently re-sort the others — which is a bug this page has had.
function renderTopDispatches(rows) {
  const body = document.getElementById('dispatches-body');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="11" class="muted" style="text-align:center;padding:24px">No subagent dispatches in selected range.</td></tr>';
    labelCells('dispatches-body');
    renderTableToggle('dispatches-foot', 0, dispatchesLimit, 'lessDispatchRows', 'moreDispatchRows', 'exportDispatchesCSV');
    return;
  }
  const shown = rows.slice(0, shownCount(dispatchesLimit, rows.length));
  body.innerHTML = shown.map(d => {
    const tokensTotal = d.input + d.output + d.cache_read + d.cache_creation;
    // Use the cost summed per (dispatch, model) row in applyFilter — recomputing
    // it from the single displayed model would reintroduce the 5x error for a
    // dispatch that spanned two.
    const cost = d.cost;
    const costCell = d.billable
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    // Turns and Tools are the two counts in this table still interpolated raw
    // rather than grouped like the ones below. Both are pinned as source TEXT
    // by test_dashboard's escaping guard, which asserts the exact
    // `${esc(d.turns)}` string, so grouping them is a two-file change.
    const col = colorForAgentType(d.agent_type);
    const typeStyle = `background:${col}22;color:${col};border:1px solid ${col}44`;
    return `<tr>
      <td><span class="model-tag" style="${typeStyle}">${esc(d.agent_type)}</span></td>
      <td class="muted">${esc(d.start || '—')}</td>
      <td><span class="model-tag">${esc(d.model)}</span></td>
      <td class="num">${esc(d.turns)}</td>
      <td class="num">${esc(d.tool_uses != null ? d.tool_uses : '—')}</td>
      <td class="muted">${esc(fmtDuration(d.duration_ms))}</td>
      <td class="num">${esc(fmt(d.input))}</td>
      <td class="num">${esc(fmt(d.output))}</td>
      <td class="num">${esc(fmt(d.cache_read))}</td>
      <td class="num"><strong>${esc(fmt(tokensTotal))}</strong></td>
      ${costCell}
    </tr>`;
  }).join('');
  labelCells('dispatches-body');
  renderTableToggle('dispatches-foot', rows.length, dispatchesLimit, 'lessDispatchRows', 'moreDispatchRows', 'exportDispatchesCSV');
}

// Fills a table card's footer with the row-reveal control. Three states:
//   - more rows fit under the cap        -> "Show more" (plus "Show less" once expanded)
//   - cap reached but more records exist -> "Download CSV to see all (N)" + "Show less"
//   - every row is already visible       -> "Show less"
// "Show less" is hidden at the initial step (nothing to collapse yet). Renders
// nothing when the whole table fits in the first step. Carets: more = down (▾),
// less = up (▴).
function renderTableToggle(footId, total, limit, lessName, moreName, csvName) {
  const foot = document.getElementById(footId);
  if (!foot) return;
  if (total <= PAGINATE_THRESHOLD) { foot.innerHTML = ''; return; }
  const less = '<button class="show-more-btn" data-table-action="' + esc(lessName) + '">Show less ▴</button>';
  const more = '<button class="show-more-btn" data-table-action="' + esc(moreName) + '">Show more ▾</button>';
  let html;
  if (limit < total && limit < TABLE_MAX) {
    // more rows fit under the cap; Show less only once we're past the first step
    html = (limit > TABLE_STEPS[0] ? less : '') + more;
  } else if (limit < total) {           // cap reached, remaining rows only via CSV
    // Grouped: this branch only fires past TABLE_MAX, so the total here is
    // routinely four figures — "(4567)" beside a table of grouped columns.
    html = '<a class="show-more-link" href="#" data-table-action="' + esc(csvName) + '">Download CSV to see all (' + NUM.format(total) + ')</a>' + less;
  } else {                              // everything already visible
    html = less;
  }
  foot.innerHTML = html;
}

// After collapsing a table, bring its top back into view — the user may have
// scrolled down through the expanded rows.
function scrollTableToTop(bodyId) {
  const card = document.getElementById(bodyId)?.closest('.table-card');
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// "Show more" advances one step (capped at TABLE_MAX); "Show less" resets to the
// first step and scrolls back to the top of that table.
function moreModelRows()   { modelLimit    = nextTableLimit(modelLimit,    lastByModel.length);        renderModelCostTable(lastByModel); }
function lessModelRows()   { modelLimit    = TABLE_STEPS[0]; renderModelCostTable(lastByModel);            scrollTableToTop('model-cost-body'); }
function moreSessionRows() { sessionsLimit = nextTableLimit(sessionsLimit, lastFilteredSessions.length); renderSessionsTable(lastFilteredSessions); }
function lessSessionRows() { sessionsLimit = TABLE_STEPS[0]; renderSessionsTable(lastFilteredSessions);    scrollTableToTop('sessions-body'); }
function moreProjectRows() { projectLimit  = nextTableLimit(projectLimit,  lastByProject.length);       renderProjectCostTable(lastByProject); }
function lessProjectRows() { projectLimit  = TABLE_STEPS[0]; renderProjectCostTable(lastByProject);        scrollTableToTop('project-cost-body'); }
function moreBranchRows()  { branchLimit   = nextTableLimit(branchLimit,   lastByProjectBranch.length); renderProjectBranchCostTable(lastByProjectBranch); }
function lessBranchRows()  { branchLimit   = TABLE_STEPS[0]; renderProjectBranchCostTable(lastByProjectBranch); scrollTableToTop('project-branch-cost-body'); }
function moreDispatchRows(){ dispatchesLimit = nextTableLimit(dispatchesLimit, lastFilteredDispatches.length); renderTopDispatches(lastFilteredDispatches); }
function lessDispatchRows(){ dispatchesLimit = TABLE_STEPS[0]; renderTopDispatches(lastFilteredDispatches);            scrollTableToTop('dispatches-body'); }

// Last Active and Duration are the only two cells in the sessions row that are
// NOT scoped to the selected range: `sessionForSelection` sums the session's
// turns in range, but these two are properties of the session, so a row can
// show one day's tokens beside a two-day duration. Said in the cells rather
// than left for the reader to discover — the mixed row is the price of a
// range-correct cost column, and an unexplained mixed row is the same class of
// defect one layer down.
const LIFETIME_CELL_NOTE = 'Whole session, not clipped to the selected range — '
  + 'unlike the token and cost columns, which count only this range.';

function renderSessionsTable(sessions) {
  const shown = sessions.slice(0, shownCount(sessionsLimit, sessions.length));
  document.getElementById('sessions-body').innerHTML = shown.length === 0 ? emptyTableRow(10, 'No sessions in selected range.') : shown.map(s => {
    const cost = s.cost;
    const costCell = s.billable
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const titleCell = s.topic
      ? `<td class="topic-cell" title="${esc(s.topic)}">${esc(s.topic)}</td>`
      : `<td class="topic-cell"><span class="untitled">Untitled</span></td>`;
    // The turn count goes through NUM, not fmt: fmt abbreviates at a thousand
    // and a session's turns are read exactly, which is the same reason the
    // Sessions tile uses NUM. `duration_min` deliberately stays raw — it is a
    // magnitude with a unit rather than a count, and the payload can carry it
    // as a string (see test_frontend_data_path), which NUM would print as NaN.
    return `<tr>
      <td class="muted" style="font-family:monospace">${esc(s.session_id.slice(0, 8))}&hellip;</td>
      <td>${esc(s.project)}</td>
      ${titleCell}
      <td class="muted" title="${esc(LIFETIME_CELL_NOTE)}">${esc(s.last)}</td>
      <td class="muted" title="${esc(LIFETIME_CELL_NOTE)}">${esc(s.duration_min)}m</td>
      <td><span class="model-tag">${esc(s.model)}</span></td>
      <td class="num">${esc(NUM.format(s.turns))}</td>
      <td class="num">${esc(fmt(s.input))}</td>
      <td class="num">${esc(fmt(s.output))}</td>
      ${costCell}
    </tr>`;
  }).join('');
  labelCells('sessions-body');
  renderTableToggle('sessions-foot', sessions.length, sessionsLimit, 'lessSessionRows', 'moreSessionRows', 'exportSessionsCSV');
}

function setModelSort(col) {
  if (modelSortCol === col) {
    modelSortDir = modelSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    modelSortCol = col;
    modelSortDir = 'desc';
  }
  updateModelSortIcons();
  applyFilter();
}

function updateModelSortIcons() {
  document.querySelectorAll('[id^="msort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('msort-' + modelSortCol);
  if (icon) icon.textContent = modelSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortModels(byModel) {
  return [...byModel].sort((a, b) => {
    let av, bv;
    if (modelSortCol === 'cost') {
      av = rowCost(a);
      bv = rowCost(b);
    } else {
      av = a[modelSortCol] ?? 0;
      bv = b[modelSortCol] ?? 0;
    }
    if (av < bv) return modelSortDir === 'desc' ? 1 : -1;
    if (av > bv) return modelSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

// A token count, the unit price it was charged at, and the resulting cost —
// written as the multiplication so the figure can be checked rather than taken
// on trust. The count stays the primary line (it is what the column sorts by).
//
// `blended` marks a rate that is an effective average rather than a list price:
// a cache-write column mixing the 5-minute and 1-hour tiers, or any column
// totalled across models. Saying so matters — an unlabelled "$6.92/M" invites
// the reader to go looking for it on the price list, where it does not appear.
function tokenCostCell(tokens, cost, blended) {
  if (cost == null) return `<td class="num">${esc(fmt(tokens))}</td>`;
  const rate = effectiveRate(tokens, cost);
  const sum = rate == null
    ? esc(fmtCost(cost))
    : `<span class="cell-rate">&times; ${esc(fmtRate(rate))}${blended ? ' avg' : ''}</span> = ${esc(fmtCost(cost))}`;
  return `<td class="num">${esc(fmt(tokens))}<span class="cell-cost">${sum}</span></td>`;
}

// Whether a row's cache writes span BOTH TTL tiers, which is the only case where
// the derived write rate is a blend. All-5-minute or all-1-hour reproduces that
// tier's list price exactly, and labelling it "avg" would send the reader hunting
// for a blend that isn't there.
function mixedTiers(m) {
  const total = Math.max(m.cache_creation || 0, 0);
  const long = Math.min(Math.max(m.cache_creation_1h || 0, 0), total);
  return long > 0 && long < total;
}

// How much of the output was thinking. Deliberately handed to tokenCostCell with
// a null cost: these tokens are a SUBSET of the output column beside them, and
// the output figure every cost path multiplies already contains them — printing
// money here would bill the same tokens a second time.
//
// Zero renders as an em dash, not "0". Only Codex reports a reasoning figure;
// Claude bills extended thinking inside output and never breaks it out, so a
// literal 0 on a Claude row would assert the model did no thinking — a claim the
// transcripts do not support. Absent and none are different answers.
function reasoningCell(tokens) {
  if (tokens > 0) return tokenCostCell(tokens, null);
  return '<td class="num muted" title="Not reported for these turns. Claude bills'
    + ' thinking inside Output without breaking it out; only Codex reports a'
    + ' separate figure.">&mdash;</td>';
}

function renderModelCostTable(byModel) {
  const sorted = sortModels(byModel);
  const shown = sorted.slice(0, shownCount(modelLimit, sorted.length));
  document.getElementById('model-cost-body').innerHTML = shown.length === 0 ? emptyTableRow(8, 'No models in selected range.') : shown.map(m => {
    const parts = rowCostParts(m);
    const cost = rowCost(m);
    const costCell = isBillable(m.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td><span class="model-tag">${esc(m.model)}</span></td>
      <td class="num">${esc(fmt(m.turns))}</td>
      ${tokenCostCell(m.input,          parts && parts.input)}
      ${tokenCostCell(m.output,         parts && parts.output)}
      ${tokenCostCell(m.cache_read,     parts && parts.cache_read)}
      ${tokenCostCell(m.cache_creation, parts && parts.cache_creation, mixedTiers(m))}
      ${reasoningCell(m.reasoning || 0)}
      ${costCell}
    </tr>`;
  }).join('');
  labelCells('model-cost-body');
  renderModelCostTotals(sorted);
  renderTableToggle('model-cost-foot', sorted.length, modelLimit, 'lessModelRows', 'moreModelRows', 'exportModelCSV');
}

// The column totals, summed over EVERY model in range rather than the rows
// currently paged in — the figure people come to this table for is "what did
// cache reads cost me", and an answer that silently excluded the models below
// the fold would be worse than no answer. Says so in the label when they differ.
function renderModelCostTotals(rows) {
  const foot = document.getElementById('model-cost-total');
  if (!foot) return;
  const t = { turns: 0, input: 0, output: 0, cache_read: 0, cache_creation: 0,
              reasoning: 0 };
  const c = { input: 0, output: 0, cache_read: 0, cache_creation: 0 };
  let anyPriced = false;
  // Per column, the distinct unit prices that actually fed it — columnRate's key,
  // the one the effort and stop-reason cards use, so the two totals rows on the
  // same screen answer "is the figure I am about to print on a price list?" the
  // same way. That claim was only ever true of the KEY: the ANSWER was a second
  // copy of blendedRate's rule until this round, and the two copies disagreed
  // (see `blended` below).
  //
  // Counting contributing MODELS answered a different question: PRICING
  // lists claude-opus-5, -4-8, -4-7, -4-6 and -4-5 as five separate literals
  // holding identical numbers, so a view spanning two of them printed the
  // published $5.00/M labelled `avg` while the card directly below printed the
  // same tokens, the same rate and the same dollars unmarked. The marker is still
  // decided per cell: a column fed at one rate is not an average even when the
  // table as a whole spans several.
  const rates = { input: new Set(), output: new Set(), cache_read: new Set(),
                  cache_creation: new Set() };
  // Whether the rate a column will print is an effective average rather than a
  // list price — blendedRate's question, asked of blendedRate. This used to be a
  // second implementation carrying one disjunct the shared one lacked (a column
  // fed ONLY by unpriced models is the set {null}, size ONE), on the stated
  // premise that "an effort bucket never meets" it. It does: one unpriced model
  // supplying a column no priced model has tokens in is enough, and the effort
  // and stop-reason cards then printed the same derived $0.00/M unmarked, one
  // card below this one. `rates` here is the bare {column: Set} map rather than
  // a bucket, so it is passed as one.
  const blended = (k) => blendedRate({ rates }, k);
  let anyMixedTiers = false;
  for (const m of rows) {
    t.turns += m.turns; t.input += m.input; t.output += m.output;
    t.cache_read += m.cache_read; t.cache_creation += m.cache_creation;
    t.reasoning += m.reasoning || 0;
    const parts = rowCostParts(m);
    for (const k of ['input', 'output', 'cache_read', 'cache_creation']) {
      // An unpriced model's tokens land in the token total but not in the money,
      // so they pull the derived rate below every real one. It contributes
      // `null` — a genuinely different rate from any published one — and the
      // result is still an average, of the models in range with some of them
      // free, rather than a list price.
      if ((m[k] || 0) > 0) rates[k].add(columnRate(m, k));
    }
    if (!parts) continue;
    anyPriced = true;
    if (mixedTiers(m)) anyMixedTiers = true;
    for (const k of ['input', 'output', 'cache_read', 'cache_creation']) c[k] += parts[k];
  }
  const grand = c.input + c.output + c.cache_read + c.cache_creation;
  const shownRows = shownCount(modelLimit, rows.length);
  const label = shownRows < rows.length
    ? `All ${rows.length} models`
    : (rows.length === 1 ? 'Total' : `All ${rows.length} models`);
  // The grand total is gated on `anyPriced` like the four columns that feed it.
  // Printing `$0.0000` under a body of `n/a` rows asserts the usage was free,
  // which is a different claim from "not priced" — and it contradicted the
  // card's own rows, on the same screen, inside the same table.
  foot.innerHTML = `
    <td><span class="total-label">${esc(label)}</span></td>
    <td class="num">${esc(fmt(t.turns))}</td>
    ${tokenCostCell(t.input,          anyPriced ? c.input : null,      blended('input'))}
    ${tokenCostCell(t.output,         anyPriced ? c.output : null,      blended('output'))}
    ${tokenCostCell(t.cache_read,     anyPriced ? c.cache_read : null,  blended('cache_read'))}
    ${tokenCostCell(t.cache_creation, anyPriced ? c.cache_creation : null, blended('cache_creation') || anyMixedTiers)}
    ${reasoningCell(t.reasoning)}
    ${anyPriced
      ? `<td class="cost">${esc(fmtCost(grand))}</td>`
      : `<td class="cost-na">n/a</td>`}`;
  labelCells('model-cost-total');
}

// ── Cost by reasoning effort ───────────────────────────────────────────────
// The same turns as Cost by Model, grouped by how hard the assistant was asked
// to think instead of by which model answered. Built from `effort_by_day_model`,
// whose rows each carry their own model, and costed one row at a time — see
// accumulateCostRow. Aggregating the tokens of a level first and pricing them
// once would charge a level used by opus and haiku entirely at one of the two.

// '' is not a level. It means the effort was never written down — a turn scanned
// before the column existed, or an assistant that does not record one — so it is
// named as an absence. Folding it into `medium` would invent a fact; dropping it
// would leave this table short of the totals every other card shows.
function effortLabel(effort) { return effort ? effort : 'not recorded'; }

// One token column of an effort row: count, unit price, money — the same shape
// Cost by Model prints, from money that was summed per model before it was
// summed per level.
// `mixedTiers` is asked about the BUCKET, not about the rows that fed it. The
// question this cell asks is "is the rate I am about to print a blend?", which
// is a property of the totals being printed: two rows that are each a single
// tier (one all-1h, one all-5m) sum to a bucket whose derived write rate sits
// between the two published ones and appears on no price list. Asking each row
// instead answered a different question and dropped the marker on exactly that
// case — the same figure the model card, which aggregates first, marks `avg`.
function effortCostCell(bucket, column) {
  const cost = bucket.billable ? ((bucket.parts && bucket.parts[column]) || 0) : null;
  const blended = blendedRate(bucket, column)
    || (column === 'cache_creation' && mixedTiers(bucket));
  return tokenCostCell(bucket[column] || 0, cost, blended);
}

function effortRowHTML(bucket) {
  const costCell = bucket.billable
    ? `<td class="cost">${esc(fmtCost(bucket.cost))}</td>`
    : `<td class="cost-na">n/a</td>`;
  return `
    ${effortCostCell(bucket, 'input')}
    ${effortCostCell(bucket, 'output')}
    ${effortCostCell(bucket, 'cache_read')}
    ${effortCostCell(bucket, 'cache_creation')}
    ${reasoningCell(bucket.reasoning || 0)}
    ${costCell}`;
}

function renderEffortCostTable(rows) {
  const body = document.getElementById('effort-cost-body');
  if (!body) return;
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="8" class="muted" style="text-align:center;padding:24px">No reasoning-effort data in selected range.</td></tr>';
    labelCells('effort-cost-body');
    renderEffortCostTotals(rows);
    return;
  }
  // Every level fits on screen — there are a handful of them, not a handful of
  // hundreds — so this table does not paginate and has no footer control.
  body.innerHTML = rows.map(e => `<tr>
      <td><span class="effort-tag${e.effort ? '' : ' unset'}">${esc(effortLabel(e.effort))}</span></td>
      <td class="num">${esc(fmt(e.turns))}</td>
      ${effortRowHTML(e)}
    </tr>`).join('');
  labelCells('effort-cost-body');
  renderEffortCostTotals(rows);
}

// The totals across every level in range. Worth printing because they are the
// same figures the Cost by Model totals row shows: two independent groupings of
// one set of turns, so a disagreement between them means one of the two rollups
// is filtering or pricing differently from the other.
function renderEffortCostTotals(rows) {
  const foot = document.getElementById('effort-cost-total');
  if (!foot) return;
  if (!rows.length) { foot.innerHTML = ''; return; }
  const total = mergeCostBuckets(rows);
  const label = rows.length === 1 ? 'Total' : `All ${rows.length} levels`;
  foot.innerHTML = `
    <td><span class="total-label">${esc(label)}</span></td>
    <td class="num">${esc(fmt(total.turns))}</td>
    ${effortRowHTML(total)}`;
  labelCells('effort-cost-total');
}

// ── Why responses ended ────────────────────────────────────────────────────
// One row per stop reason, from `stop_reason_by_day_model`. `max_tokens` is what
// this card exists for: that response was cut off at the output ceiling rather
// than finished, and it was billed in full — indistinguishable from a complete
// answer in every other figure on the page.
const TRUNCATED_STOP_REASON = 'max_tokens';

// Same rule as the effort bucket: '' is an absence, not a way a response ends.
function stopReasonLabel(reason) { return reason ? reason : 'not recorded'; }

function renderStopReasonTable(rows) {
  const body = document.getElementById('stop-reason-body');
  if (!body) return;
  const totalTurns = rows.reduce((s, r) => s + (r.turns || 0), 0);
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="4" class="muted" style="text-align:center;padding:24px">No responses in selected range.</td></tr>';
  } else {
    body.innerHTML = rows.map(r => {
      const truncated = r.stop_reason === TRUNCATED_STOP_REASON;
      return `<tr>
      <td><span class="stop-tag${truncated ? ' truncated' : ''}${r.stop_reason ? '' : ' unset'}">${esc(stopReasonLabel(r.stop_reason))}</span></td>
      <td class="num">${esc(fmt(r.turns))}</td>
      <td class="num muted">${esc(fmtPct(r.turns, totalTurns))}</td>
      ${tokenCostCell(r.output || 0, r.billable ? r.cost : null, blendedRate(r, 'output'))}
    </tr>`;
    }).join('');
  }
  labelCells('stop-reason-body');
  renderStopReasonNote(rows);
}

// The response-ending caveat distinguishes missing stop reasons from recorded
// reasons. Show max_tokens accurately even when the bucket is empty or
// contains only one response.
function renderStopReasonNote(rows) {
  const note = document.getElementById('stop-reason-note');
  if (!note) return;
  if (selectedSource !== 'claude') {
    const label = SOURCE_LABELS[selectedSource] || selectedSource;
    note.innerHTML = 'Only Claude records why a response ended. '
      + esc(label) + ' transcripts carry no stop reason at all, so every turn '
      + 'lands in <em>not recorded</em> &mdash; that is a gap in what is written '
      + 'down, not evidence that no answer was ever cut short.';
    return;
  }
  const truncated = rows.find(r => r.stop_reason === TRUNCATED_STOP_REASON);
  const lead = '<code>max_tokens</code> is the one to watch: the response stopped '
    + 'at the output ceiling instead of finishing, and it was billed in full. ';
  note.innerHTML = truncated
    ? lead + '<strong>' + esc(fmt(truncated.turns)) + ' in this range.</strong>'
    : lead + 'None in this range.';
}

// ── Project cost table sorting ────────────────────────────────────────────
function setProjectSort(col) {
  if (projectSortCol === col) {
    projectSortDir = projectSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    projectSortCol = col;
    projectSortDir = 'desc';
  }
  updateProjectSortIcons();
  applyFilter();
}

function updateProjectSortIcons() {
  document.querySelectorAll('[id^="psort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('psort-' + projectSortCol);
  if (icon) icon.textContent = projectSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortProjects(byProject) {
  return [...byProject].sort((a, b) => {
    const av = a[projectSortCol] ?? 0;
    const bv = b[projectSortCol] ?? 0;
    if (av < bv) return projectSortDir === 'desc' ? 1 : -1;
    if (av > bv) return projectSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderProjectCostTable(byProject) {
  const sorted = sortProjects(byProject);
  const shown = sorted.slice(0, shownCount(projectLimit, sorted.length));
  document.getElementById('project-cost-body').innerHTML = shown.length === 0 ? emptyTableRow(6, 'No projects in selected range.') : shown.map(p => {
    // A project is billable when ANY of its models is; one running only a local
    // model reads "n/a" rather than asserting the work was free. That is the
    // flag applyFilter already computes and the CSV export already honours —
    // this cell used to print `$0.0000` and contradict both.
    const costCell = p.billable
      ? `<td class="cost">${esc(fmtCost(p.cost))}</td>`
      : `<td class="cost-na">n/a</td>`;
    // The session count goes through NUM, like the Sessions tile — not fmt(),
    // which abbreviates at a thousand, and not raw, which is what it was: a
    // 1,234-session project read "1234" here beside the tile's "1,234" on the
    // same screen. NUM is the pinned en-US formatter every other figure uses.
    return `<tr>
      <td>${esc(p.project)}</td>
      <td class="num">${esc(NUM.format(p.sessions))}</td>
      <td class="num">${esc(fmt(p.turns))}</td>
      <td class="num">${esc(fmt(p.input))}</td>
      <td class="num">${esc(fmt(p.output))}</td>
      ${costCell}
    </tr>`;
  }).join('');
  labelCells('project-cost-body');
  renderTableToggle('project-cost-foot', sorted.length, projectLimit, 'lessProjectRows', 'moreProjectRows', 'exportProjectsCSV');
}

// ── Project+Branch cost table sorting ────────────────────────────────────
function setProjectBranchSort(col) {
  if (branchSortCol === col) {
    branchSortDir = branchSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    branchSortCol = col;
    branchSortDir = 'desc';
  }
  updateProjectBranchSortIcons();
  applyFilter();
}

function updateProjectBranchSortIcons() {
  document.querySelectorAll('[id^="pbsort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('pbsort-' + branchSortCol);
  if (icon) icon.textContent = branchSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortProjectBranch(rows) {
  // Sort by the selected column (default: cost desc), consistent with the Cost by
  // Model / Cost by Project tables. Project name is only a stable tiebreaker when
  // the sorted column ties, so a project's branches stay grouped & deterministic
  // without overriding the primary order.
  return [...rows].sort((a, b) => {
    const av = a[branchSortCol] ?? 0;
    const bv = b[branchSortCol] ?? 0;
    if (av < bv) return branchSortDir === 'desc' ? 1 : -1;
    if (av > bv) return branchSortDir === 'desc' ? -1 : 1;
    const pa = (a.project || '').toLowerCase();
    const pb = (b.project || '').toLowerCase();
    return pa < pb ? -1 : pa > pb ? 1 : 0;
  });
}

function renderProjectBranchCostTable(rows) {
  const sorted = sortProjectBranch(rows);
  const shown = sorted.slice(0, shownCount(branchLimit, sorted.length));
  document.getElementById('project-branch-cost-body').innerHTML = shown.length === 0 ? emptyTableRow(7, 'No project/branch rows in selected range.') : shown.map(pb => {
    // Same two rules as the project table above: the session count is grouped
    // by NUM, and unpriced is not free.
    const costCell = pb.billable
      ? `<td class="cost">${esc(fmtCost(pb.cost))}</td>`
      : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td>${esc(pb.project)}</td>
      <td class="muted" style="font-family:monospace">${esc(pb.branch || '\u2014')}</td>
      <td class="num">${esc(NUM.format(pb.sessions))}</td>
      <td class="num">${esc(fmt(pb.turns))}</td>
      <td class="num">${esc(fmt(pb.input))}</td>
      <td class="num">${esc(fmt(pb.output))}</td>
      ${costCell}
    </tr>`;
  }).join('');
  labelCells('project-branch-cost-body');
  renderTableToggle('project-branch-cost-foot', sorted.length, branchLimit, 'lessBranchRows', 'moreBranchRows', 'exportProjectBranchCSV');
}

