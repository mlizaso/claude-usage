#!/usr/bin/env python3
"""Check the actual history being published, without reading local usage data."""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parent.parent
# The reviewed, sanitized upstream import. An unrelated private history must
# never enter the public repository, including through a merge or an old tag.
PUBLIC_ROOT = "2c0ae319a38f250f55d05b07655cd8c75a3c4d07"
PRIVATE_DIRECTORIES = {
    ".claude", ".codex", ".agents", ".vscode", ".idea", ".ssh", ".aws",
}
PRIVATE_NAMES = {
    ".ds_store", "thumbs.db", "desktop.ini", "auth.json", "credentials.json",
    ".npmrc", ".pypirc", ".netrc", "_netrc", ".git-credentials",
}
PRIVATE_SUFFIXES = {
    ".jsonl", ".csv", ".log", ".pem", ".key", ".p12", ".pfx", ".vsix",
}


def is_private_path(name):
    path = PurePosixPath(name.lower())
    leaf = path.name
    return bool(
        set(path.parts) & PRIVATE_DIRECTORIES
        or any(part.endswith(".docker-transcripts") for part in path.parts)
        or leaf in PRIVATE_NAMES
        or leaf.startswith((".claude.json", "dashboard-url", "limit-thresholds.json"))
        or (leaf.startswith(".env") and leaf not in {".env.example", ".env.sample"})
        or path.suffix in PRIVATE_SUFFIXES
        or re.search(r"\.(?:db|sqlite3?)(?:-.*)?$", leaf)
        or leaf.endswith((".dashboard-sources.json", ".dashboard-claude.json",
                          ".dashboard-codex.json"))
        or leaf.startswith(".dashboard-snapshot-")
    )


def git(repository, *args):
    return subprocess.check_output(["git", "-C", str(repository), *args])


def private_history_paths(repository, revision):
    """Include deleted files and every intermediate tree, not just HEAD."""
    found = set()
    for commit in git(repository, "rev-list", revision).decode("ascii").splitlines():
        names = git(repository, "ls-tree", "-r", "--name-only", "-z", commit)
        for raw in names.split(b"\0"):
            name = os.fsdecode(raw)
            if raw and is_private_path(name):
                found.add(name)
    return sorted(found)


def check_history(repository, revision):
    roots = git(repository, "rev-list", "--max-parents=0", revision).decode("ascii").splitlines()
    if roots != [PUBLIC_ROOT]:
        raise ValueError("History contains an unreviewed root; do not publish private backup refs.")
    paths = private_history_paths(repository, revision)
    if paths:
        # File contents and potentially personal filenames stay out of CI logs.
        raise ValueError(f"History contains {len(paths)} private file path(s). Inspect the commits locally.")


def find_scanner(repository):
    executable = shutil.which("gitleaks")
    if executable:
        return executable
    common = Path(os.fsdecode(git(repository, "rev-parse", "--git-common-dir")).strip())
    if not common.is_absolute():
        common = repository / common
    cached = common / "tools" / ("gitleaks.exe" if os.name == "nt" else "gitleaks")
    if cached.is_file():
        return str(cached)
    raise ValueError("Gitleaks is required before publication; install it and retry.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision", nargs="?", default="HEAD")
    args = parser.parse_args(argv)
    try:
        # Only a verified object ID reaches git log options or the scanner.
        revision = git(ROOT, "rev-parse", "--verify", "--end-of-options",
                       args.revision + "^{commit}").decode("ascii").strip()
        check_history(ROOT, revision)
        scanner = find_scanner(ROOT)
        result = subprocess.run([
            scanner, "git", "--log-opts=" + revision, "--redact", "--no-banner",
            "--ignore-gitleaks-allow", "--config", str(ROOT / ".gitleaks.toml"), str(ROOT),
        ], check=False)
        if result.returncode:
            return result.returncode
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Publication blocked: {error}", file=sys.stderr)
        return 1
    print("Publication checks passed: reviewed history, no private paths or detected secrets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
