import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { locatePython, findOnPath, __testing } from "../src/python-locator";

const IS_WIN = process.platform === "win32";
const PATH_SEP = IS_WIN ? ";" : ":";

describe("python-locator", () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "py-locate-"));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  function writeFakeBinary(dir: string, name: string): string {
    const p = path.join(dir, name);
    if (IS_WIN) {
      fs.writeFileSync(p, "@echo fake\r\n");
    } else {
      fs.writeFileSync(p, "#!/bin/sh\necho fake\n");
      fs.chmodSync(p, 0o755);
    }
    return p;
  }

  function writeVersionedPython(dir: string, name: string, version: string): string {
    const p = path.join(dir, name);
    fs.writeFileSync(p, `#!/bin/sh\nprintf '%s\\n' '${version}'\n`);
    fs.chmodSync(p, 0o755);
    return p;
  }

  function locateFixture(
    configuredPath: string,
    platform: NodeJS.Platform = process.platform,
  ) {
    return locatePython(configuredPath, platform, () => true);
  }

  /**
   * Run `fn` with process.env.PATH set to `value` (or unset, for `undefined`),
   * then restore what was there.
   *
   * The restore cannot be a plain assignment: `process.env.PATH = undefined`
   * stores the literal string "undefined", which is an absolute-looking PATH
   * entry the next test would then walk. Two tests in this file used to do
   * exactly that; centralising it here is why they no longer can.
   *
   * Mutating process.env is safe for the same reason process.chdir() is below:
   * vitest.config.mts leaves the default `forks` pool, so each test file owns
   * its own process.
   */
  async function withPath<T>(
    value: string | undefined,
    fn: () => T | Promise<T>,
  ): Promise<T> {
    const previous = process.env.PATH;
    if (value === undefined) delete process.env.PATH;
    else process.env.PATH = value;
    try {
      return await fn();
    } finally {
      if (previous === undefined) delete process.env.PATH;
      else process.env.PATH = previous;
    }
  }

  describe("locatePython", () => {
    it("returns the configured path when it exists", async () => {
      const fake = writeFakeBinary(tmpDir, IS_WIN ? "python.exe" : "python3");
      expect(await locateFixture(fake)).toEqual({ kind: "found", path: fake });
    });

    it("reports a configured path that does not exist as the SETTING's failure", async () => {
      const missing = path.join(tmpDir, "no-such-python");
      expect(await locateFixture(missing)).toEqual({
        kind: "configured-unusable",
        configuredPath: missing,
      });
    });

    it("tells a broken setting apart from an empty PATH", async () => {
      // The whole reason this function returns a union instead of
      // `string | undefined`. One `undefined` for both causes is what made the
      // extension tell a user with a stale codexClaudeUsage.pythonPath — and a
      // working python3 on PATH — to install Python on their PATH. The two
      // arms must be distinguishable by the caller with no re-derivation, so
      // assert the KINDS differ, not merely that both are falsy.
      const broken = await locateFixture(path.join(tmpDir, "no-such-python"));
      const empty = await withPath(tmpDir, () => locateFixture(""));
      expect(broken.kind).toBe("configured-unusable");
      expect(empty.kind).toBe("not-on-path");
      expect(broken.kind).not.toBe(empty.kind);
    });

    it("rejects a relative configured interpreter path that does resolve to a file", async () => {
      // The relative name has to EXIST relative to the cwd, or existsSync()
      // short-circuits and the path.isAbsolute() guard this test is named for
      // is never reached — which is what it used to do, from vitest's own cwd
      // where no ./python3 exists. Deleting the guard left the whole suite
      // green.
      //
      // process.chdir() works because vitest.config.mts leaves the default
      // `forks` pool; under a threads pool it throws rather than passing
      // quietly. Same dependency as "ignores relative PATH entries" below.
      const name = IS_WIN ? "python.exe" : "python3";
      writeFakeBinary(tmpDir, name);
      const previous = process.cwd();
      process.chdir(tmpDir);
      try {
        expect(await locateFixture("./" + name)).toEqual({
          kind: "configured-unusable",
          configuredPath: "./" + name,
        });
      } finally {
        process.chdir(previous);
      }
    });

    // skipIf, not a bare `return`: vitest counts a bare return as a PASS, so
    // this used to inflate the suite's total with an assertion that never ran.
    it.skipIf(IS_WIN)("rejects a configured interpreter that is not executable", async () => {
      // No execute bit to withhold on Windows; the guard is genuinely
      // POSIX-only, so injecting a platform cannot cover it either.
      // Absolute on purpose: a relative path is refused by the isAbsolute
      // guard above and would never reach accessSync.
      const notExecutable = path.join(tmpDir, "python3");
      fs.writeFileSync(notExecutable, "#!/bin/sh\necho fake\n");
      fs.chmodSync(notExecutable, 0o644);
      expect(await locateFixture(notExecutable)).toEqual({
        kind: "configured-unusable",
        configuredPath: notExecutable,
      });
    });

    it("refuses a configured interpreter that is a directory", async () => {
      // existsSync passes and accessSync(dir, X_OK) SUCCEEDS — directories
      // carry the search bit — so statSync().isFile() is the only guard that
      // rejects one. Deleting it left the whole suite green, and the locator
      // then handed a directory to spawn().
      const dir = path.join(tmpDir, "python3");
      fs.mkdirSync(dir);
      expect(await locateFixture(dir)).toEqual({
        kind: "configured-unusable",
        configuredPath: dir,
      });
    });

    it("does not fall back to PATH when the configured interpreter is broken", async () => {
      // The configured branch is CLOSED: a perfectly good interpreter sitting
      // on PATH is never consulted. Fail-closed is the intended policy —
      // silently substituting a different interpreter for the one the user
      // named would be worse — and this pins it so that the fix to the failure
      // MESSAGE (extension.ts used to blame PATH for a broken setting) could
      // not quietly become a change to the BEHAVIOUR.
      //
      // The message fix widened the return type from `string | undefined` to a
      // union carrying the cause; what it did NOT do is let the search
      // continue. That is what the second assertion says: the answer is the
      // SETTING's failure, never the `found` the control line proves is
      // sitting right there on PATH.
      const onPath = writeFakeBinary(tmpDir, IS_WIN ? "python.exe" : "python3");
      const missing = path.join(tmpDir, "no-such-python");
      expect(await withPath(tmpDir, () => locateFixture(""))).toEqual({
        kind: "found",
        path: onPath,
      }); // control
      expect(await withPath(tmpDir, () => locateFixture(missing))).toEqual({
        kind: "configured-unusable",
        configuredPath: missing,
      });
    });

    it("falls through to PATH discovery when configured path is empty", async () => {
      // Drop a fake python3 / python.exe in tmpDir and put it first on PATH.
      const name = IS_WIN ? "python.exe" : "python3";
      const fake = writeFakeBinary(tmpDir, name);
      const previous = process.env.PATH;
      expect(await withPath(
        tmpDir + PATH_SEP + (previous ?? ""),
        () => locateFixture(""),
      )).toEqual({
        kind: "found",
        path: fake,
      });
    });

    it.skipIf(IS_WIN)(
      "skips an old interpreter and keeps searching the same PATH name",
      async () => {
        const oldDir = path.join(tmpDir, "old");
        const currentDir = path.join(tmpDir, "current");
        fs.mkdirSync(oldDir);
        fs.mkdirSync(currentDir);
        writeVersionedPython(oldDir, "python3", "3.10");
        const current = writeVersionedPython(currentDir, "python3", "3.11");

        expect(await withPath(
          oldDir + PATH_SEP + currentDir,
          () => locatePython(""),
        )).toEqual({
          kind: "found",
          path: current,
        });
      },
    );

    it.skipIf(IS_WIN)("rejects a configured interpreter older than Python 3.11", async () => {
      const old = writeVersionedPython(tmpDir, "python3", "3.10");
      expect(await locatePython(old)).toEqual({
        kind: "configured-unusable",
        configuredPath: old,
      });
    });

    it("reports not-on-path when PATH has no python anywhere", async () => {
      // Point PATH at an empty tmp dir so the candidate names can't resolve.
      expect(await withPath(tmpDir, () => locateFixture(""))).toEqual({ kind: "not-on-path" });
    });

    it.skipIf(IS_WIN)("bounds a probe whose process ignores SIGTERM", async () => {
      const hung = path.join(tmpDir, "hung-python");
      fs.writeFileSync(hung, "#!/bin/sh\ntrap '' TERM\nwhile :; do sleep 10; done\n");
      fs.chmodSync(hung, 0o755);

      const started = Date.now();
      const pending = __testing.supportsPython311(hung, 100);
      // The extension host must get control back while the hostile wrapper is
      // still alive; a spawnSync probe freezes all VS Code extension commands.
      expect(pending).toBeInstanceOf(Promise);
      expect(await pending).toBe(false);
      expect(Date.now() - started).toBeLessThan(2_000);
    });

    it("terminates the complete probe process tree on Windows", () => {
      const listeners: Record<string, (value?: unknown) => void> = {};
      const killer = {
        once: vi.fn((event: string, listener: (value?: unknown) => void) => {
          listeners[event] = listener;
          return killer;
        }),
        unref: vi.fn(),
        kill: vi.fn(),
      };
      const spawnProcess = vi.fn(() => killer);
      const child = { pid: 731, kill: vi.fn() };
      const terminate = (__testing as unknown as {
        terminateProbe: (
          childProcess: unknown,
          platform: NodeJS.Platform,
          spawner: unknown,
          windowsRoot: string,
        ) => void;
      }).terminateProbe;

      expect(typeof terminate).toBe("function");
      terminate(child, "win32", spawnProcess, "C:\\Windows");
      expect(spawnProcess).toHaveBeenCalledWith(
        "C:\\Windows\\System32\\taskkill.exe",
        ["/PID", "731", "/T", "/F"],
        { windowsHide: true, stdio: "ignore" },
      );
      expect(child.kill).not.toHaveBeenCalled();
      listeners.close(0);
      expect(killer.unref).toHaveBeenCalledOnce();
      expect(child.kill).not.toHaveBeenCalled();
    });
  });

  describe("findOnPath", () => {
    it("returns first matching directory entry", () => {
      const a = path.join(tmpDir, "a");
      const b = path.join(tmpDir, "b");
      fs.mkdirSync(a);
      fs.mkdirSync(b);
      const wantedName = IS_WIN ? "thing.exe" : "thing";
      const inA = writeFakeBinary(a, wantedName);
      writeFakeBinary(b, wantedName);
      // a comes first → should win.
      expect(findOnPath(wantedName, a + PATH_SEP + b)).toBe(inA);
    });

    it("returns undefined when envPath is an empty string", () => {
      // Note: passing undefined here would default to process.env.PATH, which
      // is platform-dependent — that's the JS default-arg machinery, not our
      // function's concern. We test the falsy-guard with an empty string.
      expect(findOnPath("python3", "")).toBeUndefined();
    });

    it("returns undefined when process.env.PATH is unset", async () => {
      // "" above does NOT reach the `if (!envPath)` guard — "".split(":")
      // .filter(Boolean) is [] and the loop simply finds nothing — so deleting
      // the guard left the suite green. Only a genuinely unset PATH reaches it.
      //
      // Deliberately not findOnPath("python3", undefined): the
      // `= process.env.PATH` default parameter intercepts an explicitly passed
      // undefined, so that form resolves the machine's real PATH and goes red
      // wherever python3 exists. Commit 3d375a9 removed exactly that line after
      // it failed on the Ubuntu runner.
      //
      // tsc already pins this guard (without it `envPath.split` is a strict-mode
      // TS18048 error, caught by both `compile` and `typecheck:test`), so this
      // is defence in depth against someone reaching for a non-null assertion.
      await withPath(undefined, async () => {
        expect(findOnPath("python3")).toBeUndefined();
        expect(await locateFixture("")).toEqual({ kind: "not-on-path" });
      });
    });

    it("skips a PATH entry whose candidate is a directory", () => {
      // mkdirSync leaves the search bit on, so accessSync(dir, X_OK) succeeds
      // and existsSync is true: isFile() is the only thing standing between a
      // directory named `python3` on PATH and spawn().
      const binDir = path.join(tmpDir, "bin");
      fs.mkdirSync(binDir);
      const name = IS_WIN ? "python.exe" : "python3";
      fs.mkdirSync(path.join(binDir, name));
      expect(findOnPath(name, binDir)).toBeUndefined();
    });

    it("skips directories that don't contain the name", () => {
      const wantedName = IS_WIN ? "needle.exe" : "needle";
      const dirWithIt = path.join(tmpDir, "yes");
      const dirWithoutIt = path.join(tmpDir, "no");
      fs.mkdirSync(dirWithIt);
      fs.mkdirSync(dirWithoutIt);
      const target = writeFakeBinary(dirWithIt, wantedName);
      expect(findOnPath(wantedName, dirWithoutIt + PATH_SEP + dirWithIt)).toBe(target);
    });

    // skipIf, not a bare `return` — see the identical note on the configured
    // interpreter's execute-bit test above.
    it.skipIf(IS_WIN)("skips a candidate that exists but is not executable", () => {
      const noExecDir = path.join(tmpDir, "no-exec");
      const execDir = path.join(tmpDir, "exec");
      fs.mkdirSync(noExecDir);
      fs.mkdirSync(execDir);
      const shadow = path.join(noExecDir, "thing");
      fs.writeFileSync(shadow, "#!/bin/sh\necho fake\n");
      fs.chmodSync(shadow, 0o644);
      const runnable = writeFakeBinary(execDir, "thing");
      // The unrunnable one comes first: accessSync throws, the loop keeps
      // walking the PATH, and the runnable one wins.
      expect(findOnPath("thing", noExecDir + PATH_SEP + execDir)).toBe(runnable);
    });

    it("ignores relative PATH entries", () => {
      const wantedName = IS_WIN ? "thing.exe" : "thing";
      writeFakeBinary(tmpDir, wantedName);
      const previous = process.cwd();
      process.chdir(tmpDir);
      try {
        expect(findOnPath(wantedName, ".")).toBeUndefined();
      } finally {
        process.chdir(previous);
      }
    });
  });

  // BRANCH tests, not real-platform tests. Nothing in this project runs this
  // suite on Windows — extension-ci.yml is a single ubuntu-latest job and the
  // maintainer's machine is macOS — so the win32 arm is driven by injecting the
  // platform rather than by being on it. path.join / path.isAbsolute stay POSIX
  // here, so what these pin is the candidate ORDER and the PATH separator, not
  // Windows path handling.
  //
  // They replace two tests that opened with a bare `if (!IS_WIN) return;` /
  // `if (IS_WIN) return;`. Vitest counts a bare return as a PASS, not a skip,
  // so the win32 order was asserted by nothing, anywhere: reversing
  // pythonCandidateNames' win32 arm shipped with all 133 tests green.
  describe("platform candidate names", () => {
    it("prefers python.exe over python3.exe on win32", () => {
      // Order, not just suffix. The python.org installer this extension
      // recommends ships python.exe/pythonw.exe and NO python3.exe, while a
      // python3.exe on the default PATH is usually the Microsoft Store App
      // Execution Alias, which opens the Store instead of running a script.
      // The bare third entry matches only an extension-less file (an
      // MSYS2/Cygwin/Git-Bash shim) — findOnPath does no PATHEXT expansion.
      expect(__testing.pythonCandidateNames("win32")).toEqual([
        "python.exe",
        "python3.exe",
        "python",
      ]);
    });

    it("prefers python3 over python off win32", () => {
      expect(__testing.pythonCandidateNames("darwin")).toEqual(["python3", "python"]);
      expect(__testing.pythonCandidateNames("linux")).toEqual(["python3", "python"]);
    });

    it("defaults to the host platform", () => {
      expect(__testing.pythonCandidateNames()).toEqual(
        __testing.pythonCandidateNames(process.platform),
      );
      expect(__testing.pythonCandidateNames()[0]).toBe(IS_WIN ? "python.exe" : "python3");
    });
  });

  describe("win32 discovery, driven by the injected platform", () => {
    it("splits PATH on ';'", () => {
      const a = path.join(tmpDir, "a");
      const b = path.join(tmpDir, "b");
      fs.mkdirSync(a);
      fs.mkdirSync(b);
      const target = path.join(b, "python.exe");
      // Not chmod'd: Windows has no execute bit, and the X_OK probe is skipped
      // on win32 — so this also pins that skip.
      fs.writeFileSync(target, "");
      // Splitting on ':' instead would leave one entry, "<a>;<b>", which is
      // absolute-looking and holds nothing: the search would find nothing at all.
      expect(findOnPath("python.exe", a + ";" + b, "win32")).toBe(target);
    });

    it("lets a python.exe in the last PATH entry beat a python3.exe in the first", async () => {
      // The search is NAME-major: findOnPath walks the WHOLE PATH for candidate
      // 1 before candidate 2 is tried, so name order beats directory order.
      // This is what README's "How discovery works" now states, and it is why
      // the win32 order is load-bearing rather than cosmetic.
      const first = path.join(tmpDir, "first");
      const last = path.join(tmpDir, "last");
      fs.mkdirSync(first);
      fs.mkdirSync(last);
      fs.writeFileSync(path.join(first, "python3.exe"), "");
      const winner = path.join(last, "python.exe");
      fs.writeFileSync(winner, "");
      expect(await withPath(
        first + ";" + last,
        () => locateFixture("", "win32"),
      )).toEqual({
        kind: "found",
        path: winner,
      });
    });
  });

  // The README is the only place a USER learns which interpreter wins, and
  // until this block nothing tied its claim to the code. That prose was wrong
  // for Windows for the whole life of the file — both it and package.json's
  // setting description promised "python3, then python" on every platform,
  // the reverse of the win32 arm — and no test, no type and no CI leg noticed,
  // for the same reason the order itself went unasserted: nothing this project
  // owns runs on Windows. Prose about a platform nobody runs rots silently.
  //
  // It parses the table rather than matching a sentence, so rewording the
  // surrounding paragraphs stays free while a REORDER goes red. Reading it
  // through __dirname (Vitest provides it despite the ESM transform) rather
  // than process.cwd() matters: two tests above chdir() into a temp dir.
  //
  // package.json's `codexClaudeUsage.pythonPath.description` — the other place a
  // user reads the order, and the one they are most likely to read, since it
  // is rendered beside the setting in the Settings UI — is covered by the
  // block below this one. It stated the POSIX order as if it were universal
  // until that block was written.
  describe("README documents the real candidate order", () => {
    const README = fs.readFileSync(path.join(__dirname, "..", "README.md"), "utf8");

    /**
     * The backticked names of the candidate-order row whose platform cell
     * matches, in document order. Throws rather than returning [] when the row
     * is gone, so a rewrite that drops the table fails loudly instead of
     * passing vacuously against an empty list.
     */
    function documentedOrder(platform: RegExp): string[] {
      const row = README.split("\n").find(
        (line) => line.startsWith("|") && platform.test(line.split("|")[1]),
      );
      if (!row) {
        throw new Error(`no candidate-order table row matching ${platform} in README.md`);
      }
      return [...row.split("|")[2].matchAll(/`([^`]+)`/g)].map((m) => m[1]);
    }

    it("lists the win32 order, python.exe first", () => {
      expect(documentedOrder(/Windows/)).toEqual(__testing.pythonCandidateNames("win32"));
    });

    it("lists the POSIX order", () => {
      expect(documentedOrder(/macOS/)).toEqual(__testing.pythonCandidateNames("linux"));
    });
  });

  // The setting's own description is what VS Code renders next to the text box
  // in the Settings UI — for most users the ONLY documentation of the order
  // they will ever see, since it needs no visit to the README. It read
  // "auto-discover python3, then python, in absolute PATH entries", the POSIX
  // order presented as universal and the exact reverse of what a Windows user
  // gets, and it said nothing about the setting suppressing the PATH search
  // entirely — the behaviour behind the wrong error message this change fixes.
  //
  // Like the README block above, this matches STRUCTURE rather than a
  // sentence: it pulls the interpreter names out in document order and
  // compares that sequence with the code's, so the surrounding prose can be
  // reworded freely while a REORDER (or a dropped platform) goes red.
  describe("the pythonPath setting description documents the real candidate order", () => {
    const pkg = JSON.parse(
      fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"),
    );
    const description: string =
      pkg.contributes.configuration.properties["codexClaudeUsage.pythonPath"].description;

    /**
     * Every interpreter name the description mentions, in document order.
     * Case-sensitive on purpose: the prose's capital-P "Python" is not a
     * candidate name and must not be counted as one.
     */
    function mentionedNames(): string[] {
      return [...description.matchAll(/python3?(?:\.exe)?/g)].map((m) => m[0]);
    }

    it("states the POSIX order first, then the win32 order", () => {
      expect(mentionedNames()).toEqual([
        ...__testing.pythonCandidateNames("linux"),
        ...__testing.pythonCandidateNames("win32"),
      ]);
    });

    it("labels the second list as the Windows one", () => {
      // Without this the sequence above would still pass if the win32 names
      // were listed with no hint that they only apply on Windows — which is
      // the same defect as the original string, one platform's order printed
      // as if it were everyone's.
      const windows = description.indexOf("Windows");
      expect(windows).toBeGreaterThan(-1);
      expect(windows).toBeLessThan(description.indexOf("python.exe"));
      expect(windows).toBeGreaterThan(description.indexOf("python3,"));
    });

    it("warns that a set path suppresses the PATH search", () => {
      // The user-visible half of locatePython's exclusivity rule. A user whose
      // setting is stale otherwise has no way to know their PATH is not even
      // being looked at.
      expect(description).toMatch(/PATH is not searched/i);
    });
  });
});
