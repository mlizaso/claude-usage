"""Bounded, control-character-safe text from untrusted sources.

Transcript metadata is attacker-influenced: it reaches a terminal through the
CLI and a browser through the JSON API. `terminal_safe` strips the escape and
bidi control characters that would otherwise let a project name rewrite the
surrounding output; `_bounded_text` caps length so a hostile transcript cannot
push an unbounded string into SQLite.

Both also handle the **lone surrogate** (U+D800-U+DFFF), which is neither a
terminal hazard nor a length problem and so reads as incidental in each. It is
not: Python holds one happily and UTF-8 cannot encode it at all, and two
producers put one here with no attacker involved — `os.fsdecode`, which decodes
a non-UTF-8 argv element or directory entry with `surrogateescape`, and
`JSON.stringify`, since Claude Code writes its transcripts from JavaScript
strings, which may legally hold an unpaired surrogate. `terminal_safe` escapes
it so a strict-UTF-8 stdout cannot abort the scan with an unhandled
`UnicodeEncodeError`; `_bounded_text` substitutes it so `sqlite3`, which
refuses to bind one, can store the row. `tests/test_safetext.py` pins both —
each was verified unprotected by mutation before that file existed.
"""

import unicodedata

MAX_TEXT_LENGTH = 8 * 1024


def terminal_safe(value):
    """Render untrusted metadata without terminal or bidi control codes."""
    result = []
    for char in str(value):
        # "Cs" is the lone surrogate — not a control code, and here for the
        # other reason in the module docstring: `print` cannot encode one.
        if unicodedata.category(char) in ("Cc", "Cf", "Cs"):
            codepoint = ord(char)
            escape = f"\\x{codepoint:02x}" if codepoint <= 0xff else f"\\u{codepoint:04x}"
            result.append(escape)
        else:
            result.append(char)
    return "".join(result)

def invocation():
    """How to spell this command back to the user, on the surface they used.

    Three of the five delivery surfaces -- pip, Homebrew and the .vsix -- put a
    `codex-claude-usage` console script on PATH and no `cli.py` anywhere the reader
    can reach, so every "run: python cli.py scan" printed there names a command
    that does not exist. The checkout carries the wrapper; the Docker image
    carries only the package, and its advice has to name `docker exec` so the
    command runs against the mounted Docker database rather than the host one.
    The message that most needs to be followable is the one printed when
    something has already gone wrong.

    Read off `sys.argv[0]` at CALL time rather than cached at import, because
    the same process can be neither: under `python -m unittest` the program name
    is the runner's, and the answer there should be the checkout's spelling.

    It lives in this leaf module for the reason `safejson` is its own module:
    `cli` and `dashboard` both need it, and a shared helper living inside one of
    its callers is how an import cycle starts. `scanner` re-exports it beside
    `terminal_safe`, which is the path `dashboard` already takes.
    """
    import os
    import re
    import sys
    from pathlib import Path
    # The launcher's own answer wins where it gives one. Homebrew's shim runs
    # `cli.py` through runpy having rewritten `sys.argv`, so argv[0] is
    # `<libexec>/cli.py` and looks exactly like a git checkout -- one of the
    # three surfaces this function exists for was reporting the other two's
    # spelling. Only the shim knows the name on PATH, so it says so.
    declared = os.environ.get("CODEX_CLAUDE_USAGE_INVOKED_AS", "").strip()
    if declared == "codex-claude-usage":
        return declared
    if declared == "docker":
        container = os.environ.get("CODEX_CLAUDE_USAGE_DOCKER_CONTAINER", "")
        if (len(container) <= 128
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container)):
            return f"docker exec {container} python3 -m codex_claude_usage.cli"
    return ("codex-claude-usage" if Path(sys.argv[0]).name == "codex-claude-usage"
            else "python cli.py")


def _bounded_text(value, limit=MAX_TEXT_LENGTH):
    """Accept only bounded strings from transcript-controlled metadata."""
    if not isinstance(value, str):
        return ""
    # The round trip is the surrogate scrub, not ceremony: for a `str` it is a
    # no-op on every input except U+D800-U+DFFF, which is what makes it look
    # deletable and what makes deleting it abort a scan on the INSERT.
    return value[:limit].encode("utf-8", "replace").decode("utf-8")
