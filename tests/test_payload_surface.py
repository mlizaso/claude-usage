"""Every field /api/data ships must be read by something.

The payload is queried and serialised on every poll — the page re-fetches it
after each background rescan — so a field nobody reads is a table scan and a
block of JSON produced forever for no reader. Worse than the cost: the two that
had accumulated (`limit_history` and `codex_limit_history`) sat under a comment
promising a plot that was never written, so someone adding a quota chart could
not tell whether they were the intended feed or abandoned. Nothing failed when
both were deleted, and nothing would have failed had a third joined them.

Three rules. The first two are about **top-level** keys, deliberately at
different strengths, because one of them shipped green over a live instance of
its own subject. The third is about the keys inside a row, which the first two
cannot see at all — `for key in sorted(self.payload)` stops at the top — and
which is how `project_by_day_model[].sessions` was shipped on every row of
every poll with no reader while this file was green. That one is an inventory
rather than a matcher, for a reason measured below.

`TestEveryPayloadFieldHasAReader` is the **lower bound**: a key counts as
consumed if its name appears anywhere in the shipped page or the extension.
That over-accepts (a name that merely occurs in a comment passes) and never
under-accepts, which is the right direction for a guard whose failure mode
should be "you added dead surface", not "you accessed it in a way I did not
anticipate".

`TestEveryFieldIsReachedThroughARealAccess` is what that bound cost. The payload
also carried a `sources` array — a second `GROUP BY` over the whole `turns`
table, duplicating what `/api/sources` already serves live — and this file
passed it, on comments about switching sources, the URL `'/api/sources'`, the
word inside `"resources"`, a same-named local variable, and a `.sources` read of
the /api/sources *response*. The second rule therefore asks for an *access
shape* (`rawData.<key>`, `d.<key>`, `["<key>"]`) rather than a mention. A word
boundary would not have helped — most of those occurrences are the whole word.

That matcher is a lower bound too, and honestly so: it cannot tell a read of
*this* payload from a read of an identically named key on another object, which
is why the `sources` field was deleted rather than left behind a sharper
matcher, and why `THE_MATCHER_CANNOT_DECIDE` names the key it must not be asked
about at all. `TestTheAccessMatcherCanFail` pins what it does and does not
accept, so nobody quietly widens it back to a substring.

`TestTheRepeatedRowSurfaceIsDeclared` is the third rule, and it is not a third
matcher — see `NESTED_PAYLOAD_SURFACE` for the measurement that says a matcher
cannot carry it. It also needs a *populated* payload, because the fixture the
first two rules use is empty by design and a walk into empty lists visits
nothing: `PopulatedPayloadFixture` therefore asserts every section it inspects
actually has rows before it inspects anything.

The lists of allowed exceptions are named below with their reasons. Adding to
one is a deliberate act; letting a field rot into the payload is not.
"""

import ast
import inspect
import os
import re
import tempfile
import unittest
from pathlib import Path

import dashboard_data
import db
import rollups
from dashboard_data import get_dashboard_data

REPO_ROOT = Path(__file__).resolve().parent.parent

# Where a payload field can legitimately be read: the document the browser gets
# (assembled from these files at import) and the VS Code extension.
CONSUMER_FILES = (
    sorted((REPO_ROOT / "web").rglob("*.js"))
    + sorted((REPO_ROOT / "web").rglob("*.html"))
    + sorted((REPO_ROOT / "vscode-extension" / "src").rglob("*.ts"))
)

# Fields deliberately served without a consumer, each with the reason it stays.
SERVED_WITHOUT_A_CONSUMER = {
    "codex_limit_history":
        "The quota curve Claude's mutable cache cannot produce (invariant 7), "
        "announced in the README and asserted by tests/test_codex_transcripts.py. "
        "The chart is the missing half; the series is not.",
}

# Fields that ARE read, but through a shape `reads_key` cannot recognise —
# destructuring, a computed key, a receiver that is not a plain identifier.
# Empty today: every consumed key is read as `<identifier>.<key>`. Landing here
# is a deliberate act with a reason, never a way to make the stricter rule stop
# asking.
READ_IN_AN_UNANTICIPATED_SHAPE = {}

# Key names `reads_key` must never be asked about, because the page reads a
# same-named key off a *different* object — so a match would prove nothing
# about this payload. Enforced by requiring the payload not to carry them at
# all, which is a claim about /api/data rather than a ban on a shape the page
# is entitled to use.
THE_MATCHER_CANNOT_DECIDE = {
    "sources":
        "/api/sources serves it live and web/js/70-bootstrap.js reads that "
        "response under the same name. The matcher rejects that line only "
        "because its receiver is `(await resp.json())` rather than an "
        "identifier — luck, not design; see reads_key.",
}

# Repeated rows need field-level ownership checks as well as top-level payload
# checks. Session counts cannot be summed across day/model rows because a
# session can appear in several rows.
NESTED_PAYLOAD_SURFACE = {
    "codex_limit_history[]": (
        "day", "group", "observed", "percent", "resets_key"),
    "daily_by_model[]": (
        "cache_creation", "cache_creation_1h", "cache_read", "day", "input",
        "model", "output", "reasoning", "source", "turns", "cost",
        "cost_parts"),
    "effort_by_day_model[]": (
        "cache_creation", "cache_creation_1h", "cache_read", "day", "effort",
        "input", "model", "output", "reasoning", "source", "turns", "cost",
        "cost_parts"),
    "hourly_by_model[]": (
        "day", "hour", "local_day", "model", "output", "source", "turns"),
    "limit_incidents[]": (
        "blocked_min", "day", "notices", "projects", "reset_hint",
        "reset_zone", "started", "status"),
    "project_by_day_model[]": (
        "branch", "cache_creation", "cache_creation_1h", "cache_read", "day",
        "input", "model", "output", "project", "source", "turns", "cost",
        "cost_parts"),
    "sessions_all[]": (
        "branch", "by_day_model", "by_model", "cache_creation",
        "cache_creation_1h", "cache_read", "duration_min", "input", "last",
        "last_date", "model", "output", "project", "session_id", "source",
        "topic", "turns"),
    # The two views of one session's splits. AGENTS.md's warning is that a key
    # in one and not the other is a column that silently reads zero in the
    # browser, so they are declared apart rather than as one shape: `day` and
    # `branch` are the only differences, and both are meant to be. `branch` is
    # summed away in `by_model` for the same reason `day` is — that view is
    # these rows added up — and it is here because `Cost by Project & Branch`
    # counts its Sessions column off it (web/js/40-filters.js
    # `sessionFromParts` -> `branchSessions`); keyed on the session's single
    # label instead, a branch row rendered real money against zero sessions.
    "sessions_all[].by_day_model[]": (
        "branch", "cache_creation", "cache_creation_1h", "cache_read", "day",
        "input", "model", "output", "turns", "cost", "cost_parts"),
    "sessions_all[].by_model[]": (
        "cache_creation", "cache_creation_1h", "cache_read", "input", "model",
        "output", "turns", "cost", "cost_parts"),
    "stop_reason_by_day_model[]": (
        "day", "model", "output", "source", "stop_reason", "turns", "cost",
        "cost_parts"),
    "subagent_by_type[]": (
        "agent_type", "cache_creation", "cache_creation_1h", "cache_read",
        "day", "dispatches", "input", "model", "output", "source", "turns",
        "cost", "cost_parts"),
    "top_dispatches[]": (
        "agent_id", "agent_type", "by_day", "cache_creation",
        "cache_creation_1h", "cache_read", "duration_ms", "input", "model",
        "output", "source", "start", "start_date", "status", "tool_uses",
        "turns", "cost", "cost_parts"),
    # The dispatch's own per-local-day split, which the table's token and cost
    # columns are range-scoped from. No `model` key: there is one of these
    # arrays per (dispatch, source, model) row, so the model is the row's. It is
    # shipped only for a row that outlived a local day — see
    # TestTheDispatchDaySplitIsShippedOnlyWhereItSaysSomething — so this section
    # exists at all only because the fixture below seeds such a row.
    "top_dispatches[].by_day[]": (
        "cache_creation", "cache_creation_1h", "cache_read", "day", "input",
        "output", "turns", "cost", "cost_parts"),
}

# Row fields shipped with no reader, each with the reason it is still there —
# the row-level twin of SERVED_WITHOUT_A_CONSUMER, and held to the same rule:
# an entry that grows a reader has to go.
NESTED_SERVED_WITHOUT_A_CONSUMER = {
    "subagent_by_type[].dispatches":
        "COUNT(DISTINCT t.agent_id) per (day, type, source, model) row. The "
        "subagent accumulator in web/js/40-filters.js sums tokens and turns "
        "and never touches it, and it could not be summed if it wanted to — a "
        "dispatch spanning two models is counted in both rows. It stays because "
        "tests/test_rollup_grouping.py asserts on it as the observable of the "
        "GROUP BY defect that file pins, so deleting it is a two-file change; "
        "make it, and delete this entry with it.",
}


def reads_key(text, key):
    """Does `text` *access* `key`, rather than merely contain its name?

    Two shapes, both requiring an identifier receiver: `<identifier>.<key>`
    (including `?.`) and `<identifier>["<key>"]`. The first decides all
    thirteen consumed keys on its own — measured over CONSUMER_FILES, the
    bracket form matches none of them — and the bracket form is kept anyway
    because `payload["daily_by_model"]` is a property access by any reading,
    and a guard that red-lights a legitimate style teaches people to widen it.

    A third shape used to be accepted: the key as a bare quoted literal,
    `'<key>'`, on the theory that a loop over key names would need it. It is
    gone, because what it actually accepted was a *mention*. It matched four
    payload keys over CONSUMER_FILES and every one of those matches is the key
    name in backticks inside a JavaScript comment — for `all_models` the whole
    of it is web/js/50-render.js's "than `selectedModels`: `all_models` is
    every model this source has ever". Rename `rawData.all_models` away and
    that comment alone kept the strict rule green: the same vacuous pass, in
    the same file, that the `sources` field is here to commemorate.

    Cutting it exposed the receiver-less bracket form as the same hole one step
    quieter — `['<key>']` with nothing in front of it is an array literal, not
    an index — hence the receiver on both shapes, and hence a key genuinely
    consumed through a key-name loop belongs in
    READ_IN_AN_UNANTICIPATED_SHAPE with its reason rather than inside a regex.

    Deliberately NOT accepted: a receiver that is not an identifier, so
    `(await resp.json()).sources` does not count. That exclusion is what
    separates the /api/sources reader from a read of this payload, and it is
    luck rather than design — rewrite that line as `const b = await …; b.sources`
    and this matcher accepts it again. No name-based matcher can do better for a
    key name a sibling endpoint also uses, which is why deleting such a field
    beats sharpening this function, and why THE_MATCHER_CANNOT_DECIDE exists.
    """
    q = re.escape(key)
    return any(re.search(pattern, text) for pattern in (
        rf"[A-Za-z_$][\w$]*\s*\??\.\s*{q}\b",                  # rawData.key / d?.key
        # No `\s*` before the `[`: `of ['key']` would otherwise read as a
        # receiver and an index, and it is an array literal.
        rf"""[A-Za-z_$][\w$]*(?:\?\.)?\[\s*(?P<b>['"]){q}(?P=b)\s*\]""",
    ))


def _consumer_text():
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in CONSUMER_FILES)


def _nested_sections(payload):
    """Every repeated-row section of `payload`, keyed by its dotted path.

    A section is a list of dicts, wherever it sits: `daily_by_model` is one and
    so is `sessions_all[].by_model`, which is why this recurses into the rows it
    finds instead of walking exactly one level. The rows come back with it so
    the caller can check each one rather than their union — a key present in
    some rows and missing from others is a column that reads zero in the
    browser for the rest, and a union hides that.
    """
    found = {}

    def walk(node, path):
        if not isinstance(node, list):
            return
        rows = [r for r in node if isinstance(r, dict)]
        if not rows:
            return
        found.setdefault(path + "[]", []).extend(rows)
        for row in rows:
            for key, value in row.items():
                walk(value, f"{path}[].{key}")

    for key in sorted(payload):
        walk(payload[key], key)
    return found


class PayloadFixture(unittest.TestCase):
    """One real payload off an empty-but-migrated database.

    Empty is enough for the two top-level rules — they are about which keys
    exist, not what is in them, and building the payload from the real
    assembler means a key added tomorrow is covered without anyone remembering
    to list it here. It is *not* enough for the nested rule, because every list
    in this payload is `[]` and a walk into them visits nothing at all; see
    PopulatedPayloadFixture.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmpdir.cleanup)
        fd, path = tempfile.mkstemp(dir=cls._tmpdir.name, suffix=".db")
        os.close(fd)
        cls.path = Path(path)
        conn = db.get_db(cls.path)
        db.init_db(conn)
        conn.close()
        cls.payload = get_dashboard_data(cls.path)
        cls.consumer_text = _consumer_text()

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.path)


class TestEveryPayloadFieldHasAReader(PayloadFixture):
    def test_the_fixture_really_produced_a_payload(self):
        """Guard the guard: an error dict has one key and would pass vacuously."""
        self.assertNotIn("error", self.payload)
        self.assertGreater(len(self.payload), 10)
        self.assertTrue(CONSUMER_FILES, "no web or extension sources were found")

    def test_no_field_is_shipped_with_nothing_to_read_it(self):
        text = self.consumer_text
        for key in sorted(self.payload):
            if key in SERVED_WITHOUT_A_CONSUMER:
                continue
            with self.subTest(key=key):
                # assertTrue, not assertIn: the haystack is the whole page plus
                # the extension, and unittest prints it on failure.
                self.assertTrue(
                    key in text,
                    f"/api/data ships '{key}' and neither the page nor the "
                    f"extension mentions it. Wire it to a renderer, delete it, "
                    f"or add it to SERVED_WITHOUT_A_CONSUMER with the reason.",
                )

    def test_the_exceptions_are_still_exceptions(self):
        """A field that grew a consumer must leave the list, or the list stops
        meaning anything."""
        text = self.consumer_text
        for key, reason in SERVED_WITHOUT_A_CONSUMER.items():
            with self.subTest(key=key):
                self.assertIn(key, self.payload,
                              f"'{key}' is listed as a served exception but is "
                              f"not in the payload at all.")
                self.assertFalse(
                    key in text,
                    f"'{key}' now has a reader; remove it from "
                    f"SERVED_WITHOUT_A_CONSUMER.")
                self.assertTrue(reason.strip(), "an exception needs a reason")


class TestEveryFieldIsReachedThroughARealAccess(PayloadFixture):
    """The rule the substring bound could not enforce.

    A mention is not a reader. This asks the page to actually take the key off
    an object somewhere, which is what caught the `sources` field the looser
    rule shipped green.
    """

    def test_every_field_is_read_as_a_property_not_merely_mentioned(self):
        text = self.consumer_text
        for key in sorted(self.payload):
            if (key in SERVED_WITHOUT_A_CONSUMER
                    or key in READ_IN_AN_UNANTICIPATED_SHAPE):
                continue
            with self.subTest(key=key):
                self.assertTrue(
                    reads_key(text, key),
                    f"/api/data ships '{key}' and the page never reads it as a "
                    f"property — its name occurs only as a comment, a URL, or "
                    f"part of a longer identifier. Wire it to a renderer, "
                    f"delete it, or, if it really is read in a shape this "
                    f"matcher cannot see, add it to "
                    f"READ_IN_AN_UNANTICIPATED_SHAPE with the reason.",
                )

    def test_the_matcher_is_not_asked_about_a_name_a_sibling_endpoint_shares(self):
        """The rule above has a precondition, and this is it.

        For a key whose name another endpoint's response already uses, a green
        run proves nothing: the page's read of *that* object satisfies the
        matcher just as well. So those names are barred from the payload
        instead. tests/test_codex_transcripts.py bars `sources` too, for the
        separate reason that it duplicates a whole-table scan; both arguments
        have to be answered before the field comes back, which is the point.
        """
        for key, reason in THE_MATCHER_CANNOT_DECIDE.items():
            with self.subTest(key=key):
                self.assertNotIn(
                    key, self.payload,
                    f"/api/data ships '{key}', and the access rule above "
                    f"cannot tell a read of it from the reader this page "
                    f"already has for another object of the same name — so a "
                    f"green run here would prove nothing. {reason}")
                self.assertTrue(reason.strip(), "an exception needs a reason")

    def test_the_unanticipated_shapes_are_still_unanticipated(self):
        """Same rule as the other list: an entry the matcher can now see is a
        stale excuse, and must go."""
        for key, reason in READ_IN_AN_UNANTICIPATED_SHAPE.items():
            with self.subTest(key=key):
                self.assertIn(key, self.payload,
                              f"'{key}' is excused from the access rule but is "
                              f"not in the payload at all.")
                self.assertFalse(
                    reads_key(self.consumer_text, key),
                    f"'{key}' is now read in a shape the matcher recognises; "
                    f"remove it from READ_IN_AN_UNANTICIPATED_SHAPE.")
                self.assertTrue(reason.strip(), "an exception needs a reason")


class TestTheAccessMatcherCanFail(unittest.TestCase):
    """A guard that cannot fail is worse than no guard.

    MENTIONS_ONLY is the page as it stood when the `sources` field was
    deleted — every line verbatim from this repository, with the file it came
    from, so its provenance can be checked by hand. Each satisfies the
    substring rule, and the payload field they were between them accepting was
    dead. It is a frozen sample on purpose: re-collecting it from the live
    files each run is what made this class fail on refactors that had nothing
    to do with the payload. REAL_ACCESSES and the second half of NOT_YET_A_READ
    are written for the occasion; both are marked as such.

    If someone widens `reads_key` back towards a substring match, these report
    it — the eight mentions individually, and their concatenation, which is the
    control that says the tightened rule would have caught the dead field.
    """

    MENTIONS_ONLY = (
        # web/js/50-render.js
        "    // Priced sources show money; unpriced ones say so. A Codex plan is a",
        # web/js/70-bootstrap.js
        "  // Already-loaded sources are kept, so coming back is instant.",
        "    const resp = await apiFetch('/api/sources');",        # a URL
        # vscode-extension/src/sidebar.ts — the word inside "resources"
        '        ? [vscode.Uri.joinPath(this.extensionUri, "resources")]',
        # vscode-extension/src/extension.ts
        '    "Could not find the claude-usage sources bundled in this private extension.",',
        # web/js/70-bootstrap.js
        "  let sources = [];",                                     # same-named local
        "  sourceTurns = new Map(sources.map(s => [s.source, s.turns]));",
        "    if (resp.ok) sources = (await resp.json()).sources || [];",  # other object
    )

    REAL_ACCESSES = (                                             # written here
        "  rawData.sources.forEach(render);",
        "  const n = d.sources;",
        "  const n = d?.sources;",
        '  const n = payload["sources"];',
    )

    # Naming a key is not yet reading it. `reads_key` used to accept both of
    # these — the first is why the bare-quoted alternation was cut, and cutting
    # it exposed the second. Paired with their key because neither is about
    # `sources`; the first is a verbatim line of web/js/50-render.js, the
    # second is written here.
    NOT_YET_A_READ = (
        ("// than `selectedModels`: `all_models` is every model this source has ever",
         "all_models"),
        ("  for (const k of ['sources']) use(k);", "sources"),
    )

    def test_a_mention_is_not_an_access(self):
        for line in self.MENTIONS_ONLY:
            with self.subTest(line=line.strip()):
                self.assertIn("sources", line, "the line must satisfy the loose rule")
                self.assertFalse(reads_key(line, "sources"),
                                 "a mention was accepted as a reader")

    def test_naming_a_key_is_not_yet_reading_it(self):
        """The vacuous pass the old bare-quoted alternation bought, pinned.

        `all_models` is a live payload key. Rename `rawData.all_models` away
        and the only occurrence left in the page is the backticked one in the
        comment below — under the old matcher
        TestEveryFieldIsReachedThroughARealAccess stayed green on it, which is
        the exact failure `sources` is in this file to commemorate.

        This asserts the *opposite* of what test_a_real_access_is_accepted used
        to say about `for (const k of ['sources'])`: that shape is now
        deliberately rejected. A key really consumed through a key-name loop
        belongs in READ_IN_AN_UNANTICIPATED_SHAPE with its reason, so the
        excuse is visible rather than built into the regex.
        """
        for line, key in self.NOT_YET_A_READ:
            with self.subTest(line=line.strip()):
                self.assertIn(key, line, "the line must satisfy the loose rule")
                self.assertFalse(reads_key(line, key),
                                 "a name was accepted as a reader")
        # The counterpart, so the cut is a narrowing and not a blinding: the
        # real reader of that same key, from web/js/50-render.js, still counts.
        self.assertTrue(
            reads_key("  if (!((rawData && rawData.all_models) || []).length) {",
                      "all_models"),
            "the genuine reader of `all_models` stopped counting")

    def test_the_page_as_it_was_would_have_rejected_the_sources_field(self):
        """The finding itself, pinned against a frozen page rather than a
        moving one.

        This used to run over the live CONSUMER_FILES, which turned a claim
        about how the page looked when `sources` was deleted into a standing
        ban on any object anywhere in web/js or the extension ever having a
        `.sources` property. Rewriting web/js/70-bootstrap.js:243 as
        `const body = await resp.json(); sources = body.sources || [];` is a
        pure refactor of the /api/sources reader, nothing to do with this
        payload, and it turned this file red with a message pointing at the
        payload surface. What that live check was really protecting is kept,
        exactly targeted, by
        TestEveryFieldIsReachedThroughARealAccess.test_the_matcher_is_not_asked
        _about_a_name_a_sibling_endpoint_shares.

        Joined rather than checked line by line, which is why this is not a
        duplicate of test_a_mention_is_not_an_access: `\\s*` spans newlines, so
        a whole-text search can match an identifier ending one line against the
        key starting the next, and the per-line pass cannot see that.
        """
        page = "\n".join(self.MENTIONS_ONLY)
        self.assertIn("sources", page, "the loose rule still passes it")
        self.assertFalse(
            reads_key(page, "sources"),
            "the matcher accepted the page that shipped a dead `sources` "
            "field; if it can no longer tell a mention from a read, the "
            "payload surface rules above are decoration")

    def test_a_real_access_is_accepted(self):
        for line in self.REAL_ACCESSES:
            with self.subTest(line=line.strip()):
                self.assertTrue(reads_key(line, "sources"),
                                "a genuine read was rejected")

    def test_a_longer_key_containing_this_one_is_not_a_reader(self):
        self.assertFalse(reads_key("rawData.codex_limits_extra.x", "codex_limits"))
        self.assertTrue(reads_key("rawData.codex_limits.windows", "codex_limits"))


class PopulatedPayloadFixture(unittest.TestCase):
    """One real payload off a database with a row in every section.

    The empty fixture cannot carry the nested rule: with every list `[]` there
    are no rows to walk, so the walk finds nothing and agrees with any
    inventory at all — the `sources` failure again, one level down. Hence rows,
    and hence `test_the_fixture_really_filled_every_declared_section`, which is
    checked before anything is concluded from the walk.

    Seeded with SQL rather than by scanning a transcript because the shape of
    the payload is what is being pinned, not the parser: two sessions (one
    Claude spanning two local days and two models, so its `by_day_model` and
    `by_model` splits differ, one Codex), three subagent dispatches — only one
    has an `agents` row, only `acompact-2` fits inside a local day (so
    `top_dispatches[].by_day`, which is shipped sparsely, has both a populated
    and an empty case), and only `agent-2` spans two models, which is what
    reaches the model half of the key those splits are matched back on — a
    throttling notice and a Codex quota observation.
    """

    _TS1 = "2026-08-01T12:00:00Z"
    _TS2 = "2026-08-02T09:30:00Z"
    # 72 hours after _TS1, so `agent-1` spans two local days in EVERY timezone.
    # _TS1 and _TS2 are 21.5 hours apart and therefore land on one local day at
    # some offsets and two at others — fine for a session, whose split is
    # declared either way, but not for a section that exists only when a row
    # spans more than one day.
    _TS3 = "2026-08-04T12:00:00Z"

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmpdir.cleanup)
        fd, path = tempfile.mkstemp(dir=cls._tmpdir.name, suffix=".db")
        os.close(fd)
        cls.path = Path(path)
        conn = db.get_db(cls.path)
        db.init_db(conn)
        cls._seed(conn)
        conn.commit()
        conn.close()
        cls.payload = get_dashboard_data(cls.path)
        cls.sections = _nested_sections(cls.payload)
        cls.consumer_text = _consumer_text()

    @classmethod
    def _seed(cls, conn):
        conn.execute(
            "INSERT INTO sessions (session_id, project_name, first_timestamp, "
            "last_timestamp, git_branch, model, turn_count, topic, source) "
            "VALUES ('s1', 'user/proj', ?, ?, 'main', 'claude-opus-5', 5, "
            "'a topic', 'claude')", (cls._TS1, cls._TS3))
        conn.execute(
            "INSERT INTO sessions (session_id, project_name, first_timestamp, "
            "last_timestamp, git_branch, model, turn_count, topic, source) "
            "VALUES ('s2', 'user/other', ?, ?, 'main', 'gpt-5-codex', 1, "
            "'b topic', 'codex')", (cls._TS1, cls._TS1))
        turns = (
            # session, model,            source,   subagent, agent_id,      ts
            ("s1", "claude-opus-5",  "claude", 0, None,         cls._TS1),
            ("s1", "claude-haiku-4", "claude", 0, None,         cls._TS2),
            ("s1", "claude-opus-5",  "claude", 1, "agent-1",    cls._TS1),
            ("s1", "claude-opus-5",  "claude", 1, "acompact-2", cls._TS2),
            ("s2", "gpt-5-codex",    "codex",  0, None,         cls._TS1),
            # `agent-1` again, three days on: the dispatch that outlives a local
            # day, and the only reason `top_dispatches[].by_day[]` has any rows
            # to declare. `acompact-2` stays single-day, which is the other half.
            ("s1", "claude-opus-5",  "claude", 1, "agent-1",    cls._TS3),
            # `agent-2` is ONE dispatch under TWO models, and the only thing in
            # the suite that asks the model half of the key `by_day` rows are
            # matched back on a question it can get wrong. Its opus half
            # outlives a local day and its haiku half does not, so the split is
            # sparse per (dispatch, source, model) row rather than per dispatch
            # — see test_a_dispatch_under_two_models_splits_each_model_apart.
            ("s1", "claude-opus-5",  "claude", 1, "agent-2",    cls._TS1),
            ("s1", "claude-opus-5",  "claude", 1, "agent-2",    cls._TS3),
            ("s1", "claude-haiku-4", "claude", 1, "agent-2",    cls._TS1),
        )
        for i, (session, model, source, subagent, agent, ts) in enumerate(turns):
            conn.execute(
                "INSERT INTO turns (session_id, timestamp, model, input_tokens, "
                "output_tokens, cache_read_tokens, cache_creation_tokens, "
                "cache_creation_1h_tokens, reasoning_output_tokens, "
                "reasoning_effort, stop_reason, message_id, is_subagent, "
                "agent_id, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (session, ts, model, 100 + i, 200 + i, 300 + i, 40 + i, 10 + i,
                 5 + i, "high", "end_turn", f"m{i}", subagent, agent, source))
        # Only one of the three dispatches has a parent record, so
        # `duration_ms`, `tool_uses` and `status` are present-and-null on the
        # others — the row shape must not depend on that.
        conn.execute(
            "INSERT INTO agents (agent_id, agent_type, dispatched_in_session, "
            "status, total_duration_ms, tool_use_count) VALUES "
            "('agent-1', 'code-reviewer', 's1', 'completed', 1234, 7)")
        conn.execute(
            "INSERT INTO limit_events (event_uuid, kind, session_id, timestamp, "
            "status, message, reset_hint, reset_zone) VALUES "
            "('e1', 'rate_limit', 's1', ?, 429, 'slow down', '3pm', 'UTC')",
            (cls._TS1,))
        conn.execute(
            "INSERT INTO usage_limits_snapshots (kind, grp, scope, resets_key, "
            "percent, severity, is_active, resets_at, fetched_at_ms, "
            "observed_at) VALUES ('codex', 'weekly', 'plus', "
            "'2026-08-05T00:00', 42, 'ok', 1, '2026-08-05T00:00:00Z', 1, ?)",
            (cls._TS1,))

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.path)


class TestTheRepeatedRowSurfaceIsDeclared(PopulatedPayloadFixture):
    """The third rule: what a row carries is payload surface too.

    Read NESTED_PAYLOAD_SURFACE's comment first — it is an inventory rather
    than a matcher, and the measurement that forced that choice is there.
    """

    def test_the_fixture_really_filled_every_declared_section(self):
        """Guard the guard, and the reason this fixture exists at all.

        A section with no rows contributes no keys, so it would agree with any
        inventory whatsoever. Run this file against the empty fixture and every
        assertion below passes while covering nothing.
        """
        self.assertNotIn("error", self.payload)
        for path in sorted(NESTED_PAYLOAD_SURFACE):
            with self.subTest(section=path):
                self.assertTrue(
                    self.sections.get(path),
                    f"'{path}' is declared but the fixture produced no rows "
                    f"for it, so nothing below can see it. Seed it in "
                    f"PopulatedPayloadFixture._seed, or remove the section.")

    def test_every_row_carries_exactly_the_declared_keys(self):
        self.assertEqual(
            sorted(self.sections), sorted(NESTED_PAYLOAD_SURFACE),
            "/api/data's repeated-row sections are not the declared ones. A "
            "new one is row surface nothing has reviewed: declare its keys in "
            "NESTED_PAYLOAD_SURFACE, naming the reader of each in review, or "
            "delete it.")
        for path, rows in sorted(self.sections.items()):
            declared = tuple(sorted(NESTED_PAYLOAD_SURFACE[path]))
            for row in rows:
                with self.subTest(section=path):
                    self.assertEqual(
                        tuple(sorted(row)), declared,
                        f"a '{path}' row does not carry the declared keys. If "
                        f"you added one, say who reads it and declare it; if "
                        f"nothing reads it, delete it rather than shipping it "
                        f"on every row of every poll — that is how "
                        f"`project_by_day_model[].sessions` survived. If some "
                        f"rows carry it and others do not, the browser reads "
                        f"zero for the rest.")

    def test_a_row_field_the_matcher_calls_unread_is_declared_or_gone(self):
        """The lower bound, applied one level down.

        It decides almost nothing here (98 of 101 names accepted, see
        NESTED_PAYLOAD_SURFACE) — but a `False` is still worth having: it means
        the name occurs nowhere in the page or the extension at all, which no
        client-side bucket can explain away.

        Sections that are themselves excused are skipped: a section shipped
        with no consumer cannot have rows with one.
        """
        for path, keys in sorted(NESTED_PAYLOAD_SURFACE.items()):
            if path.split("[")[0] in SERVED_WITHOUT_A_CONSUMER:
                continue
            for key in keys:
                field = f"{path}.{key}"
                if field in NESTED_SERVED_WITHOUT_A_CONSUMER:
                    continue
                with self.subTest(field=field):
                    self.assertTrue(
                        reads_key(self.consumer_text, key),
                        f"/api/data ships '{field}' on every row and neither "
                        f"the page nor the extension reads that name off "
                        f"anything. Wire it up, delete it, or add it to "
                        f"NESTED_SERVED_WITHOUT_A_CONSUMER with the reason.")

    def test_the_nested_exceptions_are_still_exceptions(self):
        """Same rule as the top-level lists: an excuse that expired must go."""
        for field, reason in NESTED_SERVED_WITHOUT_A_CONSUMER.items():
            path, _, key = field.rpartition(".")
            with self.subTest(field=field):
                self.assertIn(path, NESTED_PAYLOAD_SURFACE,
                              f"'{field}' excuses a section that is not "
                              f"declared.")
                self.assertIn(key, NESTED_PAYLOAD_SURFACE[path],
                              f"'{field}' is listed as a served exception but "
                              f"the rows no longer carry it.")
                self.assertFalse(
                    reads_key(self.consumer_text, key),
                    f"'{field}' now has a reader; remove it from "
                    f"NESTED_SERVED_WITHOUT_A_CONSUMER.")
                self.assertTrue(reason.strip(), "an exception needs a reason")

    # web/js/54-tables.js as it stood when `project_by_day_model[].sessions`
    # was deleted, frozen rather than re-read from the live file. What is
    # pinned is that a client bucket named like a payload row field satisfies
    # the matcher — not that this particular line survives a refactor of the
    # project table, which is the standing ban that
    # test_the_page_as_it_was_would_have_rejected_the_sources_field was
    # rewritten to stop imposing.
    A_BUCKET_READ = '      <td class="num">${esc(NUM.format(p.sessions))}</td>'

    def test_the_matcher_could_not_have_caught_the_field_that_forced_this_rule(self):
        """Why this rule is an inventory, pinned on the field that proved it.

        `project_by_day_model` shipped a per-row `sessions` count nothing read.
        Ask `reads_key` about the name and it says yes — the line below prints
        `p.sessions` off the bucket applyFilter builds from the range-filtered
        session list, which is a different object entirely. A nested walk built
        on the matcher would have shipped that field green forever, which is
        why the walk above compares against a declaration instead.
        """
        self.assertTrue(
            reads_key(self.A_BUCKET_READ, "sessions"),
            "the matcher no longer accepts a bucket read that has nothing to "
            "do with the payload, so it can no longer show why the rule above "
            "is a declaration rather than a matcher")
        self.assertNotIn(
            "sessions", NESTED_PAYLOAD_SURFACE["project_by_day_model[]"],
            "the payload row carries `sessions` again. The page cannot use it "
            "— summed over the (project, branch, day, model) rows it gave 148 "
            "sessions where 68 existed — and the matcher above cannot see that")


class TestTheDispatchDaySplitIsShippedOnlyWhereItSaysSomething(
        PopulatedPayloadFixture):
    """`top_dispatches[].by_day` is sparse, and `[]` is a claim about the row.

    This lives here rather than beside the range-scoping tests in
    tests/test_frontend_data_path.py because the fixture above is the only one
    in the suite carrying a dispatch that outlives a local day BESIDE one that
    does not, which is what the rule needs to be visible at all.

    A row on one local day needs no nested split because start_date and its
    totals already describe that day. Multi-day rows must carry enough
    information for exact range filtering.

    The other direction is what makes `[]` load-bearing: web/js/40-filters.js
    reads it as "select this row on `start_date`", which is the right answer
    only because the server promises the row lived one day. Ship `[]` for a
    multi-day row and the defect this replaced comes straight back.
    """

    def _rows(self, agent_id):
        return [r for r in self.payload["top_dispatches"]
                if r["agent_id"] == agent_id]

    def test_the_fixture_really_has_one_of_each(self):
        """Guard the guard: with two single-day dispatches, or two multi-day
        ones, one half of the rule below is vacuous."""
        self.assertEqual(len(self._rows("agent-1")), 1)
        self.assertEqual(len(self._rows("acompact-2")), 1)
        self.assertEqual(self._rows("agent-1")[0]["turns"], 2)
        self.assertEqual(self._rows("acompact-2")[0]["turns"], 1)

    def test_a_dispatch_that_outlived_a_day_ships_its_split(self):
        row = self._rows("agent-1")[0]
        self.assertEqual(len(row["by_day"]), 2)
        self.assertEqual([b["day"] for b in row["by_day"]],
                         sorted(b["day"] for b in row["by_day"]))
        self.assertEqual(row["by_day"][0]["day"], row["start_date"])
        for column in ("input", "output", "cache_read", "cache_creation",
                       "cache_creation_1h", "turns"):
            with self.subTest(column=column):
                self.assertEqual(sum(b[column] for b in row["by_day"]),
                                 row[column],
                                 "the split must account for the whole row")

    def test_a_dispatch_that_lived_one_day_ships_none(self):
        row = self._rows("acompact-2")[0]
        self.assertEqual(
            row["by_day"], [],
            "a one-day split says nothing the row does not already say, and "
            "this array is the biggest thing in the payload")

    def _row(self, agent_id, model):
        """The one (dispatch, source, model) row for this pair."""
        rows = [r for r in self._rows(agent_id) if r["model"] == model]
        self.assertEqual(
            len(rows), 1,
            f"expected exactly one {agent_id}/{model} row, got {len(rows)}")
        return rows[0]

    def test_a_dispatch_under_two_models_splits_each_model_apart(self):
        """The model half of the key a split is matched back on.

        `top_dispatches` is one row per (dispatch, source, model) — that is the
        whole reason the GROUP BY carries `model`, since a bare column has
        SQLite return one arbitrary row's model for the group and the client
        then price every token in the dispatch at it, "5x out either way" in
        the query's own comment. `by_day` is a SECOND query over the same
        turns, joined back onto those rows on that same triple, and that key is
        the only thing tying a split to the row it describes.

        Measured before this case existed: narrowing that key to `(agent_id,)`
        alone left the whole suite green — 1553 tests, OK — while every
        per-model row silently received the other model's days as well. The
        suite's only multi-day dispatch used one model for both of its turns,
        so the model half of the key was never asked anything.

        `agent-2` asks it in both directions at once. Its opus half outlives a
        local day and must carry its OWN two days and nothing else; its haiku
        half lived one day and must still ship `[]` even though the dispatch
        around it spans three, because sparseness is a claim about the row, not
        about the dispatch — and web/js/40-filters.js reads `[]` as "select
        this row on `start_date`".
        """
        opus = self._row("agent-2", "claude-opus-5")
        haiku = self._row("agent-2", "claude-haiku-4")

        # Guard the guard: one model, or one day, and neither half below can
        # tell a per-model split from a per-dispatch one.
        self.assertNotEqual(opus["model"], haiku["model"])
        self.assertEqual(opus["turns"], 2)
        self.assertEqual(haiku["turns"], 1)

        self.assertEqual(
            len(opus["by_day"]), 2,
            "this model's turns fall on two local days; the split carries "
            "more, so it is the whole dispatch's rather than this row's")
        for column in ("input", "output", "cache_read", "cache_creation",
                       "cache_creation_1h", "turns"):
            with self.subTest(column=column):
                self.assertEqual(
                    sum(b[column] for b in opus["by_day"]), opus[column],
                    "the split must account for this row and nothing else; "
                    "matched on agent_id alone it sums the whole dispatch "
                    "into every one of its per-model rows")
        self.assertEqual(
            haiku["by_day"], [],
            "this row's turns all fall on one local day, so `start_date` is "
            "exact for it; a split here is the other model's days")


# ── Which module owns each payload section ────────────────────────────────────
# The fourth rule, and the only one here that is about the *code* rather than
# the bytes on the wire. AGENTS.md's module map said dashboard_data.py "does not
# contain queries" while, in the same bullet, giving it the Codex quota
# projection — which cannot be owned by a module that never reads
# `usage_limits_snapshots`. Six queries and a PRAGMA were sitting there when
# that was noticed, and nothing had turned red, because the claim was prose.
#
# The rule that is true, and that this section makes executable:
#
#   * rollups.py owns the USAGE sections — aggregates of what the scanner
#     recorded. Every one takes `(conn, source=None)`, because the page shows
#     one assistant at a time and that scoping happens in SQL.
#   * dashboard_data.py owns the QUOTA sections. None of them can take a
#     `source`, because each IS one assistant: `claude_limits` reads
#     ~/.claude.json, and the two Codex functions are `WHERE kind = 'codex'`.
#
# So "one function per payload section" describes rollups.py's ten, not all
# every key — and the `source` parameter, not the presence of SQL, is what
# decides which side a new section belongs on.
ROLLUP_SECTIONS = (
    "all_models", "daily_by_model", "effort_by_day_model", "hourly_by_model",
    "limit_incidents", "project_by_day_model", "sessions_all",
    "stop_reason_by_day_model", "subagent_by_type", "top_dispatches",
)

# Sections that stay in dashboard_data.py, each with the reason it cannot move.
QUOTA_SECTIONS = {
    "subscription_limits":
        "account.current_limits reads ~/.claude.json and takes `env`, so there "
        "is no `source` to take; moving it would put `account` and "
        "`os.environ` inside a module whose contract is 'hand me a "
        "connection', and make dashboard.py — which imports claude_limits by "
        "name for /api/limits — import the rollups.",
    "codex_limits":
        "`WHERE kind = 'codex'`: the projection IS Codex rather than being "
        "scoped to it, and it exists to come out in the shape "
        "account.limits_projection returns so one renderer serves both "
        "assistants.",
    "codex_limit_history":
        "The other half of that pair — same table, same `WHERE kind = "
        "'codex'`, driven directly beside it in "
        "tests/test_codex_transcripts.py. Splitting the two across modules "
        "would separate a feature that is read, rendered and tested as one.",
}

# Sections that describe the DATABASE FILE rather than the usage recorded in it.
# The third side, added because a key landed that neither of the two above could
# take honestly: `unscanned` runs a query, so it is not query-free; it is not one
# assistant, so it is not a quota surface; and it cannot be source-scoped —
# `processed_files` has no `source` column and "this file has read nothing" is one
# fact, not one per assistant. Declaring a fourth category is the point of the
# rule: a key has to say which side it is on, and inventing the side it needs is
# how it says something true rather than something that merely passes.
DATABASE_STATE_SECTIONS = {
    "unscanned":
        "True when `processed_files` and `turns` are both empty — the state a "
        "first install and a freshly REBUILT database share, since db.init_db "
        "drops every table when the schema is not one this build wrote. It is a "
        "property of the FILE rather than of init_db's return, which is true only "
        "for the caller that performed the drop; the page asks /api/sources "
        "first, so on the common upgrade path that caller is `available_sources` "
        "and /api/data would report nothing at all.",
}

# Keys produced without a query at all.
NOT_PRODUCED_BY_A_QUERY = {
    "generated_at": "datetime.now().strftime — the assembler's own stamp.",
}

# Names rollups.py must not reach for. Checked against the parsed module rather
# than its text: the docstring naming this boundary mentions `account` and
# `os.environ` in prose, and a substring scan would fail on the sentence
# explaining the rule.
FORBIDDEN_ROLLUP_IMPORTS = {"sqlite3", "db", "account", "os", "dashboard_data"}
FORBIDDEN_ROLLUP_CALLS = {"connect", "init_db", "secure_db_permissions",
                          "current_limits", "close"}


def _producer(node):
    """(module, function) for the expression that fills one payload key."""
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            return (func.value.id, func.attr)
        if isinstance(func, ast.Name):
            return ("dashboard_data", func.id)
    return (None, None)


def _payload_producers():
    """{payload key: (module, function)}, read out of `_collect_dashboard_data`.

    Static rather than dynamic, because what is being pinned is where the code
    *lives*: at runtime every section arrives as a plain list either way, so a
    payload cannot show which module produced it.

    A key bound to a local name is resolved back through its assignment, so
    `subscription_limits = claude_limits(conn, os.environ)` reports
    `claude_limits` rather than a variable.
    """
    tree = ast.parse(
        (REPO_ROOT / "claude_usage" / "dashboard_data.py").read_text(
            encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "_collect_dashboard_data")
    local = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    local[target.id] = node.value
    value = next(n for n in ast.walk(fn) if isinstance(n, ast.Return)).value
    # The whole dict goes out through _safe_dashboard_value(...).
    if isinstance(value, ast.Call) and value.args:
        value = value.args[0]
    if not isinstance(value, ast.Dict):
        return {}
    out = {}
    for key, val in zip(value.keys, value.values):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            continue
        if isinstance(val, ast.Name) and val.id in local:
            val = local[val.id]
        out[key.value] = _producer(val)
    return out


def _runs_sql(tree, name, seen=None):
    """Does module-level `name` execute SQL, directly or through a helper?

    One level is not enough: `claude_limits` runs no query itself — it calls
    `usage_since` and `_correct_window_start`, which do. Following the calls is
    what lets the rule below be "this section reads the database" rather than a
    count of `conn.execute` occurrences, which a cursor-shaped refactor would
    have moved for no change in meaning.
    """
    seen = seen if seen is not None else set()
    if name in seen:
        return False
    seen.add(name)
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    fn = functions.get(name)
    if fn is None:
        return False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "execute":
            return True
        if isinstance(func, ast.Name) and _runs_sql(tree, func.id, seen):
            return True
    return False


def _rollups_module_facts():
    """The module names rollups.py imports and the function names it calls."""
    tree = ast.parse((REPO_ROOT / "claude_usage" / "rollups.py").read_text(
        encoding="utf-8"))
    imported, called = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    return imported, called


class TestEveryPayloadSectionHasADeclaredOwner(PayloadFixture):
    """The documented split between rollups.py and dashboard_data.py, executable.

    Read ROLLUP_SECTIONS' comment above for the rule and the sentence that
    forced it. This inherits the empty fixture only for
    `test_the_static_read_agrees_with_the_live_payload`; every other assertion
    is about source, not data.
    """

    def test_the_static_read_agrees_with_the_live_payload(self):
        """Guard the guard: the AST walk must see the real return dict.

        Build the payload some other way — `payload.update(...)`, a
        comprehension, a second return — and the walk goes quietly stale,
        agreeing with any declaration at all. This is what notices.
        """
        producers = _payload_producers()
        self.assertGreaterEqual(
            len(producers), 10,
            "the walk found almost nothing in _collect_dashboard_data; it is "
            "no longer reading the payload dict")
        self.assertEqual(
            sorted(producers), sorted(self.payload),
            "the keys _collect_dashboard_data is written to return are not the "
            "keys /api/data actually ships, so nothing below is pinning the "
            "real payload")

    def test_every_section_is_declared_on_one_side_or_the_other(self):
        declared = (set(ROLLUP_SECTIONS) | set(QUOTA_SECTIONS)
                    | set(DATABASE_STATE_SECTIONS)
                    | set(NOT_PRODUCED_BY_A_QUERY))
        self.assertEqual(
            sorted(_payload_producers()), sorted(declared),
            "a payload section is not declared as a usage rollup, a quota "
            "surface, a fact about the database file, or query-free. Decide "
            "which it is: a source-scoped aggregate of recorded usage goes in "
            "rollups.py and takes `(conn, source=None)`; a surface that IS one "
            "assistant stays in dashboard_data.py. Then list it above with its "
            "reason.")

    def test_the_database_state_sections_stay_here_and_take_no_source(self):
        """The third side, held to the same two rules as the quota surfaces.

        It has to live where the connection is opened, and it must not be
        scoped: a fact about the file is one fact. A section here that grew a
        `source` is either a usage rollup wearing the wrong label or a
        parameter that is lying about what it does.
        """
        tree = ast.parse(
            (REPO_ROOT / "claude_usage" / "dashboard_data.py").read_text(
                encoding="utf-8"))
        producers = _payload_producers()
        for key, reason in sorted(DATABASE_STATE_SECTIONS.items()):
            with self.subTest(section=key):
                self.assertTrue(reason.strip(), "an exception needs a reason")
                module, func = producers.get(key, (None, None))
                self.assertEqual(
                    module, "dashboard_data",
                    f"'{key}' is declared a fact about the database file but is "
                    f"no longer produced in dashboard_data.py. {reason}")
                owner = getattr(dashboard_data, func, None)
                self.assertTrue(
                    inspect.isfunction(owner),
                    f"dashboard_data.{func} is not a function")
                self.assertNotIn(
                    "source", inspect.signature(owner).parameters,
                    f"dashboard_data.{func} grew a `source` parameter, so it is "
                    f"no longer a single fact about the file.")
                self.assertTrue(
                    _runs_sql(tree, func),
                    f"'{key}' is declared a fact read out of the database, but "
                    f"dashboard_data.{func} no longer runs SQL. If it is now a "
                    f"constant, it is not a section.")

    def test_the_usage_sections_are_produced_by_rollups(self):
        producers = _payload_producers()
        for key in ROLLUP_SECTIONS:
            with self.subTest(section=key):
                module, func = producers.get(key, (None, None))
                self.assertEqual(
                    (module, func), ("rollups", key),
                    f"'{key}' is declared a usage rollup, so "
                    f"_collect_dashboard_data must fill it with "
                    f"rollups.{key}(...) — one function per payload section, "
                    f"named after the section.")
                self.assertTrue(
                    inspect.isfunction(getattr(rollups, key, None)),
                    f"rollups.{key} is not a function")

    def test_every_usage_rollup_is_source_scoped(self):
        """The discriminator itself. A rollup that cannot be scoped is not one.

        The page shows one assistant at a time and asks for it in SQL — a
        rollup that dropped the parameter would return the other assistant's
        rows into a payload the client no longer filters.
        """
        for key in ROLLUP_SECTIONS:
            with self.subTest(section=key):
                params = inspect.signature(getattr(rollups, key)).parameters
                self.assertEqual(
                    list(params), ["conn", "source"],
                    f"rollups.{key} must take (conn, source=None)")
                self.assertIsNone(
                    params["source"].default,
                    f"rollups.{key}'s `source` must default to None — that "
                    f"default is what 'everything' means for the CLI and any "
                    f"older client")

    def test_the_quota_sections_stay_here_and_cannot_be_source_scoped(self):
        """The other half, and why moving them would be a downgrade.

        Each of these IS one assistant rather than being scoped to one, so
        there is no `source` for rollups.py's contract to bind. Asserting the
        absence of the parameter is what keeps this a structural rule instead
        of a list of names someone can extend by hand.
        """
        producers = _payload_producers()
        for key, reason in sorted(QUOTA_SECTIONS.items()):
            with self.subTest(section=key):
                self.assertTrue(reason.strip(), "an exception needs a reason")
                module, func = producers.get(key, (None, None))
                self.assertEqual(
                    module, "dashboard_data",
                    f"'{key}' is declared a quota surface but is no longer "
                    f"produced in dashboard_data.py. {reason}")
                owner = getattr(dashboard_data, func, None)
                self.assertTrue(
                    inspect.isfunction(owner),
                    f"dashboard_data.{func} is not a function")
                self.assertNotIn(
                    "source", inspect.signature(owner).parameters,
                    f"dashboard_data.{func} grew a `source` parameter. If it "
                    f"is genuinely source-scoped it is a usage rollup and "
                    f"belongs in rollups.py; if it is not, the parameter is "
                    f"lying about what it does.")

    def test_the_quota_sections_really_do_query(self):
        """The claim the corrected sentence rests on, stated as a fact to check.

        'dashboard_data.py does not contain queries' was false precisely
        because these three read the database — which is also why the same
        bullet's 'owns the Codex quota projection' could not be true beside it.
        Should they ever stop querying, the boundary this class pins is
        describing something that no longer exists, and the docstrings in both
        modules need re-reading rather than trusting.

        Deliberately not a count of `conn.execute` occurrences: rewriting one
        as `conn.cursor().execute` moves that number without changing a thing
        about who owns what.
        """
        tree = ast.parse(
            (REPO_ROOT / "claude_usage" / "dashboard_data.py").read_text(
                encoding="utf-8"))
        producers = _payload_producers()
        for key in sorted(QUOTA_SECTIONS):
            with self.subTest(section=key):
                _, func = producers.get(key, (None, None))
                self.assertTrue(
                    _runs_sql(tree, func),
                    f"'{key}' is declared a quota surface that dashboard_data.py "
                    f"keeps because it reads the database, but "
                    f"dashboard_data.{func} no longer runs SQL. If nothing here "
                    f"queries any more, this whole boundary needs re-deciding.")

    def test_rollups_opens_no_connection_and_reads_no_config(self):
        """rollups.py's own stated contract, pinned.

        'Nothing here opens, closes or migrates a database' and 'nothing here
        reads the quota tables' are the two halves of why the quota surfaces
        cannot simply be dragged across. Checked against the parsed module
        rather than its text, because the docstring that states the rule names
        `account` and `os.environ` in prose.
        """
        imported, called = _rollups_module_facts()
        self.assertEqual(
            sorted(imported & FORBIDDEN_ROLLUP_IMPORTS), [],
            "rollups.py imports a module its contract excludes. It takes an "
            "open connection and returns a list; opening, migrating, or "
            "reading ~/.claude.json is dashboard_data.py's job.")
        self.assertEqual(
            sorted(called & FORBIDDEN_ROLLUP_CALLS), [],
            "rollups.py opens, closes or migrates a database, or reads the "
            "account config. Those stay in dashboard_data.py.")


# ── Which payload fields survive a cache hit ──────────────────────────────────
# The fifth rule, and the one that keeps `dashboard_data`'s payload cache honest.
#
# A repeat /api/data against an unchanged database is answered from the last
# build. That is only sound for fields that are a function of the STORED BYTES.
# Three are not: two date themselves against `datetime.now()` and one of those
# also reads ~/.claude.json, a cache Claude Code updates independently of this
# database (invariant 7). Serving those from a cache
# entry would freeze the plan panel and make `generated_at` claim a snapshot that
# was never taken — for as long as the user does no work, which is exactly when
# the database stops changing.
#
# `dashboard_data.LIVE_PAYLOAD_FIELDS` is the list the code rebuilds. This is the
# other half of it: every remaining key, named, so that a field added tomorrow
# cannot join the cached set by default. Getting the classification wrong is
# silent in both directions — a live field left cached freezes, a cached field
# marked live is merely rebuilt for nothing — so the failure this pins is the
# first one.
DATABASE_ONLY_PAYLOAD_FIELDS = (
    "all_models", "codex_limit_history", "daily_by_model", "effort_by_day_model",
    "hourly_by_model", "limit_incidents", "project_by_day_model", "sessions_all",
    "stop_reason_by_day_model", "subagent_by_type", "top_dispatches",
    # `unscanned` belongs on this side by the rule's own test: it is two EXISTS
    # over stored tables and reads no clock. A hit means nothing has been
    # committed since the build, so an emptied database is still empty and a
    # filled one is still filled — and a rebuild moves `data_version` between
    # `before` and `after`, which drops the build rather than storing it.
    "unscanned",
)


class TestTheCacheClassifiesEveryPayloadField(PayloadFixture):
    """Read DATABASE_ONLY_PAYLOAD_FIELDS' comment above before changing these."""

    def test_every_field_is_declared_live_or_database_only(self):
        self.assertEqual(
            sorted(self.payload),
            sorted(set(DATABASE_ONLY_PAYLOAD_FIELDS)
                   | set(dashboard_data.LIVE_PAYLOAD_FIELDS)),
            "a payload field is neither declared a function of the stored bytes "
            "nor listed in dashboard_data.LIVE_PAYLOAD_FIELDS. Decide which it "
            "is: anything dated against the clock, or read from outside this "
            "database, must be rebuilt on every request or it freezes for as "
            "long as nothing is committed.")

    def test_the_two_sets_do_not_overlap(self):
        """Guard the guard: a field in both would satisfy the union vacuously."""
        self.assertEqual(
            sorted(set(DATABASE_ONLY_PAYLOAD_FIELDS)
                   & set(dashboard_data.LIVE_PAYLOAD_FIELDS)), [])

    def test_the_live_fields_are_the_ones_that_consult_the_clock(self):
        """Structural, not a list check: each live field's producer must reach
        `datetime.now` or the account config, directly or through a helper.

        A field can only need rebuilding because something outside the stored
        bytes decides its value, and in this module that is one of those two.
        Left as a name list, `LIVE_PAYLOAD_FIELDS` would go stale the moment a
        quota surface stopped dating itself — and nothing would notice, because
        rebuilding a pure field is invisible.
        """
        tree = ast.parse(
            (REPO_ROOT / "claude_usage" / "dashboard_data.py").read_text(
                encoding="utf-8"))
        producers = _payload_producers()
        for key in dashboard_data.LIVE_PAYLOAD_FIELDS:
            with self.subTest(field=key):
                self.assertIn(key, self.payload)
                _, func = producers.get(key, (None, None))
                if func is None:
                    # `generated_at` is `datetime.now().strftime(...)` inline —
                    # no named producer to walk, and the clock is right there.
                    self.assertIn(key, NOT_PRODUCED_BY_A_QUERY)
                    continue
                self.assertTrue(
                    _reads_the_clock_or_the_config(tree, func),
                    f"'{key}' is declared live, but dashboard_data.{func} no "
                    f"longer consults `datetime.now` or the account config. If "
                    f"it is now a pure function of the database, move it to "
                    f"DATABASE_ONLY_PAYLOAD_FIELDS so a cache hit can serve it.")


def _reads_the_clock_or_the_config(tree, name, seen=None):
    """Does module-level `name` reach the clock or ~/.claude.json?

    Follows calls the way `_runs_sql` does, and for the same reason:
    `claude_limits` consults neither itself — it calls `account.current_limits`
    and `_correct_window_start`, and those do.
    """
    seen = seen if seen is not None else set()
    if name in seen:
        return False
    seen.add(name)
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    fn = functions.get(name)
    if fn is None:
        return False
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and node.attr in ("now", "current_limits"):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if _reads_the_clock_or_the_config(tree, node.func.id, seen):
                return True
    return False


if __name__ == "__main__":
    unittest.main()
