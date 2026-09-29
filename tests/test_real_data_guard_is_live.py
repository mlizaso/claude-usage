"""The guard in `tests/__init__.py` must actually be installed for this run.

Without this file, defence 2 was armed by ACCIDENT and nothing said so.

`python -m unittest discover -s tests` -- the command this repository documents
and CI ran -- leaves `top_level_dir` at the start directory, so every module is
imported under its BARE name (`test_scanner`, not `tests.test_scanner`) and the
package `__init__` never executes. Measured: discovery of one file yields module
name `test_safetext` and `"tests" in sys.modules` is False. The hook survived a
full run only because 21 of the 58 modules happen to carry a
`from tests.X import ...` line, and the alphabetically first of those installed
it for everybody else.

So the guard that exists to stop a test destroying the developer's real
`~/.claude/usage.db` was one refactor -- "drop that shared import" -- away from
being silently absent, with the suite still green. The suite is now launched
with `-t .`, which makes the package import deterministic; this file is what
notices if that ever stops being true.

**The probe never creates anything, and that is the point.** It opens a path
inside a directory that does not exist, with `O_WRONLY | O_CREAT | O_EXCL`.
Guarded, the hook raises before the syscall. Unguarded, the kernel answers
`FileNotFoundError` because the parent is missing. Neither branch can leave a
file behind, on a tree where leaving one behind is the whole hazard.

It deliberately does NOT `import tests` to find the exception class -- that
would install the very hook it is testing for and pass unconditionally. The type
is matched by name.
"""

import os
import unittest
from pathlib import Path


class TestTheRealDataGuardIsInstalled(unittest.TestCase):

    #: Inside ~/.claude, and inside a subdirectory that does not exist, so the
    #: unguarded path cannot create a file even if the guard is gone.
    PROBE = Path.home() / ".claude" / "__guard_probe_dir_that_does_not_exist__" \
        / "probe"

    def test_the_audit_hook_is_live(self):
        self.assertFalse(self.PROBE.parent.exists(),
                         "the probe directory must not exist; pick another name")
        try:
            handle = os.open(self.PROBE, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileNotFoundError:
            self.fail(
                "the real-data guard in tests/__init__.py is NOT installed for "
                "this run: a write under ~/.claude reached the kernel. Run the "
                "suite as `python -m unittest discover -s tests -t .` (the -t "
                "is what makes discovery import the tests package), or as "
                "`python -m unittest tests.<module>`.")
        except Exception as exc:                       # noqa: BLE001 - by name
            self.assertEqual("RealUserDataWrite", type(exc).__name__,
                             f"unexpected refusal from the guard: {exc!r}")
        else:
            os.close(handle)
            os.unlink(self.PROBE)
            self.fail("a write under ~/.claude SUCCEEDED from inside the suite")

    def test_the_probe_left_nothing_behind(self):
        self.assertFalse(self.PROBE.exists())
        self.assertFalse(self.PROBE.parent.exists())


if __name__ == "__main__":
    unittest.main()
