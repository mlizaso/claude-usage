# Contributing

Bug reports, documentation fixes and focused pull requests are welcome.
Keep examples synthetic. Do not attach your usage database, transcripts,
Docker cache, saved dashboard URL, account files or unredacted screenshots.
See [Privacy](PRIVACY.md) and [Security](SECURITY.md).

## Development

Use Python 3.11+ and a local checkout. The runtime needs no third-party Python
packages. Node is needed for JavaScript tests; Chrome or Chromium enables the
rendered browser checks. The extension CI uses Node 24 and its locked dependencies.

```bash
git clone https://github.com/mlizaso/codex-claude-usage.git
cd codex-claude-usage
python3 -m unittest discover -s tests -t . -v
python3 scripts/generate-pricing-assets.py --check
```

On Windows use `python`. Keep `-t .`: it activates the test package's guard
against accessing real usage data. Tests create temporary synthetic fixtures;
never point a test at your own account or transcript directory.

For VS Code extension changes, run from `vscode-extension/`:

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm audit signatures
npm audit --ignore-scripts --audit-level=low
npm run compile
npm run typecheck:test
npm test
npm run package
```

The package command regenerates the Python bundle before creating the VSIX.
Do not edit generated `python/` or `out/` files. No Python formatter is
configured; follow the surrounding style and run `git diff --check`.

## Changes and review

- Keep a change focused and explain the failing scenario or intended behavior.
- Add regression tests for behavior changes, using invented data and IDs.
- Preserve Windows-compatible paths and explicit UTF-8 in text I/O.
- Run relevant tests and report any skipped browser or platform checks.
- Update canonical documentation in `docs/`, not a second copy at a symlink path.
- Preserve license notices and original contributor credit.
- Use concise Conventional Commit subjects, for example
  `fix(scanner): retain model context across appended records`.

See [AGENTS.md](AGENTS.md) for architecture and packaging invariants.
Contributions are made under the repository's MIT license. Contribute only
material you have the right to share under that license.

## Bug reports

Include your OS, Python version, application version, install method, expected
result, actual result and a minimal synthetic reproduction. Remove credentials,
private repository names, local usernames, project titles and URLs with tokens.
Report security issues through the private route in [Security](SECURITY.md).
