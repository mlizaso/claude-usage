"""Make it impossible for a test to write to the developer's own data.

This package used to be empty. It is not any more, because the suite destroyed
a developer's `~/.claude/usage.db` three times over two days before anyone
connected the two, and the mechanism was invisible at every call site:
`scanner.scan` declared `db_path=DB_PATH`, a default evaluated at DEF time, so
`mock.patch.object(scanner, "DB_PATH", tmp)` moved the name and not the default.
A test that patched three module globals and then ran a real scan sent it to the
live database anyway.

`tests/test_no_test_touches_the_real_database.py` closes THAT mechanism -- no
default may equal a real user path. This file closes the CLASS: whatever a test
does, and however a future constant is spelled, a write into the real
`~/.claude`, `~/.codex` or Xcode assistant tree raises immediately, naming the
path. A corruption that took two days to attribute becomes a failing test that
names itself.

**What this covers, and what it does not.** An audit hook is per-interpreter, so
it does not follow a `subprocess`. A test that shells out to `cli.py` without
redirecting HOME is outside it -- which is the reason the sibling defence,
redirecting HOME for the whole run, is worth keeping too: that one is inherited
by children. Nor does it run when a test file is executed directly
(`python tests/test_x.py`), because a package `__init__` is not imported that
way; the documented command, and the one CI runs, is
`python -m unittest discover -s tests`.

Reads are deliberately allowed. The destructive class is writes, and some tests
legitimately look at the real transcript roots; a read that makes a test depend
on one machine's corpus is a different problem, argued in
`tests/test_codex_discovery_order.py`, and not one to solve by raising here.
"""

import os
import sys
from pathlib import Path

# Ordinary fixture scans must never discover the developer's Docker daemon.
# Docker collection tests explicitly enable it against their own fake transport.
os.environ["CODEX_CLAUDE_USAGE_DOCKER"] = "0"

# Resolved ONCE, at import, before anything redirects HOME -- so the guard keeps
# pointing at the real tree even under a fixture that moves it.
_REAL_HOME = Path(os.path.expanduser("~")).resolve()
_GUARDED = tuple(str(_REAL_HOME / part) for part in (
    ".claude", ".codex", "Library/Developer/Xcode/CodingAssistant"))

# Anything but pure reading. O_RDONLY is 0, so this mask is exactly "not a read".
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_MUTATING = frozenset((
    "os.rename", "os.replace", "os.remove", "os.unlink", "os.mkdir",
    "os.rmdir", "os.chmod", "os.chown", "os.link", "os.symlink", "os.truncate",
))


def _guarded_path(value):
    """The real-tree path `value` names, or None. Never raises.

    A sqlite3 target may be a URI rather than a path -- `sqlite3.connect(
    "file:/Users/you/.claude/usage.db?mode=rwc", uri=True)` -- and the raw
    string then starts with `file:`, not with the home directory, so a prefix
    test on it alone answers None and waves the connection through. Found by
    this guard's own verification script, whose closing read-only probe was
    written that way and was silently NOT blocked.
    """
    try:
        text = value if isinstance(value, str) else os.fsdecode(value)
    except (TypeError, ValueError):
        return None
    if text.startswith("file:"):
        text = text[len("file:"):].split("?", 1)[0]
        # file:///abs -> ///abs; sqlite treats the authority as empty.
        while text.startswith("//"):
            text = text[1:]
    return text if text.startswith(_GUARDED) else None


class RealUserDataWrite(RuntimeError):
    """A test tried to write into the developer's own Claude/Codex data."""


def _refuse(path, how):
    raise RealUserDataWrite(
        f"a test tried to {how} {path}, which is the developer's real data. "
        "Point CODEX_CLAUDE_USAGE_DB / CODEX_CLAUDE_USAGE_THRESHOLDS at a temp path, or "
        "patch the module global -- and check the function you are calling "
        "resolves it at CALL time rather than freezing it into a default. "
        "See tests/test_no_test_touches_the_real_database.py.")


def _hook(event, args):
    # Ordered by frequency: `open` dominates, and everything else returns on the
    # first comparison. The body must stay cheap -- it runs on every open in the
    # process, for the whole suite.
    if event == "open":
        if len(args) < 3:
            return
        flags = args[2]
        if isinstance(flags, int):
            if not flags & _WRITE_FLAGS:
                return
        elif isinstance(flags, str):
            if not any(c in flags for c in "wxa+"):
                return
        else:
            return
        path = _guarded_path(args[0])
        if path:
            _refuse(path, "open for writing")
    elif event == "sqlite3.connect":
        # No test has any business opening the real database at all: even a
        # read-only intent creates the file, the -wal and the -shm if the path
        # is clear, and `init_db` on a connection can DROP and VACUUM.
        path = _guarded_path(args[0])
        if path:
            _refuse(path, "connect to the SQLite database at")
    elif event in _MUTATING:
        for arg in args:
            if isinstance(arg, (str, bytes, os.PathLike)):
                path = _guarded_path(arg)
                if path:
                    _refuse(path, event.replace("os.", "") + " ")


sys.addaudithook(_hook)
