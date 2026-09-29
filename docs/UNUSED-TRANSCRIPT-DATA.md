# Transcript-field audit: shipped, declined, and still unused

This is a design reference, not an open backlog. It describes field handling
without publishing observations from a user's transcripts.

## Used by the application

| Field family | Purpose |
|---|---|
| Token usage and model | Per-turn accounting and API-equivalent estimates |
| Timestamps and session identity | Chronology, daily rollups and deduplication |
| Working directory and branch | Short project labels and branch attribution |
| Explicit session titles | Session table; never derived from the first prompt |
| Agent metadata | Subagent attribution and completion information |
| Reasoning effort and reasoning tokens | Effort breakdown and output subset |
| Stop reason and API errors | Response endings and incident views |
| Quota events | Recorded window history and alert state |

## Deliberately omitted or not implemented

Prompt/response bodies, file contents, command output, attachments, credentials
and identifying account fields do not belong in the usage database. Full
transcripts may still exist in the private Docker import cache; see [Privacy](PRIVACY.md).

Per-command timings, file-edit counts, approval policies, personality and context
pressure are not promised dashboard features. A new field needs a concrete
user-facing use, a supported input contract, privacy review and synthetic tests.
Do not assume a field is constant or absent because of a single person's logs.
