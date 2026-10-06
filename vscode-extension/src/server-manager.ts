import { spawn as defaultSpawn } from "node:child_process";
import { createHmac, randomBytes } from "node:crypto";
import * as http from "node:http";
import { setTimeout as delay } from "node:timers/promises";

/**
 * Anything the server-manager needs from a spawned process. Designed to make
 * tests trivial — pass a fake spawn function that returns an EventEmitter.
 */
export interface SpawnedLike {
  readonly pid?: number;
  kill(signal?: NodeJS.Signals | number): boolean;
  on(event: "exit", listener: (code: number | null, signal: NodeJS.Signals | null) => void): void;
  on(event: "error", listener: (err: Error) => void): void;
  /** Optional at the adapter boundary; start() rejects its absence because the exact readiness record is mandatory. */
  stdout?: { on(event: "data", listener: (chunk: Buffer) => void): void } | null;
  stderr?: { on(event: "data", listener: (chunk: Buffer) => void): void } | null;
}

export interface SpawnOptions {
  env: NodeJS.ProcessEnv;
}

export type SpawnFn = (
  command: string,
  args: ReadonlyArray<string>,
  options: SpawnOptions,
) => SpawnedLike;

/**
 * Testable process-ownership check used immediately before privileged calls.
 * The default checks the OS process table through `kill(pid, 0)`; a failed
 * permission check is treated as not-owned rather than as proof of liveness.
 */
export type ChildOwnershipFn = (pid: number) => boolean | Promise<boolean>;

export interface OutputSink {
  appendLine(line: string): void;
}

export interface ServerManagerOptions {
  command: string;
  args: ReadonlyArray<string>;
  /** URL used to probe readiness, e.g. http://127.0.0.1:9000/. */
  url: string;
  output: OutputSink;
  /** Defaults to node:child_process.spawn. */
  spawnFn?: SpawnFn;
  /** Defaults to the built-in http.get probe. */
  probeFn?: (url: string) => Promise<boolean>;
  /** Per-process API/iframe bearer passed to the child, never sent in a probe. */
  apiToken?: string;
  /** Defaults to the built-in authenticated POST /api/rescan request. */
  rescanFn?: (url: string, apiToken: string, timeoutMs: number, healthToken?: string) => Promise<void>;
  /** Defaults to an OS-level liveness/ownership check for the spawned PID. */
  childOwnershipFn?: ChildOwnershipFn;
  /** Total wall-clock bound for a rescan request. Default 10 minutes. */
  rescanTimeoutMs?: number;
  /** HMAC secret for readiness and one-shot rescan proofs; the raw secret is never sent. */
  healthToken?: string;
  /** Total time to wait for the server to become healthy. Default 10s. */
  readinessTimeoutMs?: number;
  /** Time between probes. Default 200ms. */
  readinessPollMs?: number;
}

export type ServerStatus = "stopped" | "starting" | "ready" | "exited" | "failed";

/**
 * Owns the lifecycle of a single Python dashboard process.
 *
 * State machine:
 *   stopped --start()--> starting --child-ready + probe ok--> ready
 *                                      \--timeout--> failed
 *                           \--exit before ready--> failed
 *   ready --process exits--> exited
 *   any   --dispose()--> stopped (process killed)
 */
export class ServerManager {
  private proc: SpawnedLike | undefined;
  private startupAbort: AbortController | undefined;
  private _status: ServerStatus = "stopped";
  private readonly opts: ServerManagerOptions;
  private readonly spawnFn: SpawnFn;
  private readonly probeFn: (url: string) => Promise<boolean>;
  private readonly rescanFn: (url: string, apiToken: string, timeoutMs: number, healthToken?: string) => Promise<void>;
  private readonly childOwnershipFn: ChildOwnershipFn;
  private readonly readinessTimeoutMs: number;
  private readonly readinessPollMs: number;
  private readonly rescanTimeoutMs: number;

  constructor(opts: ServerManagerOptions) {
    if (!isLoopbackHttpUrl(opts.url)) {
      throw new Error("invalid dashboard URL: expected an HTTP loopback address");
    }
    if (opts.apiToken && !/^[A-Za-z0-9_-]{32,128}$/.test(opts.apiToken)) {
      throw new Error("invalid dashboard API token");
    }
    if (opts.healthToken && !/^[A-Za-z0-9_-]{32,128}$/.test(opts.healthToken)) {
      throw new Error("invalid dashboard health token");
    }
    this.opts = opts;
    this.spawnFn = opts.spawnFn ?? ((cmd, args, options) =>
      defaultSpawn(cmd, args as string[], { env: options.env }));
    this.probeFn = opts.probeFn ?? ((url) => defaultProbe(url, opts.healthToken));
    this.rescanFn = opts.rescanFn ?? defaultRescan;
    this.childOwnershipFn = opts.childOwnershipFn ?? defaultChildOwnership;
    this.readinessTimeoutMs = opts.readinessTimeoutMs ?? 10_000;
    this.readinessPollMs = opts.readinessPollMs ?? 200;
    this.rescanTimeoutMs = opts.rescanTimeoutMs ?? 10 * 60_000;
  }

  get status(): ServerStatus {
    return this._status;
  }

  /**
   * Spawn the process and resolve only after the child reports its own bound
   * origin on inherited stdout and the bounded HTTP health probe succeeds.
   * A loopback peer can answer the probe, but it cannot write to this child's
   * stdout pipe. Callers are expected to wrap port-collision recovery at a
   * higher level (catch failure → pick new port → new ServerManager).
   */
  async start(): Promise<void> {
    if (this._status !== "stopped" && this._status !== "failed" && this._status !== "exited") {
      throw new Error(`cannot start: server is ${this._status}`);
    }
    this.startupAbort?.abort();
    const startup = new AbortController();
    this.startupAbort = startup;
    this._status = "starting";
    this.opts.output.appendLine(`[server] spawning: ${this.opts.command} ${this.opts.args.join(" ")}`);

    let proc: SpawnedLike;
    try {
      const env = sanitizedChildEnvironment();
      if (this.opts.apiToken) {
        env.CODEX_CLAUDE_USAGE_API_TOKEN = this.opts.apiToken;
        // The authenticated fragment is supplied directly to the iframe. Do
        // not duplicate the bearer token in the extension output channel.
        env.CODEX_CLAUDE_USAGE_SUPPRESS_AUTH_URL = "1";
      }
      if (this.opts.healthToken) {
        // The raw secret never crosses HTTP. It keeps extension-owned health
        // responses opaque to unauthenticated port diagnostics; the probe
        // sends a nonce and verifies the HMAC response. The same secret may
        // derive a consumed, one-shot rescan proof, never the browser bearer.
        env.CODEX_CLAUDE_USAGE_HEALTH_TOKEN = this.opts.healthToken;
      }
      proc = this.spawnFn(this.opts.command, this.opts.args, {
        env,
      });
    } catch (err) {
      this._status = "failed";
      this.opts.output.appendLine(`[server] spawn failed: ${(err as Error).message}`);
      throw err;
    }
    this.proc = proc;

    const stdout = proc.stdout;
    if (stdout === undefined || stdout === null) {
      const error = new Error("server child stdout is required for exact readiness");
      this._status = "failed";
      this.opts.output.appendLine(`[server] startup failed: ${error.message}`);
      this.dispose();
      throw error;
    }

    const expectedReadyLine = childReadyLineForUrl(this.opts.url);
    let childReady = false;
    let stdoutBuffer = "";
    const considerReadyLine = (line: string) => {
      // The line is deliberately matched in full, including the origin that
      // this manager is probing. It is a record emitted by the child after its
      // listen() succeeds, not a challenge a port occupant can replay.
      if (!childReady && expectedReadyLine !== undefined && line === expectedReadyLine) {
        childReady = true;
      }
    };
    stdout.on("data", (chunk) => {
      const text = chunk.toString("utf-8");
      this.opts.output.appendLine(`[server] ${text.trimEnd()}`);
      stdoutBuffer += text;
      let newline = stdoutBuffer.indexOf("\n");
      while (newline >= 0) {
        const line = stdoutBuffer.slice(0, newline).replace(/\r$/, "");
        stdoutBuffer = stdoutBuffer.slice(newline + 1);
        considerReadyLine(line);
        newline = stdoutBuffer.indexOf("\n");
      }
    });
    proc.stderr?.on("data", (chunk) => this.opts.output.appendLine(`[server:err] ${chunk.toString().trimEnd()}`));

    let exitedEarly = false;
    let earlyExitCode: number | null = null;
    let spawnError: Error | undefined;
    // Both listeners are scoped to the child *this* start() spawned. A real
    // ChildProcess emits 'exit' asynchronously after kill() returns, so on a
    // restart the previous child's SIGTERM exit lands while the next start()
    // is already "starting" — and an unscoped listener would flip it to
    // "failed", after which the readiness loop can never promote it (the check
    // below requires "starting") and start() polls out its whole budget before
    // throwing a bogus "did not become ready".
    proc.on("exit", (code) => {
      if (this.proc !== proc) return;
      // A well-behaved Python child prints a newline, but accepting the final
      // complete record here also makes the framing contract robust to a
      // wrapper that exits immediately after writing it.
      if (stdoutBuffer) considerReadyLine(stdoutBuffer.replace(/\r$/, ""));
      if (this._status === "starting" || this._status === "ready") {
        exitedEarly = this._status === "starting";
        earlyExitCode = code;
        this._status = this._status === "starting" ? "failed" : "exited";
        this.opts.output.appendLine(`[server] process exited with code ${code}`);
      }
    });
    proc.on("error", (err) => {
      // Logged before the identity check: an error from a child we have moved
      // on from is still worth seeing in the output channel, it just must not
      // rewrite the current child's state.
      this.opts.output.appendLine(`[server] error: ${err.message}`);
      if (this.proc !== proc) return;
      if (this._status === "starting") {
        this._status = "failed";
        // Per Node, a failed spawn "may or may not" be followed by 'exit', and
        // on the ENOENT shape it isn't — so without this the readiness loop
        // would poll a port nothing will ever listen on for the full timeout
        // and then report a timeout, throwing away the only real diagnosis.
        spawnError = err;
      }
    });

    const checkStartup = () => {
      if (startup.signal.aborted || this.proc !== proc) {
        throw new Error("server startup was cancelled");
      }
      // Checked before exitedEarly: when both fire, the error carries the cause
      // and the exit code is just its echo.
      if (spawnError) {
        this.dispose();
        throw spawnError;
      }
      if (exitedEarly) {
        throw new Error(`server exited before becoming ready (code ${earlyExitCode})`);
      }
    };
    const deadline = Date.now() + this.readinessTimeoutMs;
    while (Date.now() < deadline) {
      checkStartup();
      const healthy = await probeWithin(
        this.probeFn,
        this.opts.url,
        Math.max(1, deadline - Date.now()),
        startup.signal,
      );
      checkStartup();
      if (childReady && healthy) {
        // Process may have died after the probe but before we checked status.
        if (this._status === "starting") {
          this._status = "ready";
          this.opts.output.appendLine(`[server] ready at ${this.opts.url}`);
          return;
        }
      }
      await delay(this.readinessPollMs, undefined, { signal: startup.signal })
        .catch((error: unknown) => {
          checkStartup();
          throw error;
        });
    }
    checkStartup();
    this._status = "failed";
    this.dispose();
    throw new Error(`server did not become ready within ${this.readinessTimeoutMs}ms at ${this.opts.url}`);
  }

  /**
   * Ask this manager's authenticated dashboard process to ingest changed
   * transcripts. Keeping the endpoint and proof creation here makes the
   * lifecycle owner the only extension component with rescan authority.
   */
  async rescan(): Promise<void> {
    if (this._status !== "ready") {
      throw new Error(`cannot rescan: server is ${this._status}`);
    }
    const token = this.opts.apiToken;
    if (!token) throw new Error("cannot rescan without a dashboard API token");
    if (!this.opts.healthToken && !this.opts.rescanFn) {
      throw new Error("cannot rescan: built-in rescan requires a health secret");
    }

    const proc = this.proc;
    if (!proc || !await this.ownsLiveChild(proc)) {
      this.markChildUnavailable(proc);
      throw new Error("cannot rescan: owned server process is no longer alive");
    }

    // Re-prove the server now. This probe receives only the health challenge;
    // the API bearer is deliberately not an argument and never crosses this
    // request. A port may have been reclaimed since start(), so cached
    // `ready` is not authorization for the following POST.
    const proofDeadline = Math.max(1, Math.min(this.readinessTimeoutMs, 1_500));
    if (!await probeWithin(this.probeFn, this.opts.url, proofDeadline)) {
      this.markChildUnavailable(proc);
      throw new Error("cannot rescan: authenticated server health proof failed");
    }

    // Close the check/proof/POST race as far as the child-process and HTTP
    // APIs permit: the manager must still own the exact process it started,
    // and it must still be ready, at the last point before the one-shot proof
    // leaves. The built-in POST never sends the reusable browser bearer.
    if (this.proc !== proc || this._status !== "ready"
        || !await this.ownsLiveChild(proc)) {
      this.markChildUnavailable(proc);
      throw new Error("cannot rescan: owned server changed during validation");
    }

    let endpoint: URL;
    try {
      endpoint = new URL("/api/rescan", this.opts.url);
    } catch {
      throw new Error("cannot rescan: invalid dashboard URL");
    }
    if (endpoint.protocol !== "http:" || endpoint.username || endpoint.password) {
      throw new Error("cannot rescan: invalid dashboard URL");
    }
    await this.rescanFn(endpoint.toString(), token, this.rescanTimeoutMs,
      this.opts.healthToken);
  }

  private async ownsLiveChild(proc: SpawnedLike): Promise<boolean> {
    if (this.proc !== proc || this._status !== "ready" && this._status !== "starting") {
      return false;
    }
    const pid = proc.pid;
    if (!Number.isSafeInteger(pid) || (pid as number) <= 0) return false;
    try {
      return Boolean(await this.childOwnershipFn(pid as number));
    } catch {
      return false;
    }
  }

  private markChildUnavailable(proc: SpawnedLike | undefined): void {
    if (this.proc !== proc) return;
    this._status = "exited";
    // A failed health proof can leave the owned child alive. Release it before
    // this manager or the extension replaces it during the next startup.
    this.dispose();
  }

  dispose(): void {
    const proc = this.proc;
    this.proc = undefined;
    this.startupAbort?.abort();
    this.startupAbort = undefined;
    if (proc) {
      try {
        proc.kill();
      } catch {
        // Already gone — fine.
      }
    }
    if (this._status !== "exited" && this._status !== "failed") {
      this._status = "stopped";
    }
  }
}

/** Keep probes and authenticated API calls on the local trust boundary. */
function isLoopbackHttpUrl(value: string): boolean {
  try {
    const parsed = new URL(value);
    const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
    return parsed.protocol === "http:"
      && !parsed.username
      && !parsed.password
      && (host === "localhost" || host === "127.0.0.1" || host === "[::1]" || host === "::1");
  } catch {
    return false;
  }
}

const CHILD_ENV_ALLOWLIST = new Set([
  "HOME",
  "USERPROFILE",
  "HOMEDRIVE",
  "HOMEPATH",
  "APPDATA",
  "LOCALAPPDATA",
  "TMPDIR",
  "TMP",
  "TEMP",
  "SYSTEMROOT",
  "WINDIR",
  "LANG",
  "LANGUAGE",
  "LC_ALL",
  "LC_CTYPE",
  "TZ",
  "CODEX_CLAUDE_USAGE_DOCKER",
]);

/**
 * Give the local Python dashboard only the environment needed to find the
 * user's home directory, temporary directory, locale, timezone, and Windows
 * runtime, plus the Docker collection opt-out (a boolean, never a command).
 * In particular, API keys, cloud credentials, PYTHON* hooks, PATH, and the
 * CLI-only CODEX_CLAUDE_USAGE_DB override never cross this process boundary.
 */
export function sanitizedChildEnvironment(
  source: NodeJS.ProcessEnv = process.env,
): NodeJS.ProcessEnv {
  const result: NodeJS.ProcessEnv = {};
  for (const [key, value] of Object.entries(source)) {
    if (value !== undefined && CHILD_ENV_ALLOWLIST.has(key.toUpperCase())) {
      result[key] = value;
    }
  }
  return result;
}

function defaultChildOwnership(pid: number): boolean {
  try {
    // Signal 0 performs no delivery. On POSIX it returns EPERM for a process
    // the extension cannot control, and ESRCH for a dead/reused PID; both are
    // a fail-closed answer before any privileged dashboard request.
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

/** Keep a custom or changed probe from escaping the manager's total deadline. */
function probeWithin(
  probe: (url: string) => Promise<boolean>,
  url: string,
  timeoutMs: number,
  signal?: AbortSignal,
): Promise<boolean> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value: boolean) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", cancel);
      resolve(value);
    };
    const cancel = () => finish(false);
    const timer = setTimeout(() => finish(false), timeoutMs);
    if (signal?.aborted) {
      cancel();
      return;
    }
    signal?.addEventListener("abort", cancel, { once: true });
    Promise.resolve()
      .then(() => probe(url))
      .then((value) => finish(value), () => finish(false));
  });
}

/**
 * Build the exact post-bind record emitted by dashboard.py. The manager only
 * accepts an explicit HTTP origin with a port; accepting a prefix, a path, or
 * an arbitrary URL here would turn the child-owned signal back into a
 * replayable port challenge.
 */
function childReadyLineForUrl(url: string): string | undefined {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "http:" || parsed.username || parsed.password || !parsed.port) {
      return undefined;
    }
    return `Dashboard listening at ${parsed.origin}`;
  } catch {
    return undefined;
  }
}

/**
 * Built-in readiness probe — true only on a 200 OK whose body parses as JSON
 * containing the fixed service marker and, when configured, the HMAC for a
 * fresh nonce. Neither the API bearer nor the health secret crosses HTTP.
 *
 * Stricter than "anything <500" because a random localhost service on the
 * same port can still return 404, and we don't want to mistake that for
 * "our server is up."
 */
function defaultProbe(url: string, healthToken?: string): Promise<boolean> {
  return new Promise((resolve) => {
    let settled = false;
    let totalTimer: NodeJS.Timeout | undefined;
    let req: http.ClientRequest | undefined;
    const finish = (value: boolean) => {
      if (settled) return;
      settled = true;
      if (totalTimer !== undefined) clearTimeout(totalTimer);
      // Rejected headers may precede an endless body. Finishing the probe must
      // release its socket instead of draining that body without a deadline.
      req?.destroy();
      resolve(value);
    };
    let requestUrl = url;
    let expectedInstance: string | undefined;
    if (healthToken) {
      try {
        const challenge = randomBytes(24).toString("base64url");
        const parsed = new URL(url);
        parsed.searchParams.set("challenge", challenge);
        requestUrl = parsed.toString();
        expectedInstance = createHmac("sha256", healthToken)
          .update(`codex-claude-usage-health\0${challenge}`, "ascii")
          .digest("hex");
      } catch {
        finish(false);
        return;
      }
    }
    req = http.get(requestUrl, { timeout: 1_500 }, (res) => {
      if (res.statusCode !== 200) {
        finish(false);
        return;
      }
      const contentType = String(res.headers["content-type"] ?? "").toLowerCase();
      if (!contentType.startsWith("application/json")) {
        finish(false);
        return;
      }
      const chunks: Buffer[] = [];
      let size = 0;
      res.on("data", (c: Buffer) => {
        size += c.length;
        if (size > 4_096) {
          res.destroy();
          finish(false);
          return;
        }
        chunks.push(c);
      });
      res.on("end", () => {
        if (settled) return;
        try {
          const body = JSON.parse(Buffer.concat(chunks).toString("utf-8"));
          const ok =
            typeof body === "object" &&
            body !== null &&
            body.service === "codex-claude-usage" &&
            body.status === "ok" &&
            (!expectedInstance || body.instance === expectedInstance);
          finish(ok);
        } catch {
          finish(false);
        }
      });
    });
    req.on("error", () => finish(false));
    // The only bound on this promise. `http.get`'s `timeout` option merely
    // *emits* 'timeout'; nothing destroys the socket without this listener, so
    // against a peer that completes the handshake and then answers nothing the
    // probe would never settle — and the readiness loop re-checks its own
    // deadline only *between* probes, so start() would never settle either.
    req.on("timeout", () => {
      finish(false);
    });
    // The request timeout above is an inactivity limit: a peer can reset it by
    // dripping one byte before every interval. This independent timer bounds
    // DNS, headers, and the complete body as one operation.
    totalTimer = setTimeout(() => {
      finish(false);
    }, 1_500);
  });
}

const MAX_RESCAN_RESPONSE_BYTES = 64 * 1024;

/**
 * Perform the host-driven rescan without putting the reusable browser bearer
 * on the wire. The dashboard may legitimately scan for minutes, so this uses
 * one total deadline rather than a short socket-inactivity timeout.
 */
function defaultRescan(url: string, _apiToken: string, timeoutMs: number,
                       healthToken?: string): Promise<void> {
  return new Promise((resolve, reject) => {
    let settled = false;
    let totalTimer: NodeJS.Timeout | undefined;
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      if (totalTimer !== undefined) clearTimeout(totalTimer);
      if (error) reject(error);
      else resolve();
    };

    if (!healthToken) {
      finish(new Error("dashboard rescan requires a readiness secret"));
      return;
    }
    const headers: Record<string, string> = {
      Accept: "application/json",
      "Content-Length": "0",
    };
    // A one-shot proof is accepted by the dashboard for extension rescans.
    // Do not put the reusable browser bearer on the wire where a process that
    // wins a post-readiness port race could capture it.
    // The issue time is part of the authenticated challenge. The dashboard can
    // therefore prune its bounded replay set only after the proof itself has
    // expired; a captured pair never becomes valid again when that entry goes.
    const challenge = `${randomBytes(24).toString("base64url")}_${Date.now()}`;
    const proof = createHmac("sha256", healthToken)
      .update(`codex-claude-usage-rescan\0POST /api/rescan\0${challenge}`, "ascii")
      .digest("hex");
    headers["X-Codex-Claude-Usage-Rescan-Challenge"] = challenge;
    headers["X-Codex-Claude-Usage-Rescan-Proof"] = proof;
    const req = http.request(url, {
      method: "POST",
      headers,
    }, (res) => {
      const chunks: Buffer[] = [];
      let size = 0;
      res.on("data", (chunk: Buffer) => {
        size += chunk.length;
        if (size > MAX_RESCAN_RESPONSE_BYTES) {
          res.destroy();
          finish(new Error("dashboard returned an oversized rescan response"));
          return;
        }
        chunks.push(chunk);
      });
      res.on("aborted", () => finish(new Error("dashboard ended the rescan response early")));
      res.on("error", (err) => finish(err));
      res.on("end", () => {
        if (settled) return;
        const contentType = String(res.headers["content-type"] ?? "").toLowerCase();
        if (!contentType.startsWith("application/json")) {
          finish(new Error("dashboard returned a non-JSON rescan response"));
          return;
        }
        let body: unknown;
        try {
          body = JSON.parse(Buffer.concat(chunks).toString("utf-8"));
        } catch {
          finish(new Error("dashboard returned an invalid rescan response"));
          return;
        }
        const record = typeof body === "object" && body !== null && !Array.isArray(body)
          ? body as Record<string, unknown>
          : undefined;
        const status = res.statusCode ?? 0;
        if (status >= 200 && status < 300 && record && typeof record.error !== "string") {
          finish();
          return;
        }
        const reason = record && typeof record.error === "string"
          ? record.error.slice(0, 512)
          : `dashboard rescan failed with HTTP ${status}`;
        finish(new Error(reason));
      });
    });
    req.on("error", (err) => finish(err));
    totalTimer = setTimeout(() => {
      req.destroy();
      finish(new Error(`dashboard rescan timed out after ${timeoutMs}ms`));
    }, Math.max(1, timeoutMs));
    req.end();
  });
}
