// ── Renderers ──────────────────────────────────────────────────────────────
// Stamp every generated <td> with its column's header text, so the phone layout
// can print each row as a stack of label/value pairs (`.stack-table` in
// app.css) instead of an 11-column table viewed through a 300px window.
//
// The labels are read from the table's own <thead> rather than hardcoded per
// renderer: the <th> list lives in index.html while the <td>s are built here, so
// a second copy of the column names would drift with nothing to catch it.
function labelCells(bodyId) {
  const body = document.getElementById(bodyId);
  // Duck-typed throughout: the JS tests swap getElementById for a plain object
  // that only records innerHTML, so nothing here may assume a real element.
  const table = body && body.closest && body.closest('table');
  if (!table) return;
  // Accepts a <tbody> or a single <tr> (the totals row lives in a <tfoot>).
  const rows = body.rows || (body.cells ? [body] : null);
  if (!rows) return;
  const heads = [...table.querySelectorAll('thead th')].map(th => {
    const copy = th.cloneNode(true);
    // Drop the ' ▼' sort arrow so the label doesn't inherit it.
    copy.querySelectorAll('.sort-icon').forEach(node => node.remove());
    return copy.textContent.trim();
  });
  for (const row of rows) {
    for (let i = 0; i < row.cells.length; i++) {
      const cell = row.cells[i];
      if (cell.colSpan > 1) continue;   // the "nothing in range" placeholder row
      if (heads[i]) cell.setAttribute('data-label', heads[i]);
    }
  }
}
// Every tile states the same window, with its concrete dates — including
// Est. Cost, which used to say only "API pricing, June 2026" and left the
// reader with no idea what period the money covered. Tiles that also need an
// explanation carry it on a second line rather than in a `title` tooltip, since
// native tooltips never appear on touch.
// What the money figure actually rests on. Two independent facts, and the note
// states whichever apply rather than collapsing them into one word:
//
//  * WHOSE list price it is — the two assistants are priced by different vendors.
//  * Whether it was BILLED that way. A Codex plan here is a subscription with a
//    weekly quota, so the figure is what those tokens would have cost through
//    the API, not what was charged. That stays true even though the rates are
//    published, so it is said separately from whether they are estimates.
//  * Whether any rate in view is an estimate. Two Codex-internal model ids
//    appear in the transcripts but on no price list; saying "estimated" when
//    only those contribute would overstate the doubt, and omitting it when they
//    do would hide it.
function costBasisNote(t) {
  if (selectedSource === 'codex') {
    return 'OpenAI list rates — subscription plan, not billed per token'
      + (t.estimated ? '; some rates estimated' : '');
  }
  return 'Anthropic list API pricing' + (t.estimated ? '; some rates estimated' : '');
}

// Why there is no money to show. `t.billable` is false in four different
// situations and only one of them is about the price list, so reading that one
// flag made the tile assert "No published per-token rate for these models" over
// an empty "Today" — about models the same page had priced at list rates one
// range-selection earlier. AGENTS.md treats `n/a` and `$0.00` as different
// claims for this reason; "not priced", "not there" and "nothing scanned yet"
// are three more, and the tile has to make the one that is true.
//
// The fourth is checked first because on an unscanned database the other three
// are ALL vacuously true, and the one they picked — "No models selected" —
// blamed a filter the reader had never touched, beside a dropdown that itself
// reads "No models". The discriminator is the payload's own model list rather
// than `selectedModels`: `all_models` is every model this source has ever
// produced, so it is empty only when there is no usage to have. Unchecking
// every box leaves it full, which is what keeps that case on its own message —
// testing `t.empty` first instead would not, since an empty `byModel` is a
// SUPERSET of an empty filter, not a sibling of it.
//
// The VALUE stays `n/a` in all four. Whether zero tokens cost zero dollars is
// exactly the question the source answers ($0.00 is honest for Claude and an
// invention for Codex), and an empty view carries nothing to answer it with.
function noCostReason(t) {
  if (!((rawData && rawData.all_models) || []).length) {
    return 'No usage recorded yet — run a scan';
  }
  if (t.noModelsSelected) return 'No models selected';
  if (t.empty) return 'No usage in this range for the selected models';
  return 'No published per-token rate for these models';
}

// ── "Nothing has been read into this database" ─────────────────────────────
// The one thing on this page that is about the DATABASE rather than the usage
// in it, and it exists because the database can be emptied out from under a
// running page: `db.init_db` drops every table when the schema in front of it
// is not one this build wrote — an older install sharing ~/.claude/usage.db is
// enough — and what it leaves behind renders as a complete, plausible, entirely
// empty dashboard. The notice that explains it goes to stderr, and the reader
// of this page is looking at a browser. Auto-refresh is off by default, so the
// replacement scan's results are never fetched either: without this banner the
// wrong answer is not transient, it is what the reader is left with.
//
// Deliberately NOT the error path. `loadData` arms a three-second retry inside
// `if (d.error)`, and no amount of retrying turns an empty database into a full
// one; it would also replace the page, when what is wanted is a sentence above
// figures that are real (they are genuinely all zero) but not the reader's.
//
// The same sentence is correct for a first install whose scan has not finished,
// which is why the payload field asks "has anything been read in", not "did
// something just get dropped" — see dashboard_data._database_is_unscanned.
const DB_NOTICE_TEXT =
  'No transcripts have been read into this database, so every figure below is '
  + 'zero — this is not your usage history. A scan may still be running: '
  + 'press Rescan, or use  ' + APP_COMMANDS.scan + '  and reload.';

// Built here rather than in index.html because it is absent from the page in
// every ordinary state, and every DOM call is guarded: the JS tests replace
// `document` with a stub whose elements have no insertBefore, and a renderer
// that assumed a real one would take those suites down with it.
function databaseNoticeElement() {
  const existing = document.getElementById('db-notice');
  if (existing) return existing;
  if (typeof document.createElement !== 'function'
      || typeof document.querySelector !== 'function') return null;
  const host = document.querySelector('.container');
  if (!host || typeof host.insertBefore !== 'function') return null;
  const el = document.createElement('div');
  el.id = 'db-notice';
  // A status rather than an alert: it describes the page, it does not interrupt.
  el.setAttribute('role', 'status');
  // Inline because web/app.css is not where this lives; `style-src` allows it.
  // No `display` — that would beat the UA's `[hidden]` rule and the banner could
  // never be turned off again.
  el.style.cssText = 'margin: 0 0 18px; padding: 11px 14px; font-size: 13px;'
    + ' line-height: 1.55; border: 1px solid var(--accent);'
    + ' border-radius: 8px; background: var(--card); color: var(--text);';
  host.insertBefore(el, host.firstChild || null);
  return el;
}

function renderDatabaseNotice(unscanned) {
  const el = unscanned
    ? databaseNoticeElement() : document.getElementById('db-notice');
  if (!el) return;
  el.hidden = !unscanned;
  el.textContent = unscanned ? DB_NOTICE_TEXT : '';
}

function renderStats(t, rangeLabelFull) {
  // The argument is optional so a direct call (and the stubbed renderStats in
  // the JS tests) still renders something sane.
  const rangeLabel = rangeLabelFull || rangeLabelWithDates(selectedRange, []);
  const stats = [
    // NUM, not toLocaleString: this is the one tile that formats its own number
    // (it must print the exact count, not fmt()'s "1.2K"), and a bare
    // toLocaleString follows the VIEWER's locale — so it read "1.234.567" beside
    // an en-US "$1,500.00", which is the inconsistency 20-format.js was written
    // to end. NUM is the same pinned formatter fmt() uses underneath.
    { label: 'Sessions',        value: NUM.format(t.sessions),      sub: rangeLabel },
    { label: 'Turns',           value: fmt(t.turns),                sub: rangeLabel },
    { label: 'Input Tokens',    value: fmt(t.input),                sub: rangeLabel },
    { label: 'Output Tokens',   value: fmt(t.output),               sub: rangeLabel },
    { label: 'Subagent Tokens', value: fmt(t.subagent_tokens || 0), sub: rangeLabel, note: 'Included in the totals above' },
    { label: 'Cache Read',      value: fmt(t.cache_read),           sub: rangeLabel, note: 'Reads from the prompt cache' },
    { label: 'Cache Creation',  value: fmt(t.cache_creation),       sub: rangeLabel, note: 'Writes to the prompt cache' },
    // Priced sources show money; unpriced ones say so. A Codex plan is a
    // subscription with a weekly quota and no per-token rate anywhere in its
    // data, so any figure here would be invented — and $0.00 is the most
    // misleading invention available.
    t.billable
      ? { label: 'Est. Cost', value: fmtCostBig(t.cost), sub: rangeLabel,
          note: costBasisNote(t), color: C.green }
      : { label: 'Est. Cost', value: 'n/a', sub: rangeLabel,
          note: noCostReason(t) },
  ];
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="label">${s.label}</div>
      <div class="value" style="${s.color ? 'color:' + s.color : ''}">${esc(s.value)}</div>
      ${s.sub ? `<div class="sub">${esc(s.sub)}</div>` : ''}
      ${s.note ? `<div class="note">${esc(s.note)}</div>` : ''}
    </div>
  `).join('');
}
