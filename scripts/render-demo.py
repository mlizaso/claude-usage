"""Render documentation screenshots from deterministic, invented usage only.

Usage: python3 scripts/render-demo.py --browser /path/to/chrome-headless-shell
The scanner is never called. Account, quota and threshold readers are replaced
before building the payload. A fresh temporary browser profile is used.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parent.parent


def build_payloads(directory):
    """Build a fixture database directly; never discover or parse transcripts."""
    for key in tuple(os.environ):
        if key.startswith('CODEX_CLAUDE_USAGE_'):
            del os.environ[key]
    os.environ['CODEX_CLAUDE_USAGE_DOCKER'] = '0'
    os.environ['CODEX_CLAUDE_USAGE_LIVE_LIMITS'] = '0'
    real_data = tuple(Path.home() / part for part in (
        '.claude', '.codex', 'Library/Developer/Xcode/CodingAssistant'))
    real_account = Path.home() / '.claude.json'

    def refuse_real_data(event, args):
        if event in {'open', 'os.listdir', 'os.scandir', 'sqlite3.connect'} and args:
            if isinstance(args[0], (str, bytes)):
                path = Path(args[0].decode() if isinstance(args[0], bytes) else args[0]).absolute()
                if path == real_account or any(path.is_relative_to(root) for root in real_data):
                    raise RuntimeError('The demo must never access real usage data')

    sys.addaudithook(refuse_real_data)
    sys.path.insert(0, str(ROOT))
    with patch('pathlib.Path.home', return_value=directory):
        from codex_claude_usage import dashboard, dashboard_data
        from codex_claude_usage.scanner import get_db, init_db, insert_turns, upsert_sessions

    database = directory / 'demo.db'
    connection = get_db(database)
    init_db(connection)
    models_by_source = {
        'claude': ('claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-4-5-20251001'),
        'codex': ('gpt-5.5', 'gpt-5.4', 'gpt-5.4-mini'),
    }
    projects = ('demo/website', 'demo/mobile-app', 'demo/library')
    for source, models in models_by_source.items():
        for day in range(30):
            for model_index, model in enumerate(models):
                stamp = (date(2026, 1, 1) + timedelta(days=day)).isoformat()
                stamp += f'T{9 + model_index * 3:02}:00:00Z'
                identity = f'demo-{source}-{day}-{model_index}'
                factor = 1 + (day * 7 + model_index * 3) % 11
                tokens = {'input_tokens': 20000 * factor,
                          'output_tokens': 3000 * factor,
                          'cache_read_tokens': 50000 * factor,
                          'cache_creation_tokens': 4000 * factor}
                if source == 'codex':
                    tokens = {'input_tokens': 12000 * factor,
                              'output_tokens': 5000 * factor,
                              'cache_read_tokens': 35000 * factor,
                              'cache_creation_tokens': 0,
                              'reasoning_output_tokens': 2000 * factor}
                upsert_sessions(connection, [{
                    'session_id': identity, 'source': source,
                    'project_name': projects[model_index], 'git_branch': 'demo',
                    'topic': 'Synthetic demonstration', 'model': model,
                    'first_timestamp': stamp, 'last_timestamp': stamp,
                    'total_input_tokens': tokens['input_tokens'],
                    'total_output_tokens': tokens['output_tokens'],
                    'total_cache_read': tokens['cache_read_tokens'],
                    'total_cache_creation': tokens['cache_creation_tokens'],
                    'turn_count': 1,
                }])
                insert_turns(connection, [{
                    'session_id': identity, 'message_id': identity,
                    'source': source, 'timestamp': stamp, 'model': model,
                    'reasoning_effort': ('medium', 'high', 'xhigh' if source == 'codex' else 'max')[model_index],
                    'stop_reason': 'end_turn' if source == 'claude' else '',
                    'tool_name': None, 'cwd': '/synthetic/demo', **tokens,
                }])
    connection.commit()
    connection.close()
    # No account cache, saved quotas, thresholds or live credentials are read.
    payloads = {}
    with patch.object(dashboard_data, 'claude_limits', return_value={}), \
            patch.object(dashboard_data, 'codex_limits_projection', return_value={}):
        for source in models_by_source:
            payload = dashboard_data.get_dashboard_data(database, source=source)
            payload['generated_at'] = '2026-01-30 18:00:00'
            payloads[source] = json.dumps(payload, default=str).encode('utf-8')
    sources = {'sources': dashboard_data.available_sources(database)}
    return dashboard.HTML_TEMPLATE, payloads, json.dumps(sources).encode('utf-8')


def render(browser, output):
    # Use a fixed display timezone, never the maintainer's system setting.
    os.environ['TZ'] = 'UTC'
    if hasattr(time, 'tzset'):
        time.tzset()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='codex-claude-usage-demo-') as name:
        temporary = Path(name)
        page, payloads, sources = build_payloads(temporary)
        config = json.dumps({'version': '1.7.0 demo', 'surface': 'web', 'rate_overrides': {}})
        page = page.replace('__APP_CONFIG_JSON__', config).replace('__CSP_NONCE__', 'demo')
        # The fixture contains no subscription or Docker data. Never enable scans.
        pages = {}
        for view in ('overview', 'charts', 'tables'):
            probe = """<script nonce="demo">
            localStorage.setItem('codex-claude-usage-theme', 'light');
            applyTheme('light');
            const demoSource = '__DEMO_SOURCE__';
            let attempts = 0;
            function settleDemo() {
              if (++attempts > 200) return;
              const sourceSwitch = document.getElementById('source-switch');
              if (!sourceSwitch || sourceSwitch.hidden ||
                  sourceSwitch.querySelectorAll('[data-source]').length !== 2 ||
                  !rawData || renderedSource !== selectedSource) {
                setTimeout(settleDemo, 25);
                return;
              }
              // Use the application's real source button for the Codex capture.
              if (selectedSource !== demoSource) {
                sourceSwitch.querySelector('[data-source="' + demoSource + '"]').click();
                setTimeout(settleDemo, 25);
                return;
              }
              if (!document.querySelector('#model-cost-body tr')) {
                setTimeout(settleDemo, 25);
                return;
              }
              document.title = 'Synthetic demonstration';
              const marker = document.createElement('div');
              marker.textContent = 'SYNTHETIC DEMONSTRATION · INVENTED DATA';
              marker.style.cssText = 'position:fixed;bottom:0;left:0;right:0;z-index:9999;background:#0f172a;color:#fff;text-align:center;padding:8px;font:12px sans-serif';
              document.body.appendChild(marker);
              document.documentElement.setAttribute('data-demo-source', demoSource);
              document.documentElement.setAttribute('data-demo-ready', 'true');
            }
            setTimeout(settleDemo, 100);
            </script>"""
            probe = probe.replace('__DEMO_SOURCE__', 'codex' if view == 'tables' else 'claude')
            # Focus the documentation on existing rendered cards without scroll
            # timing or modifying their values. The source UI stays unchanged.
            focus = 'footer {display:none!important}'
            if view == 'charts':
                focus += 'header,#jump-bar,#stats-row,.table-card,#sec-subagents {display:none!important}'
            if view == 'tables':
                focus += '#jump-bar,#stats-row,.charts-grid,.table-card:not(#sec-cost-model):not(#sec-cost-effort) {display:none!important}'
            focused = page.replace('</head>', '<style>' + focus + '</style></head>')
            pages['/' + view] = focused.replace('</body>', probe + '</body>').encode('utf-8')
        chart = (ROOT / 'vendor/chart.umd.js').read_bytes()
        requested = set()
        requested_sources = set()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                path = url.path
                requested.add(path)
                if path in pages:
                    body, kind = pages[path], 'text/html; charset=utf-8'
                elif path == '/assets/chart.umd.js':
                    body, kind = chart, 'application/javascript'
                elif path == '/icon.svg':
                    body, kind = (ROOT / 'web/icon.svg').read_bytes(), 'image/svg+xml'
                elif path == '/api/data':
                    source = parse_qs(url.query).get('source', ['claude'])[0]
                    if source not in payloads:
                        self.send_error(400)
                        return
                    requested_sources.add(source)
                    body, kind = payloads[source], 'application/json'
                elif path == '/api/sources':
                    body, kind = sources, 'application/json'
                elif path == '/api/scan-status':
                    body, kind = b'{"state":"idle","generation":0}', 'application/json'
                elif path == '/api/limits':
                    body, kind = b'{}', 'application/json'
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                # A bootstrap rescan acknowledges the fixture; it runs no code.
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for view, filename, height in (('overview', 'screenshot.png', 915),
                                           ('charts', 'usage1.png', 1080),
                                           ('tables', 'usage2.png', 890)):
                url = f'http://127.0.0.1:{server.server_port}/{view}?source=claude&range=all#token=' + 'd' * 40
                result = subprocess.run([
                    str(browser), '--headless', '--disable-gpu', '--no-sandbox',
                    '--disable-background-networking', '--no-first-run',
                    '--disable-sync', '--hide-scrollbars', '--force-device-scale-factor=1',
                    '--user-data-dir=' + str(temporary / ('profile-' + view)),
                    f'--window-size=1440,{height}', '--virtual-time-budget=15000',
                    '--screenshot=' + str(output / filename), '--dump-dom', url,
                ], capture_output=True, text=True, encoding='utf-8', timeout=60)
                if result.returncode or 'data-demo-ready="true"' not in result.stdout:
                    raise RuntimeError('The synthetic dashboard did not finish rendering')
                print(filename, hashlib.sha256((output / filename).read_bytes()).hexdigest())
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
        assert '/api/data' in requested
        assert requested_sources == set(payloads)
        print('Rendered 90 invented sessions per assistant; no transcripts or account caches were read.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'docs')
    args = parser.parse_args()
    render(args.browser.resolve(), args.output_dir.resolve())
