"""The "Cost by reasoning effort" card must agree with "Cost by Model".

Both cards show the *same turns* under the *same filter state* — one grouped by
which model answered, the other by how hard it was asked to think. They are two
independent aggregations (`daily_by_model` vs `effort_by_day_model`) reduced by
two independent code paths, so their totals agreeing is the only thing that says
neither one is filtering or pricing differently from the other. A reader looking
at both has no way to tell which is the honest one when they disagree.

What this file is really guarding is the per-row costing rule stated in
AGENTS.md: **cost is summed over rows that each know their own model.** An
effort level used by opus and haiku that sums its tokens first and prices them
once charges everything at one of the two — up to 5x out, with no error, no
failing assertion anywhere else, and nothing on screen to suggest the figure is
wrong. That is why `effort_by_day_model` carries a model per row and why
`accumulateCostRow` prices one row at a time.

A single-model fixture cannot see any of that: price it right or price it wrong,
the answer is the same. So the corpus below deliberately puts **two models in
one (day, effort) bucket**, with the cheap model carrying most of the tokens, so
mispricing moves the total by a factor rather than a rounding step.

The same argument applies twice more, and the corpus is built for both:

* Include synthetic Codex reasoning output so double-pricing it creates a
visible arithmetic error. * Mix five-minute and one-hour cache writes to
verify that blended per-million rates are marked as averages. Keep a single-
tier control.

These tests drive the page's real `applyFilter` under node through
`tests.test_dashboard_js`'s harness — the same mechanism
`tests/test_project_cost_attribution.py` uses — so they measure what the card
would actually render, not a Python re-implementation of it.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path

import scanner
from dashboard import get_dashboard_data
from pricing import calc_cost

from tests.test_dashboard_js import emit, fmt_money, requires_node, run_js
from tests.timestamps import local_day, utc_ts_on_local_day

OPUS = "claude-opus-4-8"      # $5 / $25 per M in / out
HAIKU = "claude-haiku-4-5"    # $1 / $5   — exactly 5x cheaper than opus
SONNET = "claude-sonnet-4-6"  # $3 / $15

# Codex. Published rates, so the reasoning column sits on money rather than on
# an `n/a` that would make every assertion about it vacuous a second time.
SOL = "gpt-5.6-sol"    # $4.00 / $20.00 promotional rate per M in / out
LUNA = "gpt-5.6-luna"  # $0.20 /  $1.20 — 25x cheaper

TODAY = local_day(0)
YESTERDAY = local_day(1)

# Every id whose innerHTML a renderer writes is captured, so an assertion can be
# made against the markup the browser would receive. `stubEl()` comes from the
# harness's DOM stub, so the elements still satisfy every other call the app
# makes on them (classList, dataset, closest, …) — only innerHTML is observed.
_APPLY_FILTER = """(() => {
  rawData = payload;
  selectedSource = source;
  selectedModels = new Set(models === null ? payload.all_models : models);
  // `range` is either a named range the page knows ('all', 'today', ...) or an
  // explicit {start, end} pair, which we inject by shadowing getRangeBounds —
  // the page has no vocabulary for "just this one historical day".
  if (range && typeof range === 'object') {
    selectedRange = 'all';
    getRangeBounds = () => range;
  } else {
    selectedRange = range;
  }

  const html = {};
  const els = new Map();
  document.getElementById = (id) => {
    if (!els.has(id)) {
      const el = stubEl();
      Object.defineProperty(el, 'innerHTML', {
        get() { return html[id] === undefined ? '' : html[id]; },
        set(v) { html[id] = v; },
      });
      els.set(id, el);
    }
    return els.get(id);
  };

  let capturedTotals = null;
  renderStats = (t) => { capturedTotals = t; };
  applyFilter();

  const effortTotal = mergeCostBuckets(lastByEffort);
  const modelCost = (m) => calcCost(m.model, m.input, m.output, m.cache_read,
                                    m.cache_creation, m.cache_creation_1h);
  const outputPart = (m) => {
    const p = costParts(m.model, m.input, m.output, m.cache_read,
                        m.cache_creation, m.cache_creation_1h);
    return p ? p.output : 0;
  };
  const bucket = (b) => ({
    turns: b.turns, cost: b.cost, billable: !!b.billable,
    input: b.input, output: b.output, cache_read: b.cache_read,
    cache_creation: b.cache_creation, cache_creation_1h: b.cache_creation_1h,
    reasoning: b.reasoning,
  });
  // The model card's own token columns. `capturedTotals` cannot stand in for
  // these: it has no `reasoning` key at all, so a comparison against it can
  // never notice the two cards disagreeing about the one column that separates
  // the two costing paths.
  const sumOver = (rows, key) => rows.reduce((s, r) => s + (r[key] || 0), 0);
  const modelTokens = {};
  for (const k of TOKEN_COLUMNS) modelTokens[k] = sumOver(lastByModel, k);

  return {
    totals: capturedTotals,
    modelTableCost: lastByModel.reduce((s, m) => s + modelCost(m), 0),
    modelTableOutputCost: lastByModel.reduce((s, m) => s + outputPart(m), 0),
    modelTableTurns: lastByModel.reduce((s, m) => s + m.turns, 0),
    modelTableTokens: modelTokens,
    modelNames: lastByModel.map(m => m.model),
    byModel: lastByModel.map(m => ({ model: m.model, cost: modelCost(m) })),
    byEffort: lastByEffort.map(e => Object.assign(bucket(e), { effort: e.effort })),
    effortTotal: bucket(effortTotal),
    byStopReason: lastByStopReason.map(r => ({
      stop_reason: r.stop_reason, turns: r.turns, output: r.output,
      cost: r.cost, billable: !!r.billable })),
    labels: { blank: effortLabel(''), named: effortLabel('medium') },
    html: html,
  };
})()"""


def _assistant(session_id, model, ts, message_id, inp=0, out=0, cache_read=0,
               cache_creation=0, cache_1h=None, effort=None, stop_reason=None,
               cwd="/home/u/myproj", branch="main"):
    """One Claude Code assistant record.

    `effort` sits at the TOP LEVEL of the record and `stop_reason` inside
    `message` — that asymmetry is what the real transcripts carry, and
    `tests/test_reasoning_effort.py` keeps the parser honest about it.

    `cache_1h=None` means the record carries no `cache_creation` breakdown at
    all, which the scanner stores as an all-5-minute write. Passing the full
    `cache_creation` makes it an all-1-hour one. Both are *pure* tiers; only a
    value strictly between the two is a record that is itself a blend.
    """
    usage = {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }
    if cache_1h is not None:
        usage["cache_creation"] = {
            "ephemeral_1h_input_tokens": cache_1h,
            "ephemeral_5m_input_tokens": max(cache_creation - cache_1h, 0),
        }
    message = {"id": message_id, "model": model, "content": [], "usage": usage}
    if stop_reason is not None:
        message["stop_reason"] = stop_reason
    record = {
        "type": "assistant", "sessionId": session_id, "timestamp": ts,
        "cwd": cwd, "gitBranch": branch, "message": message,
    }
    if effort is not None:
        record["effort"] = effort
    return json.dumps(record)


# The Claude corpus. Three properties are load-bearing and are asserted as
# fixture guards below rather than left to trust:
#
#  * the (today, "high") bucket holds OPUS **and** HAIKU, and the (today, "")
#    bucket does too — a level that mixes models is the only case that can catch
#    per-bucket pricing;
#  * the cheap model carries four times the tokens of the expensive one, so
#    charging the level at opus overstates it by a factor, not a rounding step;
#  * the "medium" level is one model on two days, one day written entirely to
#    the 1-hour cache and the other entirely to the 5-minute one. Neither row is
#    a blend; the level they sum to is. "low" is the control: one model, one
#    tier, a rate that IS on the price list.
_CORPUS = [
    # ── today ───────────────────────────────────────────────────────────────
    _assistant("s-today", OPUS, utc_ts_on_local_day(0, 9), "m-high-opus",
               inp=1_000_000, out=200_000, cache_read=500_000,
               cache_creation=100_000, effort="high", stop_reason="end_turn"),
    _assistant("s-today", HAIKU, utc_ts_on_local_day(0, 10), "m-high-haiku",
               inp=4_000_000, out=800_000, cache_read=2_000_000,
               cache_creation=400_000, cache_1h=300_000,
               effort="high", stop_reason="end_turn"),
    # Single model, single tier: its derived write rate is sonnet's list price.
    _assistant("s-today", SONNET, utc_ts_on_local_day(0, 11), "m-low-sonnet",
               inp=300_000, out=64_000, cache_creation=120_000,
               effort="low", stop_reason="max_tokens"),
    # Half of the tier pair: all 5-minute.
    _assistant("s-today", HAIKU, utc_ts_on_local_day(0, 16), "m-medium-haiku",
               inp=200_000, out=40_000, cache_creation=200_000,
               effort="medium", stop_reason="end_turn"),
    # No `effort` key at all: unknown, which is not a level.
    _assistant("s-today", OPUS, utc_ts_on_local_day(0, 12), "m-blank-opus",
               inp=50_000, out=7_000, stop_reason="end_turn"),
    _assistant("s-today", HAIKU, utc_ts_on_local_day(0, 13), "m-blank-haiku",
               inp=80_000, out=9_000, stop_reason="tool_use"),
    # ── yesterday ───────────────────────────────────────────────────────────
    _assistant("s-yesterday", SONNET, utc_ts_on_local_day(1, 14), "y-high-sonnet",
               inp=400_000, out=90_000, effort="high", stop_reason="end_turn"),
    # The other half of the tier pair: all 1-hour, same model, same level.
    _assistant("s-yesterday", HAIKU, utc_ts_on_local_day(1, 15), "y-medium-haiku",
               inp=600_000, out=120_000, cache_creation=200_000,
               cache_1h=200_000, effort="medium", stop_reason="tool_use"),
]


# ── Codex ──────────────────────────────────────────────────────────────────
# A real rollout, parsed by the real Codex parser, because it is the only source
# that reports reasoning tokens. Same conventions as
# tests/test_codex_transcripts.py and tests/test_reasoning_effort.py.

CODEX_THREAD = "019fcf22-c0de-4dea-9e5f-0d0e0d0e0d0e"


def _codex(rtype, payload, ts):
    return json.dumps({"timestamp": ts, "type": rtype, "payload": payload})


def _codex_meta(ts):
    return _codex("session_meta", {
        "id": CODEX_THREAD, "session_id": CODEX_THREAD, "cwd": "/home/u/codexproj",
        "originator": "codex_vscode", "cli_version": "0.146.0",
        "source": "vscode", "thread_source": "user", "model_provider": "openai",
        "git": {"commit_hash": "abc123", "branch": "main",
                "repository_url": "git@example:me/codexproj.git"},
    }, ts)


def _codex_context(model, effort, ts):
    """Codex establishes (model, effort) out of band and carries them forward."""
    return _codex("turn_context", {"turn_id": "t", "cwd": "/home/u/codexproj",
                                   "model": model, "effort": effort}, ts)


def _codex_turn(cum, inp, cached, out, reasoning, ts):
    """One Codex API response.

    Two subset relations the parser normalises, and the fixture has to respect
    or it is not describing anything real: `cached` is part of `inp` (the
    scanner stores the uncached remainder), and `reasoning` is part of `out`
    (stored beside it for display, never added to it).
    """
    return _codex("event_msg", {
        "type": "token_count",
        "info": {
            "last_token_usage": {
                "input_tokens": inp, "cached_input_tokens": cached,
                "cache_write_input_tokens": 0, "output_tokens": out,
                "reasoning_output_tokens": reasoning, "total_tokens": inp + out,
            },
            # Per-thread monotonic counter; `codex:<thread>:<cumulative>` is the
            # message-id analogue the dedupe index keys on.
            "total_token_usage": {
                "input_tokens": 0, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "output_tokens": 0,
                "reasoning_output_tokens": 0, "total_tokens": cum,
            },
            "model_context_window": 258400,
        },
    }, ts)


# The synthetic Codex fixture mixes differently priced models in one effort
# bucket, with nonzero reasoning output to make double-counting detectable.
_CODEX_CORPUS = [
    _codex_meta(utc_ts_on_local_day(1, 9)),
    _codex_context(LUNA, "medium", utc_ts_on_local_day(1, 9)),
    _codex_turn(500_000, inp=500_000, cached=400_000, out=100_000,
                reasoning=70_000, ts=utc_ts_on_local_day(1, 9, 5)),
    _codex_context(SOL, "high", utc_ts_on_local_day(0, 9)),
    _codex_turn(1_500_000, inp=1_000_000, cached=800_000, out=200_000,
                reasoning=150_000, ts=utc_ts_on_local_day(0, 9, 5)),
    _codex_context(LUNA, "high", utc_ts_on_local_day(0, 10)),
    _codex_turn(5_500_000, inp=4_000_000, cached=3_000_000, out=800_000,
                reasoning=600_000, ts=utc_ts_on_local_day(0, 10, 5)),
]

# (label, date range, selected models, source). Every case is applied to BOTH
# tables, because the claim under test is that they agree under the same filter
# state — not that either one is right in isolation.
#
# Models are left at "all" for most cases on purpose: selecting one source's
# models happens to exclude the other's, so a source-isolation bug would hide
# behind the model filter. AGENTS.md names that trap explicitly.
_FILTER_STATES = [
    ("all time, every model", "all", None, "claude"),
    ("today only", {"start": TODAY, "end": TODAY}, None, "claude"),
    ("yesterday only", {"start": YESTERDAY, "end": YESTERDAY}, None, "claude"),
    # Drops the expensive model out of the mixed bucket entirely.
    ("haiku + sonnet only", "all", [HAIKU, SONNET], "claude"),
    # Keeps only the expensive model in it.
    ("opus only", "all", [OPUS], "claude"),
    # The other source, where reasoning tokens are non-zero and both cards have
    # to ignore them in exactly the same way.
    ("codex, all time", "all", None, "codex"),
    ("codex, today only", {"start": TODAY, "end": TODAY}, None, "codex"),
    ("codex, cheap model only", "all", [LUNA], "codex"),
    # Nothing in range at all: both tables must be empty together.
    ("a range with nothing in it", {"start": "1999-01-01", "end": "1999-01-02"},
     None, "claude"),
]


def _bounds(date_range):
    if isinstance(date_range, dict):
        return date_range.get("start"), date_range.get("end")
    if date_range == "all":
        return None, None
    raise AssertionError(f"unsupported range in the Python mirror: {date_range!r}")


def _rows_in_view(payload, key, date_range, models, source):
    """The payload rows the page's own predicate would keep.

    Deliberately a separate, dumb implementation: if it were shared with the JS
    it could not detect the JS filtering differently.
    """
    start, end = _bounds(date_range)
    allowed = set(payload["all_models"] if models is None else models)
    kept = []
    for row in payload[key]:
        if (row.get("source") or "claude") != source:
            continue
        if row["model"] not in allowed:
            continue
        if start and row["day"] < start:
            continue
        if end and row["day"] > end:
            continue
        kept.append(row)
    return kept


def _source_rows(payload, key="effort_by_day_model", source="claude", **match):
    """Payload rows of one source matching every `field=value` given.

    The source filter is not optional. The corpus holds two of them and they
    share level names, so a scan that omits it silently compares one source's
    rendered card against both sources' rows.
    """
    rows = [r for r in payload[key] if (r.get("source") or "claude") == source]
    for field, value in match.items():
        rows = [r for r in rows if r[field] == value]
    return rows


def _row_cost(row):
    """What a row costs. Note what is NOT passed: `reasoning`.

    It is part of `output`, which is already here, so handing it over as well
    would bill it twice — the very rule the browser's cost path is being
    measured against.
    """
    return calc_cost(row["model"], row["input"], row["output"], row["cache_read"],
                     row["cache_creation"], row["cache_creation_1h"])


def _row_cost_if_reasoning_were_billed(row):
    """The natural mistake, priced: reasoning added to the output it is part of."""
    return calc_cost(row["model"], row["input"],
                     row["output"] + row.get("reasoning", 0), row["cache_read"],
                     row["cache_creation"], row["cache_creation_1h"])


def _cost_per_effort(rows):
    """Per-row cost, each row at its own model — the only correct way."""
    out = {}
    for row in rows:
        out[row["effort"]] = out.get(row["effort"], 0.0) + _row_cost(row)
    return out


def _last_cost_cell(markup):
    """The money in a table footer's final `<td class="cost">` cell."""
    cells = re.findall(r'<td class="cost">([^<]*)</td>', markup)
    if not cells:
        raise AssertionError(f"no cost cell in rendered footer: {markup[:400]!r}")
    return cells[-1].strip()


# The columns of a rendered effort row, in the order `effortRowHTML` writes them.
# Positional because the markup carries no per-cell label (`labelCells` is a DOM
# call, which the harness's stub swallows); the length is asserted on every
# lookup so a new column shifts nothing silently.
_EFFORT_COLUMNS = ("effort", "turns", "input", "output", "cache_read",
                   "cache_creation", "reasoning", "cost")


def _effort_cell(body, level, column):
    """One `<td>` of the rendered "Cost by reasoning effort" row for `level`."""
    label = level if level else "not recorded"
    rows = [r for r in body.split("<tr>") if ">" + label + "<" in r]
    if len(rows) != 1:
        raise AssertionError(
            f"expected exactly one {label!r} row, found {len(rows)}")
    cells = re.findall(r"<td\b[^>]*>(.*?)</td>", rows[0], re.S)
    if len(cells) != len(_EFFORT_COLUMNS):
        raise AssertionError(
            f"effort row has {len(cells)} cells, expected {len(_EFFORT_COLUMNS)}: "
            f"{rows[0][:400]!r}")
    return cells[_EFFORT_COLUMNS.index(column)]


class _EffortFixture(unittest.TestCase):
    """Writes real transcripts, runs the real scanner, reads the real API."""

    @classmethod
    def build_payload(cls):
        tmp = Path(tempfile.mkdtemp())
        projects = tmp / "projects" / "u" / "proj"
        projects.mkdir(parents=True)
        (projects / "sess.jsonl").write_text("\n".join(_CORPUS) + "\n",
                                             encoding="utf-8")
        # Its own file: the parser is chosen by sniffing the first record, so
        # the two formats cannot share one. The rollout filename is the real
        # shape (only the uuid tail is an identity).
        codex = tmp / "projects" / "codex"
        codex.mkdir(parents=True)
        (codex / f"rollout-2026-01-01T09-00-00-{CODEX_THREAD}.jsonl").write_text(
            "\n".join(_CODEX_CORPUS) + "\n", encoding="utf-8")
        db = tmp / "usage.db"
        scanner.scan(projects_dir=tmp / "projects", db_path=db, verbose=False)
        return get_dashboard_data(db)

    @classmethod
    def setUpClass(cls):
        cls.payload = cls.build_payload()

    def apply_filter(self, date_range="all", models=None, source="claude"):
        return run_js(emit(_APPLY_FILTER, payload=self.payload,
                           range=date_range, models=models, source=source))


@requires_node
class TestFixtureIsDiscriminating(_EffortFixture):
    """Guards every test below: a single-model corpus proves nothing here."""

    def _bucket(self, day, effort, source="claude"):
        return _source_rows(self.payload, source=source, day=day, effort=effort)

    def test_one_day_effort_bucket_really_holds_two_models(self):
        rows = self._bucket(TODAY, "high")
        self.assertEqual({r["model"] for r in rows}, {OPUS, HAIKU},
                         "fixture is not mixed-model; the pricing bug would hide")
        self.assertEqual(len(rows), 2, "the rollup collapsed the models into one row")

    def test_the_unrecorded_bucket_is_mixed_model_too(self):
        rows = self._bucket(TODAY, "")
        self.assertEqual({r["model"] for r in rows}, {OPUS, HAIKU})

    def test_the_cheap_model_carries_most_of_the_mixed_bucket(self):
        """So pricing the level at one model moves the total by a factor."""
        rows = {r["model"]: r for r in self._bucket(TODAY, "high")}
        self.assertGreater(rows[HAIKU]["input"], 3 * rows[OPUS]["input"])

    def test_the_two_ways_of_pricing_the_bucket_are_far_apart(self):
        """States the defect as a number, so it cannot creep back unnoticed."""
        rows = self._bucket(TODAY, "high")
        honest = sum(_row_cost(r) for r in rows)
        at_one_model = calc_cost(
            OPUS,
            sum(r["input"] for r in rows), sum(r["output"] for r in rows),
            sum(r["cache_read"] for r in rows),
            sum(r["cache_creation"] for r in rows),
            sum(r["cache_creation_1h"] for r in rows))
        self.assertGreater(at_one_model, 2 * honest)

    def test_the_codex_bucket_is_mixed_model_and_cheap_heavy_too(self):
        """The same discriminating shape on the other source, which is where the
        reasoning column lives."""
        rows = {r["model"]: r for r in self._bucket(TODAY, "high", source="codex")}
        self.assertEqual(set(rows), {SOL, LUNA})
        self.assertGreater(rows[LUNA]["input"], 3 * rows[SOL]["input"])

    def test_every_codex_row_reports_reasoning_tokens(self):
        """Without this the reasoning rule is arithmetic on zero, which is what
        let 202 frontend tests pass while `accumulateCostRow` billed reasoning
        a second time."""
        rows = _source_rows(self.payload, source="codex")
        self.assertTrue(rows, "no Codex rows reached the payload at all")
        for row in rows:
            with self.subTest(day=row["day"], model=row["model"]):
                self.assertGreater(row["reasoning"], 0)
                # A subset of output, never an addition to it.
                self.assertLess(row["reasoning"], row["output"])

    def test_no_claude_row_reports_reasoning_tokens(self):
        """States why the Codex rows had to be added rather than a flag flipped:
        Claude never writes the column, so the Claude corpus cannot carry it."""
        for row in _source_rows(self.payload, source="claude"):
            self.assertEqual(row["reasoning"], 0)

    def test_the_medium_level_is_two_pure_tiers_that_sum_to_a_blend(self):
        """The exact shape that loses the `avg` marker: every contributing row
        is a single tier, and the level they add up to is not."""
        rows = _source_rows(self.payload, effort="medium")
        self.assertEqual(len(rows), 2)
        for row in rows:
            with self.subTest(day=row["day"]):
                self.assertGreater(row["cache_creation"], 0)
                self.assertIn(row["cache_creation_1h"], (0, row["cache_creation"]),
                              "this row is itself a blend; the bug would hide")
        total = sum(r["cache_creation"] for r in rows)
        long_lived = sum(r["cache_creation_1h"] for r in rows)
        self.assertTrue(0 < long_lived < total, "the level is not a blend either")

    def test_the_low_level_is_a_single_pure_tier(self):
        """The control. Its derived rate IS on the price list, so marking it
        `avg` would be as wrong as leaving `medium` unmarked."""
        rows = _source_rows(self.payload, effort="low")
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0]["cache_creation"], 0)
        self.assertEqual(rows[0]["cache_creation_1h"], 0)


@requires_node
class TestEffortTableAgreesWithModelTable(_EffortFixture):
    """The property that catches per-row mispricing."""

    def test_the_effort_total_equals_the_model_total(self):
        for label, date_range, models, source in _FILTER_STATES:
            with self.subTest(state=label):
                got = self.apply_filter(date_range, models, source)
                self.assertAlmostEqual(got["effortTotal"]["cost"],
                                       got["modelTableCost"], places=6)
                # `totals` feeds the stat tiles — a third code path over the
                # same turns, and the number the card sits underneath.
                self.assertAlmostEqual(got["effortTotal"]["cost"],
                                       got["totals"]["cost"], places=6)

    def test_the_effort_levels_sum_to_the_effort_total(self):
        for label, date_range, models, source in _FILTER_STATES:
            with self.subTest(state=label):
                got = self.apply_filter(date_range, models, source)
                self.assertAlmostEqual(sum(e["cost"] for e in got["byEffort"]),
                                       got["modelTableCost"], places=6)

    def test_the_effort_total_counts_the_same_turns(self):
        """Money agreeing while turn counts do not would mean two different
        sets of turns happened to price the same."""
        for label, date_range, models, source in _FILTER_STATES:
            with self.subTest(state=label):
                got = self.apply_filter(date_range, models, source)
                self.assertEqual(got["effortTotal"]["turns"], got["modelTableTurns"])

    def test_each_level_matches_the_per_row_python_truth(self):
        """Not just the grand total: a level overcharged and another
        undercharged by the same amount would still balance overall.

        Runs on the Codex states too — `_row_cost` prices `output` and never
        `reasoning`, so a browser that added the two would part company with
        this mirror level by level.
        """
        for label, date_range, models, source in _FILTER_STATES:
            with self.subTest(state=label):
                rows = _rows_in_view(self.payload, "effort_by_day_model",
                                     date_range, models, source)
                truth = _cost_per_effort(rows)
                got = self.apply_filter(date_range, models, source)
                shown = {e["effort"]: e["cost"] for e in got["byEffort"]}
                self.assertEqual(set(shown), set(truth))
                for effort, expected in truth.items():
                    self.assertAlmostEqual(shown[effort], expected, places=6,
                                           msg=f"level {effort!r} mispriced")

    def test_the_mixed_level_is_not_priced_at_one_model(self):
        """The concrete failure: today's `high` level used opus and haiku, and
        haiku carries 4x the tokens. Charging the level at opus is a number
        this test names."""
        rows = _source_rows(self.payload, day=TODAY, effort="high")
        at_one_model = calc_cost(
            OPUS,
            sum(r["input"] for r in rows), sum(r["output"] for r in rows),
            sum(r["cache_read"] for r in rows),
            sum(r["cache_creation"] for r in rows),
            sum(r["cache_creation_1h"] for r in rows))
        got = self.apply_filter({"start": TODAY, "end": TODAY})
        high = next(e for e in got["byEffort"] if e["effort"] == "high")
        self.assertNotAlmostEqual(high["cost"], at_one_model, places=4)
        self.assertLess(high["cost"], at_one_model / 2)
        self.assertAlmostEqual(high["cost"], sum(_row_cost(r) for r in rows),
                               places=6)

    def test_the_two_footers_print_the_same_money(self):
        """What a reader actually compares: the rendered totals rows."""
        for label, date_range, models, source in _FILTER_STATES:
            with self.subTest(state=label):
                got = self.apply_filter(date_range, models, source)
                if not got["byEffort"]:
                    # Nothing in range renders no effort footer at all, so there
                    # is no money to compare. `test_the_effort_total_equals_the
                    # _model_total` still covers this state.
                    continue
                self.assertEqual(_last_cost_cell(got["html"]["effort-cost-total"]),
                                 _last_cost_cell(got["html"]["model-cost-total"]))

    def test_the_effort_tokens_match_the_model_tokens(self):
        """The tokens under the money, column by column — `reasoning` included.

        It is the one column that separates the two costing paths, and it was
        the one column this comparison used to leave out.
        """
        for source in ("claude", "codex"):
            got = self.apply_filter("all", None, source)
            for column in ("input", "output", "cache_read", "cache_creation",
                           "cache_creation_1h", "reasoning"):
                with self.subTest(source=source, column=column):
                    self.assertEqual(got["effortTotal"][column],
                                     got["modelTableTokens"][column])

    def test_neither_source_leaks_the_others_turns(self):
        """Exactly one source is on screen at a time, and that is a correctness
        rule: a Claude cost and a Codex cost are not the same kind of number.
        Both views are checked with EVERY model selected, because selecting one
        source's models happens to exclude the other's — which is how a deleted
        source filter passes a totals assertion."""
        for source, own, other in (("claude", {OPUS, HAIKU, SONNET}, {SOL, LUNA}),
                                   ("codex", {SOL, LUNA}, {OPUS, HAIKU, SONNET})):
            with self.subTest(source=source):
                got = self.apply_filter("all", None, source)
                self.assertEqual(set(got["modelNames"]), own)
                self.assertFalse(set(got["modelNames"]) & other)
                rows = _source_rows(self.payload, source=source)
                self.assertEqual(got["effortTotal"]["turns"],
                                 sum(r["turns"] for r in rows))
                self.assertGreater(got["effortTotal"]["turns"], 0)


@requires_node
class TestReasoningTokensAreCountedButNeverPriced(_EffortFixture):
    """The newest money rule in the codebase, made executable.

    `reasoning` is a SUBSET of `output`. The output figure every cost path
    multiplies already contains it, so charging it again — as a fifth
    `COST_COLUMNS` entry, or by folding it into the output term — bills the same
    tokens twice. `COST_COLUMNS` excluding `reasoning` while `TOKEN_COLUMNS`
    includes it is the structural guard, and it sits one line away from the
    columns that ARE priced.

    Nothing could fail if that guard went. Only Codex reports the figure, and
    every frontend fixture was Claude-only, so `reasoning` was 0 on every row in
    the suite: feeding `row.output + row.reasoning` into `accumulateCostRow`'s
    `costParts` call left all 202 frontend tests green.
    """

    def setUp(self):
        self.got = self.apply_filter("all", None, "codex")
        self.rows = _source_rows(self.payload, source="codex")

    def test_the_card_shows_reasoning_tokens_at_all(self):
        self.assertGreater(self.got["effortTotal"]["reasoning"], 0)
        self.assertEqual(self.got["effortTotal"]["reasoning"],
                         sum(r["reasoning"] for r in self.rows))

    def test_billing_reasoning_a_second_time_would_be_a_visible_number(self):
        """States the defect as a number, so a fixture that stops discriminating
        (someone drops the reasoning figures) fails here rather than silently
        making every assertion below vacuous again."""
        honest = sum(_row_cost(r) for r in self.rows)
        double = sum(_row_cost_if_reasoning_were_billed(r) for r in self.rows)
        self.assertGreater(honest, 0, "an unpriced source cannot show the error")
        self.assertGreater(double, 1.4 * honest)

    def test_the_effort_cost_is_the_per_row_truth_with_reasoning_ignored(self):
        honest = sum(_row_cost(r) for r in self.rows)
        self.assertAlmostEqual(self.got["effortTotal"]["cost"], honest, places=6)

    def test_each_level_ignores_reasoning_too(self):
        """A grand total can balance while one level double-charges and another
        is short. Per level, then."""
        truth = _cost_per_effort(self.rows)
        shown = {e["effort"]: e["cost"] for e in self.got["byEffort"]}
        self.assertEqual(set(shown), set(truth))
        for effort, expected in truth.items():
            with self.subTest(level=effort):
                self.assertAlmostEqual(shown[effort], expected, places=6)

    def test_the_rendered_footer_prints_the_honest_money(self):
        """Not an internal figure: the string the reader sees."""
        honest = sum(_row_cost(r) for r in self.rows)
        self.assertEqual(_last_cost_cell(self.got["html"]["effort-cost-total"]),
                         fmt_money(honest))

    def test_the_reasoning_cell_prints_a_count_and_no_money(self):
        """`reasoningCell` is handed a null cost on purpose. A rate or a dollar
        figure in this column is the double-billing, rendered."""
        body = self.got["html"]["effort-cost-body"]
        for level in ("high", "medium"):
            cell = _effort_cell(body, level, "reasoning")
            with self.subTest(level=level):
                self.assertNotIn("$", cell)
                self.assertNotIn("cell-cost", cell)
                self.assertNotIn("cell-rate", cell)
                # A count was printed, and it is not the "never reported" dash.
                self.assertNotIn("&mdash;", cell)
                self.assertRegex(cell, r"^[0-9]")
                rows = _source_rows(self.payload, source="codex", effort=level)
                shown = next(e for e in self.got["byEffort"] if e["effort"] == level)
                self.assertEqual(shown["reasoning"],
                                 sum(r["reasoning"] for r in rows))

    def test_the_claude_card_says_the_figure_is_absent_not_zero(self):
        """Claude bills thinking inside Output without breaking it out, so a
        literal 0 would assert the model did none."""
        body = self.apply_filter("all", None, "claude")["html"]["effort-cost-body"]
        cell = _effort_cell(body, "high", "reasoning")
        self.assertIn("&mdash;", cell)
        self.assertNotIn(">0<", cell)


@requires_node
class TestABlendedCacheWriteRateSaysSo(_EffortFixture):
    """A per-million figure that appears on no price list has to say `avg`.

    Whether a cell's rate is a blend is a property of the tokens THAT CELL
    prints, not of the rows that fed it. Two rows that are each a single tier
    sum to a bucket whose derived write rate is between the two published ones —
    so asking "was any contributing row itself mixed?" answers the wrong
    question and drops the marker. `tokenCostCell`'s own comment says why it
    matters: an unlabelled "$1.6250/M" invites the reader to go looking for it
    on the price list, where it does not appear.
    """

    def setUp(self):
        self.body = self.apply_filter("all")["html"]["effort-cost-body"]

    def test_a_level_whose_rows_are_each_pure_is_still_marked_when_it_blends(self):
        cell = _effort_cell(self.body, "medium", "cache_creation")
        self.assertIn("avg", cell,
                      "the level's cache-write rate is a blend and says nothing")

    def test_the_blended_rate_really_is_on_no_price_list(self):
        """The marker matters because the number cannot be looked up."""
        rows = _source_rows(self.payload, effort="medium")
        total = sum(r["cache_creation"] for r in rows)
        cost = sum(_row_cost(r) - calc_cost(r["model"], r["input"], r["output"],
                                            r["cache_read"], 0, 0)
                   for r in rows)
        rate = cost / total * 1e6
        # haiku's two published write tiers.
        self.assertNotAlmostEqual(rate, 1.25, places=4)
        self.assertNotAlmostEqual(rate, 2.00, places=4)
        self.assertTrue(1.25 < rate < 2.00)

    def test_a_single_tier_level_is_not_marked(self):
        """The control that keeps the marker meaningful: `low` is one model on
        one tier, so its rate IS sonnet's published $3.75/M. Marking it `avg`
        would send the reader hunting for a blend that is not there."""
        cell = _effort_cell(self.body, "low", "cache_creation")
        self.assertNotIn("avg", cell)
        self.assertIn("$3.75/M", cell)

    def test_a_level_containing_a_mixed_row_is_still_marked(self):
        """The case the old flag did catch, kept: today's `high` bucket has a
        row that spans both tiers on its own."""
        cell = _effort_cell(self.body, "high", "cache_creation")
        self.assertIn("avg", cell)

    def test_the_totals_row_is_marked_too(self):
        total = self.apply_filter("all")["html"]["effort-cost-total"]
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", total, re.S)
        self.assertEqual(len(cells), len(_EFFORT_COLUMNS))
        self.assertIn("avg", cells[_EFFORT_COLUMNS.index("cache_creation")])


@requires_node
class TestUnrecordedEffortIsNamedAsAnAbsence(_EffortFixture):
    """'' means the effort was never written down. It is not a level.

    Folding it into `medium` would invent a fact about turns nobody recorded
    one for; dropping it would leave this card short of every other total on
    the page. Both were live possibilities — the rollup emits '' and the
    renderer has to decide what that means.
    """

    def setUp(self):
        self.got = self.apply_filter("all")
        self.efforts = [e["effort"] for e in self.got["byEffort"]]

    def test_the_blank_bucket_exists_and_is_its_own_row(self):
        self.assertIn("", self.efforts)
        self.assertEqual(self.efforts.count(""), 1)

    def test_it_is_not_folded_into_a_named_level(self):
        named = {e["effort"] for e in self.got["byEffort"] if e["effort"]}
        self.assertEqual(named, {"high", "low", "medium"})
        blank_rows = _source_rows(self.payload, effort="")
        blank = next(e for e in self.got["byEffort"] if e["effort"] == "")
        self.assertEqual(blank["turns"], sum(r["turns"] for r in blank_rows))
        # And every named level holds exactly its own rows — the other half of
        # "not folded", which the turn count of the blank bucket alone misses.
        for level in named:
            rows = _source_rows(self.payload, effort=level)
            shown = next(e for e in self.got["byEffort"] if e["effort"] == level)
            with self.subTest(level=level):
                self.assertEqual(shown["turns"], sum(r["turns"] for r in rows))

    def test_the_blank_bucket_is_priced_per_row_like_the_named_ones(self):
        """It is mixed-model too, so the same 5x error applies to it."""
        rows = _source_rows(self.payload, effort="")
        blank = next(e for e in self.got["byEffort"] if e["effort"] == "")
        self.assertAlmostEqual(blank["cost"], sum(_row_cost(r) for r in rows),
                               places=6)

    def test_the_label_reads_as_an_absence(self):
        self.assertEqual(self.got["labels"]["blank"], "not recorded")
        self.assertEqual(self.got["labels"]["named"], "medium")

    def test_the_rendered_row_says_not_recorded(self):
        body = self.got["html"]["effort-cost-body"]
        self.assertIn(">not recorded<", body)
        for level in ("high", "low", "medium"):
            with self.subTest(level=level):
                self.assertIn(">" + level + "<", body)

    def test_only_the_blank_row_is_marked_unset(self):
        """The `unset` class is what visually separates an absence from a level.
        One row carries it; if a named level did, the two would read alike."""
        body = self.got["html"]["effort-cost-body"]
        self.assertEqual(body.count("effort-tag unset"), 1)
        self.assertEqual(len(re.findall(r'<span class="effort-tag', body)),
                         len(self.got["byEffort"]))

    def test_the_blank_bucket_is_not_labelled_with_a_level_name(self):
        """The specific regression: 'not recorded' rendered as 'medium'."""
        blank_row = [row for row in self.got["html"]["effort-cost-body"].split("<tr>")
                     if "not recorded" in row]
        self.assertEqual(len(blank_row), 1)
        for level in ("high", "low", "medium"):
            self.assertNotIn(">" + level + "<", blank_row[0])


@requires_node
class TestStopReasonCardUsesTheSameTurns(_EffortFixture):
    """The other breakdown on the same rollup pattern.

    It costs only the output column — that is all `stop_reason_by_day_model`
    carries — so its money must equal the model table's output component, and
    its turns must equal the model table's turns. Same per-row rule: each row
    knows its own model.
    """

    def test_the_stop_reasons_cover_every_turn_in_view(self):
        for label, date_range, models, source in _FILTER_STATES:
            with self.subTest(state=label):
                got = self.apply_filter(date_range, models, source)
                self.assertEqual(sum(r["turns"] for r in got["byStopReason"]),
                                 got["modelTableTurns"])

    def test_the_stop_reason_output_cost_matches_the_model_tables(self):
        for label, date_range, models, source in _FILTER_STATES:
            with self.subTest(state=label):
                got = self.apply_filter(date_range, models, source)
                self.assertAlmostEqual(sum(r["cost"] for r in got["byStopReason"]),
                                       got["modelTableOutputCost"], places=6)

    def test_a_reason_that_was_never_recorded_renders_as_an_absence(self):
        """Symmetric with the effort card: '' is a gap in what was written
        down, not a way a response ends."""
        got = self.apply_filter("all")
        body = got["html"]["stop-reason-body"]
        self.assertIn(">end_turn<", body)
        self.assertIn(">max_tokens<", body)
        # The Claude corpus records one on every turn, so no absence is shown.
        self.assertNotIn("stop-tag unset", body)

    def test_codex_records_no_stop_reason_and_says_so(self):
        """Codex rollouts carry no stop-reason analogue at all, so every turn
        lands in the absence bucket — which must read as a gap in what was
        written down, not as evidence that nothing was ever cut short."""
        got = self.apply_filter("all", None, "codex")
        self.assertEqual([r["stop_reason"] for r in got["byStopReason"]], [""])
        self.assertIn("stop-tag unset", got["html"]["stop-reason-body"])


if __name__ == "__main__":
    unittest.main()
