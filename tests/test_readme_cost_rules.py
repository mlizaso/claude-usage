"""The README's *Cost estimates* section, checked against `pricing.PRICING`.

The rate table in README.md is a **third copy** of the price list — after
`pricing.PRICING` and the `PRICING` const in `web/js/10-pricing.js`, which
tests/test_pricing_parity.py already pins against each other. Nothing pinned the
third one, and it drifted exactly as an unenforced copy does:

* Generated tables must list every priced model, both cache-write tiers and
the exact built-in rates. Unknown models and estimated rates must be described
accurately.

Prose is not code, but the README is the user's copy of the cost rule, and this
repository has already ruled (tests/test_quota_alerts_js.py) that README prose
contradicting the code is a defect worth pinning. So this file does for the
README what test_pricing_parity.py does for the browser copy: the tables must
equal `PRICING` model for model and rate for rate, each vendor's rows must stay
under their own vendor's provenance line, the estimated ids must stay marked,
and the rule may not go back to naming a closed set of keywords.

It reads two sections and no more, so the rest of the README cannot turn it red:
*Cost estimates*, and the one paragraph of *Codex* that quantifies how much of
Codex's output is reasoning — a cost rule in its own right, since those tokens
are a subset of output that is displayed but must never be priced again.
"""

import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import reports
from pricing import ESTIMATED_RATE_MODELS, PRICING
from scanner import get_db, init_db, insert_turns, upsert_sessions
from tests.timestamps import utc_ts_on_local_day

README = Path(__file__).resolve().parent.parent / "docs" / "README.md"

# The header cell each rate column must carry, in PRICING's own field names.
# Read out of the table rather than assumed positionally, so reordering the
# columns is a rename this test follows instead of a silent misattribution.
COLUMN_FIELDS = {
    "input": "input",
    "output": "output",
    "cache write (5m)": "cache_write",
    "cache write (1h)": "cache_write_1h",
    "cache read": "cache_read",
}

ESTIMATE_MARK = "†"  # † — the footnote marking an estimated rate


def _section(name, level="## "):
    """The text of one README section, to the next heading of that level or above.

    "Or above" matters: `### OpenAI / Codex rates` is the last `###` in the file,
    so stopping only at the next `###` swallowed the whole rest of the README —
    including the `## Files` table, whose rows then parsed as rate rows.
    """
    text = README.read_text(encoding="utf-8")
    start = text.find(level + name)
    if start == -1:
        raise AssertionError(
            "README.md no longer has a %r section. If it was renamed, update "
            "this test — do not delete it; it is the only guard against the "
            "README's rate table drifting away from pricing.PRICING." % name)
    body = text.index("\n", start) + 1  # past the heading's own line
    end = re.search(r"^#{1,%d} " % len(level.strip()), text[body:], re.MULTILINE)
    return text[start:body + end.start()] if end else text[start:]


def _rate_table(section):
    """Parse one markdown rate table into {model: {field: rate}}, plus marks.

    Returns (rates, estimated) where `estimated` is the set of models carrying
    the estimate footnote marker.
    """
    rows = [line.strip() for line in section.splitlines()
            if line.strip().startswith("|")]
    if len(rows) < 3:
        raise AssertionError(
            "no markdown rate table found in:\n" + section[:400])
    header = [c.strip().lower() for c in rows[0].strip("|").split("|")]
    if header[0] != "model":
        raise AssertionError("first column of the rate table is %r, not Model"
                             % header[0])
    fields = []
    for cell in header[1:]:
        if cell not in COLUMN_FIELDS:
            raise AssertionError(
                "unknown rate column %r in the README table. Every column must "
                "name one of %s, or the figures underneath it cannot be checked "
                "against PRICING." % (cell, sorted(COLUMN_FIELDS)))
        fields.append(COLUMN_FIELDS[cell])

    rates, estimated = {}, set()
    for row in rows[2:]:  # rows[1] is the |---|---| separator
        cells = [c.strip() for c in row.strip("|").split("|")]
        model = cells[0].strip("` ")
        if model.endswith(ESTIMATE_MARK):
            model = model[:-1].strip("` ")
            estimated.add(model)
        if len(cells) - 1 != len(fields):
            raise AssertionError("row for %r has %d rate cells, header has %d"
                                 % (model, len(cells) - 1, len(fields)))
        priced = {}
        for field, cell in zip(fields, cells[1:]):
            m = re.fullmatch(r"\$([0-9]+(?:\.[0-9]+)?)/MTok", cell)
            if m is None:
                raise AssertionError(
                    "rate cell %r for %r is not written as $N.NN/MTok" % (cell, model))
            priced[field] = float(m.group(1))
        rates[model] = priced
    return rates, estimated


class TestTheReadmePricesWhatTheToolPrices(unittest.TestCase):
    """Model for model, rate for rate — the same contract test_pricing_parity
    holds the browser copy to."""

    def setUp(self):
        self.anthropic, self.anthropic_marks = _rate_table(
            _section("Anthropic rates", "### "))
        self.openai, self.openai_marks = _rate_table(
            _section("OpenAI / Codex rates", "### "))
        self.documented = dict(self.anthropic, **self.openai)
        self.marked = self.anthropic_marks | self.openai_marks

    def test_the_tables_list_exactly_the_models_pricing_prices(self):
        self.assertEqual(
            sorted(self.documented), sorted(PRICING),
            "README.md's rate tables and pricing.PRICING list different models. "
            "Every model priced by the tool must appear with its bundled "
            "estimate in the README.")

    def test_every_documented_rate_is_the_rate_that_is_charged(self):
        for model in sorted(PRICING):
            with self.subTest(model=model):
                self.assertEqual(
                    self.documented.get(model),
                    {f: PRICING[model][f] for f in COLUMN_FIELDS.values()},
                    "README.md quotes different rates for %s than pricing.py "
                    "bills it at." % model)

    def test_both_cache_write_tiers_are_documented(self):
        """Both shared-schema write fields stay visible for every vendor.

        Anthropic publishes distinct 5-minute and 1-hour prices. OpenAI exposes
        one write category, so its two columns intentionally carry one rate.
        """
        for table in (self.anthropic, self.openai):
            for model, rates in sorted(table.items()):
                with self.subTest(model=model):
                    self.assertIn("cache_write", rates)
                    self.assertIn("cache_write_1h", rates)

    def test_each_vendors_rows_stay_under_their_own_provenance_line(self):
        """The two tables carry two different datelines and two different price
        lists. An OpenAI row under the Anthropic dateline attributes OpenAI's
        prices to Anthropic."""
        vendor_split = "every `claude-*` id belongs under the Anthropic " \
                       "dateline and everything else under OpenAI's. A third " \
                       "vendor needs a third table with its own price list, " \
                       "not a row borrowing one of these two."
        self.assertEqual(sorted(self.anthropic),
                         sorted(m for m in PRICING if m.startswith("claude")),
                         vendor_split)
        self.assertEqual(sorted(self.openai),
                         sorted(m for m in PRICING if not m.startswith("claude")),
                         vendor_split)

    def test_exactly_the_estimated_rates_are_marked_as_estimates(self):
        """`is_estimated` drives a label in the dashboard; a table that printed
        those two ids as published rates would assert a price list OpenAI does
        not publish, and marking a published rate undersells it just as much."""
        self.assertEqual(
            sorted(self.marked), sorted(ESTIMATED_RATE_MODELS),
            "the ids the README marks %s and pricing.ESTIMATED_RATE_MODELS "
            "disagree." % ESTIMATE_MARK)


class TestTheReadmeStatesTheRuleGetPricingImplements(unittest.TestCase):
    """The prose above the tables, not the tables themselves."""

    def setUp(self):
        # The rule itself: everything above the first rate table. Narrower than
        # the whole section on purpose, so a rate cell cannot satisfy a prose
        # assertion by accident.
        self.rule = _section("Cost estimates").split("\n### ")[0]

    def test_the_rule_does_not_name_a_closed_set_of_families(self):
        """The sentence this replaces named five keywords and said everything
        else was "excluded (shown as `n/a`)", while `get_pricing` prices ten
        OpenAI ids by exact match and two more families by substring."""
        prose = self.rule.lower()
        self.assertNotIn("only models whose name contains", prose)
        for family in ("codex", "gpt-5"):
            with self.subTest(family=family):
                self.assertIn(
                    family, prose,
                    "the cost rule does not mention the %s family, which "
                    "get_pricing prices — so it reads as a complete list while "
                    "omitting every model a Codex user runs." % family)

    def test_all_three_resolution_tiers_are_described(self):
        """Exact id, then longest-prefix for dated snapshot ids, then family
        substring. A reader who only knows the third cannot explain why
        `gpt-5.4-mini` is not billed at `gpt-5.4`'s rate."""
        prose = self.rule.lower()
        for phrase in ("exact", "starts with", "keyword"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prose)

    def test_an_unpriced_model_is_still_documented_as_n_a_and_not_free(self):
        """The half of the old sentence that was true, and the reason
        `get_pricing` returns None instead of guessing."""
        prose = self.rule
        self.assertIn("`n/a`", prose)
        self.assertIn("$0.00", prose)

    def test_the_substring_tier_is_documented_as_reaching_local_ids(self):
        """A local model ID containing a recognized family keyword can inherit that
family rate. Document the override mechanism for correcting it."""
        self.assertIn("CLAUDE_USAGE_RATES", self.rule,
                      "the section documents no way to correct a model the "
                      "substring tier prices wrongly")


def _unpriced_cost_cell():
    """What `today` really prints in the cost column for an unpriced model.

    Driven through the real report rather than through `fmt_cost`, because the
    claim being checked is about what a user sees, and a formatter that is right
    inside a report that never calls it would read the same from here.
    """
    db_path = Path(tempfile.mkdtemp()) / "usage.db"
    ts = utc_ts_on_local_day()
    conn = get_db(db_path)
    init_db(conn)
    upsert_sessions(conn, [{
        "session_id": "sess-1", "project_name": "user/proj",
        "first_timestamp": ts, "last_timestamp": ts, "git_branch": "main",
        "model": "gemma-3", "total_input_tokens": 1_000_000,
        "total_output_tokens": 0, "total_cache_read": 0,
        "total_cache_creation": 0, "turn_count": 1,
    }])
    insert_turns(conn, [{
        "session_id": "sess-1", "timestamp": ts, "model": "gemma-3",
        "input_tokens": 1_000_000, "output_tokens": 0, "cache_read_tokens": 0,
        "cache_creation_tokens": 0, "tool_name": None, "cwd": None,
        "message_id": "m-1", "is_subagent": 0, "agent_id": None,
    }])
    conn.commit()
    buf = io.StringIO()
    with redirect_stdout(buf):
        reports._cmd_today(conn)
    conn.close()
    row = next(line for line in buf.getvalue().splitlines() if "gemma-3" in line)
    return row.rsplit("cost=", 1)[1].strip()


class TestTheReadmeAnswersForBothSurfaces(unittest.TestCase):
    """`n/a` is the *dashboard's* answer for a model with no rate.

    The terminal reports have no such cell — they print a formatted zero — so a
    rule written as an unqualified "it is never presented as free" is true of
    the page and false of `today`, `week` and `stats`. That absolute was
    introduced by the same edit that removed the keyword sentence, which is how
    easily one gets written: the dashboard's behaviour is the interesting one,
    and the section is titled as if it covered the whole tool.
    """

    def setUp(self):
        self.rule = _section("Cost estimates").split("\n### ")[0]

    def test_the_terminal_reports_print_a_zero_for_an_unpriced_model(self):
        """The code half of the claim, so the prose half cannot be a tautology.

        If this ever goes red because the reports learned to print `n/a` too,
        that is the CLI catching up with the dashboard, not a regression —
        update the README sentence and this test together rather than relaxing
        either.
        """
        self.assertEqual(_unpriced_cost_cell(), "$0.0000")

    def test_the_rule_names_what_each_surface_prints(self):
        """Both answers, or the reader is told the zero cannot happen."""
        self.assertIn("`n/a`", self.rule)
        self.assertIn(
            _unpriced_cost_cell(), self.rule,
            "the cost rule does not mention the figure `today`/`week`/`stats` "
            "actually print for an unpriced model, so it reads as if `n/a` "
            "were the tool's only answer")


def _codex_reasoning_claim():
    """The one paragraph of `## Codex` that quantifies the reasoning share."""
    section = _section("Codex")
    paragraphs = [p for p in section.split("\n\n")
                  if "reasoning" in p.lower() and "%" in p]
    if len(paragraphs) != 1:
        raise AssertionError(
            "expected exactly one paragraph under README's `## Codex` heading "
            "to quantify the reasoning share, found %d. If the claim moved, "
            "follow it here — do not drop this test, it is the only thing "
            "stopping that figure going stale invisibly again."
            % len(paragraphs))
    return paragraphs[0]


class TestTheCodexReasoningExampleIsSynthetic(unittest.TestCase):
    """The public example uses invented figures and correct subset arithmetic."""

    def setUp(self):
        self.claim = _codex_reasoning_claim()

    def test_the_example_is_explicitly_synthetic(self):
        self.assertIn("synthetic", self.claim.lower())

    def test_the_share_is_the_division_of_the_counts_it_quotes(self):
        """Auditable, and self-checking: one number cannot be updated alone."""
        counts = re.search(r"([\d]{1,3}(?:,[\d]{3})+) of ([\d]{1,3}(?:,[\d]{3})+)",
                           self.claim)
        self.assertIsNotNone(
            counts,
            "the synthetic Codex reasoning example needs both token counts "
            "so its percentage can be checked.")
        reasoning = int(counts.group(1).replace(",", ""))
        output = int(counts.group(2).replace(",", ""))
        self.assertLess(reasoning, output,
                        "reasoning tokens are a subset of output tokens")
        quoted = re.findall(r"(\d+(?:\.\d+)?)%", self.claim)
        self.assertEqual(
            len(quoted), 1,
            "expected exactly one percentage in the reasoning paragraph, "
            "found %r — this test cannot tell which one the counts belong to"
            % (quoted,))
        # Checked at the precision the sentence itself chose, so rounding to a
        # whole number stays legal and a wrong number never does.
        decimals = len(quoted[0].split(".")[1]) if "." in quoted[0] else 0
        self.assertEqual(
            round(reasoning / output * 100, decimals), float(quoted[0]),
            "the quoted reasoning share is not what its own counts divide to")


if __name__ == "__main__":
    unittest.main()
