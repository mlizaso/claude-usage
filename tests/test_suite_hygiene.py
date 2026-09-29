"""Properties of the test suite itself.

`python -m unittest discover` finds every test wherever it sits, so the suite is
green either way and these problems are invisible from CI. They bite the person
debugging one file at a time — the workflow AGENTS.md documents as
`python -m unittest tests.test_scanner -v` — which is exactly when a silently
truncated run is most expensive.
"""

import ast
import glob
import os
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def _test_modules():
    return sorted(glob.glob(str(TESTS_DIR / "test_*.py")))


def _stranded_definitions(tree):
    """Names defined below a module's `if __name__ == "__main__":` block.

    Module level, with exactly two callers, deliberately: the guard below runs
    it over every real test module, and the meta-test below that runs it over a
    module that is known to break the rule. A scan for absence reports success
    just as happily when it has quietly stopped matching anything, so the
    meta-test has to drive *this* code rather than a copy of its body — while it
    held a copy, gutting the guard to `below = []` left both tests green with a
    genuinely stranded class sitting in tests/ (verified by doing it).

    `[]` for a module with no `__main__` block: it runs in full by definition.
    """
    main_block = None
    for node in tree.body:
        if isinstance(node, ast.If) and "__main__" in ast.dump(node.test):
            main_block = node
    if main_block is None:
        return []
    return [n.name for n in tree.body
            if isinstance(n, (ast.ClassDef, ast.FunctionDef))
            and n.lineno > main_block.lineno]


class TestNothingIsDefinedBelowUnittestMain(unittest.TestCase):
    """`if __name__ == "__main__": unittest.main()` must be the LAST thing in a
    test module.

    `unittest.main()` collects from the module as it stands at the moment it
    runs. A class defined below that call does not exist yet, so running the file
    as a script — `python3 tests/test_dashboard.py` — executes a PREFIX of the
    file and reports OK. Six files were in that state: one was hiding five whole
    test classes, and the run still printed a confident pass.

    The failure mode is silence, which is why this is pinned rather than left to
    review: nothing about the output tells you the rest of the file was skipped.
    """

    def test_every_test_module_runs_in_full_as_a_script(self):
        offenders = {}
        for path in _test_modules():
            below = _stranded_definitions(
                ast.parse(Path(path).read_text(encoding="utf-8")))
            if below:
                offenders[os.path.basename(path)] = below
        self.assertEqual(
            offenders, {},
            "these modules define tests below unittest.main(), so running them "
            "as a script silently skips those definitions: "
            + "; ".join(f"{f} -> {', '.join(names)}"
                        for f, names in sorted(offenders.items())))

    def test_the_check_can_see_a_module_that_breaks_the_rule(self):
        """The guard above is a scan for absence, and a scan for absence passes
        just as happily when it is looking in the wrong place or matching
        nothing. Feed it a module that IS broken and confirm it says so.

        It has to be fed to `_stranded_definitions` — the function the guard
        itself calls. This assertion used to run against a copy of that body
        pasted into the test, which made the pair self-fulfilling: a guard
        rewritten to match nothing, plus its intact copy, still reported OK with
        a stranded class present in tests/.
        """
        broken = ast.parse(
            "import unittest\n"
            "class TestEarly(unittest.TestCase):\n"
            "    def test_a(self):\n        pass\n"
            "if __name__ == '__main__':\n    unittest.main()\n"
            "class TestStranded(unittest.TestCase):\n"
            "    def test_b(self):\n        pass\n"
            "def test_stranded_function():\n    pass\n")
        self.assertEqual(_stranded_definitions(broken),
                         ["TestStranded", "test_stranded_function"])

    def test_the_check_clears_a_module_that_keeps_main_last(self):
        """The other half of the pair. A scan hard-wired to report an offender
        would satisfy the test above; only one that actually reads the tree
        satisfies both."""
        clean = ast.parse(
            "import unittest\n"
            "class TestEarly(unittest.TestCase):\n"
            "    def test_a(self):\n        pass\n"
            "if __name__ == '__main__':\n    unittest.main()\n")
        self.assertEqual(_stranded_definitions(clean), [])
        no_main_block = ast.parse("class TestOnly:\n    pass\n")
        self.assertEqual(_stranded_definitions(no_main_block), [])


class TestEveryModuleIsDiscoverable(unittest.TestCase):
    def test_every_test_module_imports(self):
        """A module that raises on import is skipped by discovery with a loader
        error that is easy to scroll past; this turns it into a plain failure."""
        import importlib
        failures = []
        for path in _test_modules():
            name = "tests." + os.path.basename(path)[:-3]
            try:
                importlib.import_module(name)
            except Exception as exc:  # noqa: BLE001 - reporting, not handling
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
        self.assertEqual(failures, [], "test modules that fail to import: "
                                       + "; ".join(failures))


if __name__ == "__main__":
    unittest.main()
