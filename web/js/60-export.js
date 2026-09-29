// ── CSV Export ────────────────────────────────────────────────────────────
function csvField(val) {
  let s = String(val);
  // Spreadsheet applications can execute cells beginning with formula
  // sigils, including after leading control characters or spaces. Prefixing
  // an apostrophe preserves the displayed value while forcing text mode.
  if (/^[\s\u0000-\u001f\u007f-\u009f]*[=+\-@]/u.test(s)) s = "'" + s;
  // \r matters as much as \n: a bare carriage return also ends a record in
  // RFC 4180 readers, so an unquoted one splits the row and the fragment after
  // it is parsed as a new record.
  if (s.includes(',') || s.includes('"') || s.includes('\n') || s.includes('\r')) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

function csvTimestamp() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0')
    + '_' + String(d.getHours()).padStart(2,'0') + String(d.getMinutes()).padStart(2,'0');
}

function downloadCSV(reportType, header, rows) {
  const lines = [header.map(csvField).join(',')];
  for (const row of rows) {
    lines.push(row.map(csvField).join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = reportType + '_' + csvTimestamp() + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

function exportModelCSV() {
  // Mirrors the table: every token column is followed by what it cost, so the
  // export can be totalled in a spreadsheet and land on the same numbers.
  // Reasoning has no cost column beside it, unlike every other token column
  // here: it is a subset of Output, already priced inside it. A "Reasoning Cost"
  // would be the same money counted twice, and a spreadsheet totalling the row
  // would double-bill it.
  const header = ['Model', 'Turns', 'Input', 'Input Cost', 'Output', 'Output Cost',
                  'Cache Read', 'Cache Read Cost', 'Cache Creation', 'Cache Creation (1h)',
                  'Cache Creation Cost', 'Reasoning', 'Est. Cost'];
  // CSV_COST, not toFixed: every money column here is the same string the table
  // shows, minus the '$' and the grouping. See web/js/20-format.js.
  const money = (parts, key) => (parts ? CSV_COST.format(parts[key]) : '');
  const rows = sortModels(lastByModel).map(m => {
    const parts = rowCostParts(m);
    const cost = rowCost(m);
    return [m.model, m.turns,
            m.input, money(parts, 'input'),
            m.output, money(parts, 'output'),
            m.cache_read, money(parts, 'cache_read'),
            m.cache_creation, m.cache_creation_1h, money(parts, 'cache_creation'),
            // Blank rather than 0 where no figure was reported, matching the em
            // dash the table shows: none used and none recorded are different.
            m.reasoning ? m.reasoning : '',
            isBillable(m.model) ? CSV_COST.format(cost) : ''];
  });
  downloadCSV('cost_by_model', header, rows);
}

function exportSessionsCSV() {
  const header = ['Session', 'Project', 'Title', 'Last Active', 'Duration (min)', 'Model', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Cache Creation (1h)', 'Est. Cost'];
  const rows = lastFilteredSessions.map(s => {
    const cost = s.cost;
    return [s.session_id, s.project, s.topic, s.last, s.duration_min, s.model, s.turns,
            s.input, s.output, s.cache_read, s.cache_creation, s.cache_creation_1h,
            s.billable ? CSV_COST.format(cost) : ''];
  });
  downloadCSV('sessions', header, rows);
}

function exportProjectsCSV() {
  const header = ['Project', 'Sessions', 'Turns', 'Input', 'Output', 'Cache Read',
                  'Cache Creation', 'Cache Creation (1h)', 'Est. Cost'];
  const rows = lastByProject.map(p => {
    return [p.project, p.sessions, p.turns, p.input, p.output, p.cache_read,
            p.cache_creation, p.cache_creation_1h, p.billable ? CSV_COST.format(p.cost) : ''];
  });
  downloadCSV('projects', header, rows);
}

function exportProjectBranchCSV() {
  const header = ['Project', 'Branch', 'Sessions', 'Turns', 'Input', 'Output',
                  'Cache Read', 'Cache Creation', 'Cache Creation (1h)', 'Est. Cost'];
  const rows = lastByProjectBranch.map(pb => {
    return [pb.project, pb.branch, pb.sessions, pb.turns, pb.input, pb.output,
            pb.cache_read, pb.cache_creation, pb.cache_creation_1h,
            pb.billable ? CSV_COST.format(pb.cost) : ''];
  });
  downloadCSV('projects_by_branch', header, rows);
}

function exportDispatchesCSV() {
  const header = ['Type', 'Agent ID', 'Started', 'Model', 'Turns', 'Tool Uses', 'Duration (ms)', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Cache Creation (1h)', 'Total Tokens', 'Est. Cost', 'Status'];
  const rows = lastFilteredDispatches.map(d => {
    const total = d.input + d.output + d.cache_read + d.cache_creation;
    return [d.agent_type, d.agent_id, d.start, d.model, d.turns,
            d.tool_uses != null ? d.tool_uses : '', d.duration_ms != null ? d.duration_ms : '',
            d.input, d.output, d.cache_read, d.cache_creation, d.cache_creation_1h, total,
            d.billable ? CSV_COST.format(d.cost) : '', d.status || ''];
  });
  downloadCSV('subagent_dispatches', header, rows);
}
