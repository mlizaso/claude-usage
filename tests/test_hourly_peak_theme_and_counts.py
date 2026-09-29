"""The peak-hour bars' colour, and the counts the tables printed unformatted.

Two things the page rendered in a way its own rules forbid:

* The hourly chart painted its peak bars with a literal `rgba(199,78,57,…)` —
  the DARK palette's `--red` — while the legend swatch that explains them is
  `var(--red)`. A literal cannot follow a theme, so on a light page the swatch
  (#B03A26) and the bars it labels (#CD604D composited over the card) were
  different reds; on the dark page the 0.9 alpha alone already separated them
  (#C74E39 against #B64936). `C.red`, which `syncChartColors` re-reads from the
  CSS for exactly this purpose, was read by nothing.

* AGENTS.md pins every number the page prints to a grouping en-US formatter.
  Several counts went through neither `NUM` nor `fmt`: a session's turn count,
  the row total in "Download CSV to see all (4567)", and the turns in the
  subagent chart's tooltip, which sat in one line beside a `fmt`-ed token
  total. Each read as an ungrouped run of digits next to grouped ones — on an
  en-US browser, where the pinning is supposed to be invisible.

The colour tests composite both sides over `--card` and compare, because that
is what the eye does: an alpha-blended bar and an opaque swatch of the same
token are *not* the same colour on screen, which is why matching the token
alone is not enough.
"""

import re
import unittest
from pathlib import Path

from tests.test_dashboard_js import emit, requires_node, run_js

CSS = (Path(__file__).resolve().parent.parent / "web" / "app.css").read_text(
    encoding="utf-8")


def _palette(pattern):
    """The `--token: #rrggbb` pairs of one palette block."""
    match = re.search(pattern, CSS, re.S)
    assert match, "missing CSS block: " + pattern
    return dict(re.findall(r"(--[a-z-]+):\s*(#[0-9A-Fa-f]{6})", match.group(1)))


DARK = _palette(r":root\s*\{(.*?)\}")
LIGHT = _palette(r":root\[data-theme=\"light\"\]\s*\{(.*?)\}")


def _rgb(value):
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def _over(fg, alpha, bg):
    """`fg` at `alpha` composited onto opaque `bg` — what the screen shows."""
    f, b = _rgb(fg), _rgb(bg)
    return tuple(round(alpha * f[i] + (1 - alpha) * b[i]) for i in range(3))


def _parse_rgba(value):
    """'rgba(r,g,b,a)' -> ((r, g, b), a)."""
    m = re.fullmatch(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)",
                     value.strip())
    assert m, ("expected an rgba() colour carrying an alpha, got " + repr(value)
               + " — an opaque bar has no lift to hover to and does not "
                 "composite to the swatch's colour")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))), float(m.group(4))


# Renders the hourly chart under a given palette and hands back the colours the
# bars were actually built with. The Chart constructor is replaced *after* load
# so the real config object is captured instead of discarded by the stub.
_BARS = """(() => {
  document.documentElement = {};
  globalThis.getComputedStyle = () => ({
    getPropertyValue: (name) => palette[name] || '' });
  syncChartColors();
  const built = [];
  globalThis.Chart = function Chart(ctx, cfg) {
    built.push(cfg);
    return { update() {}, destroy() {}, data: cfg.data, options: cfg.options };
  };
  globalThis.Chart.defaults = {
    color: '', font: {}, borderColor: '', backgroundColor: '',
    plugins: { tooltip: { callbacks: {} }, legend: { labels: {} } },
    scale: { grid: {} }, scales: {}, elements: {}, datasets: {} };
  charts.hourly = null;
  const rows = [];
  for (let h = 0; h < 24; h++) rows.push({ day: '2026-08-03', hour: h, turns: 1, output: 10 });
  const agg = aggregateHourly(rows, 'utc');
  renderHourlyChart(agg);
  const bars = built[0].data.datasets[0];
  const peak = agg.hours.findIndex(h => h.peak);
  const off = agg.hours.findIndex(h => !h.peak);
  return { red: C.red,
           peak: bars.backgroundColor[peak], peakHover: bars.hoverBackgroundColor[peak],
           off: bars.backgroundColor[off], offHover: bars.hoverBackgroundColor[off] };
})()"""


def _bars(palette):
    return run_js(emit(_BARS, palette=palette))


@requires_node
class TestThePeakBarsArePaintedFromTheThemedRed(unittest.TestCase):
    """`C.red` is synced from `--red` on every theme change; the bars must use it.

    Before this it was dead: `grep -o '\\bC\\.[a-zA-Z]*'` over web/js found only
    accent, axis, blue, border, card and green, and the peak bars carried a copy
    of the dark red frozen into the source.
    """

    def test_the_light_palette_moves_the_bars(self):
        got = _bars(LIGHT)
        self.assertEqual(got["red"], LIGHT["--red"])
        rgb, alpha = _parse_rgba(got["peak"])
        self.assertEqual(rgb, _rgb(LIGHT["--red"]),
                         "the peak bars are not the light theme's red")
        self.assertAlmostEqual(alpha, 0.9)

    def test_the_dark_palette_moves_them_back(self):
        got = _bars(DARK)
        rgb, alpha = _parse_rgba(got["peak"])
        self.assertEqual(rgb, _rgb(DARK["--red"]))
        self.assertAlmostEqual(alpha, 0.9)

    def test_the_hover_lift_survives(self):
        """Every other bar on the page brightens to full opacity on hover
        (TOKEN_COLORS -> TOKEN_HOVER). Painting the peak bars with a single
        opaque `C.red` would theme them correctly and silently drop that lift
        from this chart alone."""
        for name, palette in (("light", LIGHT), ("dark", DARK)):
            with self.subTest(theme=name):
                got = _bars(palette)
                rest_rgb, rest_alpha = _parse_rgba(got["peak"])
                hover_rgb, hover_alpha = _parse_rgba(got["peakHover"])
                self.assertEqual(hover_rgb, rest_rgb)
                self.assertAlmostEqual(hover_alpha, 1.0)
                self.assertGreater(hover_alpha, rest_alpha,
                                   "the peak bars no longer lift on hover")

    def test_peak_stays_distinguishable_from_the_hours_around_it(self):
        """The whole point of the colour: the throttling window has to be
        readable off the chart without the tooltip."""
        for name, palette in (("light", LIGHT), ("dark", DARK)):
            with self.subTest(theme=name):
                got = _bars(palette)
                peak = _parse_rgba(got["peak"])[0]
                off = _parse_rgba(got["off"])[0]
                self.assertNotEqual(peak, off)
                self.assertGreater(peak[0], peak[2], "the peak bars are not red")
                self.assertGreater(off[2], off[0], "the off-peak bars are not blue")


@requires_node
class TestTheLegendSwatchIsTheColourOfTheBars(unittest.TestCase):
    """A legend swatch that is a different colour from its bars explains nothing.

    Both sides are composited over `--card` before comparing: the bars are drawn
    at 0.9 on a transparent canvas, so an opaque swatch of the same token misses
    them by the alpha — which is why this failed on the dark theme too, where
    the token already matched.
    """

    def _swatch(self):
        rule = re.search(r"\.peak-swatch\s*\{([^}]*)\}", CSS)
        self.assertIsNotNone(rule, "no .peak-swatch rule")
        body = rule.group(1)
        token = re.search(r"background:\s*var\((--[a-z-]+)\)", body)
        self.assertIsNotNone(token, "the swatch no longer names a palette token")
        opacity = re.search(r"opacity:\s*([\d.]+)", body)
        return token.group(1), (float(opacity.group(1)) if opacity else 1.0)

    def test_the_swatch_and_the_bars_land_on_the_same_pixel_colour(self):
        token, alpha = self._swatch()
        for name, palette in (("light", LIGHT), ("dark", DARK)):
            with self.subTest(theme=name):
                bar_rgb, bar_alpha = _parse_rgba(_bars(palette)["peak"])
                bar = tuple(round(bar_alpha * bar_rgb[i]
                                  + (1 - bar_alpha) * _rgb(palette["--card"])[i])
                            for i in range(3))
                swatch = _over(palette[token], alpha, palette["--card"])
                self.assertEqual(swatch, bar,
                                 "the legend swatch is a different red from the "
                                 "bars it labels")


# Renders the Recent Sessions table and the show-more footer into captured
# innerHTML. Counts are four digits so a missing grouping separator is visible;
# every value is a plain number, as the payload delivers it.
_TABLES = """(() => {
  const html = {};
  const els = new Map();
  document.getElementById = (id) => {
    if (!els.has(id)) {
      const el = stubEl();
      Object.defineProperty(el, 'innerHTML', {
        get() { return html[id] === undefined ? '' : html[id]; },
        set(v) { html[id] = v; },
      });
      els.set(id, el);
    }
    return els.get(id);
  };
  renderSessionsTable([{ session_id: 'abcdef0123', project: 'p', topic: 't',
    last: '2026-08-06 10:00', duration_min: 7, model: 'claude-opus-5',
    turns: 1234, input: 10, output: 20, cost: 1, billable: true }]);
  // TABLE_MAX as the limit is the "cap reached, the rest only via CSV" branch —
  // the only one that prints a total.
  renderTableToggle('csv-foot', 4567, TABLE_MAX,
                    'lessSessionRows', 'moreSessionRows', 'exportSessionsCSV');
  return html;
})()"""


@requires_node
class TestTheTablesGroupTheCountsTheyPrint(unittest.TestCase):
    """The counts left over after the two project session cells were fixed.

    (Those two are covered by
    `test_frontend_data_path.TestProjectSessionCountsAreGroupedLikeEveryOtherNumber`;
    these are the rest of the same sweep.)

    `fmt` is not the right formatter for a count: it abbreviates at a thousand,
    and a session's turn count is read exactly. `NUM` is what the Sessions tile
    uses, for the same reason.
    """

    @classmethod
    def setUpClass(cls):
        cls.html = run_js(emit(_TABLES))

    def test_a_sessions_turn_count_is_grouped(self):
        self.assertIn(">1,234<", self.html["sessions-body"])
        self.assertNotIn(">1234<", self.html["sessions-body"])

    def test_the_csv_row_count_is_grouped(self):
        """This branch only fires past TABLE_MAX (50 rows), so a four-figure
        count is the normal case rather than an edge one."""
        self.assertIn("(4,567)", self.html["csv-foot"])
        self.assertNotIn("(4567)", self.html["csv-foot"])


@requires_node
class TestTheSubagentTooltipFormatsItsTurnCount(unittest.TestCase):
    """The footer prints a token total and a turn count in one line. The tokens
    went through `fmt` and the turns through nothing, so the line read
    "Total: 1.23M · 45678 turns"."""

    def test_the_turn_count_is_formatted_like_every_other_aggregate(self):
        got = run_js(emit("""(() => {
          const built = [];
          globalThis.Chart = function Chart(ctx, cfg) {
            built.push(cfg); return { update() {}, destroy() {}, data: cfg.data };
          };
          globalThis.Chart.defaults = { color: '', font: {}, borderColor: '',
            backgroundColor: '', plugins: { tooltip: { callbacks: {} },
            legend: { labels: {} } }, scale: { grid: {} }, scales: {},
            elements: {}, datasets: {} };
          charts.subagent = null;
          renderSubagentChart([{ agent_type: 'Explore', input: 1, output: 2,
                                 cache_read: 3, cache_creation: 4, turns: 45678 }]);
          const cb = built[0].options.plugins.tooltip.callbacks;
          return cb.footer([{ raw: 1230000, dataIndex: 0 }]);
        })()"""))
        self.assertNotIn("45678", got)
        self.assertIn("45.7K", got)


if __name__ == "__main__":
    unittest.main()
