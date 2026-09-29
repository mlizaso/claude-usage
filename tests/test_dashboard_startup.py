"""Exercise saved-first startup against delayed scan and calculation responses."""

import html
import json
import re
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import dashboard
from tests.test_dashboard_js import (
    BROWSER, REPO_ROOT, _browser_payload, requires_browser, requires_node, run_js,
)


DOM = r"""
const elements = new Map();
window.location.pathname = '/';
globalThis.history = {replaceState(state, unused, path) {
  const url = new URL(path, window.location.href);
  window.location.href = url.href;
  window.location.search = url.search;
}};
const originalElement = document.getElementById;
document.getElementById = id => {
  if (!elements.has(id)) elements.set(id, originalElement(id));
  return elements.get(id);
};
const classes = new Set();
const container = {
  classList: {add: c => classes.add(c), remove: c => classes.delete(c)},
  setAttribute(){}, removeAttribute(){}
};
document.querySelector = s => s === '.container' ? container : null;
const element = id => document.getElementById(id);
element('source-chooser').hidden = true;
const paints = [];
applyFilter = () => paints.push(rawData.marker);
const deferred = () => {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return {promise, resolve};
};
const settle = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
const reply = (data, status = 200) => ({ok: status === 200, status, json: async () => data});
const data = (marker, source = 'claude') => ({
  marker, generated_at: marker, unscanned: false,
  all_models: source === 'claude' ? ['claude-sonnet-4-6'] : ['gpt-6-astra'],
  daily_by_model: [], sessions_all: [],
  subscription_limits: {available: true, windows: [], age_seconds: 7},
  codex_limits: {available: true, windows: [], age_seconds: 12},
});
const savedAt = (Date.now() - 3600000) / 1000;
const snapshot = value => ({snapshot: {saved_at: savedAt, data: value}});
const sources = [{source: 'claude', turns: 5}];
const calls = [];
const scan = deferred();
const calculation = deferred();
apiFetch = async (path, options = {}) => {
  calls.push((options.method || 'GET') + ' ' + path);
  if (path === '/api/snapshot') return reply(snapshot({sources}));
  if (path.startsWith('/api/snapshot?')) return reply(snapshot(data('saved')));
  if (path === '/api/scan-status') return reply({state: 'idle', generation: 2});
  if (path === '/api/rescan') return scan.promise;
  if (path === '/api/sources') return reply({sources});
  if (path.startsWith('/api/data?')) return calculation.promise;
  throw new Error('unexpected request ' + path);
};
const state = () => ({
  marker: rawData && rawData.marker,
  banner: !element('snapshot-status').hidden,
  text: element('snapshot-status-text').textContent,
  overlay: !element('load-overlay').hidden,
  dimmed: classes.has('loading'), meta: element('meta').innerHTML,
  source: renderedSource,
});
"""


@requires_node
class TestSavedFirstStartup(unittest.TestCase):
    def drive(self, script):
        return run_js("(async () => {\n" + DOM + script + "\n})();")

    def test_preview_is_interactive_through_scan_and_calculation_then_replaced(self):
        got = self.drive(r"""
          const boot = bootDashboard();
          await settle();
          const duringScan = state();
          const age = planSampleAge(rawData.subscription_limits);
          selectedModels.clear(); // preserve a filter changed while viewing saved data
          await autoRefreshTick(); // a remembered timer must not queue another scan
          const readsBeforeScan = calls.filter(c => c.includes('/api/data?')).length;
          scan.resolve(reply({new: 1, updated: 0}));
          await settle();
          const duringCalculation = state();
          calculation.resolve(reply(data('fresh')));
          await boot;
          console.log(JSON.stringify({duringScan, duringCalculation, after: state(),
            paints, calls, age, readsBeforeScan, selected: [...selectedModels]}));
        """)
        for stage in (got["duringScan"], got["duringCalculation"]):
            self.assertEqual(stage["marker"], "saved")
            self.assertTrue(stage["banner"])
            self.assertIn("Updating", stage["text"])
            self.assertFalse(stage["overlay"])
            self.assertFalse(stage["dimmed"])
            self.assertIn("Saved: saved", stage["meta"])
        self.assertGreaterEqual(got["age"], 3607)
        self.assertEqual(got["readsBeforeScan"], 0)
        self.assertEqual(got["calls"].count("POST /api/rescan"), 1)
        self.assertEqual(got["after"]["marker"], "fresh")
        self.assertFalse(got["after"]["banner"])
        self.assertIn("Updated: fresh", got["after"]["meta"])
        self.assertEqual(got["selected"], [])

    def test_failed_scan_keeps_saved_totals_and_offers_rescan_without_a_spinner(self):
        got = self.drive(r"""
          const boot = bootDashboard(); await settle();
          scan.resolve(reply({error: 'scan failed'}, 500));
          await boot;
          console.log(JSON.stringify({state: state(), failed: snapshotRefreshFailed,
            dataRequests: calls.filter(c => c.includes('/api/data?'))}));
        """)
        self.assertEqual(got["state"]["marker"], "saved")
        self.assertIn("Update failed", got["state"]["text"])
        self.assertIn("Rescan", got["state"]["text"])
        self.assertTrue(got["failed"])
        self.assertFalse(got["state"]["dimmed"])
        self.assertFalse(got["state"]["overlay"])
        self.assertEqual(got["dataRequests"], [])

    def test_missing_or_invalid_snapshot_uses_normal_first_load(self):
        for body in ("{snapshot: null}", "{snapshot: {saved_at: 1, data: []}}"):
            with self.subTest(body=body):
                got = self.drive(r"""
                  const realFetch = apiFetch;
                  apiFetch = (path, options) => path === '/api/snapshot'
                    ? Promise.resolve(reply(BODY)) : realFetch(path, options);
                  const boot = bootDashboard(); await settle();
                  const during = state();
                  scan.resolve(reply({new: 1}));
                  calculation.resolve(reply(data('fresh')));
                  await boot;
                  console.log(JSON.stringify({during, after: state()}));
                """.replace("BODY", body))
                self.assertIsNone(got["during"]["marker"])
                self.assertTrue(got["during"]["overlay"])
                self.assertEqual(got["after"]["marker"], "fresh")

    def test_running_startup_scan_is_joined(self):
        got = self.drive(r"""
          let checks = 0;
          const realFetch = apiFetch;
          apiFetch = async (path, options) => {
            if (path === '/api/scan-status' && checks++ === 0)
              return reply({state: 'scanning', generation: 1});
            return realFetch(path, options);
          };
          calculation.resolve(reply(data('fresh')));
          await bootDashboard();
          console.log(JSON.stringify({calls, state: state()}));
        """)
        self.assertNotIn("POST /api/rescan", got["calls"])
        self.assertEqual(got["state"]["marker"], "fresh")

    def test_two_sources_still_require_a_choice_and_only_that_snapshot_is_loaded(self):
        got = self.drive(r"""
          sources.push({source: 'codex', turns: 6});
          const realFetch = apiFetch;
          apiFetch = (path, options) => path.includes('?source=codex')
            ? Promise.resolve(reply(path.startsWith('/api/snapshot')
                ? snapshot(data('saved codex', 'codex')) : data('fresh codex', 'codex')))
            : realFetch(path, options);
          const boot = bootDashboard(); await settle();
          const chooser = !element('source-chooser').hidden;
          const beforeChoice = rawData;
          const choice = chooseSource('codex'); await settle();
          const preview = state();
          scan.resolve(reply({new: 0}));
          await boot; await choice;
          console.log(JSON.stringify({chooser, beforeChoice, preview, after: state(), calls}));
        """)
        self.assertTrue(got["chooser"])
        self.assertIsNone(got["beforeChoice"])
        self.assertEqual(got["preview"]["marker"], "saved codex")
        self.assertEqual(got["preview"]["source"], "codex")
        self.assertEqual(got["after"]["marker"], "fresh codex")
        self.assertFalse(any("?source=claude" in call for call in got["calls"]))

    def test_late_snapshot_cannot_replace_fresh_data(self):
        got = self.drive(r"""
          const delayed = deferred();
          apiFetch = () => delayed.promise;
          const restore = restoreSnapshot('claude');
          publishData('claude', data('fresh'));
          delayed.resolve(reply(snapshot(data('saved'))));
          await restore;
          console.log(JSON.stringify(state()));
        """)
        self.assertEqual(got["marker"], "fresh")
        self.assertFalse(got["banner"])

    def test_switching_source_marks_old_figures_provisional_while_reading_the_snapshot(self):
        got = self.drive(r"""
          publishData('claude', data('saved claude'), savedAt);
          selectedSource = 'codex';
          hasStartupSnapshots = true;
          const delayed = deferred();
          apiFetch = path => path.startsWith('/api/snapshot') ? delayed.promise
            : Promise.resolve(reply(data('fresh codex', 'codex')));
          const switching = loadSource('codex');
          const during = state();
          delayed.resolve(reply(snapshot(data('saved codex', 'codex'))));
          await switching;
          console.log(JSON.stringify({during, after: state()}));
        """)
        self.assertEqual(got["during"]["marker"], "saved claude")
        self.assertTrue(got["during"]["dimmed"])
        self.assertTrue(got["during"]["overlay"])
        self.assertEqual(got["after"]["marker"], "fresh codex")

    def test_saved_quota_readings_do_not_fire_alerts(self):
        got = self.drive(r"""
          const alerts = [];
          checkQuotaAlerts = (info, source) => alerts.push(source);
          publishData('claude', data('saved'), savedAt);
          const fromSaved = [...alerts];
          publishData('claude', data('fresh'));
          console.log(JSON.stringify({fromSaved, alerts}));
        """)
        self.assertEqual(got["fromSaved"], [])
        self.assertEqual(got["alerts"], ["claude", "codex"])

    def test_late_other_source_snapshot_keeps_the_fresh_claude_quota(self):
        got = self.drive(r"""
          const live = data('fresh');
          live.subscription_limits.age_seconds = 10;
          publishData('claude', live);
          const old = data('saved codex', 'codex');
          old.subscription_limits.age_seconds = 999;
          publishData('codex', old, savedAt);
          console.log(JSON.stringify({age: lastClaudeLimits.age_seconds, state: state()}));
        """)
        self.assertEqual(got["age"], 10)
        self.assertEqual(got["state"]["marker"], "fresh")

    def test_explicit_source_link_never_previews_a_different_assistant(self):
        got = self.drive(r"""
          window.location.search = '?source=codex';
          const realFetch = apiFetch;
          apiFetch = (path, options) => path === '/api/snapshot?source=codex'
            ? Promise.resolve(reply({snapshot: null})) : realFetch(path, options);
          await restoreStartupSnapshot();
          console.log(JSON.stringify({state: state(), calls}));
        """)
        self.assertIsNone(got["state"]["marker"])
        self.assertFalse(any("?source=claude" in call for call in got["calls"]))

    def test_failed_calculation_keeps_preview_with_error_status(self):
        got = self.drive(r"""
          const boot = bootDashboard(); await settle();
          scan.resolve(reply({new: 0}));
          calculation.resolve(reply({error: 'Could not calculate'}, 500));
          await boot;
          console.log(JSON.stringify(state()));
        """)
        self.assertEqual(got["marker"], "saved")
        self.assertIn("Update failed", got["text"])
        self.assertFalse(got["overlay"])


@requires_browser
class TestSavedStartupInBrowser(unittest.TestCase):
    def test_banner_is_visible_and_content_interactive_until_fresh_paint(self):
        probe = r"""
        <pre id="startup-check" hidden></pre><script nonce="probe">
        let before;
        function checkStartup() {
          const status = document.getElementById('snapshot-status');
          const container = document.querySelector('.container');
          if (!before && rawData && rawData.generated_at === 'saved') {
            const box = status.getBoundingClientRect();
            before = {
              banner: getComputedStyle(status).display !== 'none',
              text: status.textContent, width: box.width, height: box.height,
              fits: box.left >= 0 && box.right <= innerWidth,
              opacity: getComputedStyle(container).opacity,
              interactive: getComputedStyle(container).pointerEvents,
              overlay: document.getElementById('load-overlay').hidden,
              stats: document.getElementById('stats-row').textContent,
              role: status.getAttribute('role'),
            };
            fetch('/test/release');
          }
          if (before && rawData && rawData.generated_at === 'fresh') {
            const docker = document.getElementById('docker-status');
            const dockerBox = docker.getBoundingClientRect();
            document.getElementById('startup-check').textContent = JSON.stringify({
              before, hiddenAfter: status.hidden,
              statsAfter: document.getElementById('stats-row').textContent,
              metaAfter: document.getElementById('meta').textContent,
              docker: {text: docker.textContent, hidden: docker.hidden,
                height: dockerBox.height,
                fits: dockerBox.left >= 0 && dockerBox.right <= innerWidth,
                role: docker.getAttribute('role')},
            });
          } else setTimeout(checkStartup, 25);
        }
        checkStartup();
        </script>
        """
        with tempfile.TemporaryDirectory() as tmp:
            old = json.loads(_browser_payload(Path(tmp) / "old.db"))
            fresh = json.loads(_browser_payload(Path(tmp) / "fresh.db", scale=2))
            old["generated_at"], fresh["generated_at"] = "saved", "fresh"
            sources = {"sources": [{"source": "claude", "turns": 15}]}
            page = (dashboard.HTML_TEMPLATE
                    .replace("__APP_CONFIG_JSON__", '{"version":"test","surface":"web"}')
                    .replace("__CSP_NONCE__", "probe")
                    .replace("</body>", probe + "</body>"))
            release = threading.Event()

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    path = self.path.split("?")[0]
                    if path == "/":
                        self.reply(page.encode("utf-8"), "text/html")
                    elif path == "/assets/chart.umd.js":
                        self.reply((REPO_ROOT / "vendor/chart.umd.js").read_bytes(),
                                   "application/javascript")
                    elif path == "/api/snapshot":
                        self.reply_json({"snapshot": {"saved_at": time.time() - 3600,
                            "data": old if "source=" in self.path else sources}})
                    elif path == "/api/scan-status":
                        self.reply_json({"state": "idle", "generation": 2,
                                         "docker": {"state": "partial", "containers": 1, "sources": 1}})
                    elif path == "/api/sources":
                        self.reply_json(sources)
                    elif path == "/api/data":
                        self.reply_json(fresh)
                    else:
                        if path == "/test/release":
                            release.set()
                        self.reply_json({})

                def do_POST(self):
                    release.wait(10)
                    self.reply_json({"new": 15})

                def reply_json(self, value):
                    self.reply(json.dumps(value).encode("utf-8"), "application/json")

                def reply(self, body, content_type):
                    self.send_response(200)
                    self.send_header("Content-Type", content_type + "; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = (f"http://127.0.0.1:{server.server_port}/?source=claude"
                       "&range=30d#token=" + "b" * 40)
                proc = subprocess.run([
                    str(BROWSER), "--headless", "--disable-gpu", "--no-sandbox",
                    "--user-data-dir=" + tmp + "/profile", "--window-size=390,900",
                    "--virtual-time-budget=15000", "--dump-dom", url,
                ], capture_output=True, text=True, encoding="utf-8", timeout=60)
            finally:
                release.set()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
            match = re.search(r'<pre id="startup-check"[^>]*>(.*?)</pre>', proc.stdout, re.S)
            self.assertIsNotNone(match)
            self.assertTrue(match.group(1).strip(), "startup did not reach both paints")
            result = json.loads(html.unescape(match.group(1)))
            before = result["before"]
            self.assertTrue(before["banner"])
            self.assertIn("Updating usage", before["text"])
            self.assertTrue(before["fits"])
            self.assertGreater(before["height"], 20)
            self.assertEqual(before["opacity"], "1")
            self.assertEqual(before["interactive"], "auto")
            self.assertTrue(before["overlay"])
            self.assertEqual(before["role"], "status")
            self.assertTrue(result["hiddenAfter"])
            self.assertNotEqual(before["stats"], result["statsAfter"])
            self.assertIn("Updated: fresh", result["metaAfter"])
            self.assertIn("could not be refreshed", result["docker"]["text"])
            self.assertFalse(result["docker"]["hidden"])
            self.assertGreater(result["docker"]["height"], 20)
            self.assertTrue(result["docker"]["fits"])
            self.assertEqual(result["docker"]["role"], "status")
