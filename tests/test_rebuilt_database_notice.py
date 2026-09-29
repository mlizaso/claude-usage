"""What the dashboard says when the database under it holds nothing.

`db.init_db` drops every table when the schema in front of it is not one this
build wrote — an older install sharing `~/.claude/usage.db` is enough, and so is
the VS Code extension's bundled copy running beside a newer checkout. What it
leaves behind renders, today, as a complete and entirely plausible dashboard of
an empty history: HTTP 200, every section `[]`, no `error` field. The notice that
explains it goes to stderr, and the reader of this page is looking at a browser.

Two properties make that permanent rather than transient, and both were measured
before this file existed:

* auto-refresh is OFF by default (`REFRESH_DEFAULT = 0`), so the replacement
  scan's results are never fetched; and
* the page's only retry is armed inside `if (d.error)`, and the rebuild must not
  become an error — retrying cannot turn an empty database into a full one, and
  the error branch replaces the page rather than annotating it.

So the fact travels in the payload as `unscanned`, and the page renders it as a
banner over figures that are real (they genuinely are all zero) but are not the
reader's history.

**The flag asks the FILE, not the caller**, and that is the half most easily got
wrong. `init_db` returns true only to whoever performed the drop, and on the
common upgrade path that is not `/api/data`: the page asks `/api/sources` first,
`available_sources` enters `database_admission` too, and by the time the payload
is built the schema already matches. `TestTheRebuildIsReportedToWhoeverAsksNext` is that
sequence, in that order.

**What is asserted here stops at node's DOM stub, and one thing was checked by
hand instead.** The banner is built in JavaScript rather than declared in
`web/index.html`, so whether it survives the page's Content Security Policy is a
question the harness cannot answer — `style-src` is not enforced by a plain
object. Measured once, 2026-08-15, against a real `chrome-headless-shell` on a
real server over a database made foreign with `schema_meta`: `--dump-dom` showed
`<div id="db-notice" role="status" style="…">` as the first child of
`.container`, carrying the full sentence, with every declared property intact. No
browser test was added for it: a Chrome launch costs seconds, and `.container` is
already load-bearing for `clearLoading`.

**And `style-src` cannot break this banner, which is worth stating because the
first version of this paragraph claimed the opposite guard.** It said
`style-src 'self' 'unsafe-inline'` was pinned by the server-hardening suite; it
is not -- `grep -rn style-src tests/` finds only this file, and mutating the
page's policy to `style-src 'self'` leaves the whole suite green. That missing
guard would watch nothing anyway: the banner is styled through
`el.style.cssText`, a CSSOM write, and CSP does not gate those. Measured
2026-08-15 against chrome-headless-shell 152.0.7977.42, serving the same markup
under both policies: identical computed border, colour and padding.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import dashboard_data
from dashboard_data import available_sources, get_dashboard_data
from db import get_db, init_db

from tests.test_dashboard_js import emit, requires_node, run_js


def _seed(path, turns=3):
    """A database with real history in it, and a scan on record."""
    conn = get_db(path)
    try:
        init_db(conn, path)
        for i in range(turns):
            conn.execute(
                "INSERT INTO turns (session_id, timestamp, model, input_tokens, "
                "output_tokens, message_id, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("s1", f"2026-08-10T0{i}:00:00Z", "claude-opus-5",
                 1000, 100, f"msg_{i}", "claude"))
        conn.execute(
            "INSERT INTO sessions (session_id, project_name, first_timestamp, "
            "last_timestamp, model, turn_count, source) VALUES (?,?,?,?,?,?,?)",
            ("s1", "proj", "2026-08-10T00:00:00Z", "2026-08-10T02:00:00Z",
             "claude-opus-5", turns, "claude"))
        conn.execute(
            "INSERT INTO processed_files (path, mtime, lines) VALUES (?,?,?)",
            ("a" * 64, 1.0, 10))
        conn.commit()
    finally:
        conn.close()


def _make_foreign(path):
    """The realistic instance: every database from the migration era has it."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    finally:
        conn.close()


class _DatabaseFixture(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.path = Path(path)
        # The assembled-payload cache keys on (file identity, commit counter),
        # and these tests reuse one path across states. Start and end clean so a
        # neighbour's entry can never answer for this one.
        dashboard_data.reset_payload_cache()
        self.addCleanup(dashboard_data.reset_payload_cache)
        self.addCleanup(self._unlink)

    def _unlink(self):
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                candidate.unlink()

    def turn_count(self):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        finally:
            conn.close()


class TestTheRebuildIsReportedToWhoeverAsksNext(_DatabaseFixture):
    """The order the page actually uses: /api/sources, then /api/data.

    This is the sequence both judges reproduced. `available_sources` consumes
    the schema mismatch, so `/api/data` admission later yields False — a
    payload keyed on that flag would say nothing at all here.
    """

    def test_the_payload_says_the_database_holds_nothing(self):
        _seed(self.path)
        _make_foreign(self.path)
        self.assertEqual(self.turn_count(), 3, "the fixture never had history")

        with self.assertRaises(AssertionError):
            # Guard the guard: prove the rebuild is the thing being observed
            # rather than a fixture that was empty all along.
            self.assertEqual(self.turn_count(), 0)

        sources = available_sources(self.path)
        self.assertEqual(sources, [], "the rebuild did not happen at all")
        self.assertEqual(self.turn_count(), 0, "the rebuild kept rows")

        payload = get_dashboard_data(self.path)
        self.assertNotIn(
            "error", payload,
            "the rebuild must not arrive as an error: the page arms a "
            "three-second retry inside that branch and replaces the page, and "
            "retrying cannot refill a dropped database")
        self.assertTrue(
            payload["unscanned"],
            "/api/data served a complete, empty, plausible dashboard and said "
            "nothing about the database having just been dropped")

    def test_the_sections_really_are_empty_beside_that_flag(self):
        """What the banner claims about the figures next to it."""
        _seed(self.path)
        _make_foreign(self.path)
        available_sources(self.path)
        payload = get_dashboard_data(self.path)
        for key in ("all_models", "daily_by_model", "sessions_all",
                    "project_by_day_model", "top_dispatches"):
            with self.subTest(section=key):
                self.assertEqual(payload[key], [])


class TestTheFlagIsAPropertyOfTheFile(_DatabaseFixture):
    def test_a_database_with_history_says_nothing(self):
        _seed(self.path)
        payload = get_dashboard_data(self.path)
        self.assertFalse(payload["unscanned"])

    def test_a_first_install_gets_the_same_sentence(self):
        """A scan that has not finished yet and a database that was just
        dropped are the same state, and deserve the same words: nothing here
        has been read in, so this is not your history."""
        conn = get_db(self.path)
        try:
            init_db(conn, self.path)
        finally:
            conn.close()
        payload = get_dashboard_data(self.path)
        self.assertTrue(payload["unscanned"])

    def test_a_rebuild_this_process_never_saw_is_still_reported(self):
        """The cross-process shape: an older build sharing ~/.claude/usage.db.

        Nothing in this process called `init_db` on a foreign schema, so no
        return value anywhere records the drop. The file does.
        """
        _seed(self.path)
        _make_foreign(self.path)
        # Somebody else's process rebuilds it.
        conn = get_db(self.path)
        try:
            init_db(conn, self.path)
        finally:
            conn.close()
        self.assertEqual(self.turn_count(), 0)
        dashboard_data.reset_payload_cache()
        payload = get_dashboard_data(self.path)
        self.assertTrue(payload["unscanned"])

    def test_turns_without_a_file_record_is_not_nothing(self):
        """The `turns` half of the predicate, on its own.

        A database being refilled passes through this shape, and the banner's
        claim — that every figure beside it is zero — would be false over rows
        the page is drawing. Saying nothing is the conservative answer.
        """
        _seed(self.path)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("DELETE FROM processed_files")
            conn.commit()
        finally:
            conn.close()
        self.assertFalse(get_dashboard_data(self.path)["unscanned"])

    def test_a_file_record_without_turns_is_not_nothing_either(self):
        """The `processed_files` half, on its own.

        Transcripts that were read and held no usage are read transcripts. The
        banner would tell that reader to run a scan they have already run.
        """
        conn = get_db(self.path)
        try:
            init_db(conn, self.path)
            conn.execute(
                "INSERT INTO processed_files (path, mtime, lines) VALUES (?,?,?)",
                ("b" * 64, 1.0, 4))
            conn.commit()
        finally:
            conn.close()
        self.assertFalse(get_dashboard_data(self.path)["unscanned"])

    def test_history_in_the_other_assistant_is_not_nothing(self):
        """`unscanned` takes no `source` on purpose. A Claude reader on a
        Codex-only database sees zeros, but the database has been read and the
        remedy is the source switch, not a scan."""
        _seed(self.path)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("UPDATE turns SET source = 'codex'")
            conn.commit()
        finally:
            conn.close()
        payload = get_dashboard_data(self.path, source="claude")
        self.assertEqual(payload["daily_by_model"], [])
        self.assertFalse(
            payload["unscanned"],
            "a source with no rows is not a database with no rows")


@requires_node
class TestThePageRendersIt(unittest.TestCase):
    """The wiring, driven through the real `loadData`.

    A payload field nothing reads is the defect `tests/test_payload_surface.py`
    exists for; these run the shipped `web/js/*.js` and look at what reaches the
    DOM.
    """

    # Enough browser for the notice: `.container` accepts the insert and the
    # loading class, and `#db-notice` answers with whatever was inserted. Every
    # other element stays the harness's inert stub.
    _DOM = (
        "  let notice = null;\n"
        "  const container = {\n"
        "    insertBefore: (el) => { notice = el; },\n"
        "    classList: { add: () => {}, remove: () => {} },\n"
        "    setAttribute: () => {}, removeAttribute: () => {},\n"
        "  };\n"
        "  const stub = document.getElementById;\n"
        "  document.getElementById = (id) =>\n"
        "    (id === 'db-notice' ? notice : stub(id));\n"
        "  document.querySelector = (sel) =>\n"
        "    (sel === '.container' ? container : null);\n"
    )

    def _load(self, payload):
        # Concatenation rather than an f-string: the project supports 3.11,
        # where an f-string expression may not reuse the enclosing quote style.
        return run_js(
            "(async () => {\n"
            + self._DOM
            + "  globalThis.history = { replaceState: () => {} };\n"
            + "  const payload = " + json.dumps(payload) + ";\n"
            + "  apiFetch = async () => ({ ok: true, status: 200,\n"
            + "    json: async () => payload });\n"
            + "  await loadData('claude');\n"
            + "  console.log(JSON.stringify({\n"
            + "    id: notice && notice.id,\n"
            + "    role: notice ? 'created' : 'absent',\n"
            + "    text: notice ? String(notice.textContent) : null,\n"
            + "    hidden: notice ? !!notice.hidden : null,\n"
            + "  }));\n"
            + "})();")

    BASE = {
        "generated_at": "x", "all_models": [], "daily_by_model": [],
        "hourly_by_model": [], "sessions_all": [], "top_dispatches": [],
        "subagent_by_type": [], "project_by_day_model": [],
        "effort_by_day_model": [], "stop_reason_by_day_model": [],
        "limit_incidents": [], "codex_limit_history": [],
        "subscription_limits": {"available": False},
        "codex_limits": {"available": False},
    }

    def test_an_unscanned_database_gets_a_banner(self):
        got = self._load(dict(self.BASE, unscanned=True))
        self.assertEqual(got["role"], "created",
                         "nothing was inserted into the page")
        self.assertEqual(got["id"], "db-notice")
        self.assertFalse(got["hidden"])
        self.assertIn("not your usage history", got["text"])
        self.assertIn("Rescan", got["text"],
                      "the banner has to hand the reader the remedy")

    def test_a_database_with_history_gets_nothing(self):
        got = self._load(dict(self.BASE, unscanned=False))
        self.assertEqual(got["role"], "absent",
                         "the banner was built on a database that has history")

    def test_a_payload_without_the_field_gets_nothing(self):
        """An older server answering a newer page. Absent is not empty."""
        got = self._load(dict(self.BASE))
        self.assertEqual(got["role"], "absent")

    def test_the_banner_is_cleared_once_a_scan_has_run(self):
        """The self-correcting path: the Rescan button calls loadData again."""
        got = run_js(
            "(async () => {\n"
            + self._DOM
            + "  globalThis.history = { replaceState: () => {} };\n"
            + "  let unscanned = true;\n"
            + "  apiFetch = async () => ({ ok: true, status: 200,\n"
            + "    json: async () => Object.assign("
            + json.dumps(self.BASE) + ", { unscanned }) });\n"
            + "  await loadData('claude');\n"
            + "  const first = String(notice.textContent);\n"
            + "  unscanned = false;\n"
            + "  await loadData('claude');\n"
            + "  console.log(JSON.stringify({ first,\n"
            + "    then: String(notice.textContent), hidden: !!notice.hidden }));\n"
            + "})();")
        self.assertIn("not your usage history", got["first"])
        self.assertEqual(got["then"], "")
        self.assertTrue(got["hidden"])

    def test_an_abandoned_source_s_answer_does_not_clear_the_banner(self):
        """The banner call sits BELOW `wanted !== selectedSource` on purpose,
        and until 2026-08-16 nothing held it there.

        A comment said the position was deliberate; moving the call above that
        check left every JS suite green, because no test here ever drove a
        response whose source the reader had already left. This is that drive:
        the reader switches source while a Claude fetch is in flight, and
        between the two fetches another process rebuilds the file under them.
        The abandoned answer carries the stale `unscanned: false` and must not
        wipe the notice the current source's answer just raised -- an emptied
        database with no notice over it is the exact state the banner exists to
        report.

        Narrow on purpose: `unscanned` takes no `source`, so the two answers can
        only disagree when the file changed between them. That is the one case
        the banner is for.
        """
        got = run_js(
            "(async () => {\n"
            + self._DOM
            + "  globalThis.history = { replaceState: () => {} };\n"
            + "  let unscanned = true;\n"
            + "  apiFetch = async () => ({ ok: true, status: 200,\n"
            + "    json: async () => Object.assign("
            + json.dumps(self.BASE) + ", { unscanned }) });\n"
            + "  await loadData('claude');\n"
            + "  const raised = String(notice.textContent);\n"
            # The switch the reader made while the fetch was in flight. Setting
            # it before the call is the same state the await lands in, without
            # a deferred promise to sequence.
            + "  selectedSource = 'codex';\n"
            + "  unscanned = false;\n"
            + "  await loadData('claude');\n"
            + "  console.log(JSON.stringify({ raised,\n"
            + "    then: String(notice.textContent), hidden: !!notice.hidden }));\n"
            + "})();")
        self.assertIn("not your usage history", got["raised"],
                      "the fixture never raised the banner it is about to test")
        self.assertEqual(got["then"], got["raised"],
                         "a source nobody is reading cleared the notice")
        self.assertFalse(got["hidden"])


@requires_node
class TestTheNoticeSurvivesAPageWithoutOne(unittest.TestCase):
    """It is built from JS, so it must never assume a real DOM.

    `web/index.html` carries no `#db-notice` — it is absent in every ordinary
    state — and the JS suites replace `document` with a stub whose elements have
    no `insertBefore`. A renderer that assumed one would take those suites down
    with it, which is a worse outcome than the defect it fixes.
    """

    def test_it_is_inert_against_the_bare_stub(self):
        got = run_js(emit(
            "[renderDatabaseNotice(true), renderDatabaseNotice(false), 'ok']"))
        self.assertEqual(got[2], "ok")

    def test_the_creation_path_is_inert_too(self):
        """The branch the test above cannot reach.

        The bare stub answers `getElementById` with an element, so the notice
        is found rather than built. Forcing it absent takes the other branch:
        `querySelector` then hands back a stub with no `insertBefore`, which is
        exactly the shape a renderer that assumed a real DOM would die on.
        """
        got = run_js(
            "document.getElementById = () => null;\n"
            + emit("[renderDatabaseNotice(true), 'ok']"))
        self.assertEqual(got[1], "ok")

    def test_the_text_is_one_sentence_the_reader_can_act_on(self):
        got = run_js(emit("DB_NOTICE_TEXT"))
        self.assertIn("scan", got)
        self.assertNotIn("<", got, "the banner is set as textContent, not HTML")


if __name__ == "__main__":
    unittest.main()
