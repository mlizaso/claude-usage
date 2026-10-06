"""Tests for the single source-of-truth version (scanner.VERSION).

The runtime version lives in scanner.py because the canonical docs/CHANGELOG.md is
NOT bundled into the .vsix, so nothing at runtime can read the version out of
it. These tests keep the three places a version is written from drifting:
scanner.VERSION, the top docs/CHANGELOG.md heading, and
vscode-extension/package.json.
If you bump one, bump all. (That "three" is the version-writing places and is
still right; the count this sentence used to carry — of the Python files the
.vsix bundles — is the one that went stale, and TestBundlingRationale below now
guards this file against it coming back.)

They also pin the shape docs/CHANGELOG.md itself must keep for the release workflow
to be able to ship what is written in it: at most one undated heading, and it
must be the topmost one.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile
import unittest
from pathlib import Path

from scanner import VERSION

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tag-on-merge.yml"
COPY_PYTHON = REPO_ROOT / "vscode-extension" / "scripts" / "copy-python.js"
CHANGELOG = REPO_ROOT / "docs" / "CHANGELOG.md"

#: Any claim about how many Python files the `.vsix` ships, in digits or in
#: words. A near-copy guards scanner.py's VERSION comment in
#: tests/test_scanner.py — it reads scanner.py alone, which is how this module's
#: own copy of the claim outlived the correction there. Kept as a second small
#: copy rather than imported, because a test module importing another test
#: module for a constant is a worse coupling than two regexes that each guard
#: the file they live beside.
#:
#: It matches a count in *any* form rather than a shortlist of small numbers,
#: because the number is the part that rots and a corrector writes whichever
#: number is right that day. The first version spelled out one..ten plus digits
#: and was evaded four ways: by the word form of a number above ten, by a
#: capital letter, by a qualifier slipped between the count and the noun, and by
#: a lower-case "python". test_the_guard_catches_a_count_in_any_form pins all
#: four. Two boundaries are deliberate and pinned by the test beside it: the
#: noun stays Python files or modules, because this module legitimately counts
#: other things (the places a version is written), and a vague quantity is left
#: alone, because it is not a count and does not go stale into a falsehood.
_CARDINAL = (
    r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|"
    r"dozen|couple"
)
#: Digits, or one or more cardinal words joined by spaces or hyphens.
_A_COUNT = rf"(?:\d[\d,]*|(?:{_CARDINAL})(?:[-\s](?:{_CARDINAL}))*)"
#: Room for "root", "bundled source" and the like on either side of "Python".
_QUALIFIERS = r"(?:\s+\w+){0,2}"
_A_COUNT_OF_PYTHON_FILES = re.compile(
    rf"\b{_A_COUNT}{_QUALIFIERS}\s+Python{_QUALIFIERS}\s+(?:files|modules)\b",
    re.IGNORECASE)

# Transcribed from the release workflow's own ANY_HEADING, DATED and
# DATE_ANYWHERE patterns, with the POSIX classes rewritten for `re` — a heading
# is a single line, so `[[:space:]]` can only ever be a space or a tab here.
# DATED is what makes a heading releasable: the date must be the whole of the
# field after the separator, so `## v1.7.0 — TBD` and
# `## v1.7.0 — TBD (was 2026-08-01)` are both undated.
# test_release_regexes_match_the_workflow keeps the three copies equal, because
# this file's copies decide what the guards below call undated.
ANY_HEADING = r"## v[0-9]+\.[0-9]+\.[0-9]+([ \t]|$)"
DATED = (
    r"## v[0-9]+\.[0-9]+\.[0-9]+[ \t]+[^ \t]+[ \t]+"
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ \t]*$"
)
# "shows a date somewhere", which is a different question from DATED and is why
# the workflow carries it separately: it is what tells a heading nobody dated
# apart from one dated in a shape the trigger will not act on.
DATE_ANYWHERE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"


def _shows_a_date_it_cannot_release_with(line):
    """True when `line` displays a release date the workflow will not act on.

    The trigger anchors the date to end of line, so a trailing parenthetical or
    a second clause leaves a heading that reads as released and releases
    nothing. Both halves of the conjunction matter: without the DATE_ANYWHERE
    half this would reject `## vX.Y.Z — TBD`, the shape the top heading holds
    for most of the repo's life — and since this suite is itself the release
    gate, that would not delay one release, it would block every one.
    """
    return bool(re.search(DATE_ANYWHERE, line)) and not re.match("^" + DATED, line)

# Undated headings below the top one that are deliberately tolerated. EMPTY, and
# it should stay that way. `## v1.6.2 — TBD` used to sit under the released
# `## v1.6.1 — 2026-08-06`, where the release workflow could never reach it: it
# publishes one section, selected by heading, and stops at the next `## v`. Its
# bullets were folded into the top section and the heading removed. An entry here
# is a temporary record of a known violation so the guard still fails on a *new*
# one — never a licence to add another.
KNOWN_UNDATED_BELOW_TOP = set()


def _changelog_headings():
    """Every '## vX.Y.Z' heading in docs/CHANGELOG.md, top-down.

    Returns (line number, version, dated, raw line) tuples. `dated` follows the
    release workflow's rule rather than looking for the literal string TBD.
    """
    changelog = CHANGELOG.read_text(encoding="utf-8")
    headings = []
    for lineno, line in enumerate(changelog.splitlines(), 1):
        if re.match("^" + ANY_HEADING, line):
            # Field 2 is the version, the same way the workflow's release-notes
            # awk picks its section ($2 == ver).
            headings.append(
                (lineno, line.split()[1].lstrip("v"),
                 bool(re.match("^" + DATED, line)), line)
            )
    return headings


def _changelog_top_version():
    """Return the version from the first docs/CHANGELOG.md version heading."""
    headings = _changelog_headings()
    return headings[0][1] if headings else None


def _package_json_version():
    pkg = json.loads(
        (REPO_ROOT / "vscode-extension" / "package.json").read_text(encoding="utf-8")
    )
    return pkg["version"]


class TestVersion(unittest.TestCase):
    def test_version_is_strict_semver(self):
        self.assertRegex(VERSION, r"^\d+\.\d+\.\d+$")

    def test_cli_reexports_same_version(self):
        # cli.py imports VERSION from scanner so `cli.py --version` reports it.
        import cli
        self.assertEqual(cli.VERSION, VERSION)

    def test_matches_changelog_heading(self):
        top = _changelog_top_version()
        self.assertEqual(
            top, VERSION,
            f"scanner.VERSION ({VERSION}) != top CHANGELOG heading ({top}). "
            "Bump both in lockstep.",
        )

    def test_matches_package_json(self):
        pkg = _package_json_version()
        self.assertEqual(
            pkg, VERSION,
            f"scanner.VERSION ({VERSION}) != vscode-extension/package.json "
            f"version ({pkg}). The .vsix asset filename embeds the package "
            "version, so they must match.",
        )

    def test_cli_version_flag(self):
        """`python cli.py --version` prints the version and exits 0."""
        result = subprocess.run(
            # encoding, not just text=True — see AGENTS.md's Testing notes. The
            # output here is a semver string, so cp1252 and UTF-8 agree on it
            # and this site is latent rather than live; the rule is still the
            # rule, and PEP 597 named this line. PYTHONIOENCODING pins the
            # other end: `encoding=` alone would decode a cp1252 child as
            # UTF-8, which is a second mismatch rather than a fix.
            [sys.executable, "cli.py", "--version"],
            cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), VERSION)


class TestChangelogReleasability(unittest.TestCase):
    """CHANGELOG.md must keep the shape the release workflow assumes of it."""

    def test_only_the_top_heading_may_be_undated(self):
        headings = _changelog_headings()
        self.assertTrue(headings, "CHANGELOG.md has no '## vX.Y.Z' heading.")
        stranded = [h for h in headings[1:] if not h[2]]
        unrecorded = [h for h in stranded if h[1] not in KNOWN_UNDATED_BELOW_TOP]
        self.assertEqual(
            [], unrecorded,
            "CHANGELOG.md may carry at most one undated '## vX.Y.Z' heading, and "
            "it must be the topmost one. These undated headings sit below it:\n"
            + "\n".join(f"  CHANGELOG.md:{n}: {raw}" for n, _v, _d, raw in unrecorded)
            + "\n.github/workflows/tag-on-merge.yml releases the newest DATED "
            "heading and builds the release body with an awk that starts at that "
            "heading and stops at the next '## v' line, and only the top heading "
            "can be dispatched by hand — TestVersion above pins scanner.VERSION "
            "and vscode-extension/package.json to the top heading, and the "
            "workflow's build step refuses to package a .vsix whose version "
            "disagrees with the released one. So a heading below the newest one "
            "can never ship under its own version, and once the version above it "
            "is released it never can again: its bullets are stranded "
            "permanently. Fold them into the top section, or record the version "
            "in KNOWN_UNDATED_BELOW_TOP if it is being held deliberately.",
        )

    def test_the_top_heading_may_not_show_a_date_it_cannot_release_with(self):
        """The hole the guard above leaves: a top heading dated in a dead shape.

        `test_only_the_top_heading_may_be_undated` scans `headings[1:]` on
        purpose — its subject is stranded lower sections — so nothing looked at
        the top heading at all, and `test_matches_changelog_heading` reads
        `line.split()[1]`, which is happy with any trailing text. So
        `## v1.7.0 — 2026-08-11 (hotfix for #91)` kept the whole suite green
        while releasing nothing, and the workflow then told the maintainer to
        add a date the line already carried.
        """
        headings = _changelog_headings()
        self.assertTrue(headings, "CHANGELOG.md has no '## vX.Y.Z' heading.")
        lineno, _version, _dated, raw = headings[0]
        self.assertFalse(
            _shows_a_date_it_cannot_release_with(raw),
            f"CHANGELOG.md:{lineno}: {raw}\n"
            "The top heading shows a release date, but not where the release "
            "trigger looks for it. .github/workflows/tag-on-merge.yml's DATED "
            "anchors the date to the end of the line, so a trailing "
            "parenthetical or a second clause makes the heading inert: the "
            "push tags nothing, publishes nothing, and reports that the "
            "heading 'carries no release date'. Write `## vX.Y.Z — YYYY-MM-DD` "
            "with nothing after the date, or leave it `## vX.Y.Z — TBD` until "
            "it is ready to ship.",
        )

    def test_the_dated_top_heading_guard_bites(self):
        """The shapes that guard must accept and reject.

        Measured against the workflow's own DATED, extracted verbatim and run
        under `grep -E` on 2026-08-10: every line below releases or does not
        release exactly as this test says. The accepted list is what keeps the
        guard from reddening `main` — `## vX.Y.Z — TBD` is the top heading's
        normal state, and the separator is genuinely free (an ASCII hyphen
        releases too, and trailing whitespace is tolerated). The rejected list
        is what keeps it discriminating; the missing separator is its
        counter-intuitive member.
        """
        for line in (
            "## v1.7.0 — TBD",
            "## v1.7.1 — 2026-08-11",
            "## v1.7.1 - 2026-08-11",
            "## v1.7.1 — 2026-08-11  ",
        ):
            with self.subTest(accepted=line):
                self.assertFalse(
                    _shows_a_date_it_cannot_release_with(line),
                    f"{line!r} releases, or is a plain undated heading; the "
                    "guard must leave it alone.",
                )
        for line in (
            "## v1.7.1 — 2026-08-11 (hotfix for #91)",
            "## v1.7.1 — 2026-08-11, thanks @someone",
            "## v1.7.1 — 2026-08-11 — security fix",
            "## v1.7.1 (2026-08-11)",
            "## v1.7.1 2026-08-11",
            "## v1.7.0 — TBD (was 2026-08-01)",
        ):
            with self.subTest(rejected=line):
                self.assertTrue(
                    _shows_a_date_it_cannot_release_with(line),
                    f"{line!r} shows a date and releases nothing, and the "
                    "guard let it through.",
                )

    def test_release_regexes_match_the_workflow(self):
        """The three patterns above are copies; keep them equal to the source."""
        workflow = WORKFLOW.read_text(encoding="utf-8")
        for name, transcribed in (("ANY_HEADING", ANY_HEADING), ("DATED", DATED),
                                  ("DATE_ANYWHERE", DATE_ANYWHERE)):
            m = re.search(rf"^\s*{name}='(.+)'$", workflow, re.MULTILINE)
            self.assertIsNotNone(m, f"{name}='...' not found in {WORKFLOW.name}.")
            posix = m.group(1).replace("[^[:space:]]", r"[^ \t]")
            self.assertEqual(
                posix.replace("[[:space:]]", r"[ \t]"), transcribed,
                f"{WORKFLOW.name}'s {name} changed. Re-transcribe it here: this "
                "file's copy is what decides which headings the undated-heading "
                "guard treats as released, so a stale copy stops enforcing the "
                "rule the workflow actually applies.",
            )


@unittest.skipIf(os.name == "nt", "release payload preparation runs on Ubuntu")
@unittest.skipUnless(shutil.which("bash"), "release payload preparation requires bash")
class TestReleasePayloadAcrossTheRename(unittest.TestCase):
    def test_runtime_changes_before_and_after_the_rename_require_matching_ci(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        match = re.search(
            r'relevant=\$\((git log -1 --format=%H "\$SHA" -- \\\n'
            r'.*?\.github/workflows/extension-ci\.yml)\)', workflow, re.DOTALL)
        self.assertIsNotNone(match, "release CI applicability command is missing")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                result = subprocess.run(
                    ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null",
                     "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
                     "-c", "commit.gpgsign=false", *args],
                    capture_output=True, text=True, encoding="utf-8", check=True,
                )
                return result.stdout.strip()

            git("init", "--quiet")
            (root / "vscode-extension").mkdir()
            (root / "vscode-extension" / "package.json").write_text("{}\n", encoding="utf-8")
            git("add", ".")
            git("commit", "--quiet", "-m", "synthetic extension baseline")
            for package in ("claude_usage", "codex_claude_usage"):
                with self.subTest(package=package):
                    (root / package).mkdir()
                    (root / package / "runtime.py").write_text("# synthetic runtime change\n",
                                                                encoding="utf-8")
                    git("add", ".")
                    git("commit", "--quiet", "-m", "synthetic runtime change")
                    runtime_sha = git("rev-parse", "HEAD")
                    (root / "CHANGELOG.md").write_text(package + "\n", encoding="utf-8")
                    git("add", ".")
                    git("commit", "--quiet", "-m", "synthetic changelog-only release")
                    result = subprocess.run(
                        ["bash", "-c", match.group(1)], cwd=root,
                        capture_output=True, text=True, encoding="utf-8",
                        env=dict(os.environ, SHA=git("rev-parse", "HEAD")),
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(result.stdout.strip(), runtime_sha)

    def test_historical_and_current_packages_reach_the_publish_job(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        prepare = workflow.split("      - name: Prepare release notes\n", 1)[1]
        prepare = prepare.split("\n      - name:", 1)[0]
        script = textwrap.dedent(prepare.split("        run: |\n", 1)[1])
        self.assertIn("vsix_filename: ${{ steps.build.outputs.vsix_filename }}", workflow)
        self.assertIn('echo "vsix_filename=$vsix" >> "$GITHUB_OUTPUT"', workflow)
        publish = workflow.split("  release:\n", 1)[1]
        self.assertIn("VSIX_FILENAME: ${{ needs.prepare.outputs.vsix_filename }}", publish)
        self.assertIn('vsix="release-output/$VSIX_FILENAME"', publish)
        for package, version in (("claude-usage", "1.7.0"),
                                 ("codex-claude-usage", "1.8.0")):
            with self.subTest(package=package), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / f"{package}-{version}.vsix"
                identity = {"name": package, "publisher": "synthetic", "version": version}
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr("extension/package.json", json.dumps(identity))
                (root / "docs").mkdir()
                notes = f"## v{version} — 2026-10-06\n\n- Synthetic release.\n"
                (root / "docs" / "CHANGELOG.md").write_text(notes, encoding="utf-8")
                result = subprocess.run(
                    ["bash", "-c", script], cwd=root, capture_output=True,
                    text=True, encoding="utf-8",
                    env=dict(os.environ, VERSION=f"v{version}", VSIX=str(source)),
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                output = root / "release-output"
                filename = f"{package}-{version}.vsix"
                self.assertEqual({path.name for path in output.iterdir()},
                                 {filename, "notes.md"})
                self.assertEqual((output / filename).read_bytes(), source.read_bytes())
                with zipfile.ZipFile(output / filename) as archive:
                    self.assertEqual(json.loads(archive.read("extension/package.json")), identity)
                self.assertEqual((output / "notes.md").read_text(encoding="utf-8"), notes)

    def test_publish_accepts_only_reviewed_identity_and_version_filenames(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        publish = workflow.split("      - name: Create matching tag and GitHub release\n", 1)[1]
        script = publish.split("        run: |\n", 1)[1]
        script = textwrap.dedent(script.split("          # Only read the SHA", 1)[0])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "release-output"
            output.mkdir()
            (output / "notes.md").write_text("Synthetic release notes\n", encoding="utf-8")
            valid = [f"{package}-1.7.0.vsix" for package in
                     ("claude-usage", "claude-usage-private", "codex-claude-usage")]
            for filename in valid:
                (output / filename).write_bytes(b"synthetic reviewed artifact")
            cases = [(filename, "v1.7.0", True) for filename in valid]
            cases += [(filename, "v1.7.0", False) for filename in
                      ("../claude-usage-1.7.0.vsix", "/tmp/claude-usage-1.7.0.vsix",
                       "codex-claude-usage-1.8.0.vsix", "unrelated-1.7.0.vsix",
                       "claude-usage-1.7.0.vsix\nextra", "")]
            cases.append((valid[0], "v1.7.0/../../escape", False))
            for filename, version, succeeds in cases:
                with self.subTest(filename=filename, version=version):
                    result = subprocess.run(
                        ["bash", "-c", script], cwd=root, capture_output=True,
                        text=True, encoding="utf-8",
                        env=dict(os.environ, VERSION=version, VSIX_FILENAME=filename),
                    )
                    self.assertEqual(result.returncode == 0, succeeds,
                                     result.stdout + result.stderr)


class TestBundlingRationale(unittest.TestCase):
    """This module's own docstring must not count what the `.vsix` bundles.

    The docstring above justifies keeping `VERSION` in scanner.py, and it used
    to do so with a count of the Python files the `.vsix` ships. That count was
    accurate at 63b36d9, the commit that added both this file and the comment
    above `scanner.VERSION`: copy-python.js there declared exactly
    `const files = ["cli.py", "scanner.py", "dashboard.py"]`. It was first
    released in v1.3.0 — `git tag --contains 63b36d9 --sort=creatordate` heads
    that list with it — which is not the same relation as being the commit the
    tag points at. This parenthetical read "first tagged v1.3.0" until
    2026-08-10, and a reader who checks it finds `git rev-parse v1.3.0` is the
    later ba6681f. The module split grew the list and neither sentence followed.
    scanner.py's was corrected at 2072ed7 and is guarded by
    tests/test_scanner.py, which reads scanner.py alone — so the copy here
    survived, and scanner.py's corrected comment points a reader straight at it
    ("a parity test guards all three; see tests/test_version.py").

    The half that justifies the constant — CHANGELOG.md really is not bundled —
    is checked here rather than asserted in prose. Nothing is restated about
    what *is* bundled: copy-python.js is the list, tests/test_web_assets.py
    derives the real module set from disk and is the authority on it, and a
    hardcoded count is the thing that went stale twice.
    """

    def _copy_python(self):
        return COPY_PYTHON.read_text(encoding="utf-8")

    def test_the_docstring_counts_no_bundled_python_files(self):
        source = Path(__file__).read_text(encoding="utf-8")
        claim = _A_COUNT_OF_PYTHON_FILES.search(source)
        bundled = re.findall(r'"([^"]+\.py)"', self._copy_python())
        self.assertIsNone(
            claim,
            "tests/test_version.py claims %r, and copy-python.js bundles %d "
            "Python modules. Drop the count rather than correcting it — a "
            "hardcoded count is what went stale."
            % (claim.group(0) if claim else "", len(bundled)),
        )

    def test_the_guard_catches_a_count_in_any_form(self):
        """The guard above must not be evadable by writing the number differently.

        Its first version spelled out one..ten plus digits, so the word form of
        a number above ten — the very number a corrector reaches for on a tree
        that has long since outgrown "three" — walked straight past it, and so
        did a capital letter and a word slipped between the count and the noun.
        The phrases are assembled here rather than written out, because a
        literal one would live in this file and the test above reads this file
        whole.
        """
        counts = ("three", "Three", "14", "1,014", "fourteen", "Fourteen",
                  "twenty-three", "one hundred")
        qualifiers = ("", "root ", "bundled source ")
        nouns = ("Python files", "Python modules", "python modules")
        for count in counts:
            for qualifier in qualifiers:
                for noun in nouns:
                    claim = f"the .vsix ships {count} {qualifier}{noun}"
                    with self.subTest(claim=claim):
                        self.assertIsNotNone(
                            _A_COUNT_OF_PYTHON_FILES.search(claim),
                            f"{claim!r} is a count of what the .vsix bundles and "
                            "the guard let it through.",
                        )

    def test_the_guard_leaves_other_counts_and_vague_ones_alone(self):
        """The deliberate boundaries, pinned so a widening does not cross them.

        A guard that fires on a sentence this module legitimately writes is a
        guard the next author weakens, which is how the count came back the
        first time. Two boundaries: the noun must be Python files or modules
        (this module really does count other things), and the quantity must be a
        number (a vague one does not go stale into a falsehood). The failure
        message below is checked too — it names the noun itself, and a guard
        that matched it could never pass.
        """
        for text in (
            "These tests keep the three places a version is written from drifting",
            "two regexes that each guard the file they live beside",
            "copy-python.js bundles %d Python modules. Drop the count",
            "Python modules are copied by copy-python.js",
            "a handful of Python modules",
        ):
            with self.subTest(text=text):
                self.assertIsNone(
                    _A_COUNT_OF_PYTHON_FILES.search(text),
                    f"{text!r} is not a count of what the .vsix bundles, but "
                    "the guard flagged it.",
                )

    def test_the_changelog_really_is_not_bundled(self):
        """The load-bearing half of the rationale, checked rather than asserted."""
        bundled = re.findall(r'"([^"]+)"', self._copy_python())
        self.assertFalse(
            any(Path(item).name == "CHANGELOG.md" for item in bundled),
            "copy-python.js now bundles a changelog; recheck why VERSION is "
            "a runtime constant.",
        )
        self.assertFalse(
            (REPO_ROOT / "vscode-extension" / "CHANGELOG.md").exists(),
            "vscode-extension/CHANGELOG.md exists, so the .vsix may now carry a "
            "CHANGELOG after all — recheck why VERSION is a constant.",
        )


if __name__ == "__main__":
    unittest.main()
