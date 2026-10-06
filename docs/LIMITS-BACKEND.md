# Standalone limits backend and quota-client contract

Quota windows — your 5-hour limit, your weekly limits, whatever your plan gains
next — are served by a backend that is deliberately **separate from the rest of
this tool**. Two server surfaces ship today:

| surface | status / command | needs a database? | needs the other? |
|---|---|---|---|
| the full dashboard | **shipped:** `codex-claude-usage dashboard` | yes | no |
| the quota-only page and limits API | **shipped:** `python -m codex_claude_usage.limits_server` (installed) or `python limits_server.py` (checkout) | **no** | no |

The separation is not cosmetic. `codex_claude_usage/limits_server.py` imports neither
`dashboard` nor `db`, which is asserted by a test in a child process rather than
claimed — importing the dashboard would pull in the scanner, the database
layer, both transcript parsers and the payload assembler, and "run the small
backend alone" would be a fiction. Its only HTTP-policy dependency is the small
`loopback_http.py` module shared with the dashboard so the two security
boundaries cannot drift.

---

## Running each surface

### The quota page and limits API on their own

```
python -m codex_claude_usage.limits_server             # installed, 127.0.0.1:8081
python -m codex_claude_usage.limits_server --port 9100 # or set LIMITS_PORT
python limits_server.py                           # source-checkout equivalent
```

It prints one authenticated URL to **stdout** and everything else to stderr, so
a launcher can take the first line. The token is random by default and may be
pinned with `CODEX_CLAUDE_USAGE_API_TOKEN` as described under deployment:

```
http://127.0.0.1:8081/#token=Qk9…
```

Open that URL for the built-in quota page. It reads the token once from the URL
fragment, clears the address bar, and sends it only in the API header. The
server reads `~/.claude.json` for quota and
`~/.claude/limit-thresholds.json` for alert settings. It never opens `usage.db`
or scans a transcript. By default it makes no network request; the explicitly
enabled live-limits mode described below is the only exception.

### The full dashboard

```
codex-claude-usage dashboard
```

Unchanged. It serves the same limits API under its own port and token, and
reads and writes the **same threshold file** — so a threshold set in either
place governs both.

### The quota-only front end

The server ships its own compact front end: live window cards, reading age,
reset time, orphaned settings, and per-window threshold editing. It polls
without overlapping requests and PATCHes only the edited opaque window key.
It contains no database views, transcript scanning, cost calculations, or
Chart.js runtime.

---

## The API

The `/` and `/index.html` page shell and JSON `/healthz` response are
unauthenticated and carry no quota data or token on this standalone limits
surface. (The full dashboard's extension-launched `/healthz` is separately
challenge-gated: the extension sends a fresh query challenge and verifies the
HMAC `instance` response using a child-only health secret.) Every `/api/*` route needs the
bearer token in `X-Codex-Claude-Usage-Token`. Requests are refused unless `Host` names this loopback
service; an explicit Host port must equal the port this process actually bound.
A cross-origin `Origin` is rejected against that same authority. An **absent**
`Origin` is allowed on purpose — `curl` and native front ends send none, and a
hostile page cannot suppress its own.

### `GET /` or `/index.html` — unauthenticated shell, carries no data

The response is a single immutable document assembled when the process starts.
Each request gets a fresh CSP nonce plus `Cache-Control: no-store`, a
no-referrer policy, restrictive permissions policy, `nosniff`, and same-origin
opener isolation. Quota data remains behind the authenticated JSON API.

### `GET /healthz` — unauthenticated, carries no data

```json
{ "status": "ok", "version": "1.0" }
```

This `version` is the standalone limits-service contract version, not the
`codex-claude-usage` release number.

### `GET /api/limits`

The values in this example are invented.

```json
{
  "available": true,
  "reason": null,
  "plan_type": "claude_max",
  "fetched_at_ms": 1767225600000,
  "age_seconds": 60,
  "windows": [
    {
      "kind": "session", "group": "session", "scope": "",
      "percent": 80, "severity": "warning",
      "resets_at": "2026-01-01T05:00:00Z",
      "is_active": true, "expired": false,
      "key": "claude:session",
      "label": "Session (5-hour)",
      "thresholds": [30]
    }
  ],
  "orphaned": { "claude:weekly": [4] },
  "source": "claude",
  "reading": "cache",
  "version": "1.0"
}
```

Three fields carry the design:

- **`key`** — the window's opaque identity. Built from the window's own kind,
  scope and, when needed, a privacy-preserving discriminator, never from a list
  of known limits. **Never derive or parse this yourself**: a second definition
  of identity is how a threshold set on one surface silently governs a
  different window on another.
- **`thresholds`** — the percentages configured *for that window*.
- **`orphaned`** — thresholds whose window the cache is **not currently
  reporting**. Show these; see *A stale cache* below.
- **`source` / `reading`** — which assistant owns the window and whether this
  response came from the local cache or the optional live request.

`age_seconds` is how old the reading is. Interpret it together with `reading`:
a cache age describes staleness, while a live age describes the fresh response.

### `GET /api/limits/thresholds`

```json
{ "thresholds": { "claude:session": [30], "claude:weekly": [4] } }
```

### `PATCH /api/limits/thresholds`

Merges only the supplied window keys and returns the complete stored map. Use
this for ordinary edits: the server holds one read-modify-write lock across
threads and processes, so two front ends changing different windows cannot
overwrite each other. Accepts either shape:

```json
{ "thresholds": { "claude:session": [30] } }
{ "claude:session": [30] }
```

### `PUT /api/limits/thresholds`

Replaces the whole **stored** mapping. This remains for compatibility and for a
deliberate complete reset; a normal one-window editor should PATCH instead.
Accepts either shape:

```json
{ "thresholds": { "claude:session": [30] } }
{ "claude:session": [30] }
```

Responds with what was **stored**, not what was sent — every value is
revalidated, so trust the response over your own request.

Values are whole percentages 1–100. A missing live-window key uses the shipped
default `[80]`; an explicit empty list `[]` disables alerts for that window.
Whole-map **PUT** is compatibility-tolerant: invalid keys or values are dropped
and the remaining valid map is stored. Per-window **PATCH** is strict: if any
supplied update is invalid, the entire request is rejected with 400 and the file
is left unchanged, so the server never reports success for an edit it skipped.

A missing or streamed body, malformed JSON, and any top-level value other than
an object are rejected without touching the file. A refused or failed write is
a 500, never a successful response for a setting that was not persisted.

---

## Window keys

```
claude:session                  the 5-hour window
claude:weekly                   a weekly all-models window
claude:weekly_scoped:Fable      a weekly window scoped to one model
codex:weekly                    Codex's weekly window
```

Keys begin with `<source>:<kind>`, but the remaining components are an
implementation detail. They can include a hash-derived discriminator when two
upstream windows have the same visible scope. The source is part of the key
because both assistants publish a `weekly` and **they are not the same
window**. Treat the complete value as opaque.

A kind nobody has seen — say `monthly_burst` scoped to Sonnet — becomes
`claude:monthly_burst:Sonnet` and gets a label, a control and alerting with no
code change. That is the point, and a test fails if the key function ever names
a specific kind.

---

## A stale cache, and why a limit can be missing

**This is the single most confusing thing about quota data, so read this before
filing a bug that a limit is not showing.**

Claude Code keeps its quota in `~/.claude.json` under
`cachedUsageUtilization`, and refreshes it periodically. In its default mode
this tool **only reads that cache**. So:

- The percentages are as old as `age_seconds` says.
- A window that has already reset keeps reporting its old percentage until
  Claude Code refreshes. That is why `expired` exists.
- **The LIST of windows is as stale as the numbers.** A limit your plan gained
  after the last refresh is simply absent, and nothing downstream can invent it.

A changing account file does not imply a fresh quota block. The dashboard
shows the quota reading age and warns when the cache may omit windows.

If a limit is missing: open Claude Code, let it refresh, and reload, or enable
the deliberate live mode below.

---

## Live limits (optional)

**Off by default.** Everything else in this tool reads files on your machine.
This is the one thing that talks to the network. The recommended setup is
explicit and re-resolves the short-lived credential before every query:

```bash
source scripts/live-limits-env.sh
```

That script sets the network opt-in and a `CODEX_CLAUDE_USAGE_TOKEN_COMMAND`. You can
instead supply a static token manually, though it expires quickly:

```bash
export CODEX_CLAUDE_USAGE_OAUTH_TOKEN="…"     # you supply it
export CODEX_CLAUDE_USAGE_LIVE_LIMITS=1       # and you ask for it
```

The live request is also gated by the account's subscription authentication
mode. An API-key install does not contact the subscription-usage endpoint even
when both environment variables are present.

**Why you would want it.** The quota block can stay stale even when other
parts of the local account file change. A live query can show a window that
has not been written to that cache.

**Where the credential comes from.** Either directly from
`CODEX_CLAUDE_USAGE_OAUTH_TOKEN`, or from the command you explicitly place in
`CODEX_CLAUDE_USAGE_TOKEN_COMMAND`. A valid command result wins when
both are set because Claude Code's access token is short-lived; if the command
fails, times out, emits an invalid/empty token, or overflows the bounded first
nonblank token-line prefix, the static environment token remains the fallback.
Command stderr is discarded and stdout is inspected only through that prefix.
Cleanup terminates the private POSIX process group and descendants that remain
in it; on Windows, a gated wrapper is assigned before launch to a kill-on-close
Job Object containing the launched tree. The helper script configures that command
to read Claude Code's credential from the macOS login Keychain (service
`Claude Code-credentials`) or the known Linux credentials file. The tool never
consults either on its own; sourcing the script or setting the command is the
authorization. The credential itself stays out of the environment in command
mode.

The token is sent only in an `Authorization: Bearer` header, over HTTPS, and
never appears in a URL, the payload, the database, the browser or a log. Redirects
are refused rather than followed, so the bearer is never forwarded to another
origin. An `http://` endpoint override is **refused** rather than upgraded,
because sending a credential in clear text is worse than not answering.

**The endpoint** defaults to `https://api.anthropic.com/api/oauth/usage`, read
out of the Claude Code VS Code extension's own bundle rather than guessed — a
fact about one version, not a contract, which is why `CODEX_CLAUDE_USAGE_LIMITS_URL`
can override it.

**Every failure falls back to the cache**: offline, refused, expired token, a
non-200, malformed JSON, an oversized body, a total deadline exceeded anywhere
across DNS, response headers, or body, or a response whose shape this build does
not recognise. That last one matters — an undocumented endpoint can change, and
the answer is the cache rather than nonsense rendered confidently. Network work
runs through one daemon-owned single-flight operation per process. Concurrent
callers share it; if an endpoint is permanently wedged, later polls do not grow
an unbounded population of stuck server threads or sockets. Restarting the
process recovers live polling, while cache-backed quota remains available. The
single-flight identity retains only a SHA-256 digest of the bearer plus the
URL/opener identity. The caller removes `Authorization` from the shared request
before every return, including an absolute-deadline fallback while the daemon
worker is still blocked; a worker that has not sent its headers yet may then fail
unauthenticated. When the worker completes, its raw request closure is dropped too.

`/api/limits` carries **`reading: "live"` or `"cache"`** so a front end can say
which it got. The two ages mean opposite things about whether the *list* of
windows can be trusted.

Under Homebrew the shim's allowlist deliberately **withholds both credential
sources** — a token or token-reading command crossing that boundary silently is
one nobody decided to hand over. Source the helper and run the module directly
if you want live limits there.

---

## Thresholds

Stored in `~/.claude/limit-thresholds.json`:

```json
{ "claude:session": [30, 80], "claude:weekly": [4] }
```

On disk rather than in a browser, and that is what makes them **shared**: both
front ends read one file, and clearing site data cannot silently disarm your
alerts.

Every reported window defaults to an 80% alert until it has a stored entry.
Store `[]` for a window to disable that default. On POSIX, writes use an
owner-only (`0600`) temporary file; every platform uses `fsync` and atomic
replacement, so readers in either process see the old
complete map or the new complete map, never a partial document. Merely reading
a pre-existing file does not change its mode.
Per-window PATCH operations additionally hold a native advisory lock (whose
sidecar is owner-only on POSIX) across the read and replacement, so separate server processes cannot both
merge from the same stale map.
Symlinks, hard links, non-regular files and oversized files are refused.

**A threshold outlives its window on purpose.** When a window vanishes from a
stale cache its threshold is kept and reported under `orphaned`, because
dropping it would disarm an alert at exactly the moment you cannot notice.
Ordinary legacy identities are already byte-for-byte current and reattach
exactly. A changed legacy alias is used as read-only compatibility only when it
maps unambiguously among the windows currently reported; an ambiguous legacy
collision stays orphaned until the user saves the intended window. The alias is
deliberately not persisted because a stale quota cache may omit the colliding
window and cannot prove durable ownership.

Override the location with `CODEX_CLAUDE_USAGE_THRESHOLDS`.

---

## Deployment

### Both surfaces on one machine

```
codex-claude-usage dashboard &                 # 8080
python -m codex_claude_usage.limits_server &   # 8081
```

Independent processes, one shared threshold file. Stop either without affecting
the other. Both servers accept `127.0.0.1`, `localhost`, or IPv6 `::1`; generated
URLs for the IPv6 literal use `[::1]`. CLI ports are validated as integers from
1 through 65535 before binding, and an explicit Host port must match the bound
port.

### A fixed token for both

Both mint a random token per process by default. To pin one — useful for a
separate native client that must reconnect across restarts:

```
export CODEX_CLAUDE_USAGE_API_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
python -m codex_claude_usage.limits_server
```

Must match `[A-Za-z0-9_-]{32,128}`; anything else is ignored and a random token
is used instead. **Pass it by environment, never on a command line** — argv is
world-readable on Linux.

### As a background service (launchd / systemd)

The server writes no pid file and needs no working directory. A minimal user
unit:

```ini
[Service]
Environment=CODEX_CLAUDE_USAGE_API_TOKEN=…
Environment=LIMITS_PORT=8081
ExecStart=/usr/bin/env python3 -m codex_claude_usage.limits_server
Restart=on-failure
```

### Docker

The image ships both modules. The quota service binds loopback deliberately, so
the direct single-container form uses host networking rather than `-p` (a
published port cannot reach a service bound to the container's own loopback).
On Linux Docker Engine — or Docker Desktop with host networking enabled — an
actionable invocation is:

```bash
data_dir="${CODEX_CLAUDE_USAGE_DOCKER_LIMITS_DATA_DIR:-$HOME/.local/share/codex-claude-usage-limits}"
mkdir -p "$data_dir"
chmod 700 "$data_dir"
docker run --rm --network host --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$HOME/.claude.json,dst=/home/codexclaudeusage/.claude.json,readonly" \
  --mount "type=bind,src=$data_dir,dst=/data" \
  codex-claude-usage python3 -m codex_claude_usage.limits_server \
  --host 127.0.0.1 --port 8081
```

The config mount is read-only and `/data` is the only writable application
path. The image sets
`CODEX_CLAUDE_USAGE_THRESHOLDS=/data/limit-thresholds.json`, so the second mount
persists settings while the application filesystem stays read-only. Default
mode makes no outbound request; explicitly enabled live limits inherit host
network access and should be treated accordingly.

### What it will refuse

- **Binding off loopback.** `--host 0.0.0.0` is refused, not quietly rewritten:
  the API serves quota and accepts settings writes with no user account behind
  them, so it must never be reachable off the machine.
- **A threshold file that is a symlink, a hard link or a FIFO.** The open
  descriptor is interrogated before a byte is read or a replacement is made.
- **An oversized threshold file or request body**, before parsing it.
- **A request line, headers, or PUT/PATCH body that exceed the total read
  budget**, even when a client keeps sending bytes inside the per-receive socket
  timeout. The inbound watchdog stops once all request input is consumed, so it
  does not cut off a legitimate long-running rescan response. The optional live
  upstream has its own absolute DNS/header/body deadline and cache fallback.

---

## The shipped quota-only front end

The implementation lives in `web/limits/` and is assembled by
`codex_claude_usage/limits_web.py`. It makes the same two API calls a separate client
would need:

```js
const H = { 'X-Codex-Claude-Usage-Token': TOKEN };

const limits = await (await fetch('/api/limits', { headers: H })).json();
for (const w of limits.windows) {
  render(w.label, w.percent, w.thresholds);        // never rebuild w.key
}
for (const [key, list] of Object.entries(limits.orphaned)) {
  renderConfiguredButAbsent(key, list);            // do not hide these
}

await fetch('/api/limits/thresholds', {
  method: 'PATCH', headers: { ...H, 'Content-Type': 'application/json' },
  body: JSON.stringify({ thresholds: { 'claude:weekly': [4] } }),
});
```

The shipped page enforces these rules, and another client should preserve them:

1. **Use `key` as given.** Deriving your own is the one mistake that produces
   settings which appear to work and govern the wrong limit.
2. **Show `orphaned`.** A configured window the cache has stopped reporting is
   the common case, not an edge case.
3. **A first sighting is a baseline.** Announcing every threshold below the
   current percentage on load means opening at 91% fires everything. Record the
   reading, alert on the *crossing*.
4. **PATCH only the window being edited.** The backend serializes that merge
   across threads and processes. Reserve whole-map PUT for an intentional reset;
   a stale browser mirror is not an authoritative replacement.
5. **Show `reading` and age together.** A cache can be stale enough that its
   list of windows is incomplete; a live response has different semantics.

The browser also clears the fragment immediately, stores the token in no
cookie, storage API, query parameter, or HTML configuration, and writes server
values through DOM text properties rather than HTML interpolation. Threshold
crossings establish a baseline on first sight, fire once when crossed, allow a
rolled-over window to cross from zero, and suppress expired or orphaned
windows. A failed save re-fetches the authoritative map instead of leaving a
successful-looking local state.
