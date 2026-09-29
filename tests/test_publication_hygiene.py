"""Prevent accidental publication of local data and image metadata."""

import json
import os
import struct
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PRIVATE_EXAMPLES = (
    ".env", ".env.local", ".env.production", "nested/.env",
    "usage.db", "usage.db-wal", "usage.db-shm", "cache.sqlite3",
    "nested/rollout.jsonl", "export.csv", "dashboard-url",
    "dashboard-url.lock", "limit-thresholds.json", "private.pem",
    "private.key", ".DS_Store", "web/.DS_Store", "debug.log",
    ".claude/settings.local.json", ".codex/auth.json",
    ".agents/local.json", ".vscode/settings.json",
    "usage.db.docker-transcripts/container/session.jsonl",
    "usage.db.dashboard-codex.json", "extension.vsix",
)


def _git(*args, input_text=None):
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], input=input_text,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )


class TestPublicationHygiene(unittest.TestCase):
    def test_demo_ignores_inherited_data_paths_and_builds_only_invented_sessions(self):
        """A maintainer's rate/config overrides must never enter screenshots."""
        probe = textwrap.dedent('''
            import json, os, runpy, sys
            from pathlib import Path
            root, directory, forbidden = map(Path, sys.argv[1:])
            def refuse_private_paths(event, args):
                if event in {'open', 'os.listdir', 'os.scandir', 'sqlite3.connect'} and args:
                    if isinstance(args[0], (str, bytes)):
                        path = Path(os.fsdecode(args[0])).absolute()
                        if path.is_relative_to(forbidden):
                            raise AssertionError('demo accessed an inherited data path')
            sys.addaudithook(refuse_private_paths)
            demo = runpy.run_path(str(root / 'scripts/render-demo.py'))
            _, data = demo['build_payload'](directory)
            payload = json.loads(data)
            sessions = payload['sessions_all']
            print(json.dumps({
                'sessions': len(sessions),
                'ids': sorted(row['session_id'] for row in sessions),
                'projects': sorted({row['project'] for row in sessions}),
                'claude_limits': payload['subscription_limits'],
                'codex_limits': payload['codex_limits'],
                'generated_at': payload['generated_at'],
                'rates_override': os.environ.get('CLAUDE_USAGE_RATES'),
                'docker': os.environ['CLAUDE_USAGE_DOCKER'],
                'live_limits': os.environ['CLAUDE_USAGE_LIVE_LIMITS'],
            }))
        ''')
        with tempfile.TemporaryDirectory() as name:
            temporary = Path(name).resolve()
            directory = temporary / 'demo'
            directory.mkdir()
            forbidden = temporary / 'private'
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith('CLAUDE_USAGE_')}
            env.update({
                'CLAUDE_USAGE_RATES': str(forbidden / 'rates.json'),
                'CLAUDE_USAGE_DB': str(forbidden / 'usage.db'),
                'CLAUDE_USAGE_CONFIG': str(forbidden / 'account.json'),
                'CLAUDE_USAGE_PROJECTS_DIRS': str(forbidden / 'transcripts'),
                'CLAUDE_USAGE_DOCKER': '1',
                'CLAUDE_USAGE_LIVE_LIMITS': '1',
                'PYTHONIOENCODING': 'utf-8',
            })
            result = subprocess.run(
                [sys.executable, '-I', '-c', probe, str(ROOT), str(directory), str(forbidden)],
                capture_output=True, text=True, encoding='utf-8', env=env, timeout=30,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(90, payload['sessions'])
        self.assertEqual(sorted(f'demo-{day}-{model}' for day in range(30) for model in range(3)),
                         payload['ids'])
        self.assertEqual(['demo/library', 'demo/mobile-app', 'demo/website'], payload['projects'])
        self.assertEqual({}, payload['claude_limits'])
        self.assertEqual({}, payload['codex_limits'])
        self.assertEqual('2026-01-30 18:00:00', payload['generated_at'])
        self.assertIsNone(payload['rates_override'])
        self.assertEqual('0', payload['docker'])
        self.assertEqual('0', payload['live_limits'])

    def test_local_data_is_ignored_but_example_env_is_shareable(self):
        result = _git("check-ignore", "--no-index", "--stdin",
                      input_text="\n".join(PRIVATE_EXAMPLES) + "\n")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(set(PRIVATE_EXAMPLES), set(result.stdout.splitlines()))
        example = _git("check-ignore", "--no-index", ".env.example")
        self.assertEqual(1, example.returncode, example.stderr)

    def test_no_tracked_local_data_or_credentials(self):
        result = _git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
        self.assertEqual(0, result.returncode, result.stderr)
        private = []
        for name in filter(None, result.stdout.split("\0")):
            path = Path(name)
            if (set(path.parts) & {".claude", ".codex", ".agents", ".vscode", ".idea"}
                    or path.name in {".DS_Store", "dashboard-url", ".claude.json", "auth.json"}
                    or (path.name.startswith(".env") and path.name not in {".env.example", ".env.sample"})
                    or path.suffix in {".db", ".sqlite", ".sqlite3", ".jsonl", ".csv", ".pem", ".key", ".p12", ".pfx", ".vsix"}
                    or path.name.endswith((".db-wal", ".db-shm"))):
                private.append(name)
        self.assertEqual([], private, "private artifacts must not be committed")

    def test_distributed_images_have_no_exif_or_text_metadata(self):
        paths = list((ROOT / "docs").glob("*.png"))
        paths += list((ROOT / "vscode-extension/resources").glob("*.png"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(image=path.relative_to(ROOT)):
                data = path.read_bytes()
                self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
                offset = 8
                while offset < len(data):
                    size = struct.unpack(">I", data[offset:offset + 4])[0]
                    kind = data[offset + 4:offset + 8]
                    self.assertNotIn(kind, {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"})
                    offset += 12 + size
                self.assertEqual(len(data), offset)

    def test_license_notices_are_preserved_in_both_packages(self):
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 Pawel Huryn", license_text)
        self.assertEqual(license_text, (ROOT / "vscode-extension/LICENSE").read_text(encoding="utf-8"))
        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual("MIT", config["project"]["license"])
        self.assertEqual({"LICENSE", "vendor/LICENSE.chartjs.md"},
                         set(config["project"]["license-files"]))


if __name__ == "__main__":
    unittest.main()
