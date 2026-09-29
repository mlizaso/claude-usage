"""The CSV exports must carry the digits the table beside them shows.

Money is rendered twice on this page. The tables call `fmtCost`, which is the
pinned en-US `Intl.NumberFormat` in web/js/20-format.js; the CSV exports called
`Number.prototype.toFixed(4)`. The two round an exact half in opposite
directions, so 85,194 output tokens at $25/M — 2.129850 exactly — read
`$2.1299` in the Cost by Model table and exported as `2.1298`. Five exports and
six call sites were on the second formatter, and nothing in the suite compared
the two surfaces, so they were free to disagree.

The rule these tests pin is *equality by construction*: every money field of
every export is the string the table shows, minus the `$` and minus the
grouping separator. Both sides are taken from the page's own formatters rather
than hardcoded, so the test cannot go stale against a repricing — with one
deliberate exception, `test_the_table_side_is_the_real_formatter`, which pins
two literal table strings so the comparison cannot pass by both surfaces
breaking together.

Grouping is dropped rather than stripped after the fact, and that matters: the
tables' `$3,183.0286` is correct for a person, but `csvField` quotes any field
containing a comma and most spreadsheets read a quoted `"3,183.0286"` as text.
`test_a_four_figure_amount_stays_one_unquoted_numeric_field` is the guard for
that — it is a worse regression than the 1e-4 this file exists for, and it is
the shape the first proposed fix would have shipped.

Every fixture value below is an exact decimal half at the fourth decimal whose
*double* falls just below that half, because that is the only case where the two
formatters disagree: 2.130050 lands at 2.1300500000000002 and rounds up under
both, so a fixture built from round numbers cannot see this defect at all.
`test_the_fixture_can_see_the_defect` asserts that property directly.
"""

import csv
import io
import json
import unittest

from tests.test_dashboard_js import emit, requires_node, run_js

# Drives all five exporters against the real page code and returns, per export,
# the rows it produced, the lines those rows encode to, and the strings the
# TABLE would show for the same numbers.
#
# `expected` is projected onto each export's own header rather than written out
# positionally, so it survives a column being reordered — and Python's coverage
# check below fails if a *new* money column appears without an expectation.
_EXPORT_PROBE = """
(() => {
  const captured = {};
  downloadCSV = (name, header, rows) => {
    captured[name] = {
      header, rows,
      // What the file would actually contain. csvField is what decides whether
      // a money column arrives as a number or as a quoted text cell.
      lines: rows.map(r => r.map(csvField).join(',')),
    };
  };

  const OPUS_TIE  = 'claude-opus-4-8';   // $5 in / $25 out / $0.50 read / $6.25 write
  const OPUS_BIG  = 'claude-opus-4-7';   // same rates, a four-figure row
  const OPUS_MIX  = 'claude-opus-4-6';   // exercises the three non-output buckets
  const UNPRICED  = 'gemma-3-27b';       // no rate anywhere: blank, never 0.0000
  const TIE_OUT   = 85194;               // x $25/M   = 2.129850
  const BIG_OUT   = 127321142;           // x $25/M   = 3183.028550, grouped as well
  const TIE_IN    = 400010;              // x $5/M    = 2.000050
  const TIE_READ  = 4000100;             // x $0.50/M = 2.000050
  const TIE_WRITE = 300168;              // x $6.25/M = 1.876050

  const row = (model, tokens) => Object.assign(
    {model, turns: 1, input: 0, output: 0, cache_read: 0, cache_creation: 0,
     cache_creation_1h: 0, reasoning: 0}, tokens);

  // The table's rendering of the same number: what fmtCost shows, without the
  // '$' the tables prepend and without the grouping a spreadsheet cannot read.
  const table = (v) => fmtCost(v).slice(1).replace(/,/g, '');
  const project = (header, byName) =>
    header.map(h => (Object.prototype.hasOwnProperty.call(byName, h) ? byName[h] : null));

  lastByModel = [
    row(OPUS_TIE, {output: TIE_OUT}),
    row(OPUS_BIG, {output: BIG_OUT}),
    row(OPUS_MIX, {input: TIE_IN, cache_read: TIE_READ, cache_creation: TIE_WRITE}),
    row(UNPRICED, {output: TIE_OUT}),
  ];
  exportModelCSV();
  // sortModels is what the exporter orders by, so the expectations are built
  // through it rather than from the unsorted source.
  const modelExpected = sortModels(lastByModel).map(m => {
    const parts = costParts(m.model, m.input, m.output, m.cache_read,
                            m.cache_creation, m.cache_creation_1h);
    const cost = calcCost(m.model, m.input, m.output, m.cache_read,
                          m.cache_creation, m.cache_creation_1h);
    const cell = (k) => (parts ? table(parts[k]) : '');
    return project(captured['cost_by_model'].header, {
      'Input Cost': cell('input'),
      'Output Cost': cell('output'),
      'Cache Read Cost': cell('cache_read'),
      'Cache Creation Cost': cell('cache_creation'),
      'Est. Cost': isBillable(m.model) ? table(cost) : '',
    });
  });

  // The other four exports read a precomputed `cost`/`billable` pair off each
  // row, exactly as applyFilter leaves them.
  const records = [row(OPUS_TIE, {output: TIE_OUT}),
                   row(OPUS_BIG, {output: BIG_OUT}),
                   row(UNPRICED, {output: TIE_OUT})].map(r => Object.assign({}, r, {
    cost: calcCost(r.model, r.input, r.output, r.cache_read, r.cache_creation,
                   r.cache_creation_1h),
    billable: isBillable(r.model),
  }));

  lastFilteredSessions = records.map((r, i) => Object.assign(
    {session_id: 's' + i, project: 'proj', topic: 'topic', last: '2026-08-06',
     duration_min: 1}, r));
  exportSessionsCSV();

  lastByProject = records.map((r, i) => Object.assign({project: 'proj' + i, sessions: 1}, r));
  exportProjectsCSV();

  lastByProjectBranch = records.map((r, i) => Object.assign(
    {project: 'proj' + i, branch: 'main', sessions: 1}, r));
  exportProjectBranchCSV();

  lastFilteredDispatches = records.map((r, i) => Object.assign(
    {agent_type: 'general-purpose', agent_id: 'agent' + i, start: '2026-08-06',
     tool_uses: 0, duration_ms: 0, status: 'completed'}, r));
  exportDispatchesCSV();

  const estOnly = (name) => records.map(r => project(captured[name].header, {
    'Est. Cost': r.billable ? table(r.cost) : '',
  }));

  return {
    exports: captured,
    expected: {
      'cost_by_model': modelExpected,
      'sessions': estOnly('sessions'),
      'projects': estOnly('projects'),
      'projects_by_branch': estOnly('projects_by_branch'),
      'subagent_dispatches': estOnly('subagent_dispatches'),
    },
    // Each fixture value as the TABLE renders it beside what toFixed(4) would
    // have written, so the fixture's ability to see the defect is itself
    // asserted rather than assumed.
    ties: [TIE_OUT * 25 / 1e6, BIG_OUT * 25 / 1e6, TIE_IN * 5 / 1e6,
           TIE_READ * 0.5 / 1e6, TIE_WRITE * 6.25 / 1e6].map(v => ({
      table: table(v), to_fixed: v.toFixed(4),
    })),
    table_probe: {tie: fmtCost(2.12985), big: fmtCost(3183.02855)},
  };
})()
"""

_PROBE = None


def _probe():
    """Run the export probe once; every test in this file reads the same result."""
    global _PROBE
    if _PROBE is None:
        _PROBE = run_js(emit(_EXPORT_PROBE))
    return _PROBE


def _money_columns(header):
    """The columns that carry money, by name — every header ending in 'Cost'."""
    return [i for i, h in enumerate(header) if h.endswith("Cost")]


@requires_node
class TestCsvMoneyMatchesTheTable(unittest.TestCase):
    def test_every_money_cell_carries_the_digits_the_table_shows(self):
        probe = _probe()
        checked = 0
        for name, export in probe["exports"].items():
            expected = probe["expected"][name]
            self.assertEqual(len(expected), len(export["rows"]),
                             f"{name}: expectation and export disagree on row count")
            for row, want in zip(export["rows"], expected):
                for col, wanted in enumerate(want):
                    if wanted is None:
                        continue
                    checked += 1
                    self.assertEqual(
                        row[col], wanted,
                        f"{name} row {row[0]!r} column {export['header'][col]!r}: "
                        f"exported {row[col]!r} but the table shows {wanted!r}. "
                        "The CSV and the cell it exports must be the same digits.")
        # 5 money columns on the model export x 4 rows, plus one on each of the
        # other four x 3 rows. A silently shrinking fixture is the failure mode
        # this counts against.
        self.assertEqual(checked, 5 * 4 + 4 * 3)

    def test_every_money_column_has_an_expectation(self):
        """A new money column must arrive with a rule, not slip through unchecked."""
        probe = _probe()
        for name, export in probe["exports"].items():
            covered = {i for i, v in enumerate(probe["expected"][name][0]) if v is not None}
            self.assertEqual(covered, set(_money_columns(export["header"])),
                             f"{name}: the money columns and the checked columns "
                             "differ — a Cost column was added or renamed")

    def test_the_table_side_is_the_real_formatter(self):
        """Pinned literals, so the comparison above cannot pass by both surfaces
        breaking together: if fmtCost itself were rewired to toFixed, every
        expectation would follow it down and the test would stay green."""
        probe = _probe()
        self.assertEqual(probe["table_probe"]["tie"], "$2.1299")
        self.assertEqual(probe["table_probe"]["big"], "$3,183.0286")

    def test_the_fixture_can_see_the_defect(self):
        """Every tie in the fixture must be one the two formatters disagree on.

        A tie whose double lands above the exact half (2.130050 is stored as
        2.1300500000000002) rounds up under both, so a fixture built from those
        would assert nothing at all.
        """
        for i, tie in enumerate(_probe()["ties"]):
            with self.subTest(tie=i):
                self.assertNotEqual(
                    tie["table"], tie["to_fixed"],
                    "this fixture value rounds the same way under both "
                    "formatters, so it cannot detect the defect")

    def test_every_money_column_keeps_four_decimals(self):
        """The exports are four-decimal columns. Routing them through the page's
        two-decimal formatter (COST_BIG, which the tiles use) would look like a
        unification and silently drop two digits from every exported figure."""
        probe = _probe()
        for name, export in probe["exports"].items():
            for row in export["rows"]:
                for col in _money_columns(export["header"]):
                    field = row[col]
                    if field == "":
                        continue        # unpriced: blank by design, see below
                    with self.subTest(export=name, column=export["header"][col]):
                        self.assertRegex(field, r"^-?\d+\.\d{4}$")

    def test_a_four_figure_amount_stays_one_unquoted_numeric_field(self):
        """Grouping must be dropped, not stripped afterwards — and not kept.

        `csvField` quotes any field containing a comma, and a quoted
        "3,183.0286" is a text cell in most spreadsheets: a strictly worse
        regression than the last-decimal disagreement this file exists for.
        """
        probe = _probe()
        for name, export in probe["exports"].items():
            cols = _money_columns(export["header"])
            for row, line in zip(export["rows"], export["lines"]):
                parsed = next(csv.reader(io.StringIO(line)))
                self.assertEqual(len(parsed), len(export["header"]),
                                 f"{name}: {line!r} does not parse as one record")
                for col in cols:
                    field = row[col]
                    if field == "":
                        continue
                    with self.subTest(export=name, column=export["header"][col]):
                        self.assertNotIn(",", field, "a grouped money column would "
                                         "be quoted by csvField and read as text")
                        self.assertEqual(parsed[col], field)
                        # Reads back as the number it renders, in any locale.
                        self.assertAlmostEqual(float(parsed[col]), float(field), places=4)

    def test_an_unpriced_model_still_exports_blank_not_zero(self):
        """The rule the fix must not disturb, on all five exports rather than
        the one `TestUnpricedModelsExportBlankNotZero` covers: a model we have
        no rate for renders n/a in the table, so its CSV cell is empty. A
        formatter applied unconditionally would print a confident 0.0000."""
        probe = _probe()
        for name, export in probe["exports"].items():
            blanks = [row for row in export["rows"]
                      if any(row[c] == "" for c in _money_columns(export["header"]))]
            self.assertTrue(blanks, f"{name}: the fixture lost its unpriced row, so "
                                    "the blank-not-zero rule is not being tested")
            for row in blanks:
                for col in _money_columns(export["header"]):
                    self.assertEqual(row[col], "",
                                     f"{name}: an unpriced row must not export a "
                                     "confident figure in any money column")


if __name__ == "__main__":   # pragma: no cover - parity with the other suites
    unittest.main()
