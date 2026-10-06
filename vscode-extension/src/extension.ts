import * as vscode from "vscode";
import { randomBytes } from "node:crypto";
import * as path from "node:path";
import { locatePython, PythonFailure } from "./python-locator";
import { resolveInstallMode, dashboardSpawnArgs, InstallMode, InstallFailure } from "./install-mode";
import { resolveStablePort } from "./port-allocator";
import { ServerManager, OutputSink } from "./server-manager";
import { DashboardSidebar } from "./sidebar";
import { trustedUserSetting } from "./trusted-config";

/**
 * workspaceState key holding the last port the dashboard bound to. Reused on the
 * next launch so the iframe origin (and thus its localStorage: collapsed-section
 * state) survives window reloads. Per-workspace so two
 * windows don't fight over one port.
 */
const LAST_PORT_KEY = "codexClaudeUsage.lastPort";

/**
 * Lifecycle owner for the extension. Held as a module-level singleton so
 * deactivate() can find it.
 */
class Extension {
  private context: vscode.ExtensionContext;
  private output: vscode.OutputChannel;
  private sidebar: DashboardSidebar;
  private server: ServerManager | undefined;
  /**
   * In-flight startup. Subsequent openDashboard() calls await this one
   * instead of spawning a second ServerManager. Cleared on resolve/reject.
   * Prevents the double-click orphaned-process race Codex flagged.
   */
  private startupInFlight: Promise<void> | undefined;
  /** Coalesces command-palette double clicks onto one server-side scan. */
  private rescanInFlight: Promise<void> | undefined;
  /** Permanent lifecycle fence: no awaited startup may outlive deactivation. */
  private disposed = false;

  constructor(context: vscode.ExtensionContext) {
    this.context = context;
    this.output = vscode.window.createOutputChannel("Codex / Claude Usage");
    // The sidebar invokes onShow when VS Code reveals the webview — that's
    // when the user clicked the activity-bar icon, so it's the right moment
    // to spawn the server. openDashboard() coalesces repeat calls.
    //
    // The rejection is caught rather than voided. This callback returns void,
    // so anything openDashboard() rejects with would reach nothing but the
    // extension host's unhandled-rejection log — and the one throw doStartup's
    // own catch cannot see is openDashboard's `await executeCommand(…)`, which
    // runs before it. `this.sidebar` resolves at call time, not here.
    this.sidebar = new DashboardSidebar(() => {
      this.openDashboard().catch((err) => {
        const msg = startupFailureMessage(err);
        this.output.appendLine(msg);
        this.sidebar.setError(msg);
      });
    }, context.extensionUri);

    context.subscriptions.push(
      this.output,
      vscode.window.registerWebviewViewProvider(DashboardSidebar.viewId, this.sidebar),
      vscode.commands.registerCommand("codexClaudeUsage.open", () => this.openDashboard()),
      vscode.commands.registerCommand("codexClaudeUsage.rescan", () => this.rescan()),
      vscode.commands.registerCommand("codexClaudeUsage.restart", () => this.restart()),
      vscode.commands.registerCommand("codexClaudeUsage.showLogs", () => this.output.show()),
    );
  }

  /**
   * Start (or focus) the dashboard. If the server isn't running yet, this
   * resolves Python + install mode + port, spawns the server, then points
   * the sidebar at it.
   */
  async openDashboard(): Promise<void> {
    if (this.disposed) return;
    await vscode.commands.executeCommand("workbench.view.extension.codexClaudeUsageSidebar");
    if (this.disposed) return;

    if (this.server && this.server.status === "ready") {
      this.sidebar.refresh();
      return;
    }
    // Coalesce concurrent calls onto a single in-flight startup so we don't
    // spawn two Python processes and overwrite this.server.
    if (this.startupInFlight) {
      return this.startupInFlight;
    }
    this.startupInFlight = this.doStartup().finally(() => {
      this.startupInFlight = undefined;
    });
    return this.startupInFlight;
  }

  private async doStartup(): Promise<void> {
    // The try covers the whole body, not just manager.start(). Everything
    // ahead of the spawn can throw too — resolveStablePort rejects on an
    // EMFILE burst (pickFreePort wires server.once("error", reject)), and
    // resolveInstallMode / locatePython throw ERR_INVALID_ARG_TYPE on a
    // non-string machine-scoped setting — and those used to escape into a
    // voided callback, leaving the panel on "not running yet", the output
    // channel empty and no Retry button.
    //
    // `manager` is hoisted out of the try so the catch can still tell "never
    // constructed" from "constructed, then failed".
    let manager: ServerManager | undefined;
    try {
      const config = vscode.workspace.getConfiguration("codexClaudeUsage");
      const configuredPython = trustedUserSetting(config, "pythonPath", "");
      const configuredCli = trustedUserSetting(config, "cliPath", "");
      // Hardcoded to localhost. We previously exposed a `host` setting but
      // 0.0.0.0 would have made the user's usage data visible on the LAN.
      // The Python dashboard also rejects non-loopback host configuration; the
      // extension keeps the invariant explicit at its own process boundary.
      const host = "127.0.0.1";
      const configuredPort = trustedUserSetting(config, "port", 0);

      const extensionDir = this.context.extensionUri.fsPath;
      // Bundled python sources live at <extensionDir>/python/cli.py — copied
      // there from the repo root by scripts/copy-python.js at package time.
      const bundledCliPath = path.join(extensionDir, "python", "cli.py");
      const mode = resolveInstallMode({
        configuredCliPath: configuredCli,
        bundledCliPath,
      });
      // The three early exits below stay `return`s inside the try: each already
      // reports a message tailored to its own cause, which the generic catch
      // could only replace with a worse one. The failure is read off `mode`
      // rather than re-derived from `configuredCli` here, for the same reason
      // `locatePython`'s is: the resolver owns the rule, so a change to it has
      // to restate which failure it produces, and the message follows.
      if (mode.kind === "none") {
        const msg = noInstallMessage(mode);
        this.output.appendLine(msg);
        this.sidebar.setStatus(msg);
        vscode.window.showErrorMessage(msg);
        return;
      }

      // Not an early exit: resolveInstallMode deliberately falls back to the
      // bundled cli.py rather than failing closed on a broken cliPath (see its
      // doc comment). What it must not do is fall back in SILENCE — the bundled
      // copy is the one this extension shipped with, so a developer pointing
      // the setting at their own checkout would watch their edits do nothing
      // and have no way to tell, the resolved path in the log line below being
      // the only trace. A warning, not an error: the dashboard does start.
      if (mode.ignoredConfiguredPath) {
        const warning = ignoredCliPathMessage(mode.ignoredConfiguredPath, mode.cliPy);
        this.output.appendLine(warning);
        void vscode.window.showWarningMessage(
          ignoredCliPathDialogMessage(mode.ignoredConfiguredPath),
        );
      }

      // `python` stays undefined outside clone mode — dashboardSpawnArgs only
      // consults it there. The locator's answer is destructured rather than
      // re-derived: it names WHICH failure occurred, and both strings below
      // branch on that. Deciding it here from `configuredPython` instead would
      // put a second copy of the exclusivity rule in the caller.
      let python: string | undefined;
      if (mode.kind === "clone") {
        const located = await locatePython(configuredPython);
        if (this.disposed) return;
        if (located.kind !== "found") {
          const msg = noPythonMessage(located);
          this.output.appendLine(msg);
          this.sidebar.setStatus(msg);
          vscode.window.showErrorMessage(noPythonDialogMessage(located));
          return;
        }
        python = located.path;
      }

      // Reuse the last port when it's still free so the embedded dashboard's
      // localStorage (which is keyed by the iframe's http://host:port origin)
      // persists across window reloads instead of resetting every launch.
      const savedPort = this.context.workspaceState.get<number>(LAST_PORT_KEY);
      const port = await resolveStablePort(configuredPort, savedPort, host);
      if (this.disposed) return;
      void this.context.workspaceState.update(LAST_PORT_KEY, port);
      const baseUrl = `http://${host}:${port}/`;
      // Probe a dashboard-specific endpoint so we don't get fooled by some
      // other localhost service listening on the same port.
      const probeUrl = `http://${host}:${port}/healthz`;
      // The manager requires the child-owned post-bind stdout record in addition
      // to the bounded health probe, so a raced listener cannot impersonate the
      // process we just launched. Keep API authority and the readiness-only
      // HMAC secret separate; neither secret is sent to the unverified peer.
      const apiToken = randomBytes(32).toString("base64url");
      const healthToken = randomBytes(32).toString("base64url");
      // Fragments never cross the HTTP boundary. The dashboard reads this token
      // in-browser and attaches it only to same-origin API requests.
      const url = `${baseUrl}#token=${apiToken}`;
      // --no-browser: the dashboard is embedded in the webview, so the bundled
      // cli.py must not also pop a system browser (it does by default for CLI users).
      // --surface vscode: tells the dashboard it's embedded so its footer shows the
      // version only — no "get the extension" promo (we're already in it).
      const spawnArgs = dashboardSpawnArgs(mode, python, ["--no-browser", "--host", host, "--port", String(port), "--surface", "vscode"]);
      if (!spawnArgs) {
        const msg = "Could not assemble a valid command to spawn the dashboard.";
        this.output.appendLine(msg);
        this.sidebar.setStatus(msg);
        return;
      }

      this.sidebar.setStatus(`Starting dashboard at ${baseUrl}…`);
      this.output.appendLine(`[ext] install mode: ${describeMode(mode)}`);
      // Capture the manager into a local so the catch block can't dispose
      // a *different* manager that was created by a concurrent call.
      manager = new ServerManager({
        command: spawnArgs.command,
        args: spawnArgs.args,
        url: probeUrl,
        output: this.toSink(),
        apiToken,
        healthToken,
        // Give the isolated Python process and local health endpoint time to
        // initialize before giving up.
        readinessTimeoutMs: 20_000,
      });
      this.server = manager;
      await manager.start();
      if (this.disposed) {
        if (this.server === manager) {
          manager.dispose();
          this.server = undefined;
        }
        return;
      }
      this.sidebar.setUrl(url);
    } catch (err) {
      if (this.disposed) {
        if (manager && this.server === manager) {
          manager.dispose();
          this.server = undefined;
        }
        return;
      }
      const msg = startupFailureMessage(err);
      this.output.appendLine(msg);
      this.sidebar.setError(msg);
      // Optional-chained: a throw from ahead of the spawn leaves `manager`
      // unset, and a catch that throws would recreate the silent failure it
      // exists to replace.
      manager?.dispose();
      if (manager && this.server === manager) this.server = undefined;
      // Offer a one-click retry (and log access) rather than making the user
      // hunt for the command palette. The sidebar also shows a Retry button.
      void vscode.window.showErrorMessage(msg, "Retry", "Show Logs").then((choice) => {
        if (choice === "Retry") void this.openDashboard();
        else if (choice === "Show Logs") this.output.show();
      });
    }
  }

  /** Trigger an authenticated server-side scan, then reload the iframe. */
  async rescan(): Promise<void> {
    if (this.rescanInFlight) return this.rescanInFlight;
    this.rescanInFlight = this.doRescan().finally(() => {
      this.rescanInFlight = undefined;
    });
    return this.rescanInFlight;
  }

  private async doRescan(): Promise<void> {
    if (!this.server || this.server.status !== "ready") {
      await this.openDashboard();
    }
    const manager = this.server;
    if (!manager || manager.status !== "ready") return;

    try {
      this.output.appendLine("[ext] rescanning transcripts…");
      await manager.rescan();
      this.output.appendLine("[ext] rescan complete");
      this.sidebar.refresh();
    } catch (err) {
      const msg = `Rescan failed: ${String((err as Error)?.message ?? err)}`;
      this.output.appendLine(msg);
      void vscode.window.showErrorMessage(msg, "Show Logs").then((choice) => {
        if (choice === "Show Logs") this.output.show();
      });
    }
  }

  async restart(): Promise<void> {
    // If a startup is in flight, wait for it to settle so we don't dispose a
    // manager mid-spawn and leave an orphaned Python process.
    if (this.startupInFlight) {
      try { await this.startupInFlight; } catch { /* ignored — about to restart */ }
    }
    if (this.server) {
      this.server.dispose();
      this.server = undefined;
    }
    this.sidebar.setUrl(null);
    await this.openDashboard();
  }

  dispose(): void {
    this.disposed = true;
    if (this.server) {
      this.server.dispose();
      this.server = undefined;
    }
  }

  private toSink(): OutputSink {
    return { appendLine: (line) => this.output.appendLine(line) };
  }
}

function describeMode(mode: InstallMode): string {
  if (mode.kind === "clone") return `clone (${mode.cliPy})`;
  return "none";
}

/**
 * The one wording the panel, the output channel and the error dialog share.
 * Reads the message defensively: the widened try now also covers config
 * resolution, where a non-Error throw is possible.
 */
function startupFailureMessage(err: unknown): string {
  return `Failed to start dashboard: ${String((err as Error)?.message ?? err)}`;
}

/**
 * Friendly "no install found" message. With the bundled Python sources this
 * should be virtually unreachable in a packaged extension — only fires when
 * both the bundled source and any explicitly configured path are missing.
 *
 * TWO causes, two texts, for the same reason `noPythonMessage` has two. The
 * single unbranched string this replaces was printed for both and could not
 * name the rejected path, so a user whose `codexClaudeUsage.cliPath` was the cause
 * was not told which path had been refused, and a user who had never set it was
 * sent to inspect an empty setting.
 *
 * Worse, its one piece of advice — "clear it to fall back to the bundled
 * sources" — is impossible in EVERY case it was shown: `resolveInstallMode`
 * returns `none` only when the bundled copy did not resolve, so there is by
 * construction nothing to fall back to. Neither arm below repeats it.
 *
 * Unlike the Python pair there is no separate dialog wording to keep in step:
 * the call site passes this same string to the panel, the output channel and
 * `showErrorMessage`.
 */
export function noInstallMessage(reason: InstallFailure): string {
  if (reason.cause === "configured-unusable") {
    return [
      "Codex / Claude Usage cannot use the launcher named by your codexClaudeUsage.cliPath setting:",
      "",
      `    ${reason.configuredPath}`,
      "",
      "That setting must be an absolute path to an existing file, or to a directory that contains cli.py.",
      "The copy bundled in this extension is missing too, so there is nothing left to run: correct the",
      "setting, or clear it and reinstall the extension to restore the bundled copy.",
      "",
      "Use Codex / Claude Usage: Show Logs to see what was tried.",
    ].join("\n");
  }
  // Nothing to blame the user for: codexClaudeUsage.cliPath is empty (that is what
  // this arm MEANS), so the bundled python/cli.py that ships inside the .vsix
  // is simply not there.
  return [
    "Could not find the Codex / Claude Usage sources bundled in this private extension.",
    "Your codexClaudeUsage.cliPath setting is empty, so this is not a settings problem —",
    "the bundled python/cli.py is missing. Reinstalling the extension restores it.",
    "",
    "Use Codex / Claude Usage: Show Logs to see what was tried.",
  ].join("\n");
}

/**
 * Panel + output-channel wording for the case that produces no error at all:
 * `codexClaudeUsage.cliPath` was rejected, a bundled copy existed, and the dashboard
 * is starting on that instead.
 *
 * It names both paths on purpose. The failure this exists to end is a user
 * concluding their configured checkout is running when it is not, and only the
 * pair of paths side by side makes that visible.
 */
export function ignoredCliPathMessage(configuredPath: string, cliPy: string): string {
  return [
    "Codex / Claude Usage ignored your codexClaudeUsage.cliPath setting — it is not an absolute path to an existing file, or to a directory that contains cli.py:",
    "",
    `    ${configuredPath}`,
    "",
    `Starting the copy bundled in this extension instead: ${cliPy}`,
    "That copy is the one this extension shipped with, NOT your checkout — edits you make there will have no effect until the setting is corrected or cleared.",
  ].join("\n");
}

/**
 * The one-line version of `ignoredCliPathMessage` for the warning toast, which
 * gets a single line of the user's attention and no scrollback. Kept beside its
 * long form for the same reason `noPythonDialogMessage` is: the two are shown at
 * the same moment and must not describe different causes.
 */
export function ignoredCliPathDialogMessage(configuredPath: string): string {
  return `Codex / Claude Usage ignored codexClaudeUsage.cliPath (${configuredPath}) and started its own bundled cli.py instead. Run Codex / Claude Usage: Show Logs for details.`;
}

/**
 * Friendly "no Python" message for the panel and the output channel.
 *
 * TWO causes, two texts. The locator fails closed on a non-empty
 * `codexClaudeUsage.pythonPath` — it never falls back to PATH — so for
 * `configured-unusable` the PATH was not searched at all and every sentence
 * about it is false. This function used to have only the `not-on-path` arm and
 * was printed for both, so a user with a stale setting and a working `python3`
 * on their PATH was told, three times over, to put Python on their PATH.
 *
 * Whatever branches here must branch in `noPythonDialogMessage` too: the two
 * are shown at the same moment, and disagreeing about the cause is worse than
 * either being vague.
 */
export function noPythonMessage(
  reason: PythonFailure,
  platform: NodeJS.Platform = process.platform,
): string {
  if (reason.kind === "configured-unusable") {
    return [
      "Codex / Claude Usage cannot use the Python interpreter named by your codexClaudeUsage.pythonPath setting:",
      "",
      `    ${reason.configuredPath}`,
      "",
      "That setting must be an absolute path to an existing, runnable Python 3.11+ interpreter — not a directory, and not a relative path.",
      "While it is set, your PATH is NOT searched: clear the setting to go back to auto-discovery, or correct it to point at a real interpreter.",
      "",
      "After changing it, reload this VS Code window (Cmd/Ctrl+Shift+P → Developer: Reload Window).",
    ].join("\n");
  }
  // No setting to blame: this is the fresh-install case, most likely on
  // Windows. Point the user at python.org with concrete next steps.
  const installHint =
    platform === "win32"
      ? "Install Python 3.11+ from https://www.python.org/downloads/windows/ (make sure to check 'Add Python to PATH' during install)."
      : platform === "darwin"
      ? "Install Python 3.11+ with: brew install python  (or from https://www.python.org/downloads/macos/)."
      : "Install Python 3.11+ via your distro's package manager (e.g. apt install python3).";
  return [
    "Codex / Claude Usage needs Python 3.11 or newer on your PATH.",
    "",
    installHint,
    "",
    "After installing, reload this VS Code window (Cmd/Ctrl+Shift+P → Developer: Reload Window).",
    "If Python is already installed in a non-standard location, set codexClaudeUsage.pythonPath in settings.",
  ].join("\n");
}

/**
 * The one-line version of `noPythonMessage` for the modal error dialog, which
 * gets a single line of the user's attention and no scrollback.
 *
 * It is a function rather than the two string literals it replaces because
 * those literals sat inline at the call site, where the `configured-unusable`
 * cause was invisible: the dialog said "needs Python 3.11+ on PATH" for a
 * failure that never touched the PATH. Keep it in step with `noPythonMessage`.
 */
export function noPythonDialogMessage(reason: PythonFailure): string {
  return reason.kind === "configured-unusable"
    ? "Codex / Claude Usage cannot use the interpreter set in codexClaudeUsage.pythonPath (your PATH was not searched). See the dashboard panel for details."
    : "Codex / Claude Usage needs Python 3.11+ on PATH. See the dashboard panel for install links.";
}

let extension: Extension | undefined;

export function activate(context: vscode.ExtensionContext): void {
  extension = new Extension(context);
}

export function deactivate(): void {
  extension?.dispose();
  extension = undefined;
}
