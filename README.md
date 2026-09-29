# Claude Code Usage Dashboard

This commit imports the upstream v1.5.5 starting point. Subsequent commits in
this repository add Codex support, local Docker collection, privacy controls
and the maintained public documentation.

The original implementation reads local Claude Code JSONL usage logs and offers
terminal reports and a browser dashboard. It uses the Python standard library.

```sh
python3 cli.py --help
python3 -m unittest discover -s tests -t . -v
```

Use the final commit of this repository for the maintained application.

## Origin and license

Based on [phuryn/claude-usage](https://github.com/phuryn/claude-usage),
upstream commit `0c5c1c2c51d010ede658db7b0a9c6e9da83eef69`.
The upstream MIT notice and contributor credits are retained in
[LICENSE](LICENSE) and [CHANGELOG.md](CHANGELOG.md).

This is a sanitized import: local triage automation, historical screenshots
and image metadata have been removed, and introductory documentation shortened.
The upstream runtime implementation is unchanged in this import.
