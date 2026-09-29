"""Reject unlisted approximate wall-clock benchmarks in maintained prose.

Describe algorithmic work or use reproducible synthetic measurements. Private
usage measurements do not belong in public source, tests or documentation.
The pattern is deliberately narrow: approximation marker + number + seconds.
It does not claim to recognize every possible personal-data disclosure.
"""

import re
import unittest
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The approximation marker, kept out of every literal in this file so the file
# does not match its own check.
TILDE = "~"

# tilde, a number (optionally a range), a unit of seconds. See the module
# docstring for why the unit list stops at seconds.
_BENCHMARK = re.compile(
    r"~\s*\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?\s*(?:s|sec|secs|seconds)\b")

_SCANNED_GLOBS = ("*.py", "claude_usage/*.py", "tests/*.py", "web/**/*.js",
                  "web/**/*.css", "web/*.html", "docs/*.md")

# No historical personal measurements are exempted.
_PERMITTED = {}


def _scanned_files():
    """Every file this test reads, deduplicated and in a stable order."""
    seen = {}
    for pattern in _SCANNED_GLOBS:
        for path in sorted(REPO_ROOT.glob(pattern)):
            seen[path.relative_to(REPO_ROOT).as_posix()] = path
    return sorted(seen.items())


def _normalise(match):
    """Drop the marker and the spacing, so the ledger can hold a figure
    without matching itself."""
    return re.sub(r"\s+", "", match.lstrip(TILDE)).lower()


def _figures(path):
    text = path.read_text(encoding="utf-8")
    return Counter(_normalise(m) for m in _BENCHMARK.findall(text))


class TestNoUnbackedWallClockBenchmarks(unittest.TestCase):
    def test_no_scanned_file_quotes_an_unlisted_benchmark(self):
        for rel, path in _scanned_files():
            with self.subTest(file=rel):
                excess = _figures(path) - Counter(_PERMITTED.get(rel, ()))
                self.assertEqual(
                    Counter(), excess,
                    f"{rel} quotes an approximate wall-clock benchmark "
                    f"({sorted(excess)}). A measured number belongs in prose "
                    "only if something executes it; otherwise state the "
                    "qualitative fact instead. If this one genuinely must "
                    "stay, add it to _PERMITTED with a reason.")

    def test_the_ledger_names_only_files_this_test_reads(self):
        """A ledger key that matches nothing exempts nothing and rots quietly."""
        scanned = {rel for rel, _ in _scanned_files()}
        self.assertEqual(
            set(), set(_PERMITTED) - scanned,
            "_PERMITTED keys are repo-relative posix paths of files this test "
            "reads; one that is not scanned exempts nothing.")

    def test_the_ledger_carries_no_dead_exemption(self):
        """An exemption for a figure the file no longer carries is a hole.

        A ceiling only bites while it is tight. Sweep a figure out of a file
        and leave its entry standing, and the guard will let the next editor
        put the same one back — silently, because an entry that used to
        describe a live figure now describes a permitted absence. That is not
        hypothetical: `AGENTS.md`'s four entries all went dead the moment its
        figures became qualitative claims, and each would have re-admitted the
        exact number it was written for.
        """
        for rel in sorted(_PERMITTED):
            with self.subTest(file=rel):
                dead = Counter(_PERMITTED[rel]) - _figures(REPO_ROOT / rel)
                self.assertEqual(
                    Counter(), dead,
                    f"_PERMITTED[{rel!r}] exempts {sorted(dead)}, which the "
                    "file no longer carries. A dead exemption silently "
                    "re-admits the figure it was written for; delete it.")

    def test_the_guard_reads_the_canonical_documentation(self):
        """The user, agent, release, and design docs are all in scope."""
        scanned = {rel for rel, _ in _scanned_files()}
        missing = [rel for rel in ("docs/AGENTS.md", "docs/CHANGELOG.md",
                                   "docs/CODEX-FEASIBILITY.md", "docs/README.md",
                                   "docs/VS-CODE-EXTENSION.md")
                   if rel not in scanned]
        self.assertEqual(
            [], missing,
            f"{missing} fell out of _SCANNED_GLOBS. Canonical project prose "
            "belongs under docs/ and must stay covered by this guard.")

    def test_the_pattern_catches_the_shape_that_rots(self):
        for sample in (TILDE + "9.9s", TILDE + "9.9 s", TILDE + " 9.9 seconds",
                       TILDE + "13 sec", TILDE + "4-5 s",
                       TILDE + "1.0-1.3 s"):
            with self.subTest(sample=sample):
                self.assertTrue(_BENCHMARK.search(sample))

    def test_the_pattern_leaves_constants_and_other_units_alone(self):
        for sample in ("busy_timeout is 3600s", "readinessTimeoutMs = 10_000",
                       "the payload takes 4.4 seconds", TILDE + "200ms",
                       TILDE + "6 ms", TILDE + "15-30 minutes",
                       TILDE + "0.3% of output tokens"):
            with self.subTest(sample=sample):
                self.assertIsNone(_BENCHMARK.search(sample))

    def test_normalising_strips_the_marker_and_the_spacing(self):
        self.assertEqual("4.4s", _normalise(TILDE + "4.4 s"))
        self.assertEqual("4.4s", _normalise(TILDE + "4.4s"))


if __name__ == "__main__":
    unittest.main()
