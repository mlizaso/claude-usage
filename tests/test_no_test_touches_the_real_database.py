"""No default may freeze the developer's real database into a signature.

This file exists because the suite destroyed the developer's `~/.claude/usage.db`
-- three times, over two days, before anyone connected the two.

`tests/test_cli_streams.py`'s
`test_the_background_scan_does_not_bury_the_url_in_paths` patched `cli.DB_PATH`,
`scanner.DB_PATH` **and** `dashboard.DB_PATH`, then ran `cmd_dashboard`'s real
background scan. All three patches were dead: `cmd_scan` calls `scan(...)`
without a `db_path`, and `scan`'s default was evaluated at def time, so it held
the module global as it was AT IMPORT -- the real path. Every run of the suite
therefore ingested the fixture transcript into the developer's live database and
swept the end-of-scan reconciliation across every row of it.

Found by correlation and then proved: `suite6.log` finished at 05:35:07 and the
database's mtime was 05:35. A `sys.addaudithook` run over the whole suite
recorded `open` and `sqlite3.connect` against the real path, with
`test_cli_streams.py:910` on the stack.

AGENTS.md already said tests must never touch the real database, and four
functions made obeying it depend on remembering an invisible rule. They resolve
`DB_PATH` at call time now, so patching the module global does what every test
in this repository already assumes it does.

The rule below is the structural one, and it is deliberately stronger than "fix
that test": a *default* that equals the real path is the trap, wherever it is.
"""

import inspect
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import account
import cli
import dashboard
import dashboard_data
import db
import limits_core
import reports
import rollups
import scanner

MODULES = (db, scanner, cli, dashboard, dashboard_data, reports, rollups,
           limits_core, account)


class TestNoSignatureFreezesARealUserPath(unittest.TestCase):
    """A def-time default cannot be redirected by patching the global."""

    def real_paths(self):
        """Every module constant that points into the user's real home.

        DERIVED, not hand-listed, for the reason AGENTS.md gives about the five
        packaging surfaces: a hand-kept list that silently omits an entry is
        worse than no list, because it is the thing a reader trusts. A new
        constant -- another `URL_FILE`, another root -- is covered the day it is
        added, without anyone remembering this file.

        `dashboard.URL_FILE` is why that matters here rather than in theory: it
        is the token-bearing `~/.claude/dashboard-url`, it is a module constant
        of exactly this shape, and a probe in this repository's own history
        overwrote the developer's real one through the same frozen-default trap.
        """
        home = str(Path.home())
        found = {}
        for mod in MODULES:
            for name, value in vars(mod).items():
                if name.startswith("_") or not isinstance(value, (str, Path)):
                    continue
                text = str(value)
                if not text.startswith(home):
                    continue
                if any(mark in text for mark in (".claude", ".codex", "Xcode")):
                    found[f"{mod.__name__}.{name}"] = Path(text)
        if hasattr(limits_core, "thresholds_path"):
            found["limits_core.thresholds_path()"] = limits_core.thresholds_path()
        return found

    def test_the_protected_set_is_not_empty(self):
        """A derived list that derives nothing protects nothing."""
        found = self.real_paths()
        self.assertGreaterEqual(len(found), 6, sorted(found))
        self.assertTrue(any(str(p).endswith("usage.db") for p in found.values()))
        self.assertTrue(any(str(p).endswith("dashboard-url") for p in found.values()))

    def test_the_scan_is_the_one_that_bit(self):
        """Named on its own so the regression is unmissable in the output."""
        self.assertIsNone(
            inspect.signature(scanner.scan).parameters["db_path"].default,
            "scan() froze the real database into its signature again; patching "
            "scanner.DB_PATH cannot redirect it, and the suite writes to the "
            "developer's live database")

    def test_no_public_default_equals_a_real_user_path(self):
        offenders = []
        real = self.real_paths()
        for mod in MODULES:
            for name, fn in sorted(vars(mod).items()):
                if not callable(fn) or not hasattr(fn, "__defaults__"):
                    continue
                if getattr(fn, "__module__", None) != mod.__name__:
                    continue
                try:
                    sig = inspect.signature(fn)
                except (TypeError, ValueError):
                    continue
                for pname, param in sig.parameters.items():
                    if not isinstance(param.default, (str, Path)):
                        continue
                    for label, target in real.items():
                        if Path(param.default) == Path(target):
                            offenders.append(
                                f"{mod.__name__}.{name}({pname}={label})")
        self.assertEqual([], offenders,
                         "these defaults are frozen at import and cannot be "
                         "redirected by patching the module global: "
                         + ", ".join(offenders))

    def test_the_walk_actually_inspects_something(self):
        """Without this, an import failure would make the check vacuous."""
        seen = 0
        for mod in MODULES:
            for name, fn in vars(mod).items():
                if callable(fn) and getattr(fn, "__module__", None) == mod.__name__:
                    seen += 1
        self.assertGreater(seen, 100, "the module walk found almost nothing")


class TestPatchingTheGlobalNowRedirectsTheProduct(unittest.TestCase):
    """The behavioural half: the patch every test already writes must work."""

    def test_scan_follows_a_patched_module_global(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "redirected.db"
            proj = Path(tmp) / "projects"
            proj.mkdir()
            with mock.patch.object(scanner, "DB_PATH", target):
                scanner.scan(projects_dirs=[proj], verbose=False)
            self.assertTrue(target.exists(),
                            "scan ignored the patched scanner.DB_PATH")
            self.assertFalse(
                (Path(tmp) / "usage.db").exists(),
                "scan wrote somewhere other than the patched path")


if __name__ == "__main__":
    unittest.main()
