"""Documentation ownership, compatibility entry points, and local links.

Project prose is canonical under ``docs/``. A few conventional filenames must
remain discoverable where their consumers require them, so those paths are
symlinks rather than second copies. The Chart.js license is the sole
content-bearing exception: it belongs beside the vendored code it licenses.
"""

import os
import re
import unittest
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

COMPATIBILITY_LINKS = {
    "CONTRIBUTING.md": "docs/CONTRIBUTING.md",
    "SECURITY.md": "docs/SECURITY.md",
    "PRIVACY.md": "docs/PRIVACY.md",
    "THIRD-PARTY-NOTICES.md": "docs/THIRD-PARTY-NOTICES.md",
    "PUBLICATION.md": "docs/PUBLICATION.md",
    "README.md": "docs/README.md",
    "AGENTS.md": "docs/AGENTS.md",
    "CLAUDE.md": "docs/CLAUDE.md",
    "CHANGELOG.md": "docs/CHANGELOG.md",
    "CODEX-FEASIBILITY.md": "docs/CODEX-FEASIBILITY.md",
    "HOSTED-AND-CROSS-PLATFORM.md": "docs/HOSTED-AND-CROSS-PLATFORM.md",
    "LIMITS-BACKEND.md": "docs/LIMITS-BACKEND.md",
    "REMOTE-SOURCES.md": "docs/REMOTE-SOURCES.md",
    "UNUSED-TRANSCRIPT-DATA.md": "docs/UNUSED-TRANSCRIPT-DATA.md",
    "VS-CODE-EXTENSION.md": "docs/VS-CODE-EXTENSION.md",
    "vscode-extension/README.md": "../docs/VS-CODE-EXTENSION.md",
}

COMPATIBILITY_ASSETS = {
    "screenshot.png": "docs/screenshot.png",
    "usage1.png": "docs/usage1.png",
    "usage2.png": "docs/usage2.png",
}

DOCUMENT_MARKERS = {
    "CONTRIBUTING.md": ("# Contributing",),
    "SECURITY.md": ("# Security policy",),
    "PRIVACY.md": ("# Privacy and local data",),
    "THIRD-PARTY-NOTICES.md": ("# Third-party notices",),
    "PUBLICATION.md": ("# Preparing a public repository",),
    "README.md": (
        "# Claude Code and Codex Usage Dashboard",
        "## Documentation",
    ),
    "AGENTS.md": (
        "# Repository guide for coding agents",
        "Guidance for any coding agent",
    ),
    "CLAUDE.md": (
        "All guidance lives in AGENTS.md",
        "@AGENTS.md",
    ),
    "CHANGELOG.md": ("# Changelog",),
    "CODEX-FEASIBILITY.md": (
        "# Codex usage tracking — historical feasibility report",
        "Status: historical design record.",
    ),
    "HOSTED-AND-CROSS-PLATFORM.md": (
        "# Hosted and cross-platform delivery — design study",
        "Status: investigated, not implemented.",
    ),
    "LIMITS-BACKEND.md": (
        "# Standalone limits backend and quota-client contract",
        "Two server surfaces ship today",
    ),
    "REMOTE-SOURCES.md": (
        "# Remote transcript sources — design study",
        "local Docker discovery is implemented",
        "SSH fetching",
    ),
    "UNUSED-TRANSCRIPT-DATA.md": (
        "# Transcript-field audit: shipped, declined, and still unused",
        "open backlog.",
    ),
    "VS-CODE-EXTENSION.md": (
        "# Claude Code and Codex Usage — VS Code extension",
        "It makes no external API calls and sends no telemetry",
    ),
}

_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _project_markdown():
    """Markdown sources in the checkout, excluding dependencies/build output."""
    found = set()
    for path in ROOT.rglob("*.md"):
        rel = path.relative_to(ROOT)
        if any(part in {".git", ".claude", ".codex", "node_modules"}
               for part in rel.parts):
            continue
        if rel.parts[:2] == ("vscode-extension", "python"):
            continue
        found.add(rel.as_posix())
    return found


def _broken_local_links(source, logical_parent=None):
    """Local links in ``source``, resolved from its rendered location."""
    failures = []
    text = source.read_text(encoding="utf-8")
    parent = source.parent if logical_parent is None else logical_parent
    for raw_target in _MARKDOWN_LINK.findall(text):
        target = raw_target.strip().split(maxsplit=1)[0]
        if target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        path_part = unquote(target.split("#", 1)[0])
        if not path_part:
            continue
        if not (parent / path_part).resolve().exists():
            failures.append(f"{source.relative_to(ROOT)} -> {raw_target}")
    return failures


class TestDocumentationLayout(unittest.TestCase):
    def test_every_project_document_has_one_canonical_home(self):
        expected_outside_docs = set(COMPATIBILITY_LINKS) | {
            "vendor/LICENSE.chartjs.md"
        }
        actual_outside_docs = {
            rel for rel in _project_markdown() if not rel.startswith("docs/")
        }
        self.assertEqual(expected_outside_docs, actual_outside_docs)

        actual_docs = {path.name for path in DOCS.glob("*.md")}
        self.assertEqual(set(DOCUMENT_MARKERS), actual_docs)

    def test_conventional_entry_points_are_symlinks_to_docs(self):
        for rel, target in COMPATIBILITY_LINKS.items():
            with self.subTest(path=rel):
                path = ROOT / rel
                self.assertTrue(path.is_symlink(), f"{rel} must be a symlink")
                self.assertEqual(target, os.readlink(path))
                self.assertTrue(path.resolve().is_file())
                self.assertTrue(path.resolve().is_relative_to(DOCS.resolve()))

        for rel, target in COMPATIBILITY_ASSETS.items():
            with self.subTest(asset=rel):
                path = ROOT / rel
                self.assertTrue(path.is_symlink(), f"{rel} must be a symlink")
                self.assertEqual(target, os.readlink(path))
                self.assertTrue(path.resolve().is_file())

    def test_each_document_declares_its_actual_function_and_status(self):
        for name, markers in DOCUMENT_MARKERS.items():
            text = (DOCS / name).read_text(encoding="utf-8")
            with self.subTest(document=name):
                for marker in markers:
                    self.assertIn(marker, text)

    def test_agent_guide_names_every_current_database_table(self):
        db_source = (ROOT / "claude_usage" / "db.py").read_text(
            encoding="utf-8")
        expected = set(re.findall(
            r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)", db_source
        ))

        guide = (DOCS / "AGENTS.md").read_text(encoding="utf-8")
        schema_section = guide.split("### SQLite schema", 1)[1].split(
            "\nA conditional unique index", 1
        )[0]
        documented = set(re.findall(
            r"^- \*\*`([a-z_]+)`\*\*", schema_section, re.MULTILINE
        ))

        self.assertEqual(expected, documented)

    def test_the_documentation_map_names_every_canonical_document(self):
        index = (DOCS / "README.md").read_text(encoding="utf-8")
        for name in set(DOCUMENT_MARKERS) - {"README.md"}:
            with self.subTest(document=name):
                self.assertIn(f"]({name})", index)

    def test_the_user_guide_file_map_names_every_entry(self):
        """A blank path makes the architecture map impossible to use."""
        index = (DOCS / "README.md").read_text(encoding="utf-8")
        section = index.split("\n## Files\n", 1)[1].split(
            "\n---\n\n## Documentation", 1
        )[0]
        rows = [
            line for line in section.splitlines()
            if line.startswith("|")
            and not re.fullmatch(r"\|[-| ]+\|", line)
            and not line.startswith("| File |")
        ]
        self.assertTrue(rows, "README ## Files must contain a file map")
        blank = [line for line in rows if not line.split("|", 2)[1].strip()]
        self.assertEqual([], blank, "README ## Files contains blank file names")

    def test_extension_ci_watches_the_marketplace_readme_target(self):
        workflow = (
            ROOT / ".github" / "workflows" / "extension-ci.yml"
        ).read_text(encoding="utf-8")
        watched = '- "docs/VS-CODE-EXTENSION.md"'
        self.assertEqual(
            2,
            workflow.count(watched),
            "both push and pull_request filters must watch the canonical "
            "README that VSCE follows through vscode-extension/README.md",
        )

    def test_local_markdown_links_resolve_from_the_canonical_file(self):
        failures = []
        for name in DOCUMENT_MARKERS:
            failures.extend(_broken_local_links(DOCS / name))
        self.assertEqual([], failures, "broken local Markdown links")

    def test_local_links_also_resolve_from_compatibility_paths(self):
        failures = []
        for rel in COMPATIBILITY_LINKS:
            source = ROOT / rel
            failures.extend(
                _broken_local_links(source, logical_parent=source.parent)
            )
        self.assertEqual([], failures, "broken compatibility-path links")


if __name__ == "__main__":
    unittest.main()
