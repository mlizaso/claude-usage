# Repository guide for coding agents

Guidance for any coding agent working on this repository. "Claude Code" and
"Codex" refer to the products whose transcripts this application reads.

## Project shape

Python 3.11+, standard library only at runtime. Implementations live in
`claude_usage/`; root Python modules are exact-module compatibility aliases.
Keep mutable globals and patches shared between those import paths.

| Area | Modules |
|---|---|
| Commands and terminal reports | `cli.py`, `reports.py` |
| Parsing and discovery | `transcripts.py`, `codex_transcripts.py`, `scanner.py`, `docker_sources.py` |
| Storage and query projections | `db.py`, `rollups.py`, `dashboard_data.py`, `dashboard_cache.py` |
| Pricing and time | `pricing.py`, `timestamps.py`, `localdays.py` |
| Quotas | `account.py`, `live_limits.py`, `limits_core.py` |
| HTTP surfaces | `dashboard.py`, `limits_server.py`, `loopback_http.py`, `limits_web.py` |
| Shared safety and assets | `safefile.py`, `safetext.py`, `safejson.py`, `assets.py` |

The standalone limits server must remain independent of the scanner and database.
The Docker-only `proxy.py` stays outside the installable package.

### Browser assets and packaging

The dashboard concatenates `web/js/` in filename order: `00-core`, `10-pricing`,
`20-format`, `30-ranges`, `40-filters`, `50-render`, `52-charts`, `54-tables`,
`56-plan`, `58-alerts`, `60-export`, `70-bootstrap`. These are classic scripts;
load order matters. CSS and JS are inserted into `web/index.html` at import.
Keep the single-document delivery and fresh response CSP nonce intact.

New package modules require entries in `.dockerignore` and
`vscode-extension/scripts/copy-python.js`. New web/vendor assets also require
`pyproject.toml` data-file entries. Docker uses a deny-all context allowlist;
do not replace explicit paths with broad directory exceptions. Preserve the
Chart.js checksum and license in every distribution.

Maintained Markdown belongs in `docs/`. Conventional root names and
`vscode-extension/README.md` are symlinks, not duplicate sources. The vendored
Chart.js license remains beside its code. Update the README documentation map
and `tests/test_documentation_layout.py` whenever adding a document.

## Architecture

### SQLite schema

`db.SCHEMA_SQL` is the complete declaration. Query it for current columns and
indexes instead of keeping numeric counts in prose.

- **`turns`** — source of truth for per-response usage and model attribution.
- **`sessions`** — source-qualified session metadata and reconciled totals.
- **`processed_files`** — hashed path, file identity, metadata and prefix cursor.
- **`agents`** — source-qualified subagent dispatch and completion metadata.
- **`limit_events`** — transcript notices, with sortable timestamp keys.
- **`usage_limits_snapshots`** — quota-window observations and their ordering keys.

A conditional unique index deduplicates nonempty message IDs within a source.
The database is derived: schema mismatch triggers a guarded rebuild rather
than a chain of migrations. Verify ownership before rebuilding; a file with
unrelated tables is not ours. Keep rebuild and recovery transactions serialized
across processes. Explain that pruned transcripts cannot be restored by a rescan.

### Parsing and accounting invariants

1. Claude streaming records sharing a message ID describe one response.
   Retain the final usage rather than summing intermediate tallies.
2. Recompute session totals from stored turns after every scan. Deduplicated
   inserts and repaired streaming usage must not leave additive totals stale.
3. Use the same parser for full and incremental reads. Verify file identity,
   metadata and the complete raw prefix before resuming a cursor, and recheck
   before stamping it. A partial read is not a successful end-of-file.
4. Compare timestamps as UTC instants through `timestamps.py`; preserve raw
   values for display and local-calendar bucketing through `localdays.py`.
   Keep deterministic fallbacks for malformed timestamps.
5. Store hashed file identifiers, never absolute transcript paths. Session,
   message and agent identities must include their source where required.
6. Attribute Codex replayed responses to their producer, independently of
   discovery order. Preserve lineage and model/effort state on incremental reads.
7. Cached and cache-write Codex input are disjoint subsets of total input;
   reasoning tokens remain a subset of output. Never double-count either.
8. Use per-turn rates, dates, cache TTL and long-context tier before summing.
   Keep Python and generated JavaScript pricing in parity. Unknown rates are
   distinct from free usage. Honor explicit local overrides.
9. Subagent figures are subsets of overall usage. Mixed-model projects and
   sessions need per-model arithmetic, not a session's primary-model rate.
10. A cached quota can outlive its window. Show age and expiry. Use opaque
    window keys and lock the complete threshold read-modify-write transaction.

### Privacy and safety

- Keep account identifiers, prompt/response bodies and credentials out of the
  database and API. Session titles, projects, branches and usage are still private.
- Docker import caches hold full JSONL transcripts. Bound archive members,
  size, time and workers; refuse links, special files and remote daemons.
- Use `safefile.py` for descriptor-backed regular-file reads. Preserve each
  caller's confinement, ownership, symlink and hard-link policy.
- Keep token-bearing files private and replace them atomically. Validate
  ownership before replacing or deleting another process's saved URL.
- Bind HTTP servers to loopback. Preserve Host/Origin checks, bearer auth,
  connection limits, watchdogs, bounded JSON bodies and security headers.
- The extension verifies its own child with a stdout record and HMAC health
  proof. Rescan uses separate one-shot authority. Never probe an unknown peer
  with the reusable API bearer.
- Live quotas require explicit network opt-in and a credential source. Tokens
  must stay out of URLs, logs, browser payloads and persisted data.
- Treat JSONL files and metadata as untrusted. Bound text and numeric values;
  escape terminal, HTML and CSV output using the existing helpers.

## Validation

Run the stack in the checkout being changed. This standalone project uses the
host Python/Node toolchain; Docker deployment tests use controlled fixtures.
Never scan the maintainer's real home directory to validate a change.

```bash
python3 -m unittest discover -s tests -t . -v
python3 scripts/generate-pricing-assets.py --check
```

`-t .` is required: importing `tests/__init__.py` installs the real-data guard.
Keep all fixtures synthetic and temporary. Every test text read/write and
text subprocess must use explicit UTF-8. Python child processes also need
`PYTHONIOENCODING=utf-8`. Do not hide encoding errors.

Use the commands in [Contributing](CONTRIBUTING.md) for extension validation.
Inspect `.github/workflows/` for native CI gates. Browser and platform skips
must be stated; a local macOS run does not establish Windows support.
Python has no repository formatter configuration. Preserve nearby style.

When changing a pricing policy, regenerate its browser and README projections.
When changing packaging, run both asset-layout and packaging-security tests.
After a rename, search the whole repository for stale references. Validate
claims from code or executable fixtures; do not publish personal usage measurements.

## Commits and contributions

Use short Conventional Commit subjects, matching recent history. Keep changes
with their regression tests. Preserve contributor attribution and license
notices; never invent authorship or claim a human reviewed text when they did not.
Use public noreply email addresses for public commits when available.
Do not post messages, publish packages or rewrite remote history without authorization.

## Versioning and releases

`docs/CHANGELOG.md` is the canonical version reference. Keep its first
`## vX.Y.Z` heading, `scanner.VERSION`, and the extension package version in sync.
A `TBD` heading is unfinished; a dated `## vX.Y.Z — YYYY-MM-DD` heading permits
the release workflow to act. Do not date an unfinished release.

The release workflow runs from `main`, waits for the applicable Python and
extension checks, builds the VSIX under a read-only token, then passes reviewed
artifacts to a separate job with write permission. Preserve that boundary.
An existing tag at a different commit must fail rather than be moved silently.

The extension's `private: true` prevents accidental npm publication. It does
not require a private GitHub repository. Marketplace and Open VSX publication
are separate release decisions. The Homebrew formula intentionally uses `--HEAD`.
