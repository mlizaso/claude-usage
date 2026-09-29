# Hosted and cross-platform delivery — design study

Status: investigated, not implemented. This repository ships a local Python
application and VS Code extension, not a multi-user hosted service.

## Current platform boundaries

Python 3.11+ is required. User directories resolve through `Path.home()`.
The CI matrix covers Linux, macOS and Windows; consult the actual run for a
tested revision. POSIX ownership/mode checks and timezone controls are not
portable to every Windows filesystem, and some tests skip where capabilities
are unavailable. A collected test is not necessarily an executed test.

The extension runs as a local UI extension, including when a workspace is
remote. It reads the local machine's logs. The Bash Docker launcher is documented
for macOS/Linux; its Docker API collection layer also has Windows support.

## A future hosted implementation

A hosted page cannot silently read local Claude Code or Codex directories.
User-selected file import or a local companion would need a separate design.
Browser processing would also require replacing the Python parser and SQLite
runtime, handling large inputs incrementally and defining storage lifetime.

Uploading transcripts to a server introduces account authentication, tenant
isolation, retention, deletion and breach-response requirements. Prompt and
response bodies may contain credentials or confidential work. A hosted
implementation should minimize what it accepts and make transfers explicit.

For current use, keep the dashboard on loopback. Publishing this repository
does not turn the application into a safe public network service.
