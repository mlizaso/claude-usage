// Copies the root CLI launcher, qualified Python package, browser surfaces,
// and pinned vendor files into vscode-extension/python/ for the .vsix. Each
// release embeds the exact runtime snapshot from the commit it was packaged
// at, so end users get a self-contained install — their only dependency is
// Python 3.11+ on PATH.
//
// Run from vscode-extension/. Invoked automatically by `vscode:prepublish`.

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

const repoRoot = path.resolve(__dirname, "..", "..");
const targetDir = path.resolve(__dirname, "..", "python");
const expectedChartSha256 = "ecc3cd1eeb8c34d2178e3f59fd63ec5a3d84358c11730af0b9958dc886d7652a";
const files = [
  // The extension keeps one root launcher because extension.ts invokes
  // python/cli.py. Runtime implementations remain package-qualified.
  "cli.py",
  "codex_claude_usage/__init__.py",
  "codex_claude_usage/_compat.py",
  "codex_claude_usage/account.py",
  "codex_claude_usage/assets.py",
  "codex_claude_usage/cli.py",
  "codex_claude_usage/codex_transcripts.py",
  "codex_claude_usage/dashboard.py",
  "codex_claude_usage/dashboard_cache.py",
  "codex_claude_usage/dashboard_data.py",
  "codex_claude_usage/db.py",
  "codex_claude_usage/docker_sources.py",
  "codex_claude_usage/limits_core.py",
  "codex_claude_usage/live_limits.py",
  "codex_claude_usage/limits_server.py",
  "codex_claude_usage/limits_web.py",
  "codex_claude_usage/loopback_http.py",
  "codex_claude_usage/localdays.py",
  "codex_claude_usage/pricing.py",
  "codex_claude_usage/reports.py",
  "codex_claude_usage/rollups.py",
  "codex_claude_usage/safefile.py",
  "codex_claude_usage/safejson.py",
  "codex_claude_usage/safetext.py",
  "codex_claude_usage/scanner.py",
  "codex_claude_usage/transcripts.py",
  "codex_claude_usage/timestamps.py",
  "web/index.html",
  "web/app.css",
  // Served by GET /icon.svg. vsce also carries resources/icon.svg into the
  // .vsix for the sidebar webview, so this bundle is the only surface where
  // the two copies both ship; the other four have web/ and nothing else.
  "web/icon.svg",
  "web/js/00-core.js",
  "web/js/10-pricing.js",
  "web/js/20-format.js",
  "web/js/30-ranges.js",
  "web/js/40-filters.js",
  "web/js/50-render.js",
  "web/js/52-charts.js",
  "web/js/54-tables.js",
  "web/js/56-plan.js",
  "web/js/58-alerts.js",
  "web/js/60-export.js",
  "web/js/70-bootstrap.js",
  "web/limits/index.html",
  "web/limits/app.css",
  "web/limits/app.js",
  "vendor/chart.umd.js",
  "vendor/LICENSE.chartjs.md",
];

const sources = new Map();
let invalid = false;
for (const file of files) {
  const src = path.join(repoRoot, file);
  if (!fs.existsSync(src) || !fs.lstatSync(src).isFile()) {
    console.error(`copy-python: ERROR - missing source ${src}`);
    invalid = true;
    continue;
  }
  sources.set(file, src);
  if (file === "vendor/chart.umd.js") {
    const actual = crypto.createHash("sha256").update(fs.readFileSync(src)).digest("hex");
    if (actual !== expectedChartSha256) {
      console.error(`copy-python: ERROR - unreviewed Chart.js sha256 ${actual}`);
      invalid = true;
    }
  }
}

if (invalid) {
  console.error("copy-python: aborting — source validation failed before touching build output.");
  process.exit(1);
}

// python/ is generated package output. Recreate it after all inputs validate so
// stale or injected modules cannot hitch a ride in the VSIX and shadow stdlib
// imports when the isolated runner prepends this directory to sys.path.
if (fs.existsSync(targetDir)) {
  const targetStat = fs.lstatSync(targetDir);
  if (targetStat.isSymbolicLink() || !targetStat.isDirectory()) {
    console.error(`copy-python: ERROR - refusing unsafe output path ${targetDir}`);
    process.exit(1);
  }
  fs.rmSync(targetDir, { recursive: true, force: true });
}
fs.mkdirSync(targetDir, { recursive: true, mode: 0o755 });

for (const file of files) {
  const src = sources.get(file);
  const dst = path.join(targetDir, file);
  fs.mkdirSync(path.dirname(dst), { recursive: true });
  fs.copyFileSync(src, dst, fs.constants.COPYFILE_EXCL);
  fs.chmodSync(dst, 0o644);
  console.log(`copy-python: ${file} -> python/${file}`);
}
