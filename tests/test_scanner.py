"""Tests for scanner.py - JSONL parsing, DB operations, and scanning."""

import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scanner
from scanner import (
    get_db, init_db, project_name_from_cwd, parse_jsonl_file,
    aggregate_sessions, upsert_sessions, insert_turns, scan,
    secure_db_permissions, terminal_safe,
    _processed_file_key, resolve_scan_roots, DEFAULT_PROJECTS_DIRS,
)


class TestProjectNameFromCwd(unittest.TestCase):
    # Every case that involves the home fold passes `home=` explicitly. Reading
    # it from `Path.home()` instead would leave these green on a machine whose
    # home is somewhere else while asserting nothing at all about the fold --
    # the vacuous-test failure this repository keeps rediscovering.
    ELSEWHERE = "/nowhere/else"

    def test_two_components(self):
        self.assertEqual(
            project_name_from_cwd("/home/user/myproject", home=self.ELSEWHERE),
            "user/myproject")

    def test_deep_path(self):
        self.assertEqual(
            project_name_from_cwd("/a/b/c/d", home=self.ELSEWHERE), "c/d")

    def test_single_component(self):
        self.assertEqual(
            project_name_from_cwd("/root", home=self.ELSEWHERE), "/root")

    def test_windows_path(self):
        self.assertEqual(
            project_name_from_cwd("C:\\Users\\me\\project", home=self.ELSEWHERE),
            "me/project")

    def test_trailing_slash(self):
        self.assertEqual(
            project_name_from_cwd("/home/user/project/", home=self.ELSEWHERE),
            "user/project")


class TestProjectNameDoesNotCarryTheUsername(unittest.TestCase):
    """A project directly in $HOME stored the USERNAME as its parent.

    `sessions.project_name` is the one field of its kind that leaves the
    machine: it reaches /api/data, the CLI `stats` table and three CSV exports,
    so it is what appears in a screenshot attached to a public bug report --
    while `turns.cwd` is NULL and `processed_files.path` is a SHA-256. A
    developer who keeps repositories directly in `~` leaked their username on
    100% of sessions; one who nests them leaked on none, so this is bimodal by
    LAYOUT rather than rare.
    """

    def test_a_project_directly_in_home_folds_the_username_away(self):
        self.assertEqual(
            project_name_from_cwd("/Users/victim/proj", home="/Users/victim"),
            "~/proj")

    def test_it_folds_on_windows_too(self):
        self.assertEqual(
            project_name_from_cwd("C:\\Users\\bob\\app", home="C:\\Users\\bob"),
            "~/app")

    def test_a_deeper_project_keeps_its_real_parent(self):
        """`work` carries nothing, and is the label that makes it useful."""
        self.assertEqual(
            project_name_from_cwd("/Users/victim/work/proj", home="/Users/victim"),
            "work/proj")

    def test_an_unrelated_path_is_untouched(self):
        """Changing non-home paths would relabel every project for no gain."""
        for cwd, want in (("/a/b/c/d", "c/d"), ("/srv/app/src", "app/src")):
            with self.subTest(cwd=cwd):
                self.assertEqual(
                    project_name_from_cwd(cwd, home="/Users/victim"), want)

    def test_a_cwd_that_IS_the_home_directory_folds_whole(self):
        """`cd ~ && claude` -- and this test used to assert the leak.

        It read `"/Users/victim" -> "Users/victim"` with the name
        `test_home_itself_is_not_a_child_of_home`, which is true of the parent
        check and entirely beside the point: the username had simply moved from
        the label's first component to its second, where the fold was not
        looking. An adversarial review reproduced it end to end through
        `scanner.scan` -- the sibling session at `/Users/victim/myproj` stored
        `~/myproj` correctly while this one kept the username, so the fix worked
        one directory deeper and missed the directory it is named after.
        """
        self.assertEqual(
            project_name_from_cwd("/Users/victim", home="/Users/victim"), "~")
        self.assertEqual(
            project_name_from_cwd("/home/victim", home="/home/victim"), "~")
        self.assertEqual(
            project_name_from_cwd("/Users/victim/", home="/Users/victim"), "~")

    def test_an_unreadable_home_degrades_instead_of_crashing(self):
        """`Path.home()` RAISES for a uid with no passwd entry.

        The earlier version of this test passed `home=""` and `home="/"`, so it
        never entered the `home is None` branch and never reached the
        `except (OSError, RuntimeError)` it claimed to cover -- an adversarial
        review proved it vacuous by replacing the whole guarded block with a
        bare `str(Path.home())` and watching the suite stay green. This patches
        `Path.home` itself, which is the only way to exercise it.
        """
        import transcripts

        def no_passwd_entry():
            raise RuntimeError("Could not determine home directory")

        with mock.patch.object(
                transcripts.Path, "home", staticmethod(no_passwd_entry)):
            self.assertEqual(transcripts.home_directory_leaf(), "")
            self.assertEqual(
                project_name_from_cwd("/Users/victim/proj"), "victim/proj")

    def test_a_home_with_no_leaf_disables_the_fold(self):
        """`/` has no last component, so there is nothing to match against."""
        for home in ("", "/"):
            with self.subTest(home=home):
                self.assertEqual(
                    project_name_from_cwd("/Users/victim/proj", home=home),
                    "victim/proj")

    def test_the_fold_is_case_correct_for_the_platform(self):
        """`normcase` is identity on POSIX and lowercases on Windows, where a
        cwd differing only in case is the SAME directory."""
        same_case = project_name_from_cwd("/Users/victim/proj", home="/Users/victim")
        self.assertEqual(same_case, "~/proj")
        expected = "~/proj" if os.name == "nt" else "Victim/proj"
        self.assertEqual(
            project_name_from_cwd("/Users/Victim/proj", home="/Users/victim"),
            expected)

    def test_empty_string(self):
        self.assertEqual(project_name_from_cwd(""), "unknown")

    def test_none(self):
        self.assertEqual(project_name_from_cwd(None), "unknown")


class TestTerminalSafe(unittest.TestCase):
    def test_escapes_terminal_and_bidi_controls(self):
        value = "safe\x1b]52;c;payload\x07\nname\u202eexe"
        rendered = terminal_safe(value)
        self.assertEqual(
            rendered,
            r"safe\x1b]52;c;payload\x07\x0aname\u202eexe",
        )
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\u202e", rendered)


REPO_ROOT = Path(__file__).resolve().parent.parent

#: Any claim about how many Python files the `.vsix` ships. The comment above
#: `scanner.VERSION` used to make one, and it is the shape of claim that goes
#: stale rather than a particular wrong number.
#:
#: A near-copy guards the sibling claim in tests/test_version.py, which reads
#: its own file. Two small copies rather than one import, for the reason stated
#: there: a test module importing another test module for a constant is a worse
#: coupling than two regexes that each guard what they sit beside.
#:
#: This was the narrow copy — one..ten plus digits, case-sensitive, no room for
#: a qualifier — and the sibling was widened without it. Measured 2026-08-10,
#: four shapes walked straight past this one and were caught by that one:
#: "fifteen Python modules" (a word-number above ten), "Three" (a capital),
#: "fifteen root Python modules" (a word between the count and the noun) and
#: "python" in lower case. Planting the first of them in scanner.py's rationale
#: left `tests.test_scanner` green, and left `tests.test_version` green too —
#: that guard reads its own file, so this regex is the only thing standing over
#: scanner.py. test_the_guard_catches_a_count_in_any_form pins all four.
#:
#: It goes one step FURTHER than the sibling: `Python` is optional, so
#: "the fifteen root modules" is caught as well. That is affordable here and not
#: there because the two read different things. This one reads the slice of
#: scanner.py above `VERSION = ` — a one-line docstring, the imports, and the
#: rationale itself — where a count of anything shipped has no business; the
#: sibling reads a whole test module that legitimately counts other things.
#: Measured the same day, neither form matches that slice as it stands today.
#:
#: Where it stops: a claim carrying no countable noun ("the .vsix ships the root
#: modules") is prose rather than a count and is left alone, and so is a vague
#: quantity. Both boundaries are pinned by
#: test_the_guard_leaves_a_claim_that_is_not_a_count_alone.
_CARDINAL = (
    r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|"
    r"dozen|couple"
)
#: Digits, or one or more cardinal words joined by spaces or hyphens.
_A_COUNT = rf"(?:\d[\d,]*|(?:{_CARDINAL})(?:[-\s](?:{_CARDINAL}))*)"
#: Room for "root", "bundled source" and the like around the noun.
_QUALIFIERS = r"(?:\s+\w+){0,2}"
_A_COUNT_OF_PYTHON_FILES = re.compile(
    rf"\b{_A_COUNT}{_QUALIFIERS}\s+(?:Python{_QUALIFIERS}\s+)?(?:files|modules)\b",
    re.IGNORECASE)


class TestVersionConstantRationale(unittest.TestCase):
    """The comment justifying `VERSION` must not restate the packaging list.

    It read "CHANGELOG.md is the canonical version reference, but it isn't
    bundled into the .vsix \u2014 only the three Python files are". That was true
    when it was written: `copy-python.js` then listed exactly `cli.py`,
    `scanner.py` and `dashboard.py`. The module split took the list to fourteen
    modules and sixteen web/vendor entries and never came back for the comment.

    The half that justifies the constant \u2014 the CHANGELOG really is not bundled \u2014
    is still true, so it is checked here rather than asserted in prose. The count
    is not restated in either place, because a hardcoded count is precisely what
    drifted; `tests/test_web_assets.py` derives the real module list from disk
    and is the one authority on it.
    """

    COPY_PYTHON = REPO_ROOT / "vscode-extension" / "scripts" / "copy-python.js"

    def _copy_python(self):
        return self.COPY_PYTHON.read_text(encoding="utf-8")

    def _version_rationale(self):
        """Everything in scanner.py above `VERSION = ` \u2014 the guard's whole target.

        Deliberately narrow: a docstring line, the imports and the rationale
        itself. Nothing there is supposed to count what ships, which is what
        lets the pattern above drop the word `Python` from the noun without
        tripping over ordinary prose elsewhere in the module.
        """
        source = (REPO_ROOT / "claude_usage" / "scanner.py").read_text(
            encoding="utf-8")
        return source[:source.index("VERSION = ")]

    def test_the_version_comment_counts_no_bundled_python_files(self):
        claim = _A_COUNT_OF_PYTHON_FILES.search(self._version_rationale())
        bundled = re.findall(r'"([^"]+\.py)"', self._copy_python())
        self.assertIsNone(
            claim,
            "scanner.py's VERSION comment claims %r, and copy-python.js bundles "
            "%d Python modules. Drop the count rather than correcting it \u2014 a "
            "hardcoded count is what went stale. If the phrase is innocent prose "
            "that merely counts files, reword it: this slice is small enough "
            "that the guard need not tell the two apart."
            % (claim.group(0) if claim else "", len(bundled)))

    def test_the_guard_catches_a_count_in_any_form(self):
        """The pattern must not be evadable by writing the number differently.

        Its first version spelled out one..ten plus digits, case-sensitively,
        with the count hard against the noun. Measured 2026-08-10, that let four
        shapes through \u2014 a word-number above ten (the very number a corrector
        reaches for on a tree that long ago outgrew "three"), a capital letter, a
        qualifier slipped in between, and a lower-case "python" \u2014 and a fifth
        that the sibling guard in tests/test_version.py also misses: dropping
        "Python" from the noun entirely. Planting the first shape in scanner.py's
        rationale left both modules green.

        The phrases are assembled rather than written out so this file can carry
        the cases without carrying a literal claim.
        """
        counts = ("three", "Three", "14", "1,014", "fifteen", "Fifteen",
                  "twenty-three", "one hundred")
        qualifiers = ("", "root ", "bundled source ")
        nouns = ("Python files", "Python modules", "python modules",
                 "modules", "files")
        for count in counts:
            for qualifier in qualifiers:
                for noun in nouns:
                    claim = f"the .vsix ships {count} {qualifier}{noun}"
                    with self.subTest(claim=claim):
                        self.assertIsNotNone(
                            _A_COUNT_OF_PYTHON_FILES.search(claim),
                            f"{claim!r} is a count of what ships and the guard "
                            "walks past it")

    def test_the_guard_leaves_a_claim_that_is_not_a_count_alone(self):
        """Where the guard stops, stated as assertions rather than as prose.

        A noun with no number in front of it is not a count and cannot go stale
        into a falsehood, and neither is a vague quantity. Both are left alone on
        purpose \u2014 the guard exists to stop a *number* rotting, not to ban the
        subject.
        """
        for phrase in ("the .vsix ships the root Python modules",
                       "the .vsix ships every Python module it needs",
                       "requires Python 3.11 or newer",
                       "a parity test guards all three; see tests/test_version.py"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(_A_COUNT_OF_PYTHON_FILES.search(phrase))

    def test_the_guard_reads_scanner_py_and_not_this_file(self):
        """The docstring above quotes the old, false wording. Deliberately.

        The pattern matches that quote, as it should \u2014 it is a count of Python
        files. What exempts it is the guard's TARGET: `_version_rationale` reads
        the slice of scanner.py above `VERSION = ` and nothing else. Widening the
        pattern is exactly the moment someone would think to point it at this
        file too, so the boundary is pinned here instead of being rediscovered by
        a red suite over a sentence that is history rather than a claim.
        """
        historical = "only the three Python files are"
        self.assertIn(historical, Path(__file__).read_text(encoding="utf-8"),
                      "the historical quote this test exists for has moved")
        self.assertIsNotNone(
            _A_COUNT_OF_PYTHON_FILES.search(historical),
            "if the pattern stopped matching it, the exemption below would be "
            "vacuous and this test would pass while checking nothing")
        self.assertNotIn(historical, self._version_rationale())

    def test_the_changelog_really_is_not_bundled(self):
        """The load-bearing half of the rationale, checked instead of asserted.

        If the `.vsix` ever did ship the CHANGELOG, `VERSION` would not need to
        be a constant and the comment would be wrong in the other direction.
        """
        self.assertNotIn("CHANGELOG", self._copy_python())


def _make_assistant_record(session_id="sess-1", model="claude-sonnet-4-6",
                           input_tokens=100, output_tokens=50,
                           cache_read=10, cache_creation=5,
                           timestamp="2026-04-08T10:00:00Z",
                           cwd="/home/user/project",
                           message_id="", cache_creation_1h=None):
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }
    if cache_creation_1h is not None:
        # Real records carry the write split beside the flat total, and the two
        # always agree: ephemeral_1h + ephemeral_5m == cache_creation_input_tokens.
        usage["cache_creation"] = {
            "ephemeral_1h_input_tokens": cache_creation_1h,
            "ephemeral_5m_input_tokens": max(0, cache_creation - cache_creation_1h),
        }
    msg = {
        "model": model,
        "usage": usage,
        "content": [],
    }
    if message_id:
        msg["id"] = message_id
    return json.dumps({
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
        "message": msg,
    })


def _make_user_record(session_id="sess-1", timestamp="2026-04-08T09:59:00Z",
                      cwd="/home/user/project"):
    return json.dumps({
        "type": "user",
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
    })


def _make_user_record_with_text(session_id="sess-1", text="Please fix the bug",
                                timestamp="2026-04-08T09:59:00Z",
                                cwd="/home/user/project"):
    return json.dumps({
        "type": "user",
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
        "message": {"content": [{"type": "text", "text": text}]},
    })


def _make_custom_title_record(session_id="sess-1", title="Custom Topic"):
    return json.dumps({
        "type": "custom-title",
        "sessionId": session_id,
        "customTitle": title,
    })


def _make_ai_title_record(session_id="sess-1", title="AI Topic"):
    return json.dumps({
        "type": "ai-title",
        "sessionId": session_id,
        "aiTitle": title,
    })


class TestParseJsonlFile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write_jsonl(self, filename, lines):
        path = os.path.join(self.tmpdir, filename)
        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        return path

    def test_basic_parsing(self):
        path = self._write_jsonl("test.jsonl", [
            _make_user_record(),
            _make_assistant_record(),
        ])
        metas, turns, _, _, line_count = parse_jsonl_file(path)
        self.assertEqual(len(metas), 1)
        self.assertEqual(len(turns), 1)
        self.assertEqual(metas[0]["session_id"], "sess-1")
        self.assertEqual(turns[0]["input_tokens"], 100)
        self.assertEqual(turns[0]["output_tokens"], 50)
        self.assertEqual(line_count, 2)

    def test_skips_zero_token_records(self):
        path = self._write_jsonl("test.jsonl", [
            _make_assistant_record(input_tokens=0, output_tokens=0,
                                   cache_read=0, cache_creation=0),
        ])
        _, turns, _, _, _ = parse_jsonl_file(path)
        self.assertEqual(len(turns), 0)

    def test_skips_non_assistant_user_types(self):
        path = self._write_jsonl("test.jsonl", [
            json.dumps({"type": "system", "sessionId": "s1"}),
            _make_assistant_record(session_id="s1"),
        ])
        metas, turns, _, _, _ = parse_jsonl_file(path)
        self.assertEqual(len(turns), 1)

    def test_handles_malformed_json(self):
        path = self._write_jsonl("test.jsonl", [
            "not valid json",
            _make_assistant_record(),
        ])
        _, turns, _, _, _ = parse_jsonl_file(path)
        self.assertEqual(len(turns), 1)

    def test_malformed_metadata_does_not_abort_following_records(self):
        malformed_message = json.dumps({
            "type": "assistant",
            "sessionId": "bad-message",
            "message": ["not", "an", "object"],
        })
        malformed_session = json.dumps({
            "type": "assistant",
            "sessionId": ["not-hashable"],
            "message": {},
        })
        path = self._write_jsonl("test.jsonl", [
            "[]",
            malformed_message,
            malformed_session,
            _make_assistant_record(session_id="valid"),
        ])

        metas, turns, _, _, line_count = parse_jsonl_file(path)

        self.assertEqual(line_count, 4)
        self.assertEqual([m["session_id"] for m in metas], ["valid"])
        self.assertEqual([t["session_id"] for t in turns], ["valid"])

    def test_token_metadata_is_not_coerced_from_untrusted_types(self):
        record = json.loads(_make_assistant_record())
        record["message"]["usage"] = {
            "input_tokens": "<img src=x onerror=alert(1)>",
            "output_tokens": 50,
            "cache_read_input_tokens": -1,
            "cache_creation_input_tokens": 1 << 80,
        }
        path = self._write_jsonl("test.jsonl", [json.dumps(record)])

        _, turns, _, _, _ = parse_jsonl_file(path)

        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["input_tokens"], 0)
        self.assertEqual(turns[0]["output_tokens"], 50)
        self.assertEqual(turns[0]["cache_read_tokens"], 0)
        self.assertEqual(turns[0]["cache_creation_tokens"], 0)

    def test_oversized_line_is_skipped_without_hiding_following_record(self):
        """STRENGTHENED — it never reached the branch it names. It patched
        `scanner.MAX_JSONL_LINE_LENGTH`, which is a second name bound to the
        same object: rebinding it leaves the global `_iter_jsonl_lines` actually
        reads at 64 MiB, so a 2 KiB line was never oversized and the record was
        dropped by the JSON decoder instead (verified: the two paths print
        different reasons). The line here is valid JSON, so its length is the
        only thing that can drop it, and the drop is now counted rather than
        silent — that skip is the same class of permanent loss as an undecodable
        line, and reporting only one of the two leaves the total wrong."""
        import transcripts
        path = self._write_jsonl("test.jsonl", [
            json.dumps({"type": "assistant", "sessionId": "huge",
                        "padding": "x" * 2048}),
            _make_assistant_record(session_id="valid"),
        ])

        with mock.patch.object(transcripts, "MAX_JSONL_LINE_LENGTH", 1024):
            # stderr: the warning shares a stream with the read-error warning
            # beside it rather than with `cmd_dashboard`'s authenticated URL.
            with contextlib.redirect_stderr(io.StringIO()) as out:
                metas, turns, _, _, line_count = parse_jsonl_file(path)

        self.assertEqual(line_count, 2)
        self.assertEqual([m["session_id"] for m in metas], ["valid"])
        self.assertEqual([t["session_id"] for t in turns], ["valid"])
        self.assertIn("skipped 1 unreadable record", out.getvalue())

    def test_handles_empty_file(self):
        path = self._write_jsonl("test.jsonl", [])
        metas, turns, _, _, _ = parse_jsonl_file(path)
        self.assertEqual(len(metas), 0)
        self.assertEqual(len(turns), 0)

    def test_multiple_sessions(self):
        path = self._write_jsonl("test.jsonl", [
            _make_assistant_record(session_id="s1"),
            _make_assistant_record(session_id="s2"),
        ])
        metas, turns, _, _, _ = parse_jsonl_file(path)
        self.assertEqual(len(metas), 2)
        self.assertEqual(len(turns), 2)

    def test_session_timestamps_tracked(self):
        path = self._write_jsonl("test.jsonl", [
            _make_user_record(timestamp="2026-04-08T09:00:00Z"),
            _make_assistant_record(timestamp="2026-04-08T09:05:00Z"),
            _make_assistant_record(timestamp="2026-04-08T09:10:00Z"),
        ])
        metas, _, _, _, _ = parse_jsonl_file(path)
        self.assertEqual(metas[0]["first_timestamp"], "2026-04-08T09:00:00Z")
        self.assertEqual(metas[0]["last_timestamp"], "2026-04-08T09:10:00Z")

    def test_tool_name_extracted(self):
        record = json.dumps({
            "type": "assistant",
            "sessionId": "s1",
            "timestamp": "2026-04-08T10:00:00Z",
            "cwd": "/tmp",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 100, "output_tokens": 50,
                          "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0},
                "content": [{"type": "tool_use", "name": "Read"}],
            },
        })
        path = self._write_jsonl("test.jsonl", [record])
        _, turns, _, _, _ = parse_jsonl_file(path)
        self.assertEqual(turns[0]["tool_name"], "Read")


class TestScanRootResolution(unittest.TestCase):
    """Extra transcript roots are additive; the defaults are never dropped.

    The asymmetry between the two callers is the point. The CLI passes
    `include_defaults=True` so `--projects-dir` means "also look here" — a
    database that quietly lost your own ~/.claude/projects because you named a
    container's mount is worse than one carrying both. The Python API defaults
    to False so `scan(projects_dir=tmp)` still means exactly that directory;
    every other test in this suite relies on it, and without that split they
    would all start ingesting the developer's real history.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.a = self.tmpdir / "rootA"; self.a.mkdir()
        self.b = self.tmpdir / "rootB"; self.b.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_the_python_api_still_means_exactly_this_directory(self):
        roots, missing = resolve_scan_roots(projects_dir=self.a)
        self.assertEqual(roots, [self.a])
        self.assertEqual(missing, [])

    def test_the_cli_adds_to_the_defaults_rather_than_replacing_them(self):
        roots, _ = resolve_scan_roots(projects_dirs=[self.a, self.b],
                                      include_defaults=True, env={})
        existing_defaults = [d for d in DEFAULT_PROJECTS_DIRS if d.is_dir()]
        for default in existing_defaults:
            self.assertIn(default, roots,
                          "a named extra root must never drop your own history")
        self.assertIn(self.a, roots)
        self.assertIn(self.b, roots)

    def test_the_defaults_come_first_so_the_log_reads_sensibly(self):
        roots, _ = resolve_scan_roots(projects_dirs=[self.a],
                                      include_defaults=True, env={})
        if len(roots) > 1:
            self.assertEqual(roots[-1], self.a)

    def test_the_env_var_is_additive_too(self):
        roots, _ = resolve_scan_roots(
            include_defaults=True,
            env={"CLAUDE_USAGE_PROJECTS_DIRS": os.pathsep.join([str(self.a), str(self.b)])})
        self.assertIn(self.a, roots)
        self.assertIn(self.b, roots)

    def test_flag_and_env_roots_combine(self):
        roots, _ = resolve_scan_roots(
            projects_dirs=[self.a],
            env={"CLAUDE_USAGE_PROJECTS_DIRS": str(self.b)})
        self.assertEqual(roots, [self.a, self.b])

    def test_blank_entries_in_the_env_var_are_ignored(self):
        """A trailing separator, or an unset shell variable expanded into it,
        must not turn into a root of ''  — which resolves to the cwd."""
        raw = os.pathsep.join([str(self.a), "", "   "])
        roots, missing = resolve_scan_roots(
            env={"CLAUDE_USAGE_PROJECTS_DIRS": raw})
        self.assertEqual(roots, [self.a])
        self.assertEqual(missing, [])

    def test_a_root_named_twice_is_walked_once(self):
        """Overlapping roots would parse every file twice and write a second
        processed_files row for each."""
        roots, _ = resolve_scan_roots(
            projects_dirs=[self.a, str(self.a) + os.sep, self.a], env={})
        self.assertEqual(roots, [self.a])

    def test_a_requested_root_that_is_missing_is_reported(self):
        absent = self.tmpdir / "nope"
        roots, missing = resolve_scan_roots(projects_dirs=[self.a, absent], env={})
        self.assertEqual(roots, [self.a])
        self.assertEqual(missing, [absent])

    def test_an_absent_default_is_not_reported_as_missing(self):
        """Most machines have no Xcode coding-assistant directory; complaining
        about it once per scan would be noise, not a signal."""
        _roots, missing = resolve_scan_roots(include_defaults=True, env={})
        for default in DEFAULT_PROJECTS_DIRS:
            self.assertNotIn(default, missing)

    def test_a_missing_root_is_surfaced_by_an_actual_scan(self):
        absent = self.tmpdir / "nope"
        with mock.patch("builtins.print") as printed:
            scan(projects_dirs=[self.a, absent],
                 db_path=self.tmpdir / "usage.db", verbose=False)
        said = " ".join(str(c) for c in printed.call_args_list)
        self.assertIn("not found", said)

    def test_scanning_two_roots_ingests_both(self):
        for root, sid in ((self.a, "sess-a"), (self.b, "sess-b")):
            proj = root / "proj"
            proj.mkdir()
            (proj / f"{sid}.jsonl").write_text(
                _make_assistant_record(session_id=sid, message_id="m-" + sid) + "\n",
                encoding="utf-8")
        db_path = self.tmpdir / "usage.db"
        scan(projects_dirs=[self.a, self.b], db_path=db_path, verbose=False)
        conn = sqlite3.connect(db_path)
        got = {r[0] for r in conn.execute("SELECT session_id FROM sessions")}
        conn.close()
        self.assertEqual(got, {"sess-a", "sess-b"})


class TestCacheWriteTierSplit(unittest.TestCase):
    """Cache writes bill at 1.25x input for 5 minutes and 2x for an hour.

    Exercise both cache-write tiers so the one-hour subset is priced at its
    own rate."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _parse(self, record):
        path = Path(self.tmpdir) / "s.jsonl"
        path.write_text(record + "\n", encoding="utf-8")
        _metas, turns, _agents, _limits, _lines = parse_jsonl_file(str(path))
        return turns

    def test_the_one_hour_slice_is_recorded(self):
        turns = self._parse(_make_assistant_record(
            cache_creation=1000, cache_creation_1h=400))
        self.assertEqual(turns[0]["cache_creation_tokens"], 1000)
        self.assertEqual(turns[0]["cache_creation_1h_tokens"], 400)

    def test_a_record_with_no_split_reports_none_of_it_as_long_lived(self):
        """Older transcripts have no `cache_creation` object at all."""
        turns = self._parse(_make_assistant_record(cache_creation=1000))
        self.assertEqual(turns[0]["cache_creation_tokens"], 1000)
        self.assertEqual(turns[0]["cache_creation_1h_tokens"], 0)

    def test_the_slice_can_never_exceed_its_own_total(self):
        """The two are summed independently by SQL; a 1-hour figure larger than
        the write it belongs to would make the 5-minute remainder negative."""
        turns = self._parse(_make_assistant_record(
            cache_creation=100, cache_creation_1h=999))
        self.assertLessEqual(turns[0]["cache_creation_1h_tokens"],
                             turns[0]["cache_creation_tokens"])

    def test_a_malformed_split_object_is_ignored_rather_than_fatal(self):
        record = json.loads(_make_assistant_record(cache_creation=1000))
        record["message"]["usage"]["cache_creation"] = "not-an-object"
        turns = self._parse(json.dumps(record))
        self.assertEqual(turns[0]["cache_creation_1h_tokens"], 0)

    def test_the_split_survives_the_streaming_merge_and_session_rollup(self):
        """insert_turns merges on message_id with MAX() (invariant 1).

        Only the `turns` half is asserted here — this calls `insert_turns`
        directly and never runs a scan, so despite the name it says nothing
        about invariant 2. The rollup half is
        `TestCrossFileSessionTotals.test_session_across_files_not_inflated`.
        """
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        partial = {"session_id": "s1", "timestamp": "2026-04-08T10:00:00Z",
                   "model": "claude-opus-5", "input_tokens": 1, "output_tokens": 1,
                   "cache_read_tokens": 0, "cache_creation_tokens": 500,
                   "cache_creation_1h_tokens": 200, "tool_name": None,
                   "message_id": "msg-1"}
        final = dict(partial, cache_creation_tokens=1000, cache_creation_1h_tokens=400)
        insert_turns(conn, [partial])
        insert_turns(conn, [final])          # the completing streamed record
        insert_turns(conn, [partial])        # a replay must not walk it back
        row = conn.execute("SELECT cache_creation_tokens AS cc, "
                           "cache_creation_1h_tokens AS cc1h FROM turns").fetchone()
        self.assertEqual((row["cc"], row["cc1h"]), (1000, 400))
        conn.close()


class TestMessageIdDedup(unittest.TestCase):
    """Test deduplication of streaming events by message.id."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write_jsonl(self, filename, lines):
        path = os.path.join(self.tmpdir, filename)
        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        return path

    def test_streaming_events_deduped(self):
        """Multiple records with same message.id should produce one turn."""
        path = self._write_jsonl("test.jsonl", [
            # Streaming event 1: partial usage
            _make_assistant_record(message_id="msg-abc", input_tokens=50, output_tokens=10),
            # Streaming event 2: more usage (same message)
            _make_assistant_record(message_id="msg-abc", input_tokens=100, output_tokens=50),
            # Streaming event 3: final usage (same message)
            _make_assistant_record(message_id="msg-abc", input_tokens=150, output_tokens=80),
        ])
        _, turns, _, _, _ = parse_jsonl_file(path)
        self.assertEqual(len(turns), 1)
        # Last record wins (has final tallies)
        self.assertEqual(turns[0]["input_tokens"], 150)
        self.assertEqual(turns[0]["output_tokens"], 80)
        self.assertEqual(turns[0]["message_id"], "msg-abc")

    def test_different_message_ids_kept(self):
        """Records with different message.id are separate turns."""
        path = self._write_jsonl("test.jsonl", [
            _make_assistant_record(message_id="msg-1", input_tokens=100),
            _make_assistant_record(message_id="msg-2", input_tokens=200),
        ])
        _, turns, _, _, _ = parse_jsonl_file(path)
        self.assertEqual(len(turns), 2)

    def test_records_without_message_id_kept(self):
        """Records without message.id are kept as-is (no dedup)."""
        path = self._write_jsonl("test.jsonl", [
            _make_assistant_record(input_tokens=100),
            _make_assistant_record(input_tokens=200),
        ])
        _, turns, _, _, _ = parse_jsonl_file(path)
        self.assertEqual(len(turns), 2)

    def test_mixed_with_and_without_ids(self):
        """Mix of records with and without message.id."""
        path = self._write_jsonl("test.jsonl", [
            _make_assistant_record(message_id="msg-1", input_tokens=50),
            _make_assistant_record(message_id="msg-1", input_tokens=100),  # deduped
            _make_assistant_record(input_tokens=200),  # no id, kept
        ])
        _, turns, _, _, _ = parse_jsonl_file(path)
        self.assertEqual(len(turns), 2)  # 1 deduped + 1 without id
        token_sums = sorted([t["input_tokens"] for t in turns])
        self.assertEqual(token_sums, [100, 200])

    def test_equal_ids_from_different_sources_are_independent(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        base = {
            "session_id": "shared", "timestamp": "2026-08-22T10:00:00Z",
            "model": "claude-opus-5", "input_tokens": 10,
            "output_tokens": 20, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "tool_name": None,
            "message_id": "same-message", "source": "claude",
        }
        insert_turns(conn, [base])
        insert_turns(conn, [dict(
            base, source="codex", model="gpt-5.4", input_tokens=30)])
        rows = conn.execute(
            "SELECT source, model, input_tokens FROM turns ORDER BY source"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("claude", "claude-opus-5", 10), ("codex", "gpt-5.4", 30)],
        )


class TestMessageIdDedupIntegration(unittest.TestCase):
    """Integration test: dedup across scan cycles."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = Path(self.tmpdir) / "projects" / "user" / "proj"
        self.projects_dir.mkdir(parents=True)
        self.db_path = Path(self.tmpdir) / "usage.db"
        self.filepath = self.projects_dir / "sess-1.jsonl"

    def test_streaming_dedup_reduces_turn_count(self):
        """3 streaming events for 2 messages should produce 2 turns."""
        with open(self.filepath, "w", encoding="utf-8") as f:
            f.write(_make_user_record(session_id="sess-1") + "\n")
            f.write(_make_assistant_record(session_id="sess-1",
                                           message_id="msg-1",
                                           input_tokens=50, output_tokens=20) + "\n")
            f.write(_make_assistant_record(session_id="sess-1",
                                           message_id="msg-1",
                                           input_tokens=100, output_tokens=50) + "\n")
            f.write(_make_assistant_record(session_id="sess-1",
                                           message_id="msg-2",
                                           input_tokens=200, output_tokens=100) + "\n")

        result = scan(projects_dir=self.projects_dir.parent.parent,
                      db_path=self.db_path, verbose=False)
        self.assertEqual(result["turns"], 2)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        turns = conn.execute("SELECT * FROM turns ORDER BY input_tokens").fetchall()
        self.assertEqual(len(turns), 2)
        # msg-1: last record wins (100/50), msg-2: 200/100
        self.assertEqual(turns[0]["input_tokens"], 100)
        self.assertEqual(turns[1]["input_tokens"], 200)
        # Session totals should reflect deduped values
        session = conn.execute("SELECT * FROM sessions").fetchone()
        self.assertEqual(session["total_input_tokens"], 300)  # 100 + 200
        conn.close()

    def test_cross_file_dedup_via_unique_index(self):
        """Re-scanning a file shouldn't create duplicate turns for same message_id."""
        with open(self.filepath, "w", encoding="utf-8") as f:
            f.write(_make_user_record(session_id="sess-1") + "\n")
            f.write(_make_assistant_record(session_id="sess-1",
                                           message_id="msg-1",
                                           input_tokens=100, output_tokens=50) + "\n")

        scan(projects_dir=self.projects_dir.parent.parent,
             db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        count1 = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        self.assertEqual(count1, 1)

        # Delete processed_files to force re-scan
        conn.execute("DELETE FROM processed_files")
        conn.commit()
        conn.close()

        scan(projects_dir=self.projects_dir.parent.parent,
             db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        count2 = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        conn.close()
        # Should still be 1 turn (UNIQUE index prevents duplicate)
        self.assertEqual(count2, 1)

    def test_an_older_database_ends_up_with_message_id(self):
        """Renamed from `test_schema_migration_adds_message_id`, and its fixture
        rebuilt, because both described machinery that no longer exists.

        There is no migration: an older database is DROPPED and recreated, so
        the column arrives by rebuild rather than by `ALTER TABLE`. The outcome
        the test cares about is unchanged and still worth pinning.

        The fixture had to change for a second reason, and it is the interesting
        one. It hand-built a `turns` with no `message_id`, which is a state no
        released build ever produced -- running each released tag's own
        `init_db` (v1.0.0, v1.2.0, v1.5.0, v1.5.4, v1.6.1, measured 2026-08-16)
        leaves `session_id`, `message_id` and `input_tokens` all present, because
        the column was added by `_ensure_column` at open time even when the
        `CREATE TABLE` lacked it. Since the ownership gate now reads columns, a
        `turns` missing them is not an old usage.db at all -- it is somebody
        else's file, and `init_db` correctly refuses it rather than dropping it.
        """
        import db
        conn = sqlite3.connect(self.db_path)
        for table, columns in db._LEGACY_BASE_SCHEMA.items():
            declared = ", ".join(f'"{column}" TEXT' for column in columns)
            conn.execute(f'CREATE TABLE "{table}" ({declared})')
        conn.commit()
        conn.close()

        import contextlib
        import io
        from scanner import get_db, init_db
        conn = get_db(self.db_path)
        with contextlib.redirect_stderr(io.StringIO()):
            init_db(conn)
        col_names = [r["name"] for r in conn.execute("PRAGMA table_info(turns)")]
        self.assertIn("message_id", col_names)
        # And the rest of the current schema came with it, which an ALTER TABLE
        # migration would not have brought.
        self.assertIn("git_branch", col_names)
        conn.close()


class TestAggregateSessions(unittest.TestCase):
    def test_aggregation(self):
        metas = [{"session_id": "s1", "project_name": "test",
                  "first_timestamp": "t1", "last_timestamp": "t2",
                  "git_branch": "main", "model": None}]
        turns = [
            {"session_id": "s1", "input_tokens": 100, "output_tokens": 50,
             "cache_read_tokens": 10, "cache_creation_tokens": 5, "model": "claude-sonnet-4-6"},
            {"session_id": "s1", "input_tokens": 200, "output_tokens": 100,
             "cache_read_tokens": 20, "cache_creation_tokens": 10, "model": "claude-sonnet-4-6"},
        ]
        sessions = aggregate_sessions(metas, turns)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["total_input_tokens"], 300)
        self.assertEqual(sessions[0]["total_output_tokens"], 150)
        self.assertEqual(sessions[0]["turn_count"], 2)
        self.assertEqual(sessions[0]["model"], "claude-sonnet-4-6")

    def test_empty_turns(self):
        metas = [{"session_id": "s1", "project_name": "test",
                  "first_timestamp": "t1", "last_timestamp": "t2",
                  "git_branch": "main", "model": None}]
        sessions = aggregate_sessions(metas, [])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["total_input_tokens"], 0)
        self.assertEqual(sessions[0]["turn_count"], 0)


class TestSessionPrimaryModelIsScanOrderIndependent(unittest.TestCase):
    """A session's primary model must not depend on how the file was scanned.

    `upsert_sessions` picks the higher-priority model when a session is updated
    (a subagent's haiku turns must not overwrite the session's opus label), but
    `aggregate_sessions` used to pick the most FREQUENT model when the session
    was first inserted. The two rules disagree, and which one applies depends on
    whether the transcript was read in one pass or grew between scans — so the
    same bytes on disk produced 'claude-haiku-4-5' for a one-shot scan and
    'claude-opus-5' for an incremental one.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = Path(self.tmpdir) / "projects"
        (self.projects_dir / "user" / "proj").mkdir(parents=True)
        self.filepath = self.projects_dir / "user" / "proj" / "sess-1.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # Three opus turns then nine haiku ones: haiku wins on count, opus on
    # priority. This is the ordinary shape of a session that dispatched
    # subagents, not a contrived one.
    def _chunk(self, model, count, first_minute):
        return [
            _make_assistant_record(
                session_id="sess-1", model=model,
                timestamp=f"2026-04-08T10:{first_minute + i:02d}:00Z",
                message_id=f"msg-{model}-{i}")
            for i in range(count)
        ]

    def _primary_model_after(self, chunks):
        db_path = Path(self.tmpdir) / f"usage-{len(chunks)}.db"
        for index, chunk in enumerate(chunks):
            with open(self.filepath, "a", encoding="utf-8") as handle:
                handle.write("\n".join(chunk) + "\n")
            # Move mtime forward deliberately rather than sleeping: the scanner
            # skips a file whose mtime is unchanged.
            os.utime(self.filepath, (1_000_000 + index * 100, 1_000_000 + index * 100))
            scan(projects_dir=self.projects_dir, db_path=db_path, verbose=False)
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(
                "SELECT model FROM sessions WHERE session_id = 'sess-1'").fetchone()[0]
        finally:
            conn.close()
            self.filepath.unlink()
            self.filepath.touch()

    def test_one_shot_and_incremental_scans_agree(self):
        # Haiku deliberately outnumbers opus: with equal counts the frequency
        # rule happens to agree with the priority one, and the test would pass
        # against the very code it exists to catch.
        opus, haiku = self._chunk("claude-opus-5", 3, 0), self._chunk("claude-haiku-4-5", 9, 10)
        one_shot = self._primary_model_after([opus + haiku])
        incremental = self._primary_model_after([opus, haiku])
        self.assertEqual(
            one_shot, incremental,
            "the same transcript reported a different primary model depending "
            "on whether it was read in one pass or in two")

    def test_scans_agree_when_the_two_models_share_a_priority(self):
        """The same defect, for every model MODEL_PRIORITY has never heard of.

        Priority only knows Anthropic's family names, so *every* Codex model and
        every unpriced local/3rd-party model this project deliberately supports
        (gemma, glm, ...) scores 0. With the scores tied, `aggregate_sessions`
        resolves by frequency over whatever it was handed while `upsert_sessions`
        refuses to move off the incumbent (`0 > 0` is false) — so a one-shot scan
        sees the whole file's frequency and an incremental scan sees only the
        first chunk's, from identical bytes on disk.
        """
        first = self._chunk("glm-4.6", 3, 0)
        second = self._chunk("gemma-3-27b", 9, 10)
        one_shot = self._primary_model_after([first + second])
        incremental = self._primary_model_after([first, second])
        self.assertEqual(
            one_shot, incremental,
            "two models of equal priority: the primary model reported depends "
            "on when the user happened to rescan")

    def test_a_tied_priority_session_is_labelled_by_its_whole_history(self):
        """Which of the two agreeing answers is right: the one over all turns."""
        first = self._chunk("glm-4.6", 3, 0)
        second = self._chunk("gemma-3-27b", 9, 10)
        self.assertEqual(self._primary_model_after([first, second]), "gemma-3-27b")

    def test_the_more_capable_model_is_the_primary_one(self):
        """Which of the two agreeing answers is right: the documented priority."""
        opus, haiku = self._chunk("claude-opus-5", 3, 0), self._chunk("claude-haiku-4-5", 9, 10)
        self.assertEqual(self._primary_model_after([opus + haiku]), "claude-opus-5")

    def test_frequency_still_breaks_a_tie_within_one_priority(self):
        """Priority is coarse — two opus builds share a score, so count decides."""
        metas = [{"session_id": "s1", "project_name": "p", "first_timestamp": "t",
                  "last_timestamp": "t", "git_branch": "", "model": None}]
        turns = [{"session_id": "s1", "input_tokens": 1, "output_tokens": 1,
                  "cache_read_tokens": 0, "cache_creation_tokens": 0,
                  "model": model}
                 for model in ("claude-opus-4-8", "claude-opus-5", "claude-opus-5")]
        self.assertEqual(aggregate_sessions(metas, turns)[0]["model"], "claude-opus-5")

    def test_capability_outranks_frequency_in_aggregate_sessions(self):
        """The asymmetric case, on the function itself.

        Every other test of the rule reads `sessions.model` after a scan, and
        the end-of-scan reconciliation recomputes that column from `turns` — so
        reverting `aggregate_sessions` to the frequency rule its own comment
        warns about left the whole suite green. The value it returns is still
        what a scan interrupted before the reconciliation leaves committed, and
        what `COALESCE` keeps for a session whose turns carry no model.
        """
        metas = [{"session_id": "s1", "project_name": "p", "first_timestamp": "t",
                  "last_timestamp": "t", "git_branch": "", "model": None}]
        turns = [{"session_id": "s1", "input_tokens": 1, "output_tokens": 1,
                  "cache_read_tokens": 0, "cache_creation_tokens": 0,
                  "model": model}
                 for model in ["claude-opus-5"] * 3 + ["claude-haiku-4-5"] * 9]
        self.assertEqual(aggregate_sessions(metas, turns)[0]["model"],
                         "claude-opus-5")

    def test_upsert_sessions_keeps_the_more_capable_model(self):
        """The same rule's second Python copy, which is equally unobserved.

        `upsert_sessions` refuses to move a session off a higher-priority model
        (a subagent's haiku turns must not relabel an opus session); inverting
        that comparison also left the whole suite green, because the
        reconciliation overwrites the column before anything reads it.
        """
        conn = get_db(Path(self.tmpdir) / "priority.db")
        init_db(conn)
        base = {"session_id": "s1", "project_name": "p",
                "first_timestamp": "2026-04-08T10:00:00Z",
                "last_timestamp": "2026-04-08T10:00:00Z", "git_branch": "main",
                "total_input_tokens": 1, "total_output_tokens": 1,
                "total_cache_read": 0, "total_cache_creation": 0,
                "turn_count": 1}
        upsert_sessions(conn, [dict(base, model="claude-opus-5")])
        upsert_sessions(conn, [dict(base, model="claude-haiku-4-5")])
        conn.commit()
        model = conn.execute(
            "SELECT model FROM sessions WHERE session_id = 's1'").fetchone()[0]
        conn.close()
        self.assertEqual(model, "claude-opus-5")


class _ConnectionThatLosesTheInsertRace:
    """A connection proxy that lets a second writer win the INSERT race.

    `upsert_sessions` is check-then-act: it SELECTs the session row and INSERTs
    when that returns nothing. This fires `interloper` in exactly that window —
    after our SELECT decided the row was absent, before our INSERT runs — so the
    race reproduces deterministically in one process. Two real processes are
    what the defect needs, but as a regression test they cannot be trusted: a
    loaded runner that happens to serialise the two scans passes against the
    unfixed code, i.e. the test could not fail for the right reason.
    """

    def __init__(self, conn, interloper):
        self._conn = conn
        self._interloper = interloper
        self.fired = False

    def execute(self, sql, params=()):
        if not self.fired and "INSERT" in sql and "INTO sessions" in sql:
            self.fired = True
            self._interloper()
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestConcurrentFirstInsertOfASession(unittest.TestCase):
    """Two scanners that first-see the same session must both survive.

    `_SCAN_LOCK` serialises scans inside ONE process, and the product runs
    several against one ~/.claude/usage.db: a dashboard process per VS Code
    window, plus a terminal `cli.py scan`. Both take `upsert_sessions`'
    `existing is None` branch for a session neither has stored yet, and before
    the conflict clause the loser's bare INSERT raised `sqlite3.IntegrityError:
    UNIQUE constraint failed: sessions.session_id` straight out of `scan()` —
    `cli.py scan` exited 1 with a traceback, a dashboard's startup scan printed
    "Background scan failed" and ingested nothing more.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "usage.db"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _session(self, project_name):
        return {"session_id": "s1", "project_name": project_name,
                "first_timestamp": "2026-04-08T09:00:00Z",
                "last_timestamp": "2026-04-08T10:00:00Z",
                "git_branch": "main", "model": "claude-sonnet-4-6",
                "total_input_tokens": 100, "total_output_tokens": 50,
                "total_cache_read": 10, "total_cache_creation": 5,
                "turn_count": 2}

    def test_upsert_sessions_survives_another_writer_inserting_first(self):
        loser = get_db(self.db_path)
        init_db(loser)
        winner = get_db(self.db_path)

        def wins_the_race():
            upsert_sessions(winner, [self._session("winner/proj")])
            winner.commit()

        racing = _ConnectionThatLosesTheInsertRace(loser, wins_the_race)
        upsert_sessions(racing, [self._session("loser/proj")])
        loser.commit()
        row = loser.execute(
            "SELECT * FROM sessions WHERE session_id = 's1'").fetchone()
        winner.close()
        loser.close()

        self.assertTrue(racing.fired)
        # The winner's row stands — the UPDATE branch rewrites neither
        # project_name nor first_timestamp ...
        self.assertEqual(row["project_name"], "winner/proj")
        # ... and the loser falls through to that branch instead of dropping its
        # chunk, so it still contributes its topic, branch and last_timestamp.
        # Its tokens land on top of the winner's identical ones, which is why
        # the row reads double here; the end-of-scan reconciliation from `turns`
        # (invariant 2) is what repairs that, and the scan-level test below is
        # what pins it. `INSERT OR IGNORE` would leave 100/2 instead.
        self.assertEqual(row["total_input_tokens"], 200)
        self.assertEqual(row["turn_count"], 4)

    def test_a_raced_first_scan_still_reconciles_session_totals(self):
        projects_dir = Path(self.tmpdir) / "projects"
        (projects_dir / "user" / "proj").mkdir(parents=True)
        (projects_dir / "user" / "proj" / "s1.jsonl").write_text("\n".join(
            _make_assistant_record(session_id="s1", message_id=f"msg-{i}",
                                   timestamp=f"2026-04-08T10:{i:02d}:00Z")
            for i in range(3)) + "\n", encoding="utf-8")

        def wins_the_race(sessions):
            other = get_db(self.db_path)
            try:
                upsert_sessions(other, sessions)
                other.commit()
            finally:
                other.close()

        raced = []

        def racing_upsert(conn, sessions):
            if not raced:
                raced.append(True)
                conn = _ConnectionThatLosesTheInsertRace(
                    conn, lambda: wins_the_race(sessions))
            return upsert_sessions(conn, sessions)

        with mock.patch("scanner.upsert_sessions", racing_upsert):
            scan(projects_dir=projects_dir, db_path=self.db_path, verbose=False)

        self.assertTrue(raced)
        conn = get_db(self.db_path)
        turns = conn.execute(
            "SELECT COUNT(*) c, SUM(input_tokens) i, SUM(output_tokens) o "
            "FROM turns").fetchone()
        session = conn.execute(
            "SELECT * FROM sessions WHERE session_id = 's1'").fetchone()
        conn.close()

        # The scan that lost the race ran to completion instead of dying at the
        # INSERT, so its turns are stored ...
        self.assertEqual(turns["c"], 3)
        # ... and the end-of-scan reconciliation rewrote the session's totals
        # from `turns`, so the loser's additive UPDATE on top of the winner's
        # row leaves no double count behind. This is the assertion that would
        # catch a future change narrowing that reconciliation to the sessions
        # one scan touched.
        self.assertEqual(session["turn_count"], turns["c"])
        self.assertEqual(session["total_input_tokens"], turns["i"])
        self.assertEqual(session["total_output_tokens"], turns["o"])


class TestDatabaseOperations(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX permission bits only")
    def test_get_db_enforces_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "private" / "usage.db"
            conn = get_db(db_path)
            conn.close()
            self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(db_path.parent.stat().st_mode & 0o777, 0o700)

            os.chmod(db_path, 0o644)
            conn = get_db(db_path)
            conn.close()
            self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits only")
    def test_already_private_database_stays_readable_when_chmod_unavailable(self):
        """An already-private file remains usable on an immutable mount.

        The mode is 0600 before the simulated EPERM, so no group/other reader
        can observe the database while the hygiene chmod is unavailable.
        Foreign-owned and non-private files take the fail-closed branches
        below instead.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "usage.db"
            get_db(db_path).close()

            with mock.patch("os.fchmod", side_effect=PermissionError(1, "EPERM")):
                self.assertEqual(secure_db_permissions(db_path), db_path)
                self.assertEqual(secure_db_permissions(db_path, create=True),
                                 db_path)
                get_db(db_path).close()

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits only")
    def test_nonprivate_database_refuses_when_chmod_unavailable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "usage.db"
            get_db(db_path).close()
            os.chmod(db_path, 0o644)
            with mock.patch("os.fchmod", side_effect=PermissionError(1, "EPERM")):
                with self.assertRaisesRegex(RuntimeError, "non-private"):
                    get_db(db_path)

    @unittest.skipUnless(os.name == "posix", "POSIX symbolic-link behavior")
    def test_get_db_rejects_symbolic_link_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "unrelated.txt"
            target.write_text("do not touch", encoding="utf-8")
            db_path = Path(tmpdir) / "usage.db"
            os.symlink(target, db_path)

            with self.assertRaisesRegex(RuntimeError, "symbolic-link"):
                get_db(db_path)
            self.assertEqual(target.read_text(encoding="utf-8"), "do not touch")

    @unittest.skipUnless(os.name == "posix", "POSIX hard-link behavior")
    def test_get_db_rejects_hard_link_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "unrelated.txt"
            target.write_text("do not touch", encoding="utf-8")
            db_path = Path(tmpdir) / "usage.db"
            os.link(target, db_path)

            with self.assertRaisesRegex(RuntimeError, "hard-linked"):
                get_db(db_path)
            self.assertEqual(target.read_text(encoding="utf-8"), "do not touch")

    @unittest.skipUnless(os.name == "posix", "POSIX special-file behavior")
    def test_get_db_rejects_non_regular_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "usage.db"
            os.mkfifo(db_path)

            with self.assertRaisesRegex(RuntimeError, "regular file"):
                get_db(db_path)

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        self.conn = get_db(self.db_path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_init_db_creates_tables(self):
        tables = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r["name"] for r in tables}
        self.assertIn("sessions", table_names)
        self.assertIn("turns", table_names)
        self.assertIn("processed_files", table_names)

    def test_init_db_is_idempotent(self):
        # Running init_db twice should not error
        init_db(self.conn)
        init_db(self.conn)

    def test_upsert_new_session(self):
        sessions = [{
            "session_id": "s1", "project_name": "test",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 1000, "total_output_tokens": 500,
            "total_cache_read": 100, "total_cache_creation": 50,
            "turn_count": 5,
        }]
        upsert_sessions(self.conn, sessions)
        row = self.conn.execute("SELECT * FROM sessions WHERE session_id = 's1'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["total_input_tokens"], 1000)
        self.assertEqual(row["turn_count"], 5)

    def test_upsert_updates_existing_session(self):
        session = {
            "session_id": "s1", "project_name": "test",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 1000, "total_output_tokens": 500,
            "total_cache_read": 100, "total_cache_creation": 50,
            "turn_count": 5,
        }
        upsert_sessions(self.conn, [session])
        # Add more tokens
        session2 = {**session, "total_input_tokens": 200, "total_output_tokens": 100,
                    "total_cache_read": 20, "total_cache_creation": 10, "turn_count": 2}
        upsert_sessions(self.conn, [session2])
        row = self.conn.execute("SELECT * FROM sessions WHERE session_id = 's1'").fetchone()
        self.assertEqual(row["total_input_tokens"], 1200)  # 1000 + 200
        self.assertEqual(row["turn_count"], 7)  # 5 + 2

    def test_insert_turns(self):
        turns = [{
            "session_id": "s1", "timestamp": "2026-04-08T10:00:00Z",
            "model": "claude-sonnet-4-6", "input_tokens": 100,
            "output_tokens": 50, "cache_read_tokens": 10,
            "cache_creation_tokens": 5, "tool_name": "Read", "cwd": "/tmp",
        }]
        insert_turns(self.conn, turns)
        rows = self.conn.execute("SELECT * FROM turns").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "claude-sonnet-4-6")
        self.assertIsNone(rows[0]["cwd"])

class TestScanIntegration(unittest.TestCase):
    """Integration test: create fake JSONL files and run scan()."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = Path(self.tmpdir) / "projects"
        self.projects_dir.mkdir()
        self.db_path = Path(self.tmpdir) / "usage.db"

    def _write_project_jsonl(self, project_name, session_id, num_turns=3):
        project_dir = self.projects_dir / project_name
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / f"{session_id}.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write(_make_user_record(session_id=session_id) + "\n")
            for i in range(num_turns):
                ts = f"2026-04-08T10:{i:02d}:00Z"
                f.write(_make_assistant_record(
                    session_id=session_id,
                    timestamp=ts,
                    input_tokens=100 * (i + 1),
                    output_tokens=50 * (i + 1),
                ) + "\n")

    def test_scan_new_files(self):
        self._write_project_jsonl("user/myproject", "sess-1", num_turns=3)
        result = scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertEqual(result["new"], 1)
        self.assertEqual(result["turns"], 3)
        self.assertEqual(result["sessions"], 1)

    def test_scan_stores_only_hashed_processed_file_identifiers(self):
        self._write_project_jsonl("secret-client/project", "sess-1", num_turns=1)
        scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        conn = get_db(self.db_path)
        try:
            row = conn.execute("SELECT path FROM processed_files").fetchone()
        finally:
            conn.close()
        self.assertRegex(row["path"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn(str(self.projects_dir), row["path"])

    def test_scan_is_incremental(self):
        self._write_project_jsonl("user/myproject", "sess-1")
        scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        # Second scan should skip
        result = scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["new"], 0)

    def test_scan_empty_directory(self):
        result = scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertEqual(result["new"], 0)
        self.assertEqual(result["turns"], 0)

    def test_scan_multiple_files(self):
        self._write_project_jsonl("user/project-a", "sess-1", num_turns=2)
        self._write_project_jsonl("user/project-b", "sess-2", num_turns=4)
        result = scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertEqual(result["new"], 2)
        self.assertEqual(result["turns"], 6)
        self.assertEqual(result["sessions"], 2)

    @unittest.skipUnless(os.name == "posix", "POSIX symbolic-link behavior")
    def test_scan_does_not_follow_jsonl_symlinks(self):
        self._write_project_jsonl("user/inside", "inside", num_turns=1)

        outside_dir = Path(self.tmpdir) / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "outside.jsonl"
        outside_file.write_text(
            _make_user_record(session_id="outside") + "\n"
            + _make_assistant_record(session_id="outside") + "\n"
        , encoding="utf-8")
        os.symlink(outside_file, self.projects_dir / "linked-file.jsonl")
        os.symlink(outside_dir, self.projects_dir / "linked-directory")

        result = scan(
            projects_dir=self.projects_dir,
            db_path=self.db_path,
            verbose=False,
        )
        self.assertEqual(result["new"], 1)
        self.assertEqual(result["sessions"], 1)

        conn = sqlite3.connect(self.db_path)
        session_ids = {
            row[0] for row in conn.execute("SELECT session_id FROM sessions")
        }
        conn.close()
        self.assertEqual(session_ids, {"inside"})

    @unittest.skipUnless(os.name == "posix", "POSIX symbolic-link behavior")
    def test_a_directory_swapped_after_discovery_cannot_escape_the_scan_root(self):
        import scanner

        inside = self.projects_dir / "inside"
        outside = Path(self.tmpdir) / "outside"
        inside.mkdir()
        outside.mkdir()
        for directory, session in ((inside, "inside"), (outside, "outside")):
            (directory / "session.jsonl").write_text(
                _make_assistant_record(session_id=session, message_id=session) + "\n",
                encoding="utf-8",
            )
        discover = scanner.discover_jsonl_files

        def discover_then_swap(roots):
            files = discover(roots)
            inside.rename(self.projects_dir / "moved")
            inside.symlink_to(outside, target_is_directory=True)
            return files

        with mock.patch.object(scanner, "discover_jsonl_files", discover_then_swap):
            result = scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertEqual(result["turns"], 0)
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM processed_files").fetchone()[0], 0)
        finally:
            conn.close()

    @unittest.skipUnless(os.name == "posix", "POSIX symbolic-link behavior")
    def test_scan_follows_a_symlinked_projects_root(self):
        """A symlinked scan root is explicitly configured, so it is scanned.

        Symlinking ~/.claude/projects (to another volume, a dotfiles repo, ...)
        is an ordinary setup. Skipping such a root made scan() report success
        while silently ingesting nothing.
        """
        self._write_project_jsonl("user/inside", "inside", num_turns=1)
        linked_root = Path(self.tmpdir) / "projects-link"
        os.symlink(self.projects_dir, linked_root)

        result = scan(projects_dir=linked_root, db_path=self.db_path,
                      verbose=False)

        self.assertEqual(result["new"], 1)
        self.assertEqual(result["sessions"], 1)
        conn = sqlite3.connect(self.db_path)
        session_ids = {r[0] for r in conn.execute("SELECT session_id FROM sessions")}
        conn.close()
        self.assertEqual(session_ids, {"inside"})

    @unittest.skipUnless(os.name == "posix", "POSIX hard-link behavior")
    def test_scan_does_not_read_hard_linked_jsonl(self):
        outside_file = Path(self.tmpdir) / "outside.jsonl"
        outside_file.write_text(
            _make_assistant_record(session_id="outside") + "\n"
        , encoding="utf-8")
        os.link(outside_file, self.projects_dir / "linked.jsonl")

        result = scan(
            projects_dir=self.projects_dir,
            db_path=self.db_path,
            verbose=False,
        )

        self.assertEqual(result["new"], 0)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
        conn.close()


class TestScanSummaryCountsTurnsParsed(unittest.TestCase):
    """The scan summary reports turns *parsed*, which is not rows the DB gained.

    `total_turns` is only ever incremented by the length of a parsed batch, so a
    response the scan reads again is counted again even though `insert_turns`
    upserts it onto the row that already exists. That happens for real: one API
    response can be recorded in two transcripts (invariant 1), and every Codex
    subagent rollout replays its ancestors' whole usage history. The line used
    to read "Turns added:", which is a claim about the database that the counter
    cannot support.

    The `"turns"` key keeps the parsed meaning — it is in `/api/rescan`'s
    response body — so the second test pins that too: every other assertion on
    it runs on a duplicate-free corpus where parsed and landed coincide, so a
    silent redefinition would have broken nothing and been caught by nothing.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = Path(self.tmpdir) / "projects" / "user" / "proj"
        self.projects_dir.mkdir(parents=True)
        self.db_path = Path(self.tmpdir) / "usage.db"
        # Two responses, each recorded in two transcripts: four turns parsed,
        # two rows landed.
        for session_id in ("sess-1", "sess-2"):
            path = self.projects_dir / f"{session_id}.jsonl"
            with open(path, "w", encoding="utf-8") as f:
                for message_id in ("msg-a", "msg-b"):
                    f.write(_make_assistant_record(
                        session_id=session_id, message_id=message_id) + "\n")

    def _scan(self, verbose=False):
        return scan(projects_dir=self.projects_dir.parent.parent,
                    db_path=self.db_path, verbose=verbose)

    def _rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        finally:
            conn.close()

    def test_the_summary_line_says_parsed_not_added(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._scan(verbose=True)
        output = buf.getvalue()
        self.assertIn("Turns parsed:", output)
        self.assertNotIn(
            "Turns added:", output,
            "the counter is turns parsed; 'added' claims rows the database gained")

    def test_the_reported_count_is_parsed_turns_not_rows_gained(self):
        result = self._scan()
        self.assertEqual(result["turns"], 4, "turns parsed")
        self.assertEqual(self._rows(), 2, "rows the database actually gained")


class TestScanIncrementalUpdate(unittest.TestCase):
    """Test that updating a file only processes new lines (no double reads)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = Path(self.tmpdir) / "projects" / "user" / "proj"
        self.projects_dir.mkdir(parents=True)
        self.db_path = Path(self.tmpdir) / "usage.db"
        self.filepath = self.projects_dir / "sess-1.jsonl"

    def _write_initial(self):
        with open(self.filepath, "w", encoding="utf-8") as f:
            f.write(_make_user_record(session_id="sess-1",
                                      timestamp="2026-04-08T09:00:00Z") + "\n")
            f.write(_make_assistant_record(session_id="sess-1",
                                           timestamp="2026-04-08T09:01:00Z",
                                           input_tokens=100, output_tokens=50) + "\n")

    def _append_turns(self):
        # Ensure mtime visibly changes (filesystem resolution can be ~10ms)
        import time
        time.sleep(0.05)
        with open(self.filepath, "a", encoding="utf-8") as f:
            f.write(_make_assistant_record(session_id="sess-1",
                                           timestamp="2026-04-08T09:05:00Z",
                                           input_tokens=200, output_tokens=100) + "\n")
            f.write(_make_assistant_record(session_id="sess-1",
                                           timestamp="2026-04-08T09:10:00Z",
                                           input_tokens=300, output_tokens=150) + "\n")

    def test_no_duplicate_turns_on_update(self):
        """Growing a file must add only new turns, not re-insert old ones."""
        self._write_initial()
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        self._append_turns()
        result = scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["turns"], 2)  # only the 2 new turns

        conn = sqlite3.connect(self.db_path)
        total_turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        conn.close()
        self.assertEqual(total_turns, 3)  # 1 original + 2 new

    def test_token_counts_accumulate_correctly(self):
        """Session totals should reflect all turns, not double-count."""
        self._write_initial()
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        self._append_turns()
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        session = conn.execute("SELECT * FROM sessions WHERE session_id = 'sess-1'").fetchone()
        conn.close()
        # 100 + 200 + 300 = 600
        self.assertEqual(session["total_input_tokens"], 600)
        # 50 + 100 + 150 = 300
        self.assertEqual(session["total_output_tokens"], 300)
        self.assertEqual(session["turn_count"], 3)

    def test_session_timestamp_updated(self):
        """Last timestamp should advance after file grows."""
        self._write_initial()
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        self._append_turns()
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        session = conn.execute("SELECT * FROM sessions WHERE session_id = 'sess-1'").fetchone()
        conn.close()
        self.assertEqual(session["last_timestamp"], "2026-04-08T09:10:00Z")

    def test_new_session_first_timestamp_uses_earliest(self):
        """A brand-new session discovered during an incremental scan with
        non-monotonic timestamps must record the EARLIEST timestamp as
        first_timestamp. Regression for the incremental branch only tracking
        last_timestamp (parse_jsonl_file already tracked both)."""
        self._write_initial()
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        # Append two records for a NEW session 'sess-2' with timestamps in
        # reverse order — later one observed first, earlier one second.
        import time
        time.sleep(0.05)
        with open(self.filepath, "a", encoding="utf-8") as f:
            f.write(_make_assistant_record(session_id="sess-2",
                                           timestamp="2026-04-08T10:05:00Z",
                                           input_tokens=50, output_tokens=25,
                                           message_id="msg-new-2-late") + "\n")
            f.write(_make_assistant_record(session_id="sess-2",
                                           timestamp="2026-04-08T09:55:00Z",
                                           input_tokens=70, output_tokens=35,
                                           message_id="msg-new-2-early") + "\n")
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        sess2 = conn.execute("SELECT * FROM sessions WHERE session_id = 'sess-2'").fetchone()
        conn.close()
        self.assertEqual(sess2["first_timestamp"], "2026-04-08T09:55:00Z")
        self.assertEqual(sess2["last_timestamp"],  "2026-04-08T10:05:00Z")

    def test_new_session_branch_filled_from_a_later_appended_record(self):
        """A session first seen during an incremental scan must pick up its
        gitBranch from a later appended record, not only from the first one.

        Claude Code omits gitBranch on some records, so the first appended line
        for a new session often carries none. The full-parse path always filled
        the gap from a later record; the incremental path used to be a separate
        copy of the parser that only read gitBranch when it created the session
        meta, so such a session kept an empty branch forever. Both paths are now
        one parser, which is what keeps this true."""
        self._write_initial()
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        import time
        time.sleep(0.05)
        with open(self.filepath, "a", encoding="utf-8") as f:
            # First record of the new session: no gitBranch at all.
            f.write(json.dumps({
                "type": "assistant", "sessionId": "sess-branch",
                "timestamp": "2026-04-08T11:00:00Z", "cwd": "/home/user/project",
                "message": {"model": "claude-sonnet-4-6", "id": "msg-branch-1",
                            "usage": {"input_tokens": 10, "output_tokens": 5},
                            "content": []},
            }) + "\n")
            # A later record does carry it.
            f.write(json.dumps({
                "type": "assistant", "sessionId": "sess-branch",
                "timestamp": "2026-04-08T11:01:00Z", "cwd": "/home/user/project",
                "gitBranch": "feature-x",
                "message": {"model": "claude-sonnet-4-6", "id": "msg-branch-2",
                            "usage": {"input_tokens": 10, "output_tokens": 5},
                            "content": []},
            }) + "\n")

        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        sess = conn.execute(
            "SELECT git_branch FROM sessions WHERE session_id = 'sess-branch'").fetchone()
        conn.close()
        self.assertEqual(sess["git_branch"], "feature-x")

    def test_mtime_change_without_growth_skipped(self):
        """If mtime changes but line count doesn't grow, skip the file."""
        self._write_initial()
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        # Touch the file (change mtime) without adding content
        import time
        time.sleep(0.05)
        os.utime(self.filepath, None)

        result = scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["turns"], 0)


class TestAnAppendJustAfterTheStampIsStillRead(unittest.TestCase):
    """A record appended a few milliseconds after a scan must not be lost.

    `scan()` stamps the mtime it read *before* the parse, and the skip test at
    the top of the loop used to forgive a difference under 0.01 s. So a writer
    that appended one more record inside that band left the file's real mtime
    within the tolerance of the stamped one, and every later scan called the
    file unchanged and stepped straight over the record — permanently, because
    transcripts are append-only and a finished session's mtime never moves
    again. Nothing reported it: the scan prints `Skipped files: N` and exits 0.

    That is also the half of `_lines_consumed` the tolerance quietly cancelled.
    It deliberately stamps one line short whenever the file moved under the
    parse, and pays for it with a re-read that only happens if the next scan
    sees the mtime as changed — which, inside 10 ms, it did not.

    Reproduced with no forged timestamps at all before it was fixed: a writer
    *process* appending record pairs ~6 ms apart while `scan()` looped 5,621
    times lost the file's final complete record (`stored_lines` 353 against 354
    on disk, stamped-vs-real mtime delta 0.006251 s, tail newline present).
    `os.utime` appears below only to make that race deterministic; nothing in
    the scanner is patched, and the value it compares still comes from a real
    `stat()` of a real file.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.roots = Path(self.tmpdir) / "projects"
        self.projects_dir = self.roots / "user" / "proj"
        self.projects_dir.mkdir(parents=True)
        self.db_path = Path(self.tmpdir) / "usage.db"
        self.filepath = self.projects_dir / "sess-1.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _scan(self):
        return scan(projects_dir=self.roots, db_path=self.db_path, verbose=False)

    def _write(self, message_id, mode="w"):
        with open(self.filepath, mode, encoding="utf-8") as f:
            f.write(_make_assistant_record(
                message_id=message_id, timestamp="2026-04-08T10:00:00Z") + "\n")

    def _one(self, sql):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql).fetchone()[0]
        finally:
            conn.close()

    def _stored_ids(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return {r[0] for r in conn.execute("SELECT message_id FROM turns")}
        finally:
            conn.close()

    def test_a_record_appended_inside_the_old_tolerance_is_ingested(self):
        self._write("msg-1")
        self._scan()
        stamped = self._one("SELECT mtime FROM processed_files")

        self._write("msg-2", mode="a")
        # 5 ms after the stamp: a real mtime, on a real file, inside the band
        # the skip test used to forgive.
        os.utime(self.filepath, (stamped + 0.005, stamped + 0.005))
        delta = abs(os.path.getmtime(self.filepath) - stamped)
        if not 0 < delta < 0.01:
            self.skipTest(
                f"this filesystem cannot hold a 5 ms mtime step (delta={delta}); "
                "the race under test cannot occur on it either")

        # Three more scans with the file completely at rest. An append-only
        # transcript gets no second chance, so one wrong skip is forever.
        for _ in range(3):
            self._scan()

        self.assertIn(
            "msg-2", self._stored_ids(),
            "a complete, newline-terminated record appended within 0.01 s of "
            "the stamped mtime was skipped by every later scan and lost")

    def test_an_untouched_file_is_still_skipped(self):
        """The control, and the only job the tolerance could have had.

        `os.path.getmtime` returns the identical double for a file nobody wrote
        to, so comparing exactly costs no re-reads — which is what makes the
        0.01 s band pure loss rather than a trade.
        """
        self._write("msg-1")
        self._scan()

        result = self._scan()
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["turns"], 0)

    def test_the_stamped_mtime_round_trips_through_sqlite_exactly(self):
        """Why exactness is safe: SQLite's REAL is the same IEEE double Python
        hands it, so a file at rest compares equal bit for bit. If that ever
        stopped holding, every scan would re-parse every transcript."""
        self._write("msg-1")
        self._scan()

        self.assertEqual(self._one("SELECT mtime FROM processed_files"),
                         os.path.getmtime(self.filepath))


class TestMidStreamTallyRepair(unittest.TestCase):
    """A scan landing mid-response must not freeze that response's partial tally.

    Claude Code streams one API response as several records sharing a message.id,
    each carrying the cumulative (non-decreasing) usage so far. A scan that reads
    only the first few stores a partial count; the records completing the response
    arrive on a later scan. They used to lose the INSERT OR IGNORE race against
    the partial row, so the undercount was permanent and an incremental scan
    disagreed with a one-shot scan of the very same file.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = Path(self.tmpdir) / "projects" / "user" / "proj"
        self.projects_dir.mkdir(parents=True)
        self.db_path = Path(self.tmpdir) / "usage.db"
        self.filepath = self.projects_dir / "sess-1.jsonl"

    def _streaming_record(self, output_tokens, input_tokens=100):
        return _make_assistant_record(
            session_id="sess-1", timestamp="2026-04-08T09:00:00Z",
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read=0, cache_creation=0, message_id="msg-stream")

    def _scan(self):
        return scan(projects_dir=self.projects_dir.parent.parent,
                    db_path=self.db_path, verbose=False)

    def _stored(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT output_tokens, input_tokens, is_subagent, timestamp, model "
            "FROM turns WHERE message_id = 'msg-stream'").fetchall()
        session = conn.execute(
            "SELECT total_output_tokens, turn_count FROM sessions "
            "WHERE session_id = 'sess-1'").fetchone()
        conn.close()
        return rows, session

    def test_partial_tally_is_corrected_by_the_completing_scan(self):
        with open(self.filepath, "w", encoding="utf-8") as f:
            f.write(self._streaming_record(10) + "\n")
            f.write(self._streaming_record(50) + "\n")
        self._scan()
        rows, _ = self._stored()
        self.assertEqual(rows[0]["output_tokens"], 50)  # partial, as observed

        import time
        time.sleep(0.05)
        with open(self.filepath, "a", encoding="utf-8") as f:
            f.write(self._streaming_record(2000) + "\n")
        self._scan()

        rows, session = self._stored()
        self.assertEqual(len(rows), 1, "the response must stay a single turn")
        self.assertEqual(rows[0]["output_tokens"], 2000)
        # The denormalized session total has to follow the repaired turn.
        self.assertEqual(session["total_output_tokens"], 2000)
        self.assertEqual(session["turn_count"], 1)

    def test_incremental_scan_matches_a_one_shot_scan_of_the_same_file(self):
        """The property the repair exists to restore, stated directly."""
        with open(self.filepath, "w", encoding="utf-8") as f:
            f.write(self._streaming_record(10) + "\n")
            f.write(self._streaming_record(50) + "\n")
        self._scan()
        import time
        time.sleep(0.05)
        with open(self.filepath, "a", encoding="utf-8") as f:
            f.write(self._streaming_record(2000) + "\n")
        self._scan()
        incremental, _ = self._stored()

        one_shot_db = Path(self.tmpdir) / "one-shot.db"
        scan(projects_dir=self.projects_dir.parent.parent,
             db_path=one_shot_db, verbose=False)
        conn = sqlite3.connect(one_shot_db)
        conn.row_factory = sqlite3.Row
        full = conn.execute(
            "SELECT output_tokens, input_tokens FROM turns "
            "WHERE message_id = 'msg-stream'").fetchall()
        conn.close()

        self.assertEqual([r["output_tokens"] for r in incremental],
                         [r["output_tokens"] for r in full])
        self.assertEqual([r["input_tokens"] for r in incremental],
                         [r["input_tokens"] for r in full])

    def test_a_higher_later_tally_raises_every_token_column(self):
        """The other direction of the same rule, for every merged column.

        Test every merged counter in both directions. A smaller later tally
        must not reduce usage, and an increased cache category must repair the
        stored turn.

        Measured column by column against a pristine HEAD tree rather than
        assumed; an earlier draft of this docstring claimed all five were
        unprotected, which was wrong by four, and a comment asserting an unrun
        measurement is the same defect `A1-3` exists to correct two functions
        away.

        Every token column participates in the nondecreasing-tally contract.
        The synthetic fixture changes them independently so a refactor cannot
        silently omit a column from the merge."""
        conn = get_db(self.db_path)
        init_db(conn)
        base = {
            "session_id": "sess-1", "timestamp": "2026-04-08T09:00:00Z",
            "model": "claude-opus-4-8", "tool_name": None, "cwd": None,
            "message_id": "msg-stream", "is_subagent": 0, "agent_id": None,
        }
        columns = ("input_tokens", "output_tokens", "cache_read_tokens",
                   "cache_creation_tokens", "cache_creation_1h_tokens",
                   "reasoning_output_tokens")
        partial = dict(zip(columns, (1, 2, 3, 40, 4, 5)))
        # The 1-hour slice stays inside its own total (invariant 6).
        final = dict(zip(columns, (10, 200, 300, 4000, 400, 500)))
        insert_turns(conn, [dict(base, **partial)])
        insert_turns(conn, [dict(base, **final)])
        conn.commit()
        row = conn.execute(
            f"SELECT {', '.join(columns)} FROM turns WHERE message_id = 'msg-stream'"
        ).fetchone()
        conn.close()
        self.assertEqual(dict(zip(columns, tuple(row))), final)

    def test_a_lower_later_tally_never_reduces_a_stored_turn(self):
        """The merge is monotonic: only upward, never down."""
        conn = get_db(self.db_path)
        init_db(conn)
        base = {
            "session_id": "sess-1", "timestamp": "2026-04-08T09:00:00Z",
            "model": "claude-opus-4-8", "tool_name": None, "cwd": None,
            "message_id": "msg-stream", "is_subagent": 0, "agent_id": None,
        }
        insert_turns(conn, [dict(base, input_tokens=100, output_tokens=2000,
                                 cache_read_tokens=70, cache_creation_tokens=30)])
        insert_turns(conn, [dict(base, input_tokens=1, output_tokens=5,
                                 cache_read_tokens=0, cache_creation_tokens=0)])
        conn.commit()
        row = conn.execute(
            "SELECT input_tokens, output_tokens, cache_read_tokens, "
            "cache_creation_tokens FROM turns WHERE message_id = 'msg-stream'"
        ).fetchone()
        conn.close()
        self.assertEqual(tuple(row), (100, 2000, 70, 30))

    def test_duplicate_across_files_keeps_the_first_rows_attribution(self):
        """The same response in two transcripts carries identical tallies, so the
        merge must be a no-op — it must not rewrite which turn is a subagent's."""
        conn = get_db(self.db_path)
        init_db(conn)
        base = {
            "session_id": "sess-1", "timestamp": "2026-04-08T09:00:00Z",
            "model": "claude-opus-4-8", "tool_name": None, "cwd": None,
            "message_id": "msg-dup", "input_tokens": 100, "output_tokens": 200,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
        }
        insert_turns(conn, [dict(base, is_subagent=0, agent_id=None)])
        # Same response, later seen inside a subagent transcript.
        insert_turns(conn, [dict(base, is_subagent=1, agent_id="agent-9",
                                 timestamp="2026-04-08T11:00:00Z",
                                 model="claude-haiku-4-5")])
        conn.commit()
        rows = conn.execute(
            "SELECT is_subagent, agent_id, timestamp, model, output_tokens "
            "FROM turns WHERE message_id = 'msg-dup'").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1, "cross-file dedup must still collapse to one row")
        self.assertEqual(rows[0]["is_subagent"], 0)
        self.assertIsNone(rows[0]["agent_id"])
        self.assertEqual(rows[0]["timestamp"], "2026-04-08T09:00:00Z")
        self.assertEqual(rows[0]["model"], "claude-opus-4-8")
        self.assertEqual(rows[0]["output_tokens"], 200)

    def test_turns_without_a_message_id_are_never_merged(self):
        """The unique index is partial: id-less turns must all be kept."""
        conn = get_db(self.db_path)
        init_db(conn)
        base = {
            "session_id": "sess-1", "timestamp": "2026-04-08T09:00:00Z",
            "model": "claude-opus-4-8", "tool_name": None, "cwd": None,
            "message_id": "", "is_subagent": 0, "agent_id": None,
            "input_tokens": 10, "output_tokens": 20,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
        }
        insert_turns(conn, [dict(base), dict(base), dict(base)])
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM turns WHERE message_id = ''").fetchone()[0]
        conn.close()
        self.assertEqual(count, 3)


def _make_effort_record(session_id="sess-1", message_id="msg-effort",
                        effort="high", stop_reason="max_tokens",
                        output_tokens=50,
                        timestamp="2026-04-08T10:00:00Z"):
    """An assistant record carrying reasoning effort and a stop reason.

    `effort` sits at the TOP LEVEL of the record and `stop_reason` inside
    `message`; that asymmetry is real, and a fixture that put them in the same
    place could not tell a parser reading the wrong one from a correct one.
    """
    return json.dumps({
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": "/home/user/project",
        "effort": effort,
        "message": {
            "id": message_id,
            "model": "claude-opus-4-8",
            "stop_reason": stop_reason,
            "usage": {
                "input_tokens": 100,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 20,
            },
            "content": [],
        },
    })


class TestCrossFileSessionTotals(unittest.TestCase):
    """Test that session totals are correct when the same session spans multiple files."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = Path(self.tmpdir) / "projects" / "user" / "proj"
        self.projects_dir.mkdir(parents=True)
        self.db_path = Path(self.tmpdir) / "usage.db"

    def test_session_across_files_not_inflated(self):
        """Same session in 2 files with duplicate message_ids should not inflate totals.

        The cache-write columns carry a 1-hour split deliberately: every
        reconciled total in invariant 2's end-of-scan UPDATE is asserted here
        except that one, and neutralizing just its line
        (`total_cache_creation_1h = total_cache_creation_1h`) left the whole
        suite green while the same mutation of any sibling column was caught.
        A zero fixture cannot tell the two apart — the additive
        `upsert_sessions` path reaches 0 either way.
        """
        # File 1: message msg-1 with 100 input
        f1 = self.projects_dir / "file1.jsonl"
        with open(f1, "w", encoding="utf-8") as f:
            f.write(_make_user_record(session_id="sess-1") + "\n")
            f.write(_make_assistant_record(session_id="sess-1", message_id="msg-1",
                                           input_tokens=100, output_tokens=50,
                                           cache_read=0, cache_creation=1000,
                                           cache_creation_1h=600) + "\n")

        # File 2: same message msg-1 (duplicate) + new message msg-2
        f2 = self.projects_dir / "file2.jsonl"
        with open(f2, "w", encoding="utf-8") as f:
            f.write(_make_user_record(session_id="sess-1") + "\n")
            f.write(_make_assistant_record(session_id="sess-1", message_id="msg-1",
                                           input_tokens=100, output_tokens=50,
                                           cache_read=0, cache_creation=1000,
                                           cache_creation_1h=600) + "\n")
            f.write(_make_assistant_record(session_id="sess-1", message_id="msg-2",
                                           input_tokens=200, output_tokens=100,
                                           cache_read=0, cache_creation=1000,
                                           cache_creation_1h=600) + "\n")

        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        # Turns table should have 2 turns (msg-1 deduped across files)
        turns = conn.execute("SELECT COUNT(*) as c FROM turns").fetchone()["c"]
        self.assertEqual(turns, 2)

        # Session totals should match turns table, not be inflated
        session = conn.execute("SELECT * FROM sessions WHERE session_id = 'sess-1'").fetchone()
        self.assertEqual(session["total_input_tokens"], 300)  # 100 + 200
        self.assertEqual(session["total_output_tokens"], 150)  # 50 + 100
        self.assertEqual(session["total_cache_creation"], 2000)  # 1000 + 1000
        self.assertEqual(session["total_cache_creation_1h"], 1200)  # 600 + 600
        self.assertEqual(session["turn_count"], 2)
        conn.close()


class TestParseJsonlFileLineCount(unittest.TestCase):
    """Test that parse_jsonl_file returns correct line count."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_line_count_matches_file(self):
        path = os.path.join(self.tmpdir, "test.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(_make_user_record() + "\n")
            f.write(_make_assistant_record() + "\n")
            f.write(_make_assistant_record(timestamp="2026-04-08T10:01:00Z") + "\n")
        _, _, _, _, line_count = parse_jsonl_file(path)
        self.assertEqual(line_count, 3)

    def test_empty_file_returns_zero(self):
        path = os.path.join(self.tmpdir, "empty.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            pass
        _, _, _, _, line_count = parse_jsonl_file(path)
        self.assertEqual(line_count, 0)


class TestSessionTopic(unittest.TestCase):
    """Topic extraction from custom-title / ai-title records (#147)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write_jsonl(self, lines):
        path = os.path.join(self.tmpdir, "t.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        return path

    def test_custom_title_sets_topic(self):
        path = self._write_jsonl([
            _make_user_record(),
            _make_assistant_record(),
            _make_custom_title_record(title="Ship the release"),
        ])
        metas, _, _, _, _ = parse_jsonl_file(path)
        self.assertEqual(metas[0]["topic"], "Ship the release")

    def test_ai_title_used_when_no_custom(self):
        path = self._write_jsonl([
            _make_assistant_record(),
            _make_ai_title_record(title="Debug the crash"),
        ])
        metas, _, _, _, _ = parse_jsonl_file(path)
        self.assertEqual(metas[0]["topic"], "Debug the crash")

    def test_custom_title_wins_when_it_comes_after_ai_title(self):
        path = self._write_jsonl([
            _make_assistant_record(),
            _make_ai_title_record(title="AI guess"),
            _make_custom_title_record(title="User label"),
        ])
        metas, _, _, _, _ = parse_jsonl_file(path)
        self.assertEqual(metas[0]["topic"], "User label")

    def test_custom_title_not_overridden_by_later_ai_title(self):
        path = self._write_jsonl([
            _make_assistant_record(),
            _make_custom_title_record(title="User label"),
            _make_ai_title_record(title="AI guess"),
        ])
        metas, _, _, _, _ = parse_jsonl_file(path)
        self.assertEqual(metas[0]["topic"], "User label")

    def test_no_title_record_leaves_topic_empty(self):
        # No fallback to the first user message, even when its text is present
        # (#147) — an untitled session gets an empty Topic column, not the prompt.
        path = self._write_jsonl([
            _make_user_record_with_text(text="Please fix the login bug"),
            _make_assistant_record(),
        ])
        metas, _, _, _, _ = parse_jsonl_file(path)
        self.assertIsNone(metas[0]["topic"])


class TestSessionTopicScan(unittest.TestCase):
    """Topic persistence through scan(): DB write, incremental capture, and the
    no-phantom-row guard (#147)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = Path(self.tmpdir) / "projects" / "user" / "proj"
        self.projects_dir.mkdir(parents=True)
        self.db_path = Path(self.tmpdir) / "usage.db"
        self.filepath = self.projects_dir / "sess-1.jsonl"

    def _scan(self):
        return scan(projects_dir=self.projects_dir.parent.parent,
                    db_path=self.db_path, verbose=False)

    def _topic(self, session_id="sess-1"):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT topic FROM sessions WHERE session_id = ?",
                           (session_id,)).fetchone()
        conn.close()
        return row["topic"] if row else "<<no row>>"

    def test_topic_persisted_to_db(self):
        with open(self.filepath, "w", encoding="utf-8") as f:
            f.write(_make_user_record(session_id="sess-1",
                                      timestamp="2026-04-08T09:00:00Z") + "\n")
            f.write(_make_assistant_record(session_id="sess-1",
                                           timestamp="2026-04-08T09:01:00Z") + "\n")
            f.write(_make_custom_title_record(session_id="sess-1",
                                              title="Release day") + "\n")
        self._scan()
        self.assertEqual(self._topic(), "Release day")

    def test_topic_captured_when_title_arrives_in_later_scan(self):
        # First scan: turns only, no title -> empty. Claude Code appends the
        # ai-title later; the incremental rescan must pick it up via the UPDATE
        # path. Regression guard for the phantom-INSERT change.
        import time
        with open(self.filepath, "w", encoding="utf-8") as f:
            f.write(_make_user_record(session_id="sess-1",
                                      timestamp="2026-04-08T09:00:00Z") + "\n")
            f.write(_make_assistant_record(session_id="sess-1",
                                           timestamp="2026-04-08T09:01:00Z") + "\n")
        self._scan()
        self.assertIsNone(self._topic())

        time.sleep(0.05)
        with open(self.filepath, "a", encoding="utf-8") as f:
            f.write(_make_ai_title_record(session_id="sess-1",
                                          title="Generated title") + "\n")
        self._scan()
        self.assertEqual(self._topic(), "Generated title")

    def test_topic_preserved_when_later_scan_has_no_title(self):
        import time
        with open(self.filepath, "w", encoding="utf-8") as f:
            f.write(_make_user_record(session_id="sess-1",
                                      timestamp="2026-04-08T09:00:00Z") + "\n")
            f.write(_make_assistant_record(session_id="sess-1",
                                           timestamp="2026-04-08T09:01:00Z") + "\n")
            f.write(_make_custom_title_record(session_id="sess-1",
                                              title="Keep me") + "\n")
        self._scan()
        self.assertEqual(self._topic(), "Keep me")

        time.sleep(0.05)
        with open(self.filepath, "a", encoding="utf-8") as f:
            f.write(_make_assistant_record(session_id="sess-1",
                                           timestamp="2026-04-08T09:05:00Z",
                                           input_tokens=200, output_tokens=100) + "\n")
        self._scan()
        # A later, title-less rescan must not wipe the stored topic.
        self.assertEqual(self._topic(), "Keep me")

    def test_title_only_session_creates_no_phantom_row(self):
        # A record stream with a title but no turns for that session must not
        # INSERT a token-less phantom row.
        with open(self.filepath, "w", encoding="utf-8") as f:
            f.write(_make_user_record(session_id="sess-1",
                                      timestamp="2026-04-08T09:00:00Z") + "\n")
            f.write(_make_assistant_record(session_id="sess-1",
                                           timestamp="2026-04-08T09:01:00Z") + "\n")
            f.write(_make_custom_title_record(session_id="ghost",
                                              title="Orphan") + "\n")
        self._scan()
        self.assertEqual(self._topic("ghost"), "<<no row>>")  # no phantom row
        self.assertIsNone(self._topic("sess-1"))  # real session, just no title


if __name__ == "__main__":
    unittest.main()
