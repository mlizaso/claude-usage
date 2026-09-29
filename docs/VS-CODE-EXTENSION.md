# Claude Code and Codex Usage — VS Code extension

**See local Claude Code and Codex usage — tokens, costs, sessions, and projects — inside VS Code.**

The extension runs as a local UI extension, scans the same local Claude Code and
Codex JSONL roots as the bundled Python tool, and renders that dashboard inside
a VS Code sidebar. It makes no external API calls and sends no telemetry; its
sanitized child environment deliberately excludes the optional live-limit
credentials supported by the standalone CLI. All data stays on your machine,
including when the editor is connected to a Remote/SSH workspace.

Claude Code usage works on **API, Pro, and Max plans** and includes the CLI,
the official VS Code extension, Xcode's coding assistant where present, and
dispatched Code sessions. Codex rollouts under `~/.codex/sessions` appear as a
separate source.

Local Docker containers are also checked automatically during scans, including
stopped containers. With Docker Engine 29.5.1+ available through a local context,
the extension imports Claude/Codex logs without changes to container projects.
The dashboard reports Docker progress and partial refreshes; private transcript
copies are retained beside the usage database. Remote contexts are not contacted,
and containers deleted before the first scan cannot be recovered. To disable
collection, launch VS Code with `CLAUDE_USAGE_DOCKER=0`.

---

## What it shows

- **Daily token usage** and **average hourly distribution** charts (with peak-hour shading)
- **Cost by model, project, and project + branch** tables, plus **Recent Sessions** — sortable, paged, and CSV-exportable
- **Subagent attribution** — a Subagent Tokens by Type chart and a Top Subagent Dispatches table that break dispatched Task/Agent usage out from your main sessions
- A **Claude Code / Codex source chooser** when both transcript roots contain
  data, with one assistant shown at a time
- **Plan-limit windows and per-window alert thresholds** from the local quota
  cache; the extension does not enable the standalone live-limit network mode
- **Model multi-select** and a **date-range** dropdown to scope everything at once
- A sticky **section nav** for jumping between sections, and **collapsible** chart/table cards that remember what you've folded away across reloads

Cost estimates use the published Anthropic or OpenAI API list rate for the
selected source. Subscription-plan charges differ, and unpriced models remain
`n/a` rather than being shown as free.

---

## Install

Install the `.vsix` from this repository's releases or build it from source.
The extension ID is `mlizaso.claude-usage`. Other publishers' builds can
have different features and behavior.

### From a prebuilt `.vsix` (no build step)

[GitHub Releases](https://github.com/mlizaso/claude-usage/releases/latest) attach a ready-built `.vsix`. Download it, then either drag it onto the VS Code **Extensions** view, or run:

```
code --install-extension claude-usage-<version>.vsix
```

#### Upgrading from v1.6.1 or earlier — uninstall the old extension first

v1.7.0 renamed the extension, so its id moved from `mlizaso.claude-usage-private`
to `mlizaso.claude-usage`. `--install-extension --force` overwrites the *same*
id; it does not remove a different one. Every contribution id stayed
byte-identical across the rename — the same four `claudeUsage.*` commands, the
same `claudeUsageSidebar` activity-bar container, the same
`claudeUsage.dashboard` view and the same three `claudeUsage.*` settings — so
installing the new `.vsix` over an old one leaves **both** extensions installed
and enabled, each declaring all of that. Run this once before installing:

```
code --uninstall-extension mlizaso.claude-usage-private
```

It reports that the extension is not installed if you never had the old build,
which is safe to ignore. The install scripts below do this for you.

### Build and install from source

Clone the repo and run the install script for your platform. Each script **builds** the `.vsix` (`npm ci --ignore-scripts --omit=optional` + the locally pinned `vsce`) and then installs it via `code --install-extension` — you don't need an existing `.vsix`, the script produces one.

**macOS / Linux / WSL** (bash):

```bash
git clone https://github.com/mlizaso/claude-usage.git
cd claude-usage/vscode-extension
./scripts/install.sh
```

**Windows** — run the script *in PowerShell*. Invoking `.\scripts\install.ps1` from Git Bash (or double-clicking it) just opens the file in an editor, because Windows maps `.ps1` to "Edit", not "Run". The line below runs it regardless of which shell you're in or your execution-policy setting:

```powershell
git clone https://github.com/mlizaso/claude-usage.git
cd claude-usage/vscode-extension
powershell -ExecutionPolicy RemoteSigned -File scripts\install.ps1
```

---

## Requirements

- **Python 3.11 or newer on your `PATH`.** If needed, install a supported release from [python.org/downloads](https://www.python.org/downloads/). On Windows make sure to check **"Add Python to PATH"** during the installer.

That's the only dependency. The extension bundles its `python/cli.py` launcher,
the complete `python/claude_usage/` implementation package, both local web
surfaces, and the pinned Chart.js asset — no separate clone or Homebrew install
is needed.

---

## Usage

1. Click the **gauge icon** in the activity bar (left sidebar of VS Code).
2. The extension starts the dashboard server on a free local port and embeds it in a sidebar webview.
3. Filter by model, range, or project — same UI as the standalone web dashboard.

### Commands

Open the Command Palette (`Ctrl+Shift+P` / `Cmd+Shift+P`):

| Command | What it does |
|---|---|
| **Claude Usage: Open Dashboard** | Reveal the sidebar and start the server (also fires automatically when you click the activity-bar icon) |
| **Claude Usage: Rescan Transcripts** | Revalidate the owned child and its authenticated health proof, then send a one-shot HMAC-authorized request to the extension-owned dashboard server to ingest new or changed transcripts; the reusable browser bearer is never sent by this host-driven command. Reload the panel after the scan succeeds. A matching-schema scan is incremental. After an upgrade changes the declared schema, the endpoint rebuilds the derived database and refills it from every configured/default transcript root; on a large history that can take minutes. Failures (including an already-running scan) are reported without reloading stale data. |
| **Claude Usage: Restart Server** | Kill and respawn the Python process (use after changing settings) |
| **Claude Usage: Show Logs** | Open the extension's output channel — useful when something doesn't work |

### Settings

| Setting | Default | Description |
|---|---|---|
| `claudeUsage.pythonPath` | _(auto-discover)_ | Absolute path to a Python 3.11+ interpreter. Leave empty to search absolute `PATH` entries — see [which interpreter gets picked](#which-interpreter-gets-picked). |
| `claudeUsage.cliPath` | _(bundled)_ | Absolute path to an explicitly trusted custom Python launcher file, or to a directory containing `cli.py`. Empty = use the bundled copy that ships with the extension. |
| `claudeUsage.port` | `0` | Port for the local dashboard server. `0` = OS picks a free one. |

---

## How discovery works

When you click the icon, the extension resolves how to run the dashboard in this order:

1. **`claudeUsage.cliPath` setting** if you've set one
2. **The bundled `python/cli.py`** that ships inside this `.vsix` (most installs hit this)

For security, the extension never auto-executes `cli.py` from an open workspace or a `claude-usage` command from `PATH`. Source-development sessions should set `claudeUsage.cliPath` explicitly.

If `claudeUsage.cliPath` is set but is not an absolute path to an existing file (or to a directory containing `cli.py`), the extension **warns and runs its bundled copy instead** — it does not stop. A file's basename need not be `cli.py`: this is an explicit trust setting and supports renamed launchers. That is deliberately unlike `claudeUsage.pythonPath`, which refuses rather than substituting: the fallback here is the copy shipped inside the `.vsix`, whereas the interpreter fallback would be whatever is on your `PATH`. The warning names both paths, because a stale setting otherwise looks exactly like your checkout running while the bundled copy is what actually starts.

If none of those find anything, you'll get a friendly message in the sidebar — most often "Python 3.11+ is required" with a platform-specific install hint.

### Which interpreter gets picked

Set `claudeUsage.pythonPath` and that interpreter is the only one considered: if the path is not an absolute path to a runnable Python 3.11+ interpreter, discovery stops there rather than quietly running a different Python than the one you named. Clear the setting to search again.

Left empty, the extension walks the absolute entries of your `PATH` itself and skips candidates older than Python 3.11. The search is **by name, not by directory**: the first candidate name is looked for across your whole `PATH` before the second name is tried, so a supported match late on `PATH` still beats a different name sitting first.

| Platform | Candidate names, in order |
|---|---|
| macOS / Linux | `python3`, then `python` |
| Windows | `python.exe`, then `python3.exe`, then a literal `python` |

Windows leads with `python.exe` on purpose. The python.org installer this extension recommends creates `python.exe` and no `python3.exe`, while a `python3.exe` on the default Windows `PATH` is usually the Microsoft Store's App Execution Alias — a stub that opens the Store instead of running anything. The third Windows candidate matches only a file literally named `python` with no extension (an MSYS2, Cygwin or Git-Bash shim); there is no `PATHEXT` expansion.

Relative `PATH` entries (`.` above all) are skipped, so the interpreter can never be chosen by the folder you happened to open.

---

## Privacy

The extension:
- Reads local JSONL transcripts from `~/.claude/projects/`,
  `~/.codex/sessions/`, and the Xcode coding-assistant directory on macOS when
  those roots exist
- Automatically collects Claude Code and Codex transcripts from local Docker
  containers when supported; set `CLAUDE_USAGE_DOCKER=0` before starting VS Code
  to disable collection
- Runs a small HTTP server bound to `127.0.0.1` (localhost-only — never `0.0.0.0`) on the configured port, or an OS-selected free port when `claudeUsage.port` is `0`
- Embeds that server's dashboard in a VS Code webview
- Loads its pinned Chart.js asset locally and authenticates browser dashboard API calls with a random per-process API bearer; the host-driven rescan uses a separate one-shot HMAC proof instead of putting that reusable bearer on its POST
- Requires the exact child-owned post-bind `Dashboard listening at …` stdout
  record and then runs `/healthz?challenge=…`. A child without a readable stdout
  stream is terminated immediately and is never probed: a network response cannot
  substitute for the child-owned ownership record. The child receives a separate
  random `CLAUDE_USAGE_HEALTH_TOKEN`; the manager verifies the response's HMAC
  `instance` proof, so a raced localhost listener cannot impersonate the child it
  launched. Before a rescan it checks the child again, obtains a fresh health
  proof, and sends a domain-separated, consumed HMAC rescan proof; the raw
  health secret is never sent, and it derives no reusable browser bearer or
  general API authority. The API bearer is not sent by the host-driven command.
  The manager gives custom and default probes an
  absolute startup deadline, and the default HTTP request also destroys itself at
  its own hard deadline, so a peer cannot renew readiness indefinitely by
  dripping response bytes
- Runs as a local UI extension even for Remote/SSH workspaces; it does not scan transcripts on the remote host

The extension sends no telemetry and does not enable live Anthropic quota
requests. It can read the local Docker daemon to collect container logs.
Those imported files contain full transcripts and remain in a private cache;
see [Privacy](https://github.com/mlizaso/claude-usage/blob/main/docs/PRIVACY.md).

The standalone CLI's unauthenticated `/healthz` port check sends no challenge,
so an extension-owned port returns 404 and remains opaque to that diagnostic; it
is not offered a direct-kill command. Readiness succeeds only after both the
exact stdout record and the matching HMAC health response complete within those
hard total deadlines.

---

## Troubleshooting

- **"Python 3.11 or newer required"** — install from [python.org](https://www.python.org/downloads/) and reload VS Code (`Ctrl+Shift+P` → `Developer: Reload Window`). On Windows make sure "Add Python to PATH" is checked in the installer.
- **…but Python *is* installed and on your `PATH`** — check `claudeUsage.pythonPath`. A non-empty setting is the only interpreter considered, so if the path you named has moved, been upgraded away, or lost its execute bit, nothing on your `PATH` is looked at. The panel says so explicitly in that case, naming the setting and the path it points at, so the wording above about needing Python on your `PATH` now only ever means the setting is empty. Clear the setting to fall back to auto-discovery, or point it at the new location, then reload the window.
- **Sidebar stays blank or shows "starting…"** — run `Claude Usage: Show Logs`. The extension logs the resolved Python path, the install mode, the spawn command, and any stdout/stderr from the server.
- **Dashboard renders but shows "No usage recorded"** — neither Claude Code nor
  Codex has written a supported local transcript yet. Run a session in the
  source you expect, then rescan.

---

## Source

The hardened Python tool, extension, and head-only Homebrew formula live in the [`mlizaso/claude-usage`](https://github.com/mlizaso/claude-usage) fork.

Licensed under MIT. Original work by Pawel Huryn and contributors.
The project and bundled Chart.js license notices ship in the extension.
