import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import * as path from "node:path";
import type { PythonLocation } from "../src/python-locator";

// extension.ts is the composition root, and until this file existed it was the
// only module in src/ with no test at all. It decides the bind host, mints the
// one-shot API/iframe token, assembles the spawn args, and hands that token to
// the child process (via ServerManager) and the iframe (via the URL fragment).
// ServerManager separately proves child identity with the inherited stdout
// readiness record and a nonce-scoped HMAC under a readiness-only secret.
//
// Everything below the composition root is faked so the assertions are about
// *this* file's wiring. `install-mode` is deliberately left real, so the spawn
// args asserted here are the ones production actually builds, and so is
// `trusted-config`, so a wrongly-typed setting throws where it really throws.

const hooks = vi.hoisted(() => ({
  /** Global (user-scope) values trustedUserSetting will see; unset = fallback. */
  settings: {} as Record<string, unknown>,
  outputLines: [] as string[],
  errorMessages: [] as unknown[][],
  warningMessages: [] as unknown[][],
  commands: new Map<string, (...args: unknown[]) => unknown>(),
  executeCommand: (async () => undefined) as (...args: unknown[]) => Promise<unknown>,
  showLogsCalls: 0,
  serverOptions: [] as Array<Record<string, any>>,
  startBehavior: (async () => {}) as () => Promise<void>,
  rescanBehavior: (async () => {}) as () => Promise<void>,
  rescanCalls: 0,
  disposeCalls: 0,
  locatePython: ((_configured: unknown) => ({
    kind: "found",
    path: "/opt/fake/bin/python3",
  })) as (c: unknown) => PythonLocation | Promise<PythonLocation>,
  resolveStablePort: (async () => 9123) as (c: unknown, s: unknown, h: unknown) => Promise<number>,
  resolveStablePortArgs: [] as unknown[][],
  savedPort: undefined as number | undefined,
  persistedPort: undefined as number | undefined,
  sidebarCalls: [] as Array<{ method: string; arg?: unknown }>,
  sidebarOnShow: undefined as undefined | (() => void),
}));

vi.mock("vscode", () => ({
  window: {
    createOutputChannel: () => ({
      appendLine: (line: string) => hooks.outputLines.push(line),
      show: () => { hooks.showLogsCalls++; },
      dispose: () => {},
    }),
    registerWebviewViewProvider: () => ({ dispose: () => {} }),
    showErrorMessage: (...args: unknown[]) => {
      hooks.errorMessages.push(args);
      return Promise.resolve(undefined);
    },
    showWarningMessage: (...args: unknown[]) => {
      hooks.warningMessages.push(args);
      return Promise.resolve(undefined);
    },
  },
  commands: {
    registerCommand: (id: string, fn: (...args: unknown[]) => unknown) => {
      hooks.commands.set(id, fn);
      return { dispose: () => {} };
    },
    executeCommand: (...args: unknown[]) => hooks.executeCommand(...args),
  },
  workspace: {
    getConfiguration: () => ({
      inspect: (key: string) =>
        key in hooks.settings ? { globalValue: hooks.settings[key] } : undefined,
    }),
  },
}));

vi.mock("../src/sidebar", () => {
  class DashboardSidebar {
    static viewId = "claudeUsage.dashboard";
    constructor(onShow?: () => void, _extensionUri?: unknown) {
      hooks.sidebarOnShow = onShow;
    }
    setStatus(text: string): void { hooks.sidebarCalls.push({ method: "setStatus", arg: text }); }
    setError(text: string): void { hooks.sidebarCalls.push({ method: "setError", arg: text }); }
    setUrl(url: string | null): void { hooks.sidebarCalls.push({ method: "setUrl", arg: url }); }
    refresh(): void { hooks.sidebarCalls.push({ method: "refresh" }); }
  }
  return { DashboardSidebar };
});

vi.mock("../src/server-manager", () => {
  class ServerManager {
    status = "stopped";
    constructor(opts: Record<string, any>) { hooks.serverOptions.push(opts); }
    async start(): Promise<void> { await hooks.startBehavior(); this.status = "ready"; }
    async rescan(): Promise<void> { hooks.rescanCalls++; await hooks.rescanBehavior(); }
    dispose(): void { hooks.disposeCalls++; }
  }
  return { ServerManager };
});

vi.mock("../src/python-locator", () => ({
  locatePython: (configured: unknown) => hooks.locatePython(configured),
}));

vi.mock("../src/port-allocator", () => ({
  resolveStablePort: (configured: unknown, saved: unknown, host: unknown) => {
    hooks.resolveStablePortArgs.push([configured, saved, host]);
    return hooks.resolveStablePort(configured, saved, host);
  },
}));

import {
  activate,
  deactivate,
  ignoredCliPathDialogMessage,
  ignoredCliPathMessage,
  noInstallMessage,
  noPythonMessage,
  noPythonDialogMessage,
} from "../src/extension";

let extensionDir: string;

/** Fake ExtensionContext carrying only what extension.ts reads. */
function makeContext() {
  return {
    extensionUri: { scheme: "file", fsPath: extensionDir, toString: () => `file://${extensionDir}` },
    subscriptions: [] as Array<{ dispose(): void }>,
    workspaceState: {
      get: () => hooks.savedPort,
      update: async (_key: string, value: number) => { hooks.persistedPort = value; },
    },
  } as any;
}

/** Activate, then run the same startup the activity-bar icon triggers. */
async function startup(): Promise<void> {
  activate(makeContext());
  const open = hooks.commands.get("claudeUsage.open");
  if (!open) throw new Error("claudeUsage.open was never registered");
  await open();
}

/** Let a voided promise chain settle. */
function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function lastCall(method: string): { method: string; arg?: unknown } | undefined {
  return [...hooks.sidebarCalls].reverse().find((c) => c.method === method);
}

beforeEach(() => {
  hooks.settings = {};
  hooks.outputLines = [];
  hooks.errorMessages = [];
  hooks.warningMessages = [];
  hooks.commands = new Map();
  hooks.executeCommand = async () => undefined;
  hooks.showLogsCalls = 0;
  hooks.serverOptions = [];
  hooks.startBehavior = async () => {};
  hooks.rescanBehavior = async () => {};
  hooks.rescanCalls = 0;
  hooks.disposeCalls = 0;
  hooks.locatePython = () => ({ kind: "found", path: "/opt/fake/bin/python3" });
  hooks.resolveStablePort = async () => 9123;
  hooks.resolveStablePortArgs = [];
  hooks.savedPort = undefined;
  hooks.persistedPort = undefined;
  hooks.sidebarCalls = [];
  hooks.sidebarOnShow = undefined;

  // A packaged extension carries python/cli.py, and resolveInstallMode stats it
  // for real — so give it a real directory rather than mocking install-mode out.
  // Without one, doStartup takes the `mode.kind === "none"` early return and the
  // wiring assertions below would pass while asserting nothing.
  extensionDir = mkdtempSync(path.join(tmpdir(), "claude-usage-ext-"));
  mkdirSync(path.join(extensionDir, "python"));
  writeFileSync(path.join(extensionDir, "python", "cli.py"), "# stub\n");
});

afterEach(() => {
  deactivate();
  rmSync(extensionDir, { recursive: true, force: true });
});

describe("extension startup wiring", () => {
  it("spawns the dashboard on loopback with the embedded-surface flags", async () => {
    await startup();

    const opts = hooks.serverOptions[0];
    expect(opts).toBeDefined();
    // --no-browser: the dashboard lives in the webview, so the child must not
    // also pop a system browser. --surface vscode: suppresses the footer's
    // "get the extension" promo. Both are asserted at the CLI end
    // (tests/test_dashboard.py, tests/test_cli.py); nothing asserted that the
    // extension actually emits them.
    expect(opts.args).toContain("--no-browser");
    expect(opts.args[opts.args.indexOf("--surface") + 1]).toBe("vscode");
    expect(opts.args[opts.args.indexOf("--host") + 1]).toBe("127.0.0.1");
    expect(opts.args).not.toContain("0.0.0.0");
    expect(opts.args[opts.args.indexOf("--port") + 1]).toBe("9123");
    expect(opts.url).toBe("http://127.0.0.1:9123/healthz");
    expect(opts.command).toBe("/opt/fake/bin/python3");
  });

  it("mints separate API and health tokens ServerManager accepts", async () => {
    await startup();

    // server-manager.ts:71 rejects anything outside this shape before spawning,
    // so a shorter mint would be a startup failure rather than a weak token —
    // but only once it reaches the real ServerManager.
    const { apiToken, healthToken } = hooks.serverOptions[0];
    expect(apiToken).toMatch(/^[A-Za-z0-9_-]{32,128}$/);
    expect(healthToken).toMatch(/^[A-Za-z0-9_-]{32,128}$/);
    expect(healthToken).not.toBe(apiToken);
  });

  it("does not spawn after deactivation while interpreter discovery is pending", async () => {
    let finishDiscovery!: (location: PythonLocation) => void;
    hooks.locatePython = () => new Promise((resolve) => {
      finishDiscovery = resolve;
    });
    activate(makeContext());
    const open = hooks.commands.get("claudeUsage.open");
    const starting = Promise.resolve(open?.());
    await flush();

    deactivate();
    finishDiscovery({ kind: "found", path: "/opt/fake/bin/python3" });
    await starting;

    expect(hooks.serverOptions).toHaveLength(0);
    expect(lastCall("setUrl")).toBeUndefined();
  });

  it("does not publish or double-dispose a server deactivated during startup", async () => {
    let finishStart!: () => void;
    hooks.startBehavior = () => new Promise((resolve) => {
      finishStart = resolve;
    });
    activate(makeContext());
    const open = hooks.commands.get("claudeUsage.open");
    const starting = Promise.resolve(open?.());
    await flush();
    expect(hooks.serverOptions).toHaveLength(1);

    deactivate();
    expect(hooks.disposeCalls).toBe(1);
    finishStart();
    await starting;

    expect(hooks.disposeCalls).toBe(1);
    expect(lastCall("setUrl")).toBeUndefined();
  });

  it("hands the iframe the very token the child process was given", async () => {
    await startup();

    // The only place these two are tied together. `apiToken` is one
    // optional line in a nine-property object literal, and dropping it leaves
    // every /api/data call 403 while the iframe has no bearer to attach.
    const token = hooks.serverOptions[0].apiToken;
    expect(typeof token).toBe("string");
    expect(lastCall("setUrl")?.arg).toBe(`http://127.0.0.1:9123/#token=${token}`);
  });

  it("reuses the saved port and persists the one it settled on", async () => {
    // The iframe's localStorage is keyed by origin, so a fresh port each launch
    // silently wipes the collapsed-section state.
    hooks.savedPort = 8123;
    hooks.resolveStablePort = async () => 8123;

    await startup();

    expect(hooks.resolveStablePortArgs[0]).toEqual([0, 8123, "127.0.0.1"]);
    expect(hooks.persistedPort).toBe(8123);
  });

  it("reports the tailored message when no cli.py can be found", async () => {
    rmSync(path.join(extensionDir, "python"), { recursive: true, force: true });

    await startup();

    expect(lastCall("setStatus")?.arg).toBe(noInstallMessage({
      kind: "none",
      cause: "nothing-found",
    }));
    expect(hooks.serverOptions).toHaveLength(0);
  });

  it("blames the setting, not the bundle, when claudeUsage.cliPath is broken", async () => {
    // The install-side twin of the pythonPath defect above. One unbranched
    // string was printed whether or not the setting was the cause, and it never
    // named the path that had been refused — so the user could not tell which
    // of the two had failed, and the path they had typed appeared nowhere.
    rmSync(path.join(extensionDir, "python"), { recursive: true, force: true });
    const stale = path.join(extensionDir, "moved-checkout", "cli.py");
    hooks.settings = { cliPath: stale };

    await startup();

    const panel = String(lastCall("setStatus")?.arg);
    // The setting really reached the resolver — otherwise the message could be
    // "right" while naming a path the user never typed.
    expect(panel).toContain(stale);
    expect(panel).toContain("claudeUsage.cliPath");
    // Both surfaces come from the one helper, so the panel and the modal cannot
    // drift into describing two different causes.
    expect(panel).toBe(noInstallMessage({
      kind: "none",
      cause: "configured-unusable",
      configuredPath: stale,
    }));
    expect(String(hooks.errorMessages[0]?.[0])).toBe(panel);
    expect(hooks.outputLines.some((l) => l.includes(stale))).toBe(true);
    expect(hooks.serverOptions).toHaveLength(0);
  });

  it("keeps the two no-install causes on distinct wordings", async () => {
    // A single unbranched string would satisfy every "contains" assertion in
    // the test above if it merely mentioned the setting. This says the branch
    // is real.
    const broken = {
      kind: "none",
      cause: "configured-unusable",
      configuredPath: "/opt/gone/cli.py",
    } as const;
    const nothing = { kind: "none", cause: "nothing-found" } as const;
    expect(noInstallMessage(broken)).not.toBe(noInstallMessage(nothing));
    // The no-setting arm must not send the user to inspect a setting that is
    // empty by definition of this arm.
    expect(noInstallMessage(nothing)).not.toContain("Check your claudeUsage.cliPath setting");
    expect(noInstallMessage(nothing)).not.toContain("/opt/gone/cli.py");
    // And NEITHER may repeat the old remedy. resolveInstallMode returns `none`
    // only when the bundled copy did not resolve, so "clear the setting to fall
    // back to the bundled sources" is impossible at the moment it is printed —
    // the single message offered it in every case it was ever shown.
    for (const reason of [broken, nothing]) {
      expect(noInstallMessage(reason)).not.toContain("fall back to the bundled sources");
    }
  });

  it("warns instead of silently running the bundled cli.py for a broken cliPath", async () => {
    // The reachable half of this defect, and the one that produced no message
    // on any surface. A packaged .vsix always carries the bundled copy, so a
    // stale cliPath does not fail — the dashboard starts on the extension's own
    // already-stale copy while the user believes their checkout is running.
    const stale = path.join(extensionDir, "moved-checkout", "cli.py");
    hooks.settings = { cliPath: stale };

    await startup();

    // Still starts: the fallback is deliberate (see resolveInstallMode's doc
    // comment), and only its silence was the defect.
    expect(hooks.serverOptions).toHaveLength(1);
    expect(hooks.serverOptions[0].args).toContain(path.join(extensionDir, "python", "cli.py"));
    // …and says so, on both surfaces, naming the rejected path AND the copy it
    // ran instead. Only the two side by side make the substitution visible.
    const logged = hooks.outputLines.find((l) => l.includes("ignored your claudeUsage.cliPath"));
    expect(logged).toBe(
      ignoredCliPathMessage(stale, path.join(extensionDir, "python", "cli.py")),
    );
    expect(hooks.warningMessages).toHaveLength(1);
    expect(hooks.warningMessages[0][0]).toBe(ignoredCliPathDialogMessage(stale));
    expect(String(hooks.warningMessages[0][0])).toContain(stale);
    // A warning, not an error: the dashboard is up.
    expect(hooks.errorMessages).toHaveLength(0);
  });

  it("does not warn about a cliPath the user never set", async () => {
    // The ordinary install is the overwhelming majority, and a warning on every
    // launch there would train the user to dismiss the one that matters.
    await startup();

    expect(hooks.serverOptions).toHaveLength(1);
    expect(hooks.warningMessages).toHaveLength(0);
    expect(hooks.outputLines.some((l) => l.includes("claudeUsage.cliPath"))).toBe(false);
  });

  it("does not warn about a cliPath that resolved", async () => {
    // …nor may a setting that WORKED be reported as ignored: the extension is
    // running exactly the file the user named.
    const own = path.join(extensionDir, "my-fork", "cli.py");
    mkdirSync(path.dirname(own));
    writeFileSync(own, "# my fork\n");
    hooks.settings = { cliPath: own };

    await startup();

    expect(hooks.serverOptions[0].args).toContain(own);
    expect(hooks.warningMessages).toHaveLength(0);
  });

  it("reports the tailored message when no Python interpreter is found", async () => {
    hooks.locatePython = () => ({ kind: "not-on-path" });

    await startup();

    expect(lastCall("setStatus")?.arg).toBe(noPythonMessage({ kind: "not-on-path" }));
    expect(String(hooks.errorMessages[0]?.[0])).toContain("Claude Usage needs Python 3.11+ on PATH");
    expect(hooks.serverOptions).toHaveLength(0);
  });

  it("blames the setting, not PATH, when claudeUsage.pythonPath is broken", async () => {
    // The defect this pair of assertions exists for: locatePython fails CLOSED
    // on a non-empty setting, so the PATH is never searched — and both the
    // panel text and the modal said "needs Python 3.11+ on PATH" anyway. A
    // user with a stale setting and a working python3 on PATH was told to
    // install Python on their PATH.
    const stale = "/opt/gone/bin/python3";
    hooks.settings = { pythonPath: stale };
    hooks.locatePython = (configured) => ({
      kind: "configured-unusable",
      configuredPath: String(configured),
    });

    await startup();

    const panel = String(lastCall("setStatus")?.arg);
    const dialog = String(hooks.errorMessages[0]?.[0]);
    // The setting really reached the locator — otherwise the message could be
    // "right" while naming a path the user never typed.
    expect(panel).toContain(stale);
    expect(panel).toContain("claudeUsage.pythonPath");
    expect(dialog).toContain("claudeUsage.pythonPath");
    // Neither string may repeat the old advice.
    expect(panel).not.toContain("needs Python 3.11 or newer on your PATH");
    expect(dialog).not.toContain("needs Python 3.11+ on PATH");
    expect(panel).not.toContain("python.org");
    // Both come from the shared helpers, so the panel and the modal cannot
    // drift into describing two different causes.
    expect(panel).toBe(noPythonMessage({ kind: "configured-unusable", configuredPath: stale }));
    expect(dialog).toBe(
      noPythonDialogMessage({ kind: "configured-unusable", configuredPath: stale }),
    );
    expect(hooks.serverOptions).toHaveLength(0);
  });

  it("keeps the two no-Python causes on distinct wordings", async () => {
    // A single unbranched string would satisfy every "contains" assertion in
    // the pair of tests above if it merely mentioned both causes. These two
    // say the branch is real.
    const broken = { kind: "configured-unusable", configuredPath: "/opt/gone/bin/python3" } as const;
    expect(noPythonMessage(broken)).not.toBe(noPythonMessage({ kind: "not-on-path" }));
    expect(noPythonDialogMessage(broken)).not.toBe(
      noPythonDialogMessage({ kind: "not-on-path" }),
    );
    // The not-on-path arm keeps its per-platform install hint.
    expect(noPythonMessage({ kind: "not-on-path" }, "win32")).toContain("downloads/windows/");
    expect(noPythonMessage({ kind: "not-on-path" }, "darwin")).toContain("brew install python");
    expect(noPythonMessage({ kind: "not-on-path" }, "linux")).toContain("apt install python3");
    // …and the broken-setting arm has none: the user demonstrably HAS chosen
    // an interpreter, so "go install Python" is the wrong next step on every
    // platform.
    for (const platform of ["win32", "darwin", "linux"] as const) {
      expect(noPythonMessage(broken, platform)).not.toContain("Install Python");
    }
  });

  it("surfaces a failure from manager.start() with Retry and Show Logs", async () => {
    hooks.startBehavior = async () => { throw new Error("server exited before becoming ready (code 1)"); };

    await startup();

    expect(String(lastCall("setError")?.arg)).toContain("server exited before becoming ready");
    expect(hooks.errorMessages[0]).toEqual([
      expect.stringContaining("server exited before becoming ready"),
      "Retry",
      "Show Logs",
    ]);
    expect(hooks.disposeCalls).toBe(1);
  });

  it("the Rescan command scans through the authenticated manager before refreshing", async () => {
    await startup();
    hooks.sidebarCalls = [];

    const rescan = hooks.commands.get("claudeUsage.rescan");
    expect(rescan).toBeDefined();
    await rescan?.();

    expect(hooks.rescanCalls).toBe(1);
    expect(hooks.sidebarCalls).toEqual([{ method: "refresh" }]);
  });

  it("starts the dashboard before a Rescan command issued on a cold extension", async () => {
    activate(makeContext());

    const rescan = hooks.commands.get("claudeUsage.rescan");
    await rescan?.();

    expect(hooks.serverOptions).toHaveLength(1);
    expect(hooks.rescanCalls).toBe(1);
    expect(lastCall("refresh")).toBeDefined();
  });

  it("reports a failed Rescan and does not refresh stale data", async () => {
    await startup();
    hooks.sidebarCalls = [];
    hooks.rescanBehavior = async () => { throw new Error("A rescan is already running"); };

    const rescan = hooks.commands.get("claudeUsage.rescan");
    await rescan?.();

    expect(hooks.sidebarCalls).toEqual([]);
    expect(hooks.outputLines.at(-1)).toContain("A rescan is already running");
    expect(hooks.errorMessages.at(-1)).toEqual([
      expect.stringContaining("A rescan is already running"),
      "Show Logs",
    ]);
  });
});

describe("extension startup failures ahead of the spawn", () => {
  // Everything before `new ServerManager` used to sit outside doStartup's
  // try/catch, and the sidebar's onShow callback voided the resulting
  // rejection — so a throw here left the panel on "not running yet", the
  // output channel empty and no Retry button, while the extension host logged
  // an unhandled rejection the user never sees.

  it("reports a port-resolution failure instead of rejecting silently", async () => {
    // pickFreePort wires server.once("error", reject), so an EMFILE burst
    // propagates out of resolveStablePort.
    hooks.resolveStablePort = async () => { throw new Error("listen EMFILE: too many open files"); };

    await startup();

    expect(String(lastCall("setError")?.arg)).toContain("listen EMFILE: too many open files");
    expect(hooks.outputLines.some((l) => l.includes("listen EMFILE"))).toBe(true);
    expect(hooks.errorMessages[0]).toEqual([
      expect.stringContaining("listen EMFILE"),
      "Retry",
      "Show Logs",
    ]);
    // Nothing was constructed, so nothing may be disposed — a catch that
    // dereferenced an unset `manager` would replace one silent failure with
    // another.
    expect(hooks.disposeCalls).toBe(0);
  });

  it("reports a wrongly-typed cliPath setting instead of rejecting silently", async () => {
    // Not simulated: resolveInstallMode is the real module here, and
    // resolveCliPy calls path.isAbsolute outside any try, so a non-string
    // machine-scoped setting throws ERR_INVALID_ARG_TYPE for real.
    hooks.settings = { cliPath: 123 };

    await startup();

    expect(String(lastCall("setError")?.arg)).toContain("must be of type string");
    expect(hooks.outputLines.some((l) => l.includes("must be of type string"))).toBe(true);
    expect(hooks.disposeCalls).toBe(0);
  });

  it("surfaces a rejection from the sidebar's auto-start callback", async () => {
    // onShow returns void, so anything openDashboard() rejects with *outside*
    // doStartup — notably the executeCommand that reveals the view container,
    // which runs before doStartup is even called — is otherwise discarded.
    hooks.executeCommand = async () => { throw new Error("view container is not registered"); };
    activate(makeContext());

    hooks.sidebarOnShow?.();
    await flush();

    expect(String(lastCall("setError")?.arg)).toContain("view container is not registered");
    expect(hooks.outputLines.some((l) => l.includes("view container is not registered"))).toBe(true);
  });
});
