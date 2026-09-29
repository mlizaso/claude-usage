# Security policy

## Supported versions

Security fixes target the current `main` branch and the latest release.
Older releases may lack subsequent hardening. There is no separate long-term
support branch or guaranteed response time.

## Reporting a vulnerability

Use GitHub's **Security → Advisories → Report a vulnerability** when private
reporting is enabled for this repository:

[Open a private security report](https://github.com/mlizaso/claude-usage/security/advisories/new)

If that option is unavailable, open a minimal issue asking the maintainer for
a private reporting channel. Do not include the vulnerability details, exploit,
credentials, transcripts or private usage data in that public issue.

Provide affected versions, the attack prerequisites, impact and a synthetic
reproduction through the private channel. Never test against another person's
dashboard or data without permission.

## Deployment boundary

This is a local, single-user tool. Its HTTP servers bind to loopback and require
authentication for usage and quota data. A bearer token is not a multi-user
access-control system. Public network hosting and arbitrary reverse proxies
are outside the supported deployment model.

Transcripts and container archives are untrusted inputs. The implementation
uses bounded reads, file/path checks, archive limits, HTML/terminal escaping,
Host/Origin validation and a restrictive Content Security Policy.
These protections do not make stored usage anonymous; see [Privacy](PRIVACY.md).

## Maintainer release checks

Use synthetic fixtures, verify the bundled Chart.js checksum, run the native
Python/extension checks and inspect the actual distribution contents. Keep
GitHub Actions on read-only tokens except the final release job. Enable private
vulnerability reporting and secret scanning when configuring the public repository.
