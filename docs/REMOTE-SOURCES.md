# Remote transcript sources — design study

Status: local Docker discovery is implemented. SSH fetching, remote enrollment
and explicit host attribution remain proposals, not available features.

## Available today

Host CLI and VS Code scans can import Claude Code and Codex logs from local
Docker containers. See [Docker support](README.md#docker) for requirements,
opt-out and cache privacy. The isolated Docker-hosted dashboard has no Docker
socket and cannot perform this discovery.

A user-managed local copy of another machine's transcripts can be passed with
`--projects-dir`. The scanner reads supported JSONL files and deduplicates known
identities. Preserve timestamps when copying to avoid unnecessary full reads.
The application does not run SSH, rsync or remote commands for you.

## Requirements for future remote support

- Define explicit host identity separately from assistant source and session ID.
- Use deliberate user configuration and existing SSH authentication; never
  store private keys or passwords in this repository or browser state.
- Keep subprocess arguments separate, refuse arbitrary remote shell fragments,
  and bound connection time, copied bytes and concurrent work.
- Store imported transcripts privately and make retention/deletion visible.
- Treat remote archives and paths as untrusted, including links and traversal.
- Report partial refreshes without discarding the last complete results.

No example here contains a real hostname, user, project or credential.
