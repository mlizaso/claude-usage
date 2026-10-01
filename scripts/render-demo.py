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
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent.parent


def build_payload(directory):
    """Build a fixture database directly; never discover or parse transcripts."""
    for key in tuple(os.environ):
        if key.startswith('CLAUDE_USAGE_'):
            del os.environ[key]
    os.environ['CLAUDE_USAGE_DOCKER'] = '0'
    os.environ['CLAUDE_USAGE_LIVE_LIMITS'] = '0'
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
        from claude_usage import dashboard, dashboard_data
        from claude_usage.scanner import get_db, init_db, insert_turns, upsert_sessions

    database = directory / 'demo.db'
    connection = get_db(database)
    init_db(connection)
    models = ('claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-4-5-20251001')
    projects = ('demo/website', 'demo/mobile-app', 'demo/library')
    for day in range(30):
        for model_index, model in enumerate(models):
            stamp = (date(2026, 1, 1) + timedelta(days=day)).isoformat()
            stamp += f'T{9 + model_index * 3:02}:00:00Z'
            identity = f'demo-{day}-{model_index}'
            factor = 1 + (day * 7 + model_index * 3) % 11
            tokens = {'input_tokens': 20000 * factor,
                      'output_tokens': 3000 * factor,
                      'cache_read_tokens': 50000 * factor,
                      'cache_creation_tokens': 4000 * factor}
            upsert_sessions(connection, [{
                'session_id': identity, 'source': 'claude',
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
                'source': 'claude', 'timestamp': stamp, 'model': model,
                'reasoning_effort': ('medium', 'high', 'max')[model_index],
                'stop_reason': 'end_turn', 'tool_name': None,
                'cwd': '/synthetic/demo', **tokens,
            }])
    connection.commit()
    connection.close()
    # No account cache, saved quotas, thresholds or live credentials are read.
    with patch.object(dashboard_data, 'claude_limits', return_value={}), \
            patch.object(dashboard_data, 'codex_limits_projection', return_value={}):
        payload = dashboard_data.get_dashboard_data(database, source='claude')
    payload['generated_at'] = '2026-01-30 18:00:00'
    return dashboard.HTML_TEMPLATE, json.dumps(payload, default=str).encode('utf-8')


def render(browser, output):
    # Use a fixed display timezone, never the maintainer's system setting.
    os.environ['TZ'] = 'UTC'
    if hasattr(time, 'tzset'):
        time.tzset()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='claude-usage-demo-') as name:
        temporary = Path(name)
        page, payload = build_payload(temporary)
        config = json.dumps({'version': '1.7.0 demo', 'surface': 'web', 'rate_overrides': {}})
        page = page.replace('__APP_CONFIG_JSON__', config).replace('__CSP_NONCE__', 'demo')
        # The fixture contains no subscription or Docker data. Never enable scans.
        pages = {}
        for view in ('overview', 'charts', 'tables'):
            probe = """<script nonce="demo">
            localStorage.setItem('claude-usage-theme', 'light');
            applyTheme('light');
            let attempts = 0;
            function settleDemo() {
              if (!document.querySelector('#model-cost-body tr')) {
                if (++attempts < 200) setTimeout(settleDemo, 25);
                return;
              }
              document.title = 'Synthetic demonstration';
              const marker = document.createElement('div');
              marker.textContent = 'SYNTHETIC DEMONSTRATION · INVENTED DATA';
              marker.style.cssText = 'position:fixed;bottom:0;left:0;right:0;z-index:9999;background:#0f172a;color:#fff;text-align:center;padding:8px;font:12px sans-serif';
              document.body.appendChild(marker);
              document.documentElement.setAttribute('data-demo-ready', 'true');
            }
            setTimeout(settleDemo, 100);
            </script>"""
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

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                requested.add(path)
                if path in pages:
                    body, kind = pages[path], 'text/html; charset=utf-8'
                elif path == '/assets/chart.umd.js':
                    body, kind = chart, 'application/javascript'
                elif path == '/icon.svg':
                    body, kind = (ROOT / 'web/icon.svg').read_bytes(), 'image/svg+xml'
                elif path == '/api/data':
                    body, kind = payload, 'application/json'
                elif path == '/api/sources':
                    body, kind = b'[{"source":"claude","turns":90}]', 'application/json'
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
        print('Rendered 90 invented sessions; no transcripts or account caches were read.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'docs')
    args = parser.parse_args()
    render(args.browser.resolve(), args.output_dir.resolve())
