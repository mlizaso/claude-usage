"""Behavior of the standalone quota page against its actual shipped script."""

import unittest
from pathlib import Path

from tests.test_dashboard_js import _run_js_source, requires_node


APP = Path(__file__).resolve().parent.parent / "web" / "limits" / "app.js"
DOM = r"""
function node() {
  return {textContent: '', hidden: false, children: [],
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    setAttribute() {}, addEventListener() {}};
}
const nodes = new Map();
globalThis.document = {
  createElement: node,
  getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, node());
    return nodes.get(id);
  },
};
globalThis.location = {hash: '', pathname: '/', search: ''};
globalThis.history = {replaceState() {}};
globalThis.window = {};
globalThis.fetch = () => { throw new Error('unexpected network request'); };
"""


def run_limits(body):
    return _run_js_source(DOM + APP.read_text(encoding="utf-8") + "\n" + body)


@requires_node
class TestStandaloneQuotaReadings(unittest.TestCase):
    def test_an_older_refresh_cannot_replace_a_newer_reading_or_error(self):
        got = run_limits(r"""
          (async () => {
            const pending = [];
            api = () => new Promise((resolve, reject) => pending.push({resolve, reject}));
            const errors = [];
            const old = refresh().catch(error => errors.push(error.message));
            const current = refresh();
            pending[1].resolve({windows: [{key: 'claude:session', percent: 85}],
              age_seconds: 0});
            await current;
            pending[0].resolve({windows: [{key: 'claude:session', percent: 60}],
              age_seconds: 30});
            await old;
            const retained = state.payload.windows[0].percent;
            const failed = refresh().catch(error => errors.push(error.message));
            const recovered = refresh();
            pending[3].resolve({windows: [], age_seconds: 0});
            await recovered;
            pending[2].reject(new Error('obsolete failure'));
            await failed;
            console.log(JSON.stringify({retained, errors}));
          })();
        """)
        self.assertEqual(got, {"retained": 85, "errors": []})

    def test_unknown_percentage_and_age_remain_unknown(self):
        got = run_limits(r"""
          const values = [null, undefined, '', false, NaN, Infinity, 0, 42];
          const percentages = values.map(value => liveCard({percent: value})
            .children[0].children[1].textContent);
          Date.now = () => 10000;
          const ages = values.map(age => {
            state.payload = {age_seconds: age, reading: 'cache'};
            state.receivedAt = 9000;
            updateReading();
            return document.getElementById('reading').textContent;
          });
          console.log(JSON.stringify({percentages, ages}));
        """)
        self.assertEqual(got["percentages"], ["—"] * 6 + ["0%", "42%"])
        self.assertEqual(got["ages"], ["Claude Code cache"] * 6 + [
            "Claude Code cache · 1s old", "Claude Code cache · 43s old",
        ])

    def test_crossings_preserve_window_identity_and_high_water_mark(self):
        got = run_limits(r"""
          const notices = [];
          showNotice = message => notices.push(message);
          const a = '2099-01-01T12:00:00Z', b = '2099-01-01T17:00:00Z';
          const read = (percent, resets_at = a, extra = {}) => {
            reportCrossings({windows: [{key: 'claude:session', label: 'Session',
              percent, resets_at, thresholds: [80, 90], ...extra}]});
            return notices.splice(0);
          };
          console.log(JSON.stringify({
            unknown: read(null), first: read(79), crossing: read(80),
            resting: read(80), correction: read(70), repeated: read(81),
            jitter: read(85, '2099-01-01T11:59:59.900Z'),
            next: read(90, b), expired: read(95, b, {expired: true}),
            orphan: read(95, b, {orphaned: true}),
          }));
        """)
        self.assertEqual(got, {
            "unknown": [], "first": [], "crossing": ["Session crossed 80%"],
            "resting": [], "correction": [], "repeated": [], "jitter": [],
            "next": ["Session crossed 80%, 90%"], "expired": [], "orphan": [],
        })

    def test_first_known_high_reading_is_a_baseline(self):
        got = run_limits(r"""
          const notices = [];
          showNotice = message => notices.push(message);
          for (const percent of [null, 85, 85, 70, 81]) {
            reportCrossings({windows: [{key: 'claude:weekly', percent,
              resets_at: '2099-01-07T00:00:00Z', thresholds: [80]}]});
          }
          console.log(JSON.stringify(notices));
        """)
        self.assertEqual(got, [])
