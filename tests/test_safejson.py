"""Tests for the values the JSON API is allowed to ship.

`safejson` is the trust boundary between SQLite — which is dynamically typed,
and every value in which originated in a transcript — and the browser. Nothing
in the suite referenced `dashboard_number` or `MAX_DASHBOARD_INTEGER`, and
replacing the whole body of `dashboard_number` with `return value` left the full
suite green: the module's stated guarantee ("numbers range-checked against what
JavaScript represents exactly", AGENTS.md) was protected by nothing, and a
refactor could delete it without a signal.

**What the clamp actually buys**, stated carefully because it is easy to
overclaim: it does not make a corrupt number right. `2**53 + 1` is reported as
`2**53 - 1` with the clamp and `2**53` without it, and both differ from what was
stored. What it buys is that the integer the browser renders is the integer the
server sent — no silent re-rounding between the two — which is exactly what the
comment at safejson.py:17-18 says.

Aggregate values can exceed the safe integer range even when individual turns
are bounded. JSON output must remain finite and JavaScript-safe.
"""

import json
import math
import sys
import unittest

import codex_transcripts
from safejson import (MAX_DASHBOARD_INTEGER, dashboard_number, dashboard_text,
                      optional_dashboard_number, safe_dashboard_value)


class TestTheJavaScriptPrecisionCeiling(unittest.TestCase):
    def test_the_ceiling_is_the_largest_exactly_representable_integer(self):
        self.assertEqual(MAX_DASHBOARD_INTEGER, 2 ** 53 - 1)

    def test_a_value_below_the_ceiling_passes_through_unchanged(self):
        for value in (0, 1, 12_345, 2 ** 53 - 2, MAX_DASHBOARD_INTEGER):
            with self.subTest(value=value):
                self.assertEqual(dashboard_number(value), value)

    def test_the_ceiling_itself_is_clamped_although_it_is_representable(self):
        """2^53 *is* exact as a double; only 2^53+1 is not. The clamp is
        deliberately conservative by one — pinned so a later "correction" is a
        decision rather than a drift."""
        self.assertEqual(dashboard_number(2 ** 53), MAX_DASHBOARD_INTEGER)

    def test_a_value_above_the_ceiling_is_clamped(self):
        for value in (2 ** 53 + 1, 2 ** 62, 10 ** 30):
            with self.subTest(value=value):
                self.assertEqual(dashboard_number(value), MAX_DASHBOARD_INTEGER)

    def test_the_clamped_value_survives_the_round_trip_to_javascript(self):
        """The property the clamp exists for: what the browser renders is what
        the server sent. An unclamped 2^53+1 is re-rounded to 2^53 in transit —
        off by one, with no error anywhere."""
        clamped = dashboard_number(2 ** 53 + 1)
        self.assertEqual(int(float(clamped)), clamped)
        self.assertNotEqual(int(float(2 ** 53 + 1)), 2 ** 53 + 1)

    def test_a_large_float_is_clamped_too(self):
        self.assertEqual(dashboard_number(1e300), MAX_DASHBOARD_INTEGER)
        self.assertEqual(dashboard_number(float(2 ** 53 + 1024)),
                         MAX_DASHBOARD_INTEGER)

    def test_the_bound_codex_uses_is_the_same_number(self):
        """codex_transcripts derives its cumulative-counter ceiling from this one
        by name in a comment and by value in code; nothing noticed if one moved."""
        self.assertEqual(codex_transcripts.MAX_CUMULATIVE_TOKENS,
                         MAX_DASHBOARD_INTEGER)


class TestWhatDashboardNumberRejects(unittest.TestCase):
    """Direct-call behaviour of the converter, not a claim about SQLite.

    SQLite cannot hand back a NaN (it stores one as NULL) or a Python bool (it
    returns an integer), so these pin the contract of a defensive function
    rather than a scenario the database can produce.
    """

    def test_a_negative_number_becomes_the_default(self):
        for value in (-1, -0.5, -2 ** 60):
            with self.subTest(value=value):
                self.assertEqual(dashboard_number(value), 0)
                self.assertEqual(dashboard_number(value, default=7), 7)

    def test_a_bool_becomes_the_default_rather_than_1_or_0(self):
        """`True` is an `int` in Python, so without the check it would ship as a
        token count of 1."""
        self.assertEqual(dashboard_number(True), 0)
        self.assertEqual(dashboard_number(False), 0)
        self.assertIsNot(dashboard_number(True), True)

    def test_a_non_finite_float_becomes_the_default(self):
        """NaN and Infinity are not JSON; `json.dumps` emits bare NaN/Infinity,
        which `JSON.parse` rejects — the whole payload, not one number."""
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=repr(value)):
                self.assertEqual(dashboard_number(value), 0)

    def test_a_non_number_becomes_the_default(self):
        for value in (None, "12", b"12", [], {}, object()):
            with self.subTest(value=repr(value)):
                self.assertEqual(dashboard_number(value), 0)

    def test_zero_is_a_number_and_not_a_rejection(self):
        self.assertEqual(dashboard_number(0, default=9), 0)
        self.assertEqual(dashboard_number(0.0, default=9), 0.0)

    def test_a_fractional_value_keeps_its_fraction(self):
        """Durations and percentages come through here as well as token counts."""
        self.assertEqual(dashboard_number(1.5), 1.5)


class TestOptionalDashboardNumber(unittest.TestCase):
    """Same converter, `None` as the default: a column that is genuinely absent
    (a subagent's duration before the agents table knew it) must read as absent
    rather than as zero, which is a different claim."""

    def test_an_absent_value_stays_absent(self):
        self.assertIsNone(optional_dashboard_number(None))
        self.assertIsNone(optional_dashboard_number(-1))
        self.assertIsNone(optional_dashboard_number(float("nan")))

    def test_a_real_value_comes_through(self):
        self.assertEqual(optional_dashboard_number(42), 42)
        self.assertEqual(optional_dashboard_number(2 ** 60), MAX_DASHBOARD_INTEGER)


class TestSafeDashboardValueRecursion(unittest.TestCase):
    """Every string crossing the API goes through terminal_safe, at any depth —
    the payload is nested lists of dicts, so a top-level-only pass would escape
    nothing that actually ships."""

    HOSTILE = "hi\x1b[31mthere"

    def test_a_bare_string_is_escaped(self):
        self.assertNotIn("\x1b", safe_dashboard_value(self.HOSTILE))

    def test_a_string_nested_in_lists_and_dicts_is_escaped(self):
        payload = {"rows": [{"project": self.HOSTILE}, ["a", self.HOSTILE]]}
        cleaned = safe_dashboard_value(payload)
        self.assertNotIn("\x1b", cleaned["rows"][0]["project"])
        self.assertNotIn("\x1b", cleaned["rows"][1][1])

    def test_non_strings_pass_through_untouched(self):
        self.assertEqual(safe_dashboard_value({"n": 5, "f": 1.5, "z": None}),
                         {"n": 5, "f": 1.5, "z": None})

    def test_dict_keys_are_left_alone_because_none_of_them_are_transcript_data(self):
        """Recursion covers values, not keys — pinned as the deliberate limit it
        is. Every key in the payload is a literal the server chose ("project",
        "cost", ...); no rollup builds a dict keyed by a model, a project or any
        other transcript string, so there is nothing untrusted here to escape.
        A rollup that ever does needs this widened, and this is where to look.
        """
        self.assertEqual(safe_dashboard_value({self.HOSTILE: "x"}),
                         {self.HOSTILE: "x"})


class TestTheWrapIsIdempotentAndLeavesRealTextAlone(unittest.TestCase):
    """Every string the API ships goes through `terminal_safe` twice.

    `dashboard._send_json` wraps whatever payload it is handed, and the two
    `dashboard_data` assembles (`_collect_dashboard_data`, `available_sources`)
    were already wrapped on their way out of that module — `rollups` coerces
    types but does not escape — which is only harmless if a second pass is a
    no-op. That was asserted in prose alone until this class existed, and the
    prose was wrong: it said the second pass finds nothing "because
    terminal_safe's output is printable ASCII". It is not. Non-ASCII survives
    byte for byte, and the two halves have to be checked separately, because an
    idempotency assertion over hostile input alone passes under the false
    rationale too.
    """

    HOSTILE = "hi\x1b[31mthere"
    # A right-to-left override inside a filename, a lone surrogate (which
    # `os.fsdecode` and `JSON.stringify` both put here with no attacker
    # involved), and a NUL. Written as escapes rather than as the characters
    # themselves: an unescaped U+202E would reorder this very line in the
    # reader's editor, which is the attack.
    HOSTILE_TEXT = (HOSTILE, "file\u202egnp.exe", "bad\ud800tail", "a\x00b")
    # Ordinary metadata a real transcript carries: an accented project name, a
    # CJK path, an emoji commit subject.
    REAL_TEXT = ("café", "プロジェクト/中文", "deploy \U0001f680 done")

    def test_a_second_pass_changes_nothing(self):
        for value in self.HOSTILE_TEXT + self.REAL_TEXT:
            with self.subTest(value=repr(value)):
                once = safe_dashboard_value(value)
                self.assertEqual(safe_dashboard_value(once), once)

    def test_a_second_pass_changes_nothing_at_depth(self):
        """`_send_json` wraps whole payloads, so the no-op has to hold through
        the nested lists of dicts the rollups ship, not just a bare string."""
        payload = {"rows": [{"project": self.HOSTILE, "branch": "café"},
                            list(self.HOSTILE_TEXT + self.REAL_TEXT)],
                   "n": 5, "z": None}
        once = safe_dashboard_value(payload)
        self.assertEqual(safe_dashboard_value(once), once)
        self.assertNotEqual(once, payload)

    def test_non_ascii_survives_byte_for_byte(self):
        """The half the false rationale would have got wrong: `terminal_safe`
        replaces Cc/Cf/Cs and nothing else, so an accented project name, a CJK
        path and an emoji reach the browser unaltered."""
        for value in self.REAL_TEXT:
            with self.subTest(value=repr(value)):
                self.assertEqual(safe_dashboard_value(value), value)
                self.assertFalse(safe_dashboard_value(value).isascii())

    def test_the_hostile_text_really_is_altered(self):
        """The control: without it the idempotency assertions above would pass
        just as well against an identity function."""
        for value in self.HOSTILE_TEXT:
            with self.subTest(value=repr(value)):
                self.assertNotEqual(safe_dashboard_value(value), value)

    def test_a_second_pass_changes_nothing_for_any_code_point(self):
        """The exhaustive form, computed rather than quoted.

        A total written into a comment here would rot: which code points are
        Cc/Cf/Cs is a function of `unicodedata.unidata_version`, which moves
        with CPython. The property does not — the escapes `terminal_safe`
        emits are drawn from `\\`, `x`, `u` and the hex digits, categories Po,
        Ll and Nd, none of them among the three it replaces — so it is the
        property that is asserted, over the whole code point space.
        """
        every_code_point = "".join(chr(cp) for cp in range(sys.maxunicode + 1))
        once = safe_dashboard_value(every_code_point)
        self.assertEqual(safe_dashboard_value(once), once)
        # Non-vacuous in both directions: it escaped something, and it is not
        # escaping everything either.
        self.assertNotEqual(once, every_code_point)
        self.assertIn("é", once)


class TestDashboardText(unittest.TestCase):
    def test_a_string_comes_through(self):
        self.assertEqual(dashboard_text("main"), "main")

    def test_anything_else_becomes_the_default(self):
        for value in (None, 5, b"main", ["main"]):
            with self.subTest(value=repr(value)):
                self.assertEqual(dashboard_text(value), "")
                self.assertEqual(dashboard_text(value, "unknown"), "unknown")


class TestEveryNumberTheApiShipsIsJsonAndExactInJavaScript(unittest.TestCase):
    """The two properties together, over the whole input space this converter is
    documented to accept."""

    def test_the_result_is_always_json_serialisable_and_exact(self):
        for value in (0, 1, -1, 0.5, -0.5, 2 ** 53 - 1, 2 ** 53, 2 ** 53 + 1,
                      2 ** 62, 1e300, float("nan"), float("inf"),
                      float("-inf"), True, False, None, "12", b"12"):
            with self.subTest(value=repr(value)):
                result = dashboard_number(value)
                self.assertIsInstance(result, (int, float))
                self.assertTrue(math.isfinite(result))
                self.assertGreaterEqual(result, 0)
                self.assertLessEqual(result, MAX_DASHBOARD_INTEGER)
                # Exact as a double, so the browser renders what was sent.
                self.assertEqual(json.loads(json.dumps(result)), result)
                if isinstance(result, int):
                    self.assertEqual(int(float(result)), result)


if __name__ == "__main__":
    unittest.main()
