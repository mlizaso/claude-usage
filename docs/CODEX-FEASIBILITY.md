# Codex usage tracking — historical feasibility report

Status: historical design record. Codex ingestion is implemented. This public
summary retains the design decisions without private transcript examples or
measurements. See [README](README.md#codex) for current usage.

## Shipped design

- A dedicated `codex_transcripts.py` parser understands rollout records.
- `session_meta` establishes identity; headerless standard filenames retain
  their UUID fallback, while arbitrary filenames use a canonical-path digest.
- Usage events become source-qualified turns. Parent history replayed by a
  child is deduplicated using lineage; producer attribution wins.
- Model and reasoning-effort context carry across incremental reads.
- Cached/cache-write input is normalized separately; reasoning output remains
  part of total output. Pricing stays in the shared pricing module.
- Quota observations are extracted from supported rate-limit events.
- The dashboard selects one assistant at a time; terminal reports can combine them.

## Limits of the format

Transcripts are a client-generated record, not a billing API. Events, field
availability and retention can change. Missing parent files and deleted logs
limit reconstruction. A filename's local wall time is not proof of chronology,
especially around daylight-saving changes.

Keep regression examples synthetic. Exercise full scans, append-only resumes,
duplicate discovery, reordered files, changed prefixes and missing metadata.
The maintained parser contract is expressed by `tests/test_codex_transcripts.py`,
`tests/test_codex_discovery_order.py` and the shared scanner suites.
