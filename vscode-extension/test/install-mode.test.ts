import { afterEach, beforeEach, describe, expect, it } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { dashboardSpawnArgs, resolveInstallMode } from "../src/install-mode";

describe("resolveInstallMode", () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "install-mode-"));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("uses an explicitly configured cli.py file", () => {
    const cli = path.join(tmpDir, "cli.py");
    fs.writeFileSync(cli, "# explicit placeholder\n");
    expect(resolveInstallMode({ configuredCliPath: cli }))
      .toEqual({ kind: "clone", cliPy: cli });
  });

  it("accepts an explicitly configured directory containing cli.py", () => {
    const cli = path.join(tmpDir, "cli.py");
    fs.writeFileSync(cli, "# explicit placeholder\n");
    expect(resolveInstallMode({ configuredCliPath: tmpDir }))
      .toEqual({ kind: "clone", cliPy: cli });
  });

  it("uses the bundled source by default", () => {
    const bundled = path.join(tmpDir, "bundled-cli.py");
    fs.writeFileSync(bundled, "# bundled placeholder\n");
    expect(resolveInstallMode({ configuredCliPath: "", bundledCliPath: bundled }))
      .toEqual({ kind: "clone", cliPy: bundled });
  });

  it("lets an explicit user path override the bundled source", () => {
    const bundled = path.join(tmpDir, "bundled-cli.py");
    const configured = path.join(tmpDir, "configured-cli.py");
    fs.writeFileSync(bundled, "# bundled\n");
    fs.writeFileSync(configured, "# explicit\n");
    expect(resolveInstallMode({ configuredCliPath: configured, bundledCliPath: bundled }))
      .toEqual({ kind: "clone", cliPy: configured });
  });

  it("returns none instead of auto-executing a workspace or PATH candidate", () => {
    // The resolver deliberately has no workspace/PATH inputs. Only a bundled
    // source or explicit user setting is trusted.
    fs.writeFileSync(path.join(tmpDir, "cli.py"), "# untrusted workspace file\n");
    fs.writeFileSync(path.join(tmpDir, "codex-claude-usage"), "#!/bin/sh\n");
    expect(resolveInstallMode({ configuredCliPath: "" }))
      .toEqual({ kind: "none", cause: "nothing-found" });
  });

  it("refuses a configured directory that contains no cli.py", () => {
    // No bundledCliPath on purpose (cf. the relative-path test below): a
    // bundled path that resolved would let the assertion pass for the wrong
    // reason. Dropping the existsSync() guard on the directory candidate left
    // the whole suite green.
    expect(resolveInstallMode({ configuredCliPath: tmpDir }))
      .toEqual({ kind: "none", cause: "configured-unusable", configuredPath: tmpDir });
  });

  it("falls back to the bundled source when the configured directory has no cli.py", () => {
    // The shape a real user hits: a packaged .vsix ALWAYS carries a bundled
    // cli.py, so without resolveCliPy's existsSync() check on the directory
    // candidate the resolver would hand back <configured dir>/cli.py — a file
    // that does not exist — and the extension would spawn Python against it
    // instead of falling back here.
    //
    // The fallback is deliberate and NOT the fail-closed rule locatePython
    // uses — see resolveInstallMode's doc comment for why the two differ. What
    // it may not be is silent: `ignoredConfiguredPath` is what the caller
    // reports, and without it the user watches the bundled copy run while
    // believing their own checkout is.
    const bundled = path.join(tmpDir, "bundled-cli.py");
    fs.writeFileSync(bundled, "# bundled\n");
    const emptyDir = path.join(tmpDir, "no-cli-here");
    fs.mkdirSync(emptyDir);
    expect(resolveInstallMode({ configuredCliPath: emptyDir, bundledCliPath: bundled }))
      .toEqual({ kind: "clone", cliPy: bundled, ignoredConfiguredPath: emptyDir });
  });

  it("falls back when a configured directory's cli.py is not a regular file", () => {
    const bundled = path.join(tmpDir, "bundled-cli.py");
    fs.writeFileSync(bundled, "# bundled\n");
    const configured = path.join(tmpDir, "configured");
    fs.mkdirSync(configured);
    fs.mkdirSync(path.join(configured, "cli.py"));

    expect(resolveInstallMode({
      configuredCliPath: configured,
      bundledCliPath: bundled,
    })).toEqual({
      kind: "clone",
      cliPy: bundled,
      ignoredConfiguredPath: configured,
    });
  });

  it("names the rejected cliPath when it falls back to the bundled copy", () => {
    // The commonest shape of the same defect: an absolute path that simply is
    // not there any more (a moved or deleted checkout). A packaged .vsix always
    // carries the bundled copy, so this — not `none` — is what a real user with
    // a stale setting hits, and before `ignoredConfiguredPath` existed it
    // produced no message on any surface at all.
    const bundled = path.join(tmpDir, "bundled-cli.py");
    fs.writeFileSync(bundled, "# bundled\n");
    const stale = path.join(tmpDir, "moved-checkout", "cli.py");
    expect(resolveInstallMode({ configuredCliPath: stale, bundledCliPath: bundled }))
      .toEqual({ kind: "clone", cliPy: bundled, ignoredConfiguredPath: stale });
  });

  it("reports no ignored path when the user configured none", () => {
    // The other half of the pair: the ordinary install must not carry the flag,
    // or the caller warns about a setting nobody set on every single launch.
    // toEqual ignores undefined-valued keys, so this is asserted explicitly.
    const bundled = path.join(tmpDir, "bundled-cli.py");
    fs.writeFileSync(bundled, "# bundled\n");
    const mode = resolveInstallMode({ configuredCliPath: "", bundledCliPath: bundled });
    expect(mode).not.toHaveProperty("ignoredConfiguredPath");
  });

  it("reports no ignored path when the configured one was used", () => {
    // …and nor may a setting that WORKED be reported as ignored.
    const bundled = path.join(tmpDir, "bundled-cli.py");
    const configured = path.join(tmpDir, "configured-cli.py");
    fs.writeFileSync(bundled, "# bundled\n");
    fs.writeFileSync(configured, "# explicit\n");
    const mode = resolveInstallMode({ configuredCliPath: configured, bundledCliPath: bundled });
    expect(mode).not.toHaveProperty("ignoredConfiguredPath");
  });

  it("ignores missing configured and bundled paths", () => {
    const missing = path.join(tmpDir, "missing-cli.py");
    expect(resolveInstallMode({
      configuredCliPath: missing,
      bundledCliPath: path.join(tmpDir, "missing-bundled.py"),
    })).toEqual({ kind: "none", cause: "configured-unusable", configuredPath: missing });
  });

  it("separates the two ways of finding no cli.py at all", () => {
    // The single unbranched `{ kind: "none" }` could not tell the caller which
    // of these happened, so one message was printed for both — and its only
    // remedy ("clear the setting to fall back to the bundled sources") is
    // impossible in both, since `none` is returned ONLY when the bundled copy
    // did not resolve.
    const configured = resolveInstallMode({
      configuredCliPath: path.join(tmpDir, "missing-cli.py"),
    });
    const nothing = resolveInstallMode({ configuredCliPath: "" });
    expect(configured).not.toEqual(nothing);
    expect(nothing).not.toHaveProperty("configuredPath");
  });

  it("rejects a relative configured path that does resolve to a file", () => {
    // The relative name has to EXIST relative to the cwd, or existsSync()
    // short-circuits and the path.isAbsolute() guard this test is named for is
    // never reached — which is what it used to do, from vitest's own cwd where
    // no ./cli.py exists. Deleting the guard left the whole suite green.
    // The scenario is a workspace opened with `code .`: the extension host's
    // cwd is the workspace root, so "./cli.py" is a file the workspace
    // controls.
    //
    // process.chdir() works because vitest.config.mts leaves the default
    // `forks` pool; under a threads pool it throws rather than passing
    // quietly. Same dependency as python-locator.test.ts's "ignores relative
    // PATH entries".
    fs.writeFileSync(path.join(tmpDir, "cli.py"), "# untrusted workspace file\n");
    const previous = process.cwd();
    process.chdir(tmpDir);
    try {
      expect(resolveInstallMode({
        configuredCliPath: "./cli.py",
        // Missing on purpose: a bundled path that resolved would let the
        // assertion pass for the wrong reason all over again.
        bundledCliPath: path.join(tmpDir, "missing-bundled.py"),
      })).toEqual({
        kind: "none",
        cause: "configured-unusable",
        configuredPath: "./cli.py",
      });
    } finally {
      process.chdir(previous);
    }
  });
});

describe("dashboardSpawnArgs", () => {
  it("passes Python and dashboard arguments without a shell", () => {
    const mode = { kind: "clone" as const, cliPy: "/repo/cli.py" };
    const result = dashboardSpawnArgs(
      mode, "/usr/bin/python3", ["--host", "127.0.0.1"],
    );
    expect(result?.command).toBe("/usr/bin/python3");
    expect(result?.args.slice(0, 5)).toEqual(["-I", "-S", "-B", "-u", "-c"]);
    // Three load-bearing statements, asserted by intent rather than by
    // comparing the whole one-liner as text: an exact comparison would go red
    // on any reformatting, and the reflex fix (paste the new string in)
    // re-admits whatever went missing. `toContain("runpy.run_path")` alone was
    // satisfied by all three of the mutants below, each of which shipped with
    // the suite green. Measured against this repo's real cli.py, python3
    // 3.13.3, with `--help` in place of `dashboard`:
    //   without run_name='__main__'   → exit 0, ZERO bytes of output. run_path
    //     defaults run_name to '<run_path>', so cli.py's
    //     `if __name__ == "__main__"` never fires: every module imported,
    //     main() defined, never called. The uniquely SILENT one.
    //   without sys.path.insert(0,root) → exit 1, ModuleNotFoundError: scanner
    //     (-I implies -s and -S is passed, so root is on sys.path only here).
    //   without sys.argv=sys.argv[2:]   → exit 1, "unknown command: <root>"
    //     (run_path does not touch sys.argv, so cli.py parses root as its
    //     command).
    // None of the three is a silent 20 s hang: ServerManager's exit listener
    // catches the early exit and reports "server exited before becoming ready
    // (code 0)" in a fraction of a second. The cost is a dead extension with a
    // generic message, not a misdiagnosed one.
    const runner = result!.args[5];
    expect(runner).toMatch(
      /runpy\.run_path\(\s*script\s*,\s*run_name\s*=\s*['"]__main__['"]\s*\)/,
    );
    expect(runner).toMatch(/sys\.path\.insert\(\s*0\s*,\s*root\s*\)/);
    expect(runner).toMatch(/sys\.argv\s*=\s*sys\.argv\[\s*2\s*:\s*\]/);
    expect(result?.args.slice(6)).toEqual([
      "/repo",
      "/repo/cli.py",
      "dashboard",
      "--host",
      "127.0.0.1",
    ]);
  });

  it("runs the child unbuffered so its stdout reaches the output channel", () => {
    // Without -u, CPython block-buffers stdout whenever it is a pipe (which it
    // always is here — ServerManager forwards proc.stdout to the OutputChannel).
    // Nothing arrives while the server runs, and dispose()'s SIGTERM then
    // terminates the process without flushing, so "Background scan failed: …",
    // "Scanning in the background…" and "Dashboard listening at …" are not just
    // late, they are destroyed. stderr is line-buffered and unaffected, so
    // tracebacks land while every print() line vanishes.
    //
    // PYTHONUNBUFFERED is NOT an alternative: -I implies -E, so the child
    // ignores it even if the env allowlist let it through.
    const mode = { kind: "clone" as const, cliPy: "/repo/cli.py" };
    const result = dashboardSpawnArgs(mode, "/usr/bin/python3", []);
    expect(result?.args).toContain("-u");
    // Must precede -c, or it would be read as part of the runner's argv.
    expect(result!.args.indexOf("-u")).toBeLessThan(result!.args.indexOf("-c"));
  });

  it("returns undefined when no Python interpreter is available", () => {
    const mode = { kind: "clone" as const, cliPy: "/repo/cli.py" };
    expect(dashboardSpawnArgs(mode, undefined, [])).toBeUndefined();
  });

  it("returns undefined when no trusted CLI source exists", () => {
    // Both failure causes, because `dashboardSpawnArgs` branches on `kind` and
    // a future arm added to the union must not become a spawnable one.
    expect(dashboardSpawnArgs(
      { kind: "none", cause: "nothing-found" }, "/usr/bin/python3", [],
    )).toBeUndefined();
    expect(dashboardSpawnArgs(
      { kind: "none", cause: "configured-unusable", configuredPath: "/opt/gone/cli.py" },
      "/usr/bin/python3",
      [],
    )).toBeUndefined();
  });
});
