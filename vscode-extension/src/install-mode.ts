import { existsSync, statSync } from "node:fs";
import * as path from "node:path";

/**
 * Why no `cli.py` could be run.
 *
 * `kind: "none"` used to be the whole answer, and it collapsed two failures a
 * user has to be told apart: a `claudeUsage.cliPath` setting that names
 * something unusable, versus no setting at all and no bundled copy. The single
 * message printed for both told the user to "clear it to fall back to the
 * bundled sources" — advice that cannot work in *either* case, because reaching
 * `none` at all means the bundled copy did not resolve.
 *
 * `cause` is a nested discriminant because `kind` is already spent separating
 * success from failure.
 */
export type InstallFailure =
  /**
   * `configuredCliPath` was non-empty and did not resolve, AND no bundled copy
   * resolved either. Note the second half: with a bundled copy present this is
   * NOT the outcome — see the fallback note on `resolveInstallMode`.
   */
  | { kind: "none"; cause: "configured-unusable"; configuredPath: string }
  /** No `configuredCliPath` was set, and no bundled copy resolved. */
  | { kind: "none"; cause: "nothing-found" };

/**
 * How we invoke the dashboard.
 *
 * - `clone`: we have a bundled or explicitly configured `cli.py`. We need a Python
 *   interpreter to run it: `python3 /path/to/cli.py dashboard ...`.
 *
 * - `none`: we couldn't find either — `cause` says which.
 */
export type InstallMode =
  | {
      kind: "clone";
      cliPy: string;
      pythonHint?: string;
      /**
       * Set only when a non-empty `claudeUsage.cliPath` was rejected and the
       * BUNDLED copy is being run in its place. Callers must report it: the
       * bundled copy is the one this extension shipped with, not the user's
       * checkout, so running it silently makes an edited fork look inert.
       */
      ignoredConfiguredPath?: string;
    }
  | InstallFailure;

interface ResolveOptions {
  /** Value of the `claudeUsage.cliPath` setting (empty string if unset). */
  configuredCliPath: string;
  /** Path to the bundled `python/cli.py` shipped inside the .vsix.
   *  Always present in a packaged extension; absent only in tests. This is
   *  the default-and-most-reliable mode for local/private installs — users only
   *  need Python on PATH, no separate claude-usage install. */
  bundledCliPath?: string;
}

/**
 * Resolve an explicitly trusted launcher file, or `cli.py` inside a directory.
 * A directly configured file may be renamed; explicit configuration is the
 * trust decision, while a directory needs the conventional discovery name.
 */
function resolveCliPy(p: string): string | undefined {
  if (!p || !path.isAbsolute(p)) return undefined;
  try {
    if (!existsSync(p)) return undefined;
    const stat = statSync(p);
    if (stat.isFile()) return p;
    if (stat.isDirectory()) {
      const candidate = path.join(p, "cli.py");
      if (existsSync(candidate) && statSync(candidate).isFile()) return candidate;
    }
  } catch {
    return undefined;
  }
  return undefined;
}

/**
 * Decide how we'll run the dashboard.
 *
 * Resolution order:
 * 1. `configuredCliPath` setting → `clone` (explicit user override always wins)
 * 2. **Bundled `python/cli.py`** shipped inside the .vsix → `clone`
 *    (this is the default for local/private installs — users only need Python on PATH)
 * 3. `none`
 *
 * Never auto-execute a workspace file or a PATH command. Both are mutable
 * trust boundaries; development installs can use the explicit setting.
 *
 * DELIBERATELY NOT FAIL-CLOSED, unlike its sibling `locatePython`. A non-empty
 * `configuredCliPath` that does not resolve does not stop the search: step 2
 * still runs and the bundled copy is used. The two resolvers differ because
 * what they fall back TO differs — `locatePython`'s fallback is the ambient
 * `PATH`, a mutable trust boundary, while this one's is the copy shipped inside
 * the .vsix, which is the most trusted `cli.py` available. So the chain is kept
 * (a stale dev setting should not brick the panel), but it is no longer SILENT:
 * the returned mode carries `ignoredConfiguredPath`, and the caller says so.
 * Silence was the actual defect — the extension ran its own already-stale
 * bundled copy while the user believed their configured checkout was running,
 * and the only trace was the resolved path in one output-channel line.
 *
 * Making it fail closed instead is a one-line change (return the
 * `configured-unusable` failure right here). It is a user-visible behaviour
 * change — a broken setting would stop the dashboard rather than degrade it —
 * so it is the maintainer's call, not a silent refactor.
 */
export function resolveInstallMode(opts: ResolveOptions): InstallMode {
  // 1. Explicit setting.
  const cli = resolveCliPy(opts.configuredCliPath);
  if (cli) return { kind: "clone", cliPy: cli };

  // Non-empty and did not resolve. This changes NOTHING about the search below
  // — it only means every arm from here on has to name the rejected path.
  // `configuredCliPath` is read after `resolveCliPy`, never before, so a
  // wrongly-typed setting still throws ERR_INVALID_ARG_TYPE where it always did.
  const ignoredConfiguredPath = opts.configuredCliPath || undefined;

  // 2. Bundled cli.py inside the packaged extension. Most reliable: ships
  //    with the extension version, no separate install needed.
  if (opts.bundledCliPath) {
    const bundled = resolveCliPy(opts.bundledCliPath);
    if (bundled) {
      return ignoredConfiguredPath
        ? { kind: "clone", cliPy: bundled, ignoredConfiguredPath }
        : { kind: "clone", cliPy: bundled };
    }
  }

  return ignoredConfiguredPath
    ? { kind: "none", cause: "configured-unusable", configuredPath: ignoredConfiguredPath }
    : { kind: "none", cause: "nothing-found" };
}

/**
 * Build the spawn args for starting the dashboard, given the install mode +
 * a python interpreter path (used only in `clone` mode).
 *
 * Returned as { command, args } so callers can pass straight to spawn() with
 * no shell.
 */
export function dashboardSpawnArgs(
  mode: InstallMode,
  python: string | undefined,
  extraArgs: string[],
): { command: string; args: string[] } | undefined {
  if (mode.kind === "clone") {
    if (!python) return undefined;
    const runner = [
      "import runpy,sys",
      "root,script=sys.argv[1:3]",
      "sys.path.insert(0,root)",
      "sys.argv=sys.argv[2:]",
      "runpy.run_path(script,run_name='__main__')",
    ].join(";");
    return {
      command: python,
      args: [
        "-I",
        "-S",
        "-B",
        // -u: unbuffered stdout. The extension pipes the child's stdout to its
        // OutputChannel, and CPython block-buffers a piped stdout — so every
        // print() line (notably cli.py's "Background scan failed: …") would sit
        // in an 8 KB buffer and then be destroyed by the SIGTERM that
        // ServerManager.dispose() sends, which terminates without flushing.
        // PYTHONUNBUFFERED cannot substitute: -I implies -E.
        "-u",
        "-c",
        runner,
        path.dirname(mode.cliPy),
        mode.cliPy,
        "dashboard",
        ...extraArgs,
      ],
    };
  }
  return undefined;
}
