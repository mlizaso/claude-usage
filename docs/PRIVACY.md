# Privacy and local data

The application stores data on the machine where it runs. It sends no
telemetry and makes no automatic internet request in the default configuration.
Local Docker collection talks to the local Docker daemon. Optional live
Anthropic quota lookup requires explicit opt-in and a credential source.

## What is read and stored

| Data | Location / handling |
|---|---|
| Claude Code and Codex logs | Read-only JSONL inputs from supported roots |
| Usage database | `~/.claude/usage.db`, or `CODEX_CLAUDE_USAGE_DB` |
| Startup snapshots | Beside the chosen database; private copies of dashboard payloads |
| Imported Docker logs | `<database>.docker-transcripts/`; full JSONL transcripts |
| Quota cache | Read from local account/transcript metadata; identifying account fields are discarded |
| Alert settings | `~/.claude/limit-thresholds.json`, or `CODEX_CLAUDE_USAGE_THRESHOLDS` |
| Dashboard recovery link | Beside the database; contains authentication secrets |
| CSV exports and screenshots | Wherever you save them; may contain private metadata |

The database contains token counts, model IDs, timestamps, session and agent
IDs, explicit session titles, shortened project names, branch names, tool
names and quota/error metadata. Incremental file paths are hashed. It does
not store full working-directory paths or prompt/response bodies.

**The Docker cache is different:** its imported files retain full transcript
contents, potentially including prompts, responses, commands and credentials.
POSIX cache permissions restrict access to the current user, but that does
not make the files safe to share. Cached history persists when a container
is removed or Docker becomes unavailable. Disable collection with
`CODEX_CLAUDE_USAGE_DOCKER=0` before launching if you do not want these copies.

## Network access and credentials

The dashboard and quota page use authenticated loopback HTTP. Their printed
URLs contain bearer tokens in fragments. Do not paste those URLs into issues,
messages, screenshots or shared browser bookmarks.

Live Anthropic quotas are off by default. Enabling them requires
`CODEX_CLAUDE_USAGE_LIVE_LIMITS=1` and `CODEX_CLAUDE_USAGE_OAUTH_TOKEN` or an explicitly
configured `CODEX_CLAUDE_USAGE_TOKEN_COMMAND`. The credential is used in an HTTPS
Authorization header and is kept out of the browser, database and logs.
See [the limits reference](LIMITS-BACKEND.md#live-limits-optional) for the helper
script, endpoint override and cache fallback. Installation and explicit
external links can contact package registries or other sites separately.

## Sharing and deleting data

Use invented names, IDs and usage values for bug reports. Review screenshots
visually and strip image metadata. A session title or branch can reveal
confidential work even when there are no API keys in the file.

To remove collected data, stop the dashboard and extension, then delete the
selected database, its SQLite sidecars, startup snapshots, Docker transcript
cache and saved recovery link. Remove alert settings and any exports separately
if desired. Check custom paths before deleting anything. Original provider logs
are separate and are never deleted by this tool; scanning them again rebuilds
derived history. Docker deployment stores its database under
`~/.local/share/codex-claude-usage-docker` by default.

Repository ignore rules reduce accidental commits of these files. They do not
remove files already committed and do not protect copies outside Git.
