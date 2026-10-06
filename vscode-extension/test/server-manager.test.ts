import { describe, it, expect, beforeEach, vi } from "vitest";
import { createHmac } from "node:crypto";
import { EventEmitter } from "node:events";
import {
  ServerManager,
  SpawnedLike,
  SpawnFn,
  OutputSink,
  sanitizedChildEnvironment,
} from "../src/server-manager";

class FakeStream extends EventEmitter {}

class FakeProcess extends EventEmitter implements SpawnedLike {
  pid = 1234;
  stdout = new FakeStream();
  stderr = new FakeStream();
  killed = false;
  kill(_signal?: NodeJS.Signals | number): boolean {
    this.killed = true;
    return true;
  }
}

/**
 * A child that dies the way a real one does: kill() returns, and 'exit'
 * arrives a few ticks later. FakeProcess emits nothing on kill(), which is why
 * the restart test below could not see a stale exit landing on the *next*
 * start. Kept as a separate class so the twenty-odd tests that rely on
 * FakeProcess emitting nothing keep their current semantics.
 */
class DyingProcess extends EventEmitter implements SpawnedLike {
  pid = 4321;
  stdout = new FakeStream();
  stderr = new FakeStream();
  killed = false;
  kill(_signal?: NodeJS.Signals | number): boolean {
    this.killed = true;
    setTimeout(() => this.emit("exit", null, "SIGTERM"), 20);
    return true;
  }
}

class NoStdoutProcess extends EventEmitter implements SpawnedLike {
  pid = 9876;
  stdout = undefined;
  stderr = new FakeStream();
  killed = false;
  kill(_signal?: NodeJS.Signals | number): boolean {
    this.killed = true;
    return true;
  }
}

class MemorySink implements OutputSink {
  lines: string[] = [];
  appendLine(line: string): void {
    this.lines.push(line);
  }
}

function fakeProbe(answers: Array<boolean | (() => boolean)>): (url: string) => Promise<boolean> {
  let i = 0;
  return async () => {
    const ans = answers[Math.min(i, answers.length - 1)];
    i++;
    return typeof ans === "function" ? ans() : !!ans;
  };
}

type TestProcess = FakeProcess | DyingProcess;

function childReadyRecord(url: string): Buffer {
  const parsed = new URL(url);
  if (!parsed.port) throw new Error(`test URL must include a port: ${url}`);
  return Buffer.from(`Dashboard listening at ${parsed.origin}\n`);
}

function queueChildReady(process: TestProcess, url: string): void {
  // The real child cannot write until ServerManager has attached its stdout
  // listener. A microtask models that ordering without making the test sleep.
  queueMicrotask(() => process.stdout.emit("data", childReadyRecord(url)));
}

function readySpawn(url: string, process: TestProcess = new FakeProcess()): SpawnFn {
  return () => {
    queueChildReady(process, url);
    return process;
  };
}

function readySpawnFactory(url: string, factory: () => TestProcess): SpawnFn {
  return () => {
    const process = factory();
    queueChildReady(process, url);
    return process;
  };
}

describe("ServerManager", () => {
  it("refuses a non-loopback dashboard URL before it can receive API authority", () => {
    expect(() => new ServerManager({
      command: "python",
      args: [],
      url: "http://example.test:9000/healthz",
      output: new MemorySink(),
      apiToken: "a".repeat(32),
    })).toThrow(/HTTP loopback address/);
  });

  let proc: FakeProcess;
  let spawnFn: SpawnFn;
  let sink: MemorySink;

  beforeEach(() => {
    proc = new FakeProcess();
    spawnFn = readySpawn("http://127.0.0.1:9000/", proc);
    sink = new MemorySink();
  });

  it("status starts as stopped", () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([false]),
      readinessTimeoutMs: 100,
      readinessPollMs: 10,
    });
    expect(mgr.status).toBe("stopped");
  });

  it("refuses the built-in rescan before probing without a health secret", async () => {
    let rescanProbes = 0;
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      apiToken: "a".repeat(32),
      spawnFn,
      probeFn: async () => {
        rescanProbes++;
        return true;
      },
      childOwnershipFn: () => true,
    });
    await mgr.start();
    const probesAtReady = rescanProbes;
    await expect(mgr.rescan()).rejects.toThrow(/requires a health secret/);
    expect(rescanProbes).toBe(probesAtReady);
    expect(mgr.status).toBe("ready");
  });

  it("becomes ready when the probe succeeds", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([false, false, true]),
      readinessTimeoutMs: 500,
      readinessPollMs: 10,
    });
    await mgr.start();
    expect(mgr.status).toBe("ready");
    expect(sink.lines.some((l) => l.includes("ready at"))).toBe(true);
  });

  it("fails when the process exits before becoming ready", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([false]),
      readinessTimeoutMs: 500,
      readinessPollMs: 10,
    });
    const startPromise = mgr.start();
    // Simulate process dying before the first successful probe
    setTimeout(() => proc.emit("exit", 1, null), 20);
    await expect(startPromise).rejects.toThrow(/exited before becoming ready/);
    expect(mgr.status).toBe("failed");
  });

  it("fails after the readiness timeout when the probe never succeeds", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([false]),
      readinessTimeoutMs: 60,
      readinessPollMs: 10,
    });
    await expect(mgr.start()).rejects.toThrow(/did not become ready/);
    expect(mgr.status).toBe("failed");
  });

  it("fails immediately when child stdout is unavailable", async () => {
    const noStdout = new NoStdoutProcess();
    let probeCalls = 0;
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn: () => noStdout,
      probeFn: async () => {
        probeCalls++;
        return true;
      },
      readinessTimeoutMs: 3_000,
      readinessPollMs: 10,
    });
    const began = Date.now();

    await expect(mgr.start()).rejects.toThrow(
      /child stdout is required for exact readiness/,
    );

    expect(Date.now() - began).toBeLessThan(1_000);
    expect(probeCalls).toBe(0);
    expect(noStdout.killed).toBe(true);
    expect(mgr.status).toBe("failed");
    expect(sink.lines).toContain(
      "[server] startup failed: server child stdout is required for exact readiness",
    );
  });

  it("dispose kills the child process", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([true]),
      readinessTimeoutMs: 200,
      readinessPollMs: 10,
    });
    await mgr.start();
    expect(mgr.status).toBe("ready");
    mgr.dispose();
    expect(proc.killed).toBe(true);
    expect(mgr.status).toBe("stopped");
  });

  it("dispose is safe when nothing was started", () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([false]),
      readinessTimeoutMs: 100,
      readinessPollMs: 10,
    });
    expect(() => mgr.dispose()).not.toThrow();
    expect(mgr.status).toBe("stopped");
  });

  it.each(["probe", "poll"])("dispose cancels startup promptly during a %s", async (waitingOn) => {
    vi.useFakeTimers();
    const mgr = new ServerManager({
      command: "noop", args: [], url: "http://127.0.0.1:9000/",
      output: sink, spawnFn,
      probeFn: waitingOn === "probe" ? () => new Promise(() => {}) : async () => false,
      readinessTimeoutMs: 10_000,
      readinessPollMs: 200,
    });
    let outcome = "pending";
    const starting = mgr.start().then(
      () => { outcome = "ready"; },
      (error: Error) => { outcome = error.message; },
    );
    try {
      await vi.advanceTimersByTimeAsync(0);
      mgr.dispose();
      await vi.advanceTimersByTimeAsync(0);
      expect(outcome).toMatch(/cancelled/);
      expect(mgr.status).toBe("stopped");
      expect(proc.killed).toBe(true);
      await vi.advanceTimersByTimeAsync(10_500);
      expect(mgr.status).toBe("stopped");
    } finally {
      mgr.dispose();
      await vi.runAllTimersAsync();
      await starting;
      vi.useRealTimers();
    }
  });

  it("an old readiness response cannot promote a replacement process", async () => {
    vi.useFakeTimers();
    const replacement = new FakeProcess();
    let spawns = 0;
    let probes = 0;
    let answerOld!: (value: boolean) => void;
    let answerNew!: (value: boolean) => void;
    const oldProbe = new Promise<boolean>((resolve) => { answerOld = resolve; });
    const newProbe = new Promise<boolean>((resolve) => { answerNew = resolve; });
    const mgr = new ServerManager({
      command: "noop", args: [], url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn: () => ++spawns === 1 ? spawnFn("noop", [], { env: {} }) : replacement,
      probeFn: () => ++probes === 1 ? oldProbe : newProbe,
      readinessTimeoutMs: 10_000,
      readinessPollMs: 200,
    });
    const first = mgr.start().catch((error: Error) => error.message);
    let second: Promise<void | string> | undefined;
    try {
      await vi.advanceTimersByTimeAsync(0);
      mgr.dispose();
      second = mgr.start().catch((error: Error) => error.message);
      await vi.advanceTimersByTimeAsync(0);
      answerOld(true);
      await vi.advanceTimersByTimeAsync(0);
      expect(mgr.status).toBe("starting");
      expect(await first).toMatch(/cancelled/);
      replacement.stdout.emit("data", childReadyRecord("http://127.0.0.1:9000/"));
      answerNew(true);
      await vi.advanceTimersByTimeAsync(0);
      expect(await second).toBeUndefined();
      expect(mgr.status).toBe("ready");
      expect(replacement.killed).toBe(false);
    } finally {
      mgr.dispose();
      answerOld(false);
      answerNew(false);
      await vi.runAllTimersAsync();
      await Promise.all([first, second]);
      vi.useRealTimers();
    }
  });

  it("stdout is forwarded to the sink", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([true]),
      readinessTimeoutMs: 200,
      readinessPollMs: 10,
    });
    await mgr.start();
    proc.stdout.emit("data", Buffer.from("Dashboard running at http://127.0.0.1:9000\n"));
    expect(sink.lines.some((l) => l.includes("[server] Dashboard running"))).toBe(true);
  });

  it("stderr is forwarded with [server:err] prefix", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([true]),
      readinessTimeoutMs: 200,
      readinessPollMs: 10,
    });
    await mgr.start();
    proc.stderr.emit("data", Buffer.from("Address already in use\n"));
    expect(sink.lines.some((l) => l.startsWith("[server:err]"))).toBe(true);
  });

  it("propagates spawn-time errors as failure", async () => {
    const failingSpawn: SpawnFn = () => {
      throw new Error("ENOENT: python3 not found");
    };
    const mgr = new ServerManager({
      command: "python3",
      args: ["cli.py"],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn: failingSpawn,
      probeFn: fakeProbe([false]),
      readinessTimeoutMs: 100,
      readinessPollMs: 10,
    });
    await expect(mgr.start()).rejects.toThrow(/ENOENT/);
    expect(mgr.status).toBe("failed");
  });

  it("fails fast with the real cause when the process emits an async spawn error", async () => {
    // Node's docs: on a failed spawn the 'exit' event "may or may not" follow
    // the 'error' event. When it doesn't (the ENOENT shape), `exitedEarly`
    // stays false, so a loop that only watches that flag polls a port nothing
    // will ever listen on for the whole readiness timeout and then blames the
    // timeout — discarding "spawn ... ENOENT", the only useful diagnosis.
    // Real trigger: a `brew upgrade python` retargeting the symlink between
    // locatePython() and spawn, a stale codexClaudeUsage.pythonPath, or a disabled
    // Windows App Execution Alias (findOnPath skips X_OK there, so it sees a
    // file that spawn cannot execute).
    const mgr = new ServerManager({
      command: "python3",
      args: [],
      url: "http://127.0.0.1:9000/healthz",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([false]),
      readinessTimeoutMs: 3_000,
      readinessPollMs: 10,
    });
    const startPromise = mgr.start();
    // Emit 'error' only — deliberately no 'exit'.
    setTimeout(() => proc.emit("error", new Error("spawn /usr/bin/python3 ENOENT")), 20);
    const began = Date.now();
    await expect(startPromise).rejects.toThrow(/spawn \/usr\/bin\/python3 ENOENT/);
    // Must surface the cause promptly, not sit out the full timeout.
    expect(Date.now() - began).toBeLessThan(1_000);
    expect(mgr.status).toBe("failed");
  });

  it("does not report a readiness timeout when the real cause was a spawn error", async () => {
    const mgr = new ServerManager({
      command: "python3",
      args: [],
      url: "http://127.0.0.1:9000/healthz",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([false]),
      readinessTimeoutMs: 3_000,
      readinessPollMs: 10,
    });
    const startPromise = mgr.start();
    setTimeout(() => proc.emit("error", new Error("spawn /usr/bin/python3 ENOENT")), 20);
    await expect(startPromise).rejects.not.toThrow(/did not become ready/);
  });

  it("kills the child when a spawn error aborts startup", async () => {
    const mgr = new ServerManager({
      command: "python3",
      args: [],
      url: "http://127.0.0.1:9000/healthz",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([false]),
      readinessTimeoutMs: 3_000,
      readinessPollMs: 10,
    });
    const startPromise = mgr.start();
    setTimeout(() => proc.emit("error", new Error("spawn /usr/bin/python3 ENOENT")), 20);
    await expect(startPromise).rejects.toThrow(/ENOENT/);
    // Same cleanup the timeout path performs — an error that did NOT kill the
    // child (e.g. an EPIPE on a live process) must not leak it.
    expect(proc.killed).toBe(true);
  });

  it("does not go ready when the child dies inside the probe's round trip", async () => {
    // The `if (this._status === "starting")` guard on the healthy branch is the
    // only thing between a successful probe and a manager that has already been
    // moved to "failed" by its exit handler. The exit is emitted synchronously
    // from inside the probe, before it resolves — scheduling it with a timer
    // instead would land after the await's continuation, leaving the status
    // still "starting" and the manager correctly ready, so the test would pass
    // against the broken code too.
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: async () => {
        proc.emit("exit", 3, null);
        return true;
      },
      readinessTimeoutMs: 500,
      readinessPollMs: 10,
    });

    await expect(mgr.start()).rejects.toThrow(/exited before becoming ready \(code 3\)/);

    expect(mgr.status).toBe("failed");
    // What the user would otherwise see: setUrl pointing the iframe at a port
    // with nothing behind it, instead of the failure dialog and Retry.
    expect(sink.lines.some((l) => l.includes("ready at"))).toBe(false);
  });

  it("a spawn error after the server is ready does not retroactively fail startup", async () => {
    // Only 'starting' aborts. Once ready, a late stdio error must not rewrite
    // a resolved start() into a failure.
    const mgr = new ServerManager({
      command: "python3",
      args: [],
      url: "http://127.0.0.1:9000/healthz",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([true]),
      readinessTimeoutMs: 500,
      readinessPollMs: 10,
    });
    await mgr.start();
    expect(mgr.status).toBe("ready");
    proc.emit("error", new Error("EPIPE"));
    expect(mgr.status).toBe("ready");
  });

  it("can be restarted after dispose", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn: readySpawnFactory("http://127.0.0.1:9000/", () => new FakeProcess()),
      probeFn: fakeProbe([true]),
      readinessTimeoutMs: 200,
      readinessPollMs: 10,
    });
    await mgr.start();
    mgr.dispose();
    expect(mgr.status).toBe("stopped");
    await mgr.start();
    expect(mgr.status).toBe("ready");
  });

  it("can be restarted after the previous child actually exits", async () => {
    // The test above passes with a fake whose kill() emits nothing. A real
    // child emits 'exit' asynchronously *after* kill() returns, so the dead
    // child's SIGTERM exit lands while the next start() is already "starting".
    // Unless the listener is scoped to the process that start() spawned, it
    // flips the new attempt to "failed", the readiness loop then can never
    // promote it, and the second start throws "did not become ready" — with a
    // fresh Python taking hundreds of ms to bind while a SIGTERM'd child dies
    // in tens, that is the normal shape of a restart, not a narrow race.
    const sink2 = new MemorySink();
    let readyAfter = 0;
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink2,
      spawnFn: () => {
        const child = new DyingProcess();
        queueChildReady(child, "http://127.0.0.1:9000/");
        return child;
      },
      probeFn: fakeProbe([true, () => Date.now() >= readyAfter]),
      readinessTimeoutMs: 400,
      readinessPollMs: 10,
    });
    await mgr.start();
    mgr.dispose();
    // The second child must take longer to answer than the first takes to die,
    // or the stale exit lands after "ready" and the test proves nothing.
    readyAfter = Date.now() + 80;

    await mgr.start();

    expect(mgr.status).toBe("ready");
    // The stale exit fires 20ms into a start that only reaches ready at 80ms;
    // give it room to land and confirm it did not rewrite the new child's state.
    await new Promise((r) => setTimeout(r, 50));
    expect(mgr.status).toBe("ready");
  });

  it("ignores an error raised by the child a restart already replaced", async () => {
    // Same scoping rule, the other listener: a SIGTERM'd child's stdio can
    // error while its replacement is still starting, and an unscoped handler
    // records that as the new attempt's spawnError — so the restart dies
    // reporting the *old* child's failure. The log line is emitted before the
    // identity check, so a stale error is still visible in the output channel.
    const spawned: DyingProcess[] = [];
    const sink2 = new MemorySink();
    let readyAfter = 0;
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink2,
      spawnFn: () => {
        const p = new DyingProcess();
        spawned.push(p);
        queueChildReady(p, "http://127.0.0.1:9000/");
        return p;
      },
      probeFn: fakeProbe([true, () => Date.now() >= readyAfter]),
      readinessTimeoutMs: 400,
      readinessPollMs: 10,
    });
    await mgr.start();
    mgr.dispose();
    readyAfter = Date.now() + 80;

    const second = mgr.start();
    setTimeout(() => spawned[0].emit("error", new Error("EPIPE")), 20);

    await expect(second).resolves.toBeUndefined();
    expect(mgr.status).toBe("ready");
    expect(sink2.lines.some((l) => l.includes("[server] error: EPIPE"))).toBe(true);
  });

  it("refuses to start while already starting/ready", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([true]),
      readinessTimeoutMs: 200,
      readinessPollMs: 10,
    });
    await mgr.start();
    await expect(mgr.start()).rejects.toThrow(/cannot start/);
  });

  it("passes separate health and API tokens into the sanitized child env", async () => {
    let spawnedEnv: NodeJS.ProcessEnv | undefined;
    const apiToken = "a".repeat(43);
    const healthToken = "h".repeat(43);
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/healthz",
      output: sink,
      apiToken,
      healthToken,
      spawnFn: (_command, _args, options) => {
        spawnedEnv = options.env;
        queueChildReady(proc, "http://127.0.0.1:9000/healthz");
        return proc;
      },
      probeFn: fakeProbe([true]),
    });

    await mgr.start();

    expect(spawnedEnv?.CODEX_CLAUDE_USAGE_HEALTH_TOKEN).toBe(healthToken);
    expect(spawnedEnv?.CODEX_CLAUDE_USAGE_API_TOKEN).toBe(apiToken);
    expect(spawnedEnv?.CODEX_CLAUDE_USAGE_SUPPRESS_AUTH_URL).toBe("1");
    expect(spawnedEnv?.ANTHROPIC_API_KEY).toBeUndefined();
  });

  it("rejects malformed API tokens before spawning", () => {
    expect(() => new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/healthz",
      output: sink,
      apiToken: "bad\r\nheader",
    })).toThrow(/invalid dashboard API token/);
  });

  it("rejects malformed health tokens before spawning", () => {
    expect(() => new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/healthz",
      output: sink,
      healthToken: "bad\r\nheader",
    })).toThrow(/invalid dashboard health token/);
  });

  it("refuses a rescan until the owned server is ready", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/healthz",
      output: sink,
      apiToken: "a".repeat(43),
      spawnFn,
      probeFn: fakeProbe([true]),
      childOwnershipFn: () => true,
    });

    await expect(mgr.rescan()).rejects.toThrow(/server is stopped/);
  });

  it("requires API authority for a rescan", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/healthz",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([true]),
    });
    await mgr.start();

    await expect(mgr.rescan()).rejects.toThrow(/API token/);
  });

  it("posts a rescan with the one-shot proof and accepts bounded JSON", async () => {
    const http = await import("node:http");
    let observedMethod = "";
    let observedPath = "";
    let observedToken: string | undefined;
    const server = http.createServer((req, res) => {
      observedMethod = req.method ?? "";
      observedPath = req.url ?? "";
      observedToken = req.headers["x-codex-claude-usage-token"] as string | undefined;
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ new: 1, updated: 0, skipped: 3 }));
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    if (!address || typeof address === "string") throw new Error("no address");
    const healthUrl = `http://127.0.0.1:${address.port}/healthz`;
    const token = "r".repeat(43);
    const healthToken = "h".repeat(43);
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: healthUrl,
      output: sink,
      apiToken: token,
      healthToken,
      spawnFn: readySpawn(healthUrl),
      probeFn: fakeProbe([true]),
      childOwnershipFn: () => true,
      rescanTimeoutMs: 1_000,
    });
    try {
      await mgr.start();
      await mgr.rescan();
      expect(observedMethod).toBe("POST");
      expect(observedPath).toBe("/api/rescan");
      expect(observedToken).toBeUndefined();
    } finally {
      mgr.dispose();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });

  it("uses a one-shot health proof instead of the reusable bearer", async () => {
    const http = await import("node:http");
    let observedToken: string | undefined;
    let observedChallenge: string | undefined;
    let observedProof: string | undefined;
    const server = http.createServer((req, res) => {
      observedToken = req.headers["x-codex-claude-usage-token"] as string | undefined;
      observedChallenge = req.headers["x-codex-claude-usage-rescan-challenge"] as string | undefined;
      observedProof = req.headers["x-codex-claude-usage-rescan-proof"] as string | undefined;
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ new: 0, updated: 0, skipped: 0 }));
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    if (!address || typeof address === "string") throw new Error("no address");
    const healthUrl = `http://127.0.0.1:${address.port}/healthz`;
    const token = "r".repeat(43);
    const healthToken = "h".repeat(43);
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: healthUrl,
      output: sink,
      apiToken: token,
      healthToken,
      spawnFn: readySpawn(healthUrl),
      probeFn: fakeProbe([true]),
      childOwnershipFn: () => true,
      rescanTimeoutMs: 1_000,
    });
    try {
      await mgr.start();
      await mgr.rescan();
      expect(observedToken).toBeUndefined();
      expect(observedChallenge).toMatch(/^[A-Za-z0-9_-]{32,128}$/);
      expect(observedProof).toBe(createHmac("sha256", healthToken)
        .update(`codex-claude-usage-rescan\0POST /api/rescan\0${observedChallenge}`, "ascii")
        .digest("hex"));
    } finally {
      mgr.dispose();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });

  it("surfaces the dashboard's bounded rescan error", async () => {
    const http = await import("node:http");
    const server = http.createServer((_req, res) => {
      res.writeHead(409, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "A rescan is already running" }));
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    if (!address || typeof address === "string") throw new Error("no address");
    const healthUrl = `http://127.0.0.1:${address.port}/healthz`;
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: healthUrl,
      output: sink,
      apiToken: "s".repeat(43),
      healthToken: "t".repeat(43),
      spawnFn: readySpawn(healthUrl),
      probeFn: fakeProbe([true]),
      childOwnershipFn: () => true,
      rescanTimeoutMs: 1_000,
    });
    try {
      await mgr.start();
      await expect(mgr.rescan()).rejects.toThrow("A rescan is already running");
    } finally {
      mgr.dispose();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });

  it("does not send the bearer when the owned child exits during the proof", async () => {
    const token = "r".repeat(43);
    let ownershipChecks = 0;
    let postCalls = 0;
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/healthz",
      output: sink,
      apiToken: token,
      spawnFn,
      probeFn: async (url) => {
        expect(url).toBe("http://127.0.0.1:9000/healthz");
        return true;
      },
      childOwnershipFn: () => ++ownershipChecks === 1,
      rescanFn: async () => { postCalls++; },
    });

    await mgr.start();
    await expect(mgr.rescan()).rejects.toThrow(/changed during validation/);
    expect(postCalls).toBe(0);
    expect(mgr.status).toBe("exited");
  });

  it("releases the child when its rescan health proof fails", async () => {
    let healthy = true;
    const children: FakeProcess[] = [];
    const post = vi.fn(async () => {});
    const url = "http://127.0.0.1:9000/healthz";
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url,
      output: sink,
      apiToken: "r".repeat(43),
      spawnFn: readySpawnFactory(url, () => {
        const child = new FakeProcess();
        children.push(child);
        return child;
      }),
      probeFn: async () => healthy,
      childOwnershipFn: () => true,
      rescanFn: post,
    });

    try {
      await mgr.start();
      healthy = false;
      await expect(mgr.rescan()).rejects.toThrow(/health proof failed/);
      expect(mgr.status).toBe("exited");
      expect(post).not.toHaveBeenCalled();
      expect(children[0].killed).toBe(true);

      healthy = true;
      await mgr.start();
      expect(mgr.status).toBe("ready");
      expect(children[1].killed).toBe(false);
    } finally {
      mgr.dispose();
    }
    expect(children.every((child) => child.killed)).toBe(true);
  });

  it("freshly proves health without giving the probe API authority", async () => {
    const token = "r".repeat(43);
    const probeArguments: unknown[][] = [];
    let postCalls = 0;
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/healthz",
      output: sink,
      apiToken: token,
      spawnFn,
      probeFn: async (...args: [string]) => {
        probeArguments.push(args);
        return true;
      },
      childOwnershipFn: () => true,
      rescanFn: async (_url, receivedToken) => {
        postCalls++;
        expect(receivedToken).toBe(token);
      },
    });

    await mgr.start();
    await mgr.rescan();
    expect(postCalls).toBe(1);
    expect(probeArguments.length).toBeGreaterThanOrEqual(2);
    expect(probeArguments.every((args) => args.length === 1)).toBe(true);
    expect(probeArguments.flat()).not.toContain(token);
  });
});

describe("sanitizedChildEnvironment", () => {
  it("keeps TZ while stripping unsafe Python environment hooks", () => {
    expect(sanitizedChildEnvironment({
      TZ: "Pacific/Kiritimati",
      PYTHONPATH: "/untrusted/python",
      PYTHONSTARTUP: "/untrusted/startup.py",
    })).toEqual({
      TZ: "Pacific/Kiritimati",
    });
  });

  it("keeps runtime basics and strips credentials and Python hooks", () => {
    expect(sanitizedChildEnvironment({
      HOME: "/Users/test",
      TMPDIR: "/tmp/test",
      LANG: "en_US.UTF-8",
      SystemRoot: "C:\\Windows",
      PATH: "/untrusted/bin",
      PYTHONPATH: "/untrusted/python",
      PYTHONSTARTUP: "/untrusted/startup.py",
      ANTHROPIC_API_KEY: "secret-anthropic",
      GITHUB_TOKEN: "secret-github",
      CODEX_CLAUDE_USAGE_DB: "/tmp/redirected.db",
      CODEX_CLAUDE_USAGE_DOCKER: "0",
      DOCKER_HOST: "ssh://unexpected-host",
      DOCKER_CONTEXT: "remote-context",
      CODEX_CLAUDE_USAGE_HEALTH_TOKEN: "attacker-controlled",
      CODEX_CLAUDE_USAGE_API_TOKEN: "attacker-controlled",
      CODEX_CLAUDE_USAGE_SUPPRESS_AUTH_URL: "attacker-controlled",
    })).toEqual({
      HOME: "/Users/test",
      TMPDIR: "/tmp/test",
      LANG: "en_US.UTF-8",
      SystemRoot: "C:\\Windows",
      CODEX_CLAUDE_USAGE_DOCKER: "0",
    });
  });
});

describe("default probe (integration via fake http server)", () => {
  // Live test of the default probe behavior — start a tiny http server that
  // returns various responses and confirm only the right shape passes.
  // This is the strictness Codex asked for: any old localhost service
  // returning 404/HTML on /healthz must NOT be treated as healthy.

  import("node:http").then(/* type-only resolve so vitest doesn't get confused */);

  async function makeServer(handler: (req: any, res: any) => void): Promise<{ url: string; close: () => void }> {
    const http = await import("node:http");
    return new Promise((resolve) => {
      const server = http.createServer(handler);
      server.listen(0, "127.0.0.1", () => {
        const addr = server.address();
        if (!addr || typeof addr === "string") throw new Error("no addr");
        resolve({
          url: `http://127.0.0.1:${addr.port}/healthz`,
          close: () => server.close(),
        });
      });
    });
  }

  it("does not accept a rogue echo server without child-owned readiness", async () => {
    const apiToken = "a".repeat(43);
    const healthToken = "b".repeat(43);
    let observedInstanceHeader: string | string[] | undefined;
    const srv = await makeServer((req, res) => {
      // This is the old replay shape: a listener on the raced port echoes any
      // marker it receives. The fixed probe sends only a nonce, and even a
      // plausible response cannot replace the child's inherited stdout record.
      observedInstanceHeader = req.headers["x-codex-claude-usage-instance"];
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({
        service: "codex-claude-usage",
        status: "ok",
        version: "1.5.5",
        instance: req.headers["x-codex-claude-usage-instance"],
      }));
    });
    try {
      const child = new FakeProcess();
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        apiToken,
        healthToken,
        spawnFn: () => child,
        // Intentionally NOT injecting probeFn so the real HTTP probe runs.
        readinessTimeoutMs: 200,
        readinessPollMs: 30,
      });
      await expect(mgr.start()).rejects.toThrow(/did not become ready/);
      expect(mgr.status).toBe("failed");
      expect(child.killed).toBe(true);
      expect(observedInstanceHeader).toBeUndefined();
    } finally {
      srv.close();
    }
  });

  it("rejects an echoed health marker even after child-owned readiness", async () => {
    const healthToken = "e".repeat(43);
    const srv = await makeServer((req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ service: "codex-claude-usage", status: "ok",
        instance: req.headers["x-codex-claude-usage-instance"] }));
    });
    try {
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        apiToken: "a".repeat(43),
        healthToken,
        spawnFn: readySpawn(srv.url),
        readinessTimeoutMs: 200,
        readinessPollMs: 30,
      });
      await expect(mgr.start()).rejects.toThrow(/did not become ready/);
      expect(mgr.status).toBe("failed");
    } finally {
      srv.close();
    }
  });

  it("accepts the matching health proof only after child-owned readiness", async () => {
    const healthToken = "h".repeat(43);
    const srv = await makeServer((req, res) => {
      const challenge = new URL(req.url, "http://127.0.0.1").searchParams.get("challenge");
      const instance = createHmac("sha256", healthToken)
        .update(`codex-claude-usage-health\0${challenge}`, "ascii")
        .digest("hex");
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ service: "codex-claude-usage", status: "ok", version: "1.5.5",
        instance }));
    });
    try {
      const child = new FakeProcess();
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        apiToken: "b".repeat(43),
        healthToken,
        spawnFn: readySpawn(srv.url, child),
        readinessTimeoutMs: 500,
        readinessPollMs: 30,
      });
      await mgr.start();
      expect(mgr.status).toBe("ready");
      mgr.dispose();
    } finally {
      srv.close();
    }
  });

  it("rejects a child record for a different origin", async () => {
    const srv = await makeServer((_req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ service: "codex-claude-usage", status: "ok", version: "1.5.5" }));
    });
    try {
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        apiToken: "c".repeat(43),
        spawnFn: readySpawn("http://127.0.0.1:1/"),
        readinessTimeoutMs: 200,
        readinessPollMs: 30,
      });
      await expect(mgr.start()).rejects.toThrow(/did not become ready/);
    } finally {
      srv.close();
    }
  });

  it("rejects the old unauthenticated data shape", async () => {
    const srv = await makeServer((_req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ all_models: [], sessions_all: [] }));
    });
    try {
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        spawnFn: readySpawn(srv.url),
        readinessTimeoutMs: 500,
        readinessPollMs: 30,
      });
      await expect(mgr.start()).rejects.toThrow(/did not become ready/);
    } finally {
      srv.close();
    }
  });

  it("rejects 200 + JSON that isn't the health shape", async () => {
    const srv = await makeServer((_req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ greeting: "hello" }));
    });
    try {
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        spawnFn: readySpawn(srv.url),
        readinessTimeoutMs: 200,
        readinessPollMs: 30,
      });
      await expect(mgr.start()).rejects.toThrow(/did not become ready/);
    } finally {
      srv.close();
    }
  });

  it("rejects a random localhost service's bare 404", async () => {
    // Realistic shape, but it never reaches the status gate: with no
    // Content-Type at all it is the content-type check that rejects it. The
    // test below is the one that pins the status check.
    const srv = await makeServer((_req, res) => {
      res.writeHead(404);
      res.end("Not found");
    });
    try {
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        spawnFn: readySpawn(srv.url),
        readinessTimeoutMs: 200,
        readinessPollMs: 30,
      });
      await expect(mgr.start()).rejects.toThrow(/did not become ready/);
    } finally {
      srv.close();
    }
  });

  it("rejects a non-200 status even when the body is a valid health marker", async () => {
    // Only the status gate can reject this one: JSON content type, and the
    // exact marker the healthy branch looks for. Without it, weakening the
    // status check to "anything <500" — or deleting it outright — left the
    // whole suite green, including the test above whose name claimed it.
    const srv = await makeServer((_req, res) => {
      res.writeHead(404, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ service: "codex-claude-usage", status: "ok", version: "1.5.5" }));
    });
    try {
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        spawnFn: readySpawn(srv.url),
        readinessTimeoutMs: 200,
        readinessPollMs: 30,
      });
      await expect(mgr.start()).rejects.toThrow(/did not become ready/);
    } finally {
      srv.close();
    }
  });

  it("rejects 200 + non-JSON body (HTML)", async () => {
    const srv = await makeServer((_req, res) => {
      res.writeHead(200, { "Content-Type": "text/html" });
      res.end("<html><body>some other server</body></html>");
    });
    try {
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        spawnFn: readySpawn(srv.url),
        readinessTimeoutMs: 200,
        readinessPollMs: 30,
      });
      await expect(mgr.start()).rejects.toThrow(/did not become ready/);
    } finally {
      srv.close();
    }
  });

  it("rejects the marker under a non-JSON content type", async () => {
    const srv = await makeServer((_req, res) => {
      res.writeHead(200, { "Content-Type": "text/plain" });
      res.end(JSON.stringify({ service: "codex-claude-usage", status: "ok" }));
    });
    try {
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        spawnFn: readySpawn(srv.url),
        readinessTimeoutMs: 200,
        readinessPollMs: 30,
      });
      await expect(mgr.start()).rejects.toThrow(/did not become ready/);
    } finally {
      srv.close();
    }
  });

  it("gives up on a peer that accepts the connection and never answers", async () => {
    // The probe's own socket-timeout handler is the only bound on start().
    // http.get's `timeout` option merely emits 'timeout'; nothing destroys the
    // socket without that listener, and the readiness loop re-checks its
    // deadline only *between* probes — so a probe that never settles means
    // start() never settles, whatever readinessTimeoutMs says. Measured with
    // the handler deleted: this test does not fail slowly, it hangs to the
    // per-test timeout below. With it, one probe ends at its fixed 1_500ms and
    // the loop is past its deadline by the next check (~1.5s total here).
    const srv = await makeServer(() => {
      /* accept, and never respond */
    });
    try {
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        spawnFn: readySpawn(srv.url),
        readinessTimeoutMs: 1_000,
        readinessPollMs: 30,
      });
      const began = Date.now();
      await expect(mgr.start()).rejects.toThrow(/did not become ready/);
      expect(Date.now() - began).toBeLessThan(5_000);
      expect(mgr.status).toBe("failed");
    } finally {
      srv.close();
    }
  }, 15_000);

  it("the manager deadline bounds a probe promise that never settles", async () => {
    const child = new FakeProcess();
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/healthz",
      output: new MemorySink(),
      spawnFn: readySpawn("http://127.0.0.1:9000/healthz", child),
      probeFn: async () => new Promise<boolean>(() => undefined),
      readinessTimeoutMs: 80,
      readinessPollMs: 10,
    });

    const began = Date.now();
    await expect(mgr.start()).rejects.toThrow(/did not become ready/);
    expect(Date.now() - began).toBeLessThan(1_000);
    expect(child.killed).toBe(true);
    expect(mgr.status).toBe("failed");
  });

  it("a slow-drip health body cannot renew the probe deadline", async () => {
    let drip: NodeJS.Timeout | undefined;
    const srv = await makeServer((_req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.write("{");
      drip = setInterval(() => res.write(" "), 100);
    });
    try {
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        spawnFn: readySpawn(srv.url),
        readinessTimeoutMs: 2_500,
        readinessPollMs: 30,
      });
      const began = Date.now();
      await expect(mgr.start()).rejects.toThrow(/did not become ready/);
      expect(Date.now() - began).toBeLessThan(5_000);
      expect(mgr.status).toBe("failed");
    } finally {
      if (drip !== undefined) clearInterval(drip);
      srv.close();
    }
  }, 10_000);

  it.each([
    [503, "application/json"],
    [200, "text/plain"],
  ])("closes a rejected streaming response (%s, %s)", async (status, contentType) => {
    const http = await import("node:http");
    let responseClosed: () => void = () => {};
    const closed = new Promise<boolean>((resolve) => {
      responseClosed = () => resolve(true);
    });
    const server = http.createServer((_req, res) => {
      res.writeHead(status, { "Content-Type": contentType });
      res.write("unavailable");
      const drip = setInterval(() => res.write("."), 20);
      res.on("close", () => {
        clearInterval(drip);
        responseClosed();
      });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    if (!address || typeof address === "string") throw new Error("no address");
    const url = `http://127.0.0.1:${address.port}/healthz`;
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url,
      output: new MemorySink(),
      spawnFn: readySpawn(url),
      readinessTimeoutMs: 1_000,
      readinessPollMs: 50,
    });
    const startup = mgr.start().catch(() => {});
    let timeout: NodeJS.Timeout | undefined;
    try {
      const released = await Promise.race([
        closed,
        new Promise<boolean>((resolve) => {
          timeout = setTimeout(() => resolve(false), 250);
        }),
      ]);
      expect(released).toBe(true);
      expect(mgr.status).not.toBe("ready");
    } finally {
      if (timeout) clearTimeout(timeout);
      mgr.dispose();
      await startup;
      server.closeAllConnections();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });

  it("rejects an oversized health response", async () => {
    const srv = await makeServer((_req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({
        service: "codex-claude-usage",
        status: "ok",
        padding: "x".repeat(5_000),
      }));
    });
    try {
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        spawnFn: readySpawn(srv.url),
        readinessTimeoutMs: 200,
        readinessPollMs: 30,
      });
      await expect(mgr.start()).rejects.toThrow(/did not become ready/);
    } finally {
      srv.close();
    }
  });
});
