import { accessSync, constants, existsSync, statSync } from "node:fs";
import * as path from "node:path";
import { spawn } from "node:child_process";
import { performance } from "node:perf_hooks";

type PythonVersionProbe = (
  candidate: string,
  timeoutMs: number,
) => boolean | Promise<boolean>;

const PYTHON_PROBE_TIMEOUT_MS = 2_000;
const PYTHON_DISCOVERY_BUDGET_MS = 5_000;
const MAX_VERSION_OUTPUT_BYTES = 64;
const WINDOWS_TASKKILL_TIMEOUT_MS = 1_000;

function reportsPython311(stdout: string): boolean {
  const match = stdout.trim().match(/^(\d+)\.(\d+)$/);
  if (!match) return false;
  const major = Number(match[1]);
  const minor = Number(match[2]);
  return major > 3 || (major === 3 && minor >= 11);
}

function killProbeDirectly(child: ReturnType<typeof spawn>): void {
  try { child.kill("SIGKILL"); } catch { /* already gone */ }
}

function terminateProbe(
  child: ReturnType<typeof spawn>,
  platform: NodeJS.Platform = process.platform,
  spawnProcess: typeof spawn = spawn,
  windowsRoot = process.env.SystemRoot ?? process.env.WINDIR ?? "C:\\Windows",
): void {
  if (child.pid && platform === "win32") {
    let killer: ReturnType<typeof spawn>;
    try {
      // Node cannot signal a Windows process group. taskkill's /T is the OS
      // primitive that terminates wrappers and every descendant which may
      // still own the inherited stdout pipe after the probe deadline.
      killer = spawnProcess(
        path.win32.join(windowsRoot, "System32", "taskkill.exe"),
        ["/PID", String(child.pid), "/T", "/F"],
        { windowsHide: true, stdio: "ignore" },
      );
    } catch {
      killProbeDirectly(child);
      return;
    }

    let settled = false;
    let fallback: NodeJS.Timeout | undefined;
    const finish = (treeTerminated: boolean): void => {
      if (settled) return;
      settled = true;
      if (fallback) clearTimeout(fallback);
      if (!treeTerminated) killProbeDirectly(child);
    };
    killer.once("error", () => finish(false));
    killer.once("close", (code) => finish(code === 0));
    killer.unref();
    fallback = setTimeout(() => {
      try { killer.kill(); } catch { /* already gone */ }
      finish(false);
    }, WINDOWS_TASKKILL_TIMEOUT_MS);
    fallback.unref();
    return;
  }

  if (child.pid) {
    try {
      // `detached` gives the probe its own process group on POSIX. Kill the
      // group so a wrapper cannot keep inherited pipes open via a child after
      // its own deadline.
      process.kill(-child.pid, "SIGKILL");
      return;
    } catch {
      // It may have exited between the timeout and this call. The direct kill
      // below is harmless in that case and covers platforms without groups.
    }
  }
  killProbeDirectly(child);
}

function supportsPython311(
  candidate: string,
  timeoutMs = PYTHON_PROBE_TIMEOUT_MS,
): Promise<boolean> {
  return new Promise((resolve) => {
    let child: ReturnType<typeof spawn>;
    let output = "";
    let settled = false;
    let timer: NodeJS.Timeout | undefined;

    const finish = (usable: boolean, terminate = false): void => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      if (terminate) terminateProbe(child);
      child.stdin?.destroy();
      child.stdout?.destroy();
      child.stderr?.destroy();
      child.unref();
      resolve(usable);
    };

    try {
      child = spawn(
        candidate,
        ["-I", "-S", "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        {
          detached: process.platform !== "win32",
          windowsHide: true,
          stdio: ["ignore", "pipe", "ignore"],
        },
      );
    } catch {
      resolve(false);
      return;
    }

    child.stdout?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => {
      output += chunk;
      if (Buffer.byteLength(output, "utf8") > MAX_VERSION_OUTPUT_BYTES) {
        finish(false, true);
      }
    });
    child.once("error", () => finish(false));
    child.once("close", (code) => finish(code === 0 && reportsPython311(output)));
    timer = setTimeout(() => finish(false, true), Math.max(1, timeoutMs));
  });
}

async function probeWithin(
  candidate: string,
  timeoutMs: number,
  probe: PythonVersionProbe,
): Promise<boolean> {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      Promise.resolve().then(() => probe(candidate, timeoutMs)).catch(() => false),
      new Promise<boolean>((resolve) => {
        timer = setTimeout(() => resolve(false), Math.max(1, timeoutMs));
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/**
 * Interpreter names to try, in order.
 *
 * Windows differs from POSIX in ORDER, not merely by a suffix: `python.exe`
 * comes BEFORE `python3.exe`. The python.org installer this extension points
 * Windows users at ships `python.exe`/`pythonw.exe` and no `python3.exe`,
 * while a `python3.exe` on the default PATH is usually the Microsoft Store
 * App Execution Alias, which opens the Store instead of running a script. Do
 * not "tidy" the two arms into one order. The third, extension-less `python`
 * matches only a literal file of that name (an MSYS2/Cygwin/Git-Bash shim) —
 * `findOnPath` does no PATHEXT expansion.
 *
 * `platform` is a parameter so the tests can assert both arms from one host —
 * a branch test, not a real-platform test, since no CI leg and no maintainer
 * machine runs this suite on Windows. Before it was one, the win32 arm was
 * asserted by nothing anywhere: reversing it shipped with the suite green.
 */
function pythonCandidateNames(platform: NodeJS.Platform = process.platform): string[] {
  return platform === "win32"
    ? ["python.exe", "python3.exe", "python"]
    : ["python3", "python"];
}

/**
 * Walk the PATH manually looking for a command, with platform-aware extensions.
 * No shell involvement — safe to pass any string without sanitisation, but we
 * only ever call this with hard-coded candidate names anyway.
 *
 * Exported for tests; the env var lookup makes it trivial to stub. `platform`
 * is injectable for the same reason as in `pythonCandidateNames` above.
 */
function *candidatesOnPath(
  commandName: string,
  envPath: string | undefined = process.env.PATH,
  platform: NodeJS.Platform = process.platform,
): Generator<string> {
  if (!envPath) return;
  const sep = platform === "win32" ? ";" : ":";
  const dirs = envPath.split(sep).filter(Boolean);
  for (const dir of dirs) {
    // Relative PATH entries (especially ".") make executable selection depend
    // on the opened workspace. Ignore them at this trust boundary.
    if (!path.isAbsolute(dir)) continue;
    const candidate = path.join(dir, commandName);
    try {
      // isFile() is not redundant with the X_OK check below: directories carry
      // the search bit, so accessSync(dir, X_OK) SUCCEEDS. Without it a
      // directory named `python3` on PATH is handed straight to spawn().
      if (existsSync(candidate) && statSync(candidate).isFile()) {
        if (platform !== "win32") accessSync(candidate, constants.X_OK);
        yield candidate;
      }
    } catch {
      // Permission errors etc — skip.
    }
  }
}

export function findOnPath(
  commandName: string,
  envPath: string | undefined = process.env.PATH,
  platform: NodeJS.Platform = process.platform,
): string | undefined {
  for (const candidate of candidatesOnPath(commandName, envPath, platform)) {
    return candidate;
  }
  return undefined;
}

/**
 * Which interpreter was resolved — or WHY none was.
 *
 * This used to be `string | undefined`, and that `undefined` collapsed the two
 * failures a user has to be told apart. The caller could not distinguish them
 * and guessed, so someone with a stale `codexClaudeUsage.pythonPath` and a perfectly
 * good `python3` on their `PATH` was told to install Python on their `PATH` —
 * advice that is wrong twice over, because with that setting non-empty the
 * `PATH` is never looked at.
 *
 * The union carries the cause instead of leaving the caller to re-derive it
 * from the setting it passed in. That derivation would be correct today and
 * silently wrong the moment the exclusivity rule below changed; making the
 * locator name its own failure means a change to the search has to restate
 * which failure it produces, and the message follows.
 *
 * Version support is part of usability: Python older than 3.11 is rejected
 * before the bundled CLI is spawned.
 */
export type PythonLocation =
  /** A usable interpreter: the configured one, or the first candidate found on PATH. */
  | { kind: "found"; path: string }
  /**
   * `configuredPath` was non-empty and is not an absolute path to a runnable
   * regular file. PATH was NOT searched (see the exclusivity rule below).
   */
  | { kind: "configured-unusable"; configuredPath: string }
  /** No `configuredPath` was set, and no candidate name resolved on PATH. */
  | { kind: "not-on-path" };

/** The failure arms of `PythonLocation` — what a "could not start" message branches on. */
export type PythonFailure = Exclude<PythonLocation, { kind: "found" }>;

/**
 * Locate a Python interpreter to run the Claude usage CLI.
 *
 * A non-empty `configuredPath` is EXCLUSIVE, not the first step of a fallback
 * chain: if it is not an absolute path to a usable regular file the function
 * fails and the PATH scan below is never reached. That is deliberate —
 * silently substituting a different interpreter for the one the user named
 * would be worse — and `tests/python-locator.test.ts`'s "does not fall back to
 * PATH when the configured interpreter is broken" pins it. The failure is
 * reported as `configured-unusable` rather than as a bare absence precisely so
 * that fail-closed does not have to be paid for with a misleading message.
 *
 * With an empty `configuredPath` the search is NAME-major: each candidate name
 * is looked for across every absolute PATH entry before the next name is tried,
 * skipping files that cannot report Python 3.11+. Name order therefore beats
 * directory order — a supported `python.exe` in the last PATH entry wins over
 * a `python3.exe` in the first. Order per platform is `pythonCandidateNames`
 * above.
 *
 * Note: this resolves the interpreter only. The dashboard source is the
 * extension's bundled `cli.py`, unless the user explicitly configures another
 * trusted path.
 */
export async function locatePython(
  configuredPath: string,
  platform: NodeJS.Platform = process.platform,
  versionProbe: PythonVersionProbe = supportsPython311,
): Promise<PythonLocation> {
  if (configuredPath) {
    const unusable: PythonLocation = { kind: "configured-unusable", configuredPath };
    if (!path.isAbsolute(configuredPath)) return unusable;
    try {
      // isFile() again: a configured path pointing at a directory passes
      // existsSync and accessSync(X_OK) alike (see findOnPath above).
      if (!existsSync(configuredPath) || !statSync(configuredPath).isFile()) {
        return unusable;
      }
      if (platform !== "win32") accessSync(configuredPath, constants.X_OK);
      if (!await probeWithin(
        configuredPath, PYTHON_PROBE_TIMEOUT_MS, versionProbe)) return unusable;
      return { kind: "found", path: configuredPath };
    } catch {
      return unusable;
    }
  }
  const deadline = performance.now() + PYTHON_DISCOVERY_BUDGET_MS;
  for (const name of pythonCandidateNames(platform)) {
    for (const candidate of candidatesOnPath(name, process.env.PATH, platform)) {
      const remaining = deadline - performance.now();
      if (remaining <= 0) return { kind: "not-on-path" };
      const timeout = Math.min(PYTHON_PROBE_TIMEOUT_MS, remaining);
      if (await probeWithin(candidate, timeout, versionProbe)) {
        return { kind: "found", path: candidate };
      }
    }
  }
  return { kind: "not-on-path" };
}

// Exposed for tests.
export const __testing = {
  pythonCandidateNames,
  supportsPython311,
  terminateProbe,
  PYTHON_PROBE_TIMEOUT_MS,
  PYTHON_DISCOVERY_BUDGET_MS,
};
