"""Tests for the untrusted-text boundary — `safetext`.

AGENTS.md calls this module the guard between attacker-influenced transcript
metadata and either a terminal or SQLite, and until this file existed it had
exactly one test: `tests/test_scanner.py`'s `TestTerminalSafe`, whose fixture
holds Cc and Cf characters only. Two of the module's guards were therefore
asserted by nothing, and that is a mutation result rather than an inspection —
deleting `"Cs"` from `terminal_safe`'s category tuple, and deleting
`_bounded_text`'s UTF-8 round trip, each left the full suite at 1430 tests OK
while changing what the CLI does on real input.

Both guards are about the **lone surrogate** (U+D800-U+DFFF): a code point
Python holds happily, UTF-8 cannot encode at all, and two producers put in
front of this module with no attacker involved.

- `os.fsdecode` — POSIX decodes a non-UTF-8 argv element or directory entry
  with `surrogateescape`, so a `--projects-dir` argument, a discovered
  filename, and the `OSError` message quoting either can each arrive as `Cs`.
- `JSON.stringify` — Claude Code writes its transcripts from JavaScript
  strings, which may legally hold an unpaired surrogate (a truncated emoji
  pasted into a prompt), and Node emits it as the escape `\\ud800`, which
  `json.loads` decodes straight back to the real code point.

The two guards then answer different questions, which is why one test cannot
stand in for the other: `terminal_safe` escapes the character so `print` can
render it, `_bounded_text` replaces it with `?` so `sqlite3` can bind it. The
same category, deliberately handled two different ways.

Synthetic hostile text covers lone surrogates, control characters and escape
sequences independently of whether ordinary transcripts contain them.

"""

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from safetext import MAX_TEXT_LENGTH, _bounded_text, terminal_safe
from scanner import get_db, scan

#: The fixture, written as a literal rather than derived from bytes.
#: `os.fsdecode(b"/tmp/bad\xffdir")` yields this character only where the
#: filesystem error handler is `surrogateescape`; Windows sets `surrogatepass`,
#: under which the same call raises `UnicodeDecodeError` — so a bytes-derived
#: fixture would ERROR the windows-latest leg of the CI matrix instead of
#: asserting anything. The literal is identical on every platform.
BAD_PATH = "/tmp/bad\udcffdir"


class TestTerminalSafeEscapesALoneSurrogate(unittest.TestCase):
    """The `"Cs"` element of `terminal_safe`'s category tuple.

    It is the one element the function's own docstring does not name — it
    speaks of "terminal or bidi control codes", and a surrogate is neither — so
    a reader narrowing the tuple to `("Cc", "Cf")` has the documentation on
    their side. What they would actually delete is the reason
    `python3 cli.py scan --projects-dir <path with non-UTF-8 bytes>` prints a
    warning and exits 0 rather than dying at cli.py:400 with an unhandled
    `UnicodeEncodeError`. Stated carefully: the guard keeps a lone surrogate
    out of stdout so that a strict-UTF-8 terminal cannot abort the scan. Under
    a C/POSIX-coerced locale `print` uses `surrogateescape` and emits the raw
    byte instead, so the crash is real but not unconditional.
    """

    def test_a_lone_surrogate_is_escaped_rather_than_passed_through(self):
        self.assertEqual(terminal_safe(BAD_PATH), r"/tmp/bad\udcffdir")
        self.assertNotIn("\udcff", terminal_safe(BAD_PATH))

    def test_the_escaped_form_is_what_a_strict_stdout_can_encode(self):
        """The assertion that actually bites. The rendering above pins a
        spelling; this pins the property the spelling exists for, and it is the
        one a narrowed category tuple loses."""
        with self.assertRaises(UnicodeEncodeError):
            BAD_PATH.encode("utf-8")            # what `print` faces unguarded
        terminal_safe(BAD_PATH).encode("utf-8")  # and what it faces guarded

    def test_a_path_object_quoting_a_bad_path_is_escaped_when_printed(self):
        """The non-`str` call-site shape that actually carries a surrogate.

        `terminal_safe(filepath)` and `terminal_safe(d)` — scanner.py:149,
        :790, :796, :940, transcripts.py:594, :600, codex_transcripts.py:572,
        :579 and cli.py:400 — are handed a `Path`, and `str(Path)` reproduces
        the code point verbatim, so the escaping is this function's to do.
        This is also the reachable route on macOS, where APFS rejects such a
        filename outright (`OSError 92`) and only argv can produce one.
        """
        rendered = terminal_safe(Path(BAD_PATH))
        self.assertIn(r"\udcff", rendered)
        self.assertNotIn("\udcff", rendered)
        rendered.encode("utf-8")

    def test_the_exception_half_of_those_lines_is_pre_escaped_by_repr(self):
        """Why there is no `terminal_safe(exc)` assertion beside the one above,
        recorded so it is not "fixed" by adding one back.

        Every call site above prints `{terminal_safe(filepath)}:
        {terminal_safe(e)}`, and the obvious second assertion — build the
        `OSError` that `open()` raises and render it — is a **fake**:
        `OSError(errno, strerror, filename).__str__` formats `filename`
        through `repr()`, so the surrogate is already the ASCII text
        `\\udcff` before `terminal_safe` ever sees it. Such a test passes with
        the `"Cs"` category deleted; it was in this file until a mutation run
        showed it surviving. Only the `filepath` half of those lines
        discriminates. An exception does carry one raw when a path is
        interpolated into its *message* rather than its `filename` slot, which
        the second half of this test covers.
        """
        pre_escaped = str(OSError(2, "No such file or directory", BAD_PATH))
        self.assertNotIn("\udcff", pre_escaped)   # repr did it, not us
        self.assertIn(r"\udcff", pre_escaped)
        pre_escaped.encode("utf-8")               # safe with no guard at all

        rendered = terminal_safe(OSError(f"cannot read {BAD_PATH}"))
        self.assertIn(r"\udcff", rendered)
        self.assertNotIn("\udcff", rendered)
        rendered.encode("utf-8")


class TestBoundedTextRemovesALoneSurrogate(unittest.TestCase):
    """The UTF-8 round trip at the end of `_bounded_text`.

    For a `str`, `.encode("utf-8", "replace").decode("utf-8")` is a no-op on
    every input except U+D800-U+DFFF — it is precisely and solely the surrogate
    scrub, which is exactly why it reads as a redundant round trip to anyone
    simplifying the line, especially with a module docstring that justifies
    only the length cap beside it. `sqlite3` refuses to bind a lone surrogate
    and this is the only sanitiser between the parser and the INSERT, so
    deleting it turns a stored `?` into an unhandled `UnicodeEncodeError`.
    """

    def test_a_lone_surrogate_becomes_a_replacement_character(self):
        """Characterisation, not the contract: `terminal_safe` escapes the same
        category and `_bounded_text` substitutes it, so a future unification of
        the two would have to rewrite this line. The test below is the one that
        states what the database needs."""
        self.assertEqual(_bounded_text("a\ud800b"), "a?b")

    def test_the_result_is_what_sqlite_can_bind(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE t (v TEXT)")
            with self.assertRaises(UnicodeEncodeError):
                conn.execute("INSERT INTO t VALUES (?)", ("a\ud800b",))
            conn.execute("INSERT INTO t VALUES (?)",
                         (_bounded_text("a\ud800b"),))
        finally:
            conn.close()

    def test_a_surrogate_arrives_the_way_a_transcript_actually_carries_one(self):
        """Not hand-built: `json.dumps` escapes a lone surrogate exactly as
        Node's `JSON.stringify` does, and `json.loads` decodes it back — so a
        transcript can hold one in ASCII-clean, perfectly valid JSON."""
        line = json.dumps({"gitBranch": "main\ud800br"})
        self.assertEqual(line, '{"gitBranch": "main\\ud800br"}')
        self.assertEqual(_bounded_text(json.loads(line)["gitBranch"]),
                         "main?br")


class TestBoundedTextBounds(unittest.TestCase):
    """The other two clauses of the same function, for completeness of the
    contract. The `isinstance` guard is protected indirectly and heavily (55
    failures across the suite when removed); the *default* limit is not
    protected at all — the one test that reaches the truncation,
    `tests/test_reasoning_effort.py`'s 64-character effort bound, passes its own
    limit, so `MAX_TEXT_LENGTH = None` leaves the suite green while restoring
    exactly the unbounded string into SQLite that the module docstring says the
    cap prevents.
    """

    def test_it_truncates_at_its_default_bound(self):
        self.assertIsInstance(MAX_TEXT_LENGTH, int)
        self.assertEqual(len(_bounded_text("x" * (MAX_TEXT_LENGTH + 10))),
                         MAX_TEXT_LENGTH)

    def test_an_explicit_limit_wins_over_the_default(self):
        self.assertEqual(_bounded_text("x" * 100, 8), "x" * 8)

    def test_a_non_string_becomes_the_empty_string(self):
        """Not `str(value)`. A transcript field that arrived as a dict or a
        list is metadata the parser has no meaning for, and coercing it would
        store `{'level': 'high'}` as though it were an effort level."""
        for value in (None, 12, b"bytes", ["a"], {"a": 1}):
            with self.subTest(value=repr(value)):
                self.assertEqual(_bounded_text(value), "")


class TestAScanSurvivesASurrogateInTranscriptMetadata(unittest.TestCase):
    """The unit tests above pin the helper; this pins the product.

    A refactor that stops *calling* `_bounded_text` on `gitBranch` — or that
    adds a new stored string field without it — reintroduces the same aborted
    scan with every assertion above still green, because the value reaches
    `sqlite3` through `upsert_sessions` with no second sanitiser in between and
    nothing in `scan()` catches it. Asserted on the turn count and the stored
    row, deliberately: the failing run still creates the database file (schema
    only, 0 rows), so a test that checked for the file's absence would be green
    for the wrong reason.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.projects_dir = Path(self.tmpdir) / "projects"
        (self.projects_dir / "user" / "myproject").mkdir(parents=True)
        self.db_path = Path(self.tmpdir) / "usage.db"

    def _write_transcript(self, git_branch):
        record = {
            "type": "assistant",
            "sessionId": "sess-surrogate",
            "timestamp": "2026-04-08T10:00:00Z",
            "cwd": "/home/user/project",
            "gitBranch": git_branch,
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "content": [],
            },
        }
        path = self.projects_dir / "user" / "myproject" / "sess-surrogate.jsonl"
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    def test_a_branch_name_holding_one_is_stored_instead_of_aborting_the_scan(self):
        self._write_transcript("main\ud800br")

        result = scan(projects_dir=self.projects_dir, db_path=self.db_path,
                      verbose=False)

        self.assertEqual(result["turns"], 1)
        conn = get_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT git_branch FROM sessions WHERE session_id = ?",
                ("sess-surrogate",)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        row["git_branch"].encode("utf-8")
        self.assertEqual(row["git_branch"], "main?br")


if __name__ == "__main__":
    unittest.main()
