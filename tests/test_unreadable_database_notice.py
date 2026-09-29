"""A database the server cannot read must stop the page retrying.

The failure this pins was live and looked exactly like a hang. Against a real
`~/.claude/usage.db` whose page 1 had been overwritten -- SQLite answering
`file is not a database` -- `GET /api/sources` and `GET /api/data` both returned
`500 {"error": "Failed to read the usage database"}`, the page fell through to
the single-source path, rendered its whole chrome over twelve empty cards, and
re-armed a three-second retry that could never succeed. It ran that way for
hours. The terminal beside it had already printed the diagnosis and the remedy:
`cli.cmd_dashboard` says "every data request will fail the same way until that
is fixed", and the page it was describing had no way to know it.

The rule these tests hold down is the one the 403 branch already follows: a
refusal that cannot clear itself gets a screen that explains it, and a refusal
that can gets a retry. The split is `db.a_retry_could_succeed`.
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard
from dashboard import (API_TOKEN, API_TOKEN_HEADER, DashboardHandler,
                       DashboardHTTPServer)
from db import ForeignDatabaseError, a_retry_could_succeed
from tests.test_dashboard_js import requires_node, run_js


class TestWhichRefusalsAReadRetryCanOutlast(unittest.TestCase):
    """`a_retry_could_succeed` decides whether the page keeps asking."""

    def test_a_lock_and_a_rebuild_window_are_worth_retrying(self):
        for exc in (sqlite3.OperationalError("database is locked"),
                    sqlite3.OperationalError("database table is locked"),
                    sqlite3.OperationalError("no such table: turns")):
            with self.subTest(exc=str(exc)):
                self.assertTrue(a_retry_could_succeed(exc))

    def test_nothing_else_is(self):
        """The four that fail identically forever, and the two shapes of damage.

        `file is not a database` is a destroyed page 1; `database disk image is
        malformed` is the commoner shape, where page 1 survives and a later read
        walks into the damage. Both were live on one machine at once.
        """
        for exc in (sqlite3.DatabaseError("file is not a database"),
                    sqlite3.DatabaseError("database disk image is malformed"),
                    ForeignDatabaseError("not ours"),
                    sqlite3.OperationalError("attempt to write a readonly database"),
                    sqlite3.OperationalError("unable to open database file"),
                    RuntimeError("refused: the path is a symbolic link")):
            with self.subTest(exc=str(exc)):
                self.assertFalse(a_retry_could_succeed(exc))

    def test_it_is_not_the_operational_error_class(self):
        """The class is a superset of the sentence, which is the trap `cli`
        already documents: two members of that family never clear."""
        readonly = sqlite3.OperationalError("attempt to write a readonly database")
        self.assertIsInstance(readonly, sqlite3.OperationalError)
        self.assertFalse(a_retry_could_succeed(readonly))


class _ServedOverARealSocket(unittest.TestCase):
    """A real server, a real socket and a real token, against a real bad file."""

    PAYLOAD = b"this is not a database"

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._tmp.name) / "usage.db"
        cls.db_path.write_bytes(cls.PAYLOAD)
        cls._orig = dashboard.DB_PATH
        dashboard.DB_PATH = cls.db_path
        cls.server = DashboardHTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        # shutdown() stops serve_forever; server_close() releases the LISTENING
        # socket. Without the second the port stays bound for the rest of the
        # run, still accepting connections that nothing will ever answer, and
        # the suite prints a ResourceWarning for it. `unittest discover` runs
        # every module in one process, so a leak here is a leak for all of it.
        cls.server.shutdown()
        cls.server.server_close()
        dashboard.DB_PATH = cls._orig
        cls._tmp.cleanup()

    def get(self, path):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={API_TOKEN_HEADER: API_TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class TestAnUnreadableFileIsAnsweredAsPermanent(_ServedOverARealSocket):

    def test_both_data_endpoints_say_so(self):
        """Both, because the page asks /api/sources FIRST. Marking only
        /api/data would draw the entire chrome before saying anything."""
        for path in ("/api/sources", "/api/data"):
            with self.subTest(path=path):
                status, body = self.get(path)
                self.assertEqual(500, status)
                self.assertIs(True, body.get("permanent"))

    def test_the_body_still_names_no_path_and_no_sqlite_text(self):
        """Everything in this body is written into the document. The precise
        diagnosis carries a filesystem path and stays in the terminal."""
        _, body = self.get("/api/data")
        self.assertEqual("Failed to read the usage database", body["error"])
        self.assertNotIn(str(self.db_path), json.dumps(body))
        self.assertNotIn("not a database", json.dumps(body))

    def test_it_answers_the_same_way_again(self):
        """The whole point: the second request is not better than the first,
        which is what makes a retry loop a lie rather than a delay."""
        first, second = self.get("/api/data"), self.get("/api/data")
        self.assertEqual(first, second)


class TestATransientFailureAtTheEndpointIsNotMarkedPermanent(_ServedOverARealSocket):
    """The symmetric half, and the half nothing covered.

    Every other test here drives the endpoint with a file that is permanently
    unreadable, so all of them would still pass if `_database_error_body`
    returned `permanent` unconditionally -- which is the version of this change
    that breaks the fresh-install retry. This one raises a LOCK from inside
    `get_dashboard_data` and demands the flag stay off.
    """

    def test_a_lock_answers_500_without_the_flag(self):
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(dashboard, "get_dashboard_data", side_effect=locked):
            status, body = self.get("/api/data")
        self.assertEqual(500, status)
        self.assertEqual("Failed to read the usage database", body["error"])
        self.assertNotIn("permanent", body)

    def test_a_rebuild_window_answers_500_without_the_flag(self):
        """Another process between the DROP and the CREATE (invariant 6). The
        page must keep asking: the rebuild finishes and the next request is
        answered normally."""
        rebuilding = sqlite3.OperationalError("no such table: turns")
        with mock.patch.object(dashboard, "get_dashboard_data", side_effect=rebuilding):
            status, body = self.get("/api/data")
        self.assertEqual(500, status)
        self.assertNotIn("permanent", body)


@unittest.skipUnless(os.name == "posix",
                     "creating a symlink needs elevation on Windows")
class TestARefusedPathIsPermanentNotMissing(unittest.TestCase):
    """A dangling symlink at the database path used to retry forever.

    `Path.exists()` FOLLOWS a symlink, so a dangling one answered False and
    `_collect_dashboard_data` returned "Database not found. Run: python cli.py
    scan" -- at HTTP 200, with no `permanent`, so the page retried it every
    three seconds for the life of the tab. The command it named exited 1
    without creating anything, because `db.get_db` refuses the same path. The
    reader got advice that could not be followed, on a loop that could not end.

    `secure_db_permissions` is the only thing that inspects the path itself, so
    it now runs BEFORE the exists() guard in both entry points -- the ordering
    `cli.require_db` already used. Reproduced before the fix and after.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.link = Path(self._tmp.name) / "usage.db"
        self.link.symlink_to(Path(self._tmp.name) / "nowhere-at-all.db")
        self._orig = dashboard.DB_PATH
        dashboard.DB_PATH = self.link
        self.server = DashboardHTTPServer(("127.0.0.1", 0), DashboardHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        dashboard.DB_PATH = self._orig
        self._tmp.cleanup()

    def get(self, path):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={API_TOKEN_HEADER: API_TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_the_dangling_link_is_not_reported_as_a_missing_database(self):
        for path in ("/api/data", "/api/sources"):
            with self.subTest(path=path):
                status, body = self.get(path)
                self.assertEqual(500, status)
                self.assertIs(True, body.get("permanent"))
                self.assertNotIn("Database not found", body.get("error", ""))

    def test_the_refusal_never_names_the_path_it_refused(self):
        """db's refusal message carries the path; the body must not."""
        _, body = self.get("/api/data")
        self.assertNotIn(str(self.link), json.dumps(body))


class TestAFreshInstallIsStillRetried(unittest.TestCase):
    """The case the retry was written for must keep it.

    The server binds and serves before the first scan finishes, so a database
    that does not exist yet is answered with an error and no `permanent` --
    absent rather than false, so the flag reads as an assertion someone made.
    """

    def test_a_missing_database_is_not_permanent(self):
        from dashboard_data import get_dashboard_data
        with tempfile.TemporaryDirectory() as tmp:
            body = get_dashboard_data(Path(tmp) / "absent.db")
            self.assertIn("error", body)
            self.assertEqual(body.get("recovery"), "scan")
            self.assertNotIn("permanent", body)


_BRANCH_PROBE = r"""
const els = {};
const handlers = {};
function trackedEl(id) {
  const el = stubEl();
  el.addEventListener = (ev, fn) => { handlers[id + ':' + ev] = fn; };
  // web/index.html declares <div id="auth-notice" class="source-chooser" hidden>,
  // so the stub starts it hidden too. stubEl()'s own default is false, which
  // makes an UNTOUCHED element indistinguishable from a revealed one -- and
  // dropping `notice.hidden = false` then passes. Measured: with the plain
  // default that mutation survived; with this line it reds.
  if (id === 'auth-notice') el.hidden = true;
  return el;
}
document.getElementById = (id) => (els[id] || (els[id] = trackedEl(id)));
// .container is reached through querySelector, and clearLoading is the only
// thing that touches it -- observing it is how a dropped clearLoading() shows.
const container = stubEl();
let containerClassRemoved = [];
container.classList = { add: noop, remove: (c) => containerClassRemoved.push(c),
                        toggle: noop, contains: () => false };
document.querySelector = (sel) => (sel === '.container' ? container : stubEl());
let armed = [];
globalThis.setTimeout = (fn, ms) => { armed.push(ms); return 0; };
window.setTimeout = globalThis.setTimeout;
// Intervals are cleared, not re-armed, so the only way to see the difference
// between `clearInterval(t)` and a bare `t = null` is to record the call.
let cleared = [];
globalThis.clearInterval = (t) => { cleared.push(t); };
window.clearInterval = globalThis.clearInterval;
globalThis.setInterval = () => 4242;
window.setInterval = globalThis.setInterval;

function reset() {
  for (const k of Object.keys(els)) delete els[k];
  for (const k of Object.keys(handlers)) delete handlers[k];
  armed = []; cleared = []; containerClassRemoved = [];
  autoRefreshTimer = 101; planPollTimer = 202;   // both live, so both must be cleared
}
function snap(label) {
  const n = els['auth-notice'] || stubEl();
  return { label, armed: armed.slice(),
           // Asserted on CONTENT, not on `hidden`: stubEl() defaults hidden to
           // false, so an untouched element reports exactly what a shown one
           // does and the flag discriminates nothing.
           noticeHasTitle: /cannot be read/.test(n.innerHTML || ''),
           noticeHasRemedy: /cli\.py scan/.test(n.innerHTML || ''),
           noticeNamesAPath: /~\/|usage\.db|\/tmp/.test(n.innerHTML || ''),
           meta: (els['meta'] || stubEl()).textContent,
           rescanDisabled: (els['rescan-btn'] || stubEl()).disabled,
           noticeShown: n.hidden === false && !!(n.innerHTML || ''),
           clearedTimers: cleared.slice().sort(),
           loadingCleared: containerClassRemoved.includes('loading'),
           reloadWired: typeof handlers['db-retry:click'] === 'function' };
}

(async () => {
  const out = [];
  selectedSource = 'claude';

  reset(); rawData = null;
  apiFetch = async () => ({ status: 500, ok: false, json: async () =>
    ({ error: 'Failed to read the usage database', permanent: true }) });
  await loadData('claude');
  out.push(snap('permanent'));

  reset(); rawData = null;
  apiFetch = async () => ({ status: 200, ok: true, json: async () =>
    ({ error: 'Failed to read the usage database' }) });
  await loadData('claude');
  out.push(snap('transient'));

  console.log(JSON.stringify(out));
})();
"""


_START_PROBE = r"""
const els = {};
const handlers = {};
document.getElementById = (id) => (els[id] || (els[id] = stubEl()));
const asked = [];
apiFetch = async (path) => {
  asked.push(path);
  if (path === '/api/scan-status') {
    return { status: 200, ok: true,
      json: async () => ({ state: 'idle', generation: 0 }) };
  }
  if (path === '/api/sources') {
    return { status: 500, ok: false,
             json: async () => ({ error: 'Failed to read the usage database',
                                  permanent: true }) };
  }
  return { status: 200, ok: true, json: async () => ({}) };
};
(async () => {
  await start();
  const n = els['auth-notice'] || stubEl();
  console.log(JSON.stringify({
    asked,
    dataWasFetched: asked.some(p => p.startsWith('/api/data')),
    noticeShown: /cannot be read/.test(n.innerHTML || ''),
  }));
})();
"""


@requires_node
class TestTheSourcesCallAnswersItWithoutDrawingTheChrome(unittest.TestCase):
    """/api/sources is the first database request the page makes.

    The database-free scan-status check now precedes it. Letting a permanent
    sources failure fall through to the single-source path still draws a filter
    bar over twelve empty cards and only then says anything -- and the reviewers
    measured that reverting this half entirely left 604 JS-facing tests green,
    because nothing exercised `start()` at all.
    """

    @classmethod
    def setUpClass(cls):
        cls.result = run_js(_START_PROBE)

    def test_it_never_asks_for_data_it_cannot_get(self):
        self.assertFalse(self.result["dataWasFetched"],
                         f"start() went on to fetch: {self.result['asked']}")

    def test_it_shows_the_notice_straight_away(self):
        self.assertTrue(self.result["noticeShown"])


@requires_node
class TestThePageStopsRetryingOnlyOnPermanent(unittest.TestCase):
    """The real loadData, run under node, driven down both branches.

    These replaced source-text assertions, which is the point worth recording:
    a test that greps the shipped file for `if (d.permanent)` passes whether or
    not the branch does anything, and would have passed against a version that
    armed the timer anyway. Running the function and looking at what it did to
    the timer is the only form of this test that distinguishes them.
    """

    @classmethod
    def setUpClass(cls):
        cls.result = {r["label"]: r for r in run_js(_BRANCH_PROBE)}

    def test_a_permanent_failure_arms_no_retry(self):
        """The whole defect in one assertion."""
        self.assertEqual([], self.result["permanent"]["armed"])

    def test_a_permanent_failure_explains_itself_and_offers_the_remedy(self):
        got = self.result["permanent"]
        self.assertTrue(got["noticeHasTitle"])
        self.assertTrue(got["noticeHasRemedy"])
        self.assertEqual("Database unreadable", got["meta"])

    def test_the_rendered_remedy_names_no_database_path(self):
        """Asserted against the RENDERED html, not the source.

        The first version of this test read the source of the function and
        matched the comment that explains why the path was removed -- green
        would have meant nothing and red meant nothing. It named
        `~/.claude/usage.db` for one round, which is the wrong file whenever
        CLAUDE_USAGE_DB is set (the Dockerfile sets it, and AGENTS.md tells
        users to set it per version to escape the two-installs rebuild loop):
        a reader following it moved a WORKING database aside and still had the
        broken one. The notice now tells them to run the command that prints
        the real path.
        """
        self.assertFalse(self.result["permanent"]["noticeNamesAPath"])

    def test_a_permanent_failure_disables_the_button_that_would_fail_too(self):
        """Rescan opens the same file through the same init_db that refused."""
        self.assertTrue(self.result["permanent"]["rescanDisabled"])

    def test_it_clears_both_intervals_rather_than_only_dropping_the_handles(self):
        """`clearInterval(t); t = null` and a bare `t = null` are
        indistinguishable to any test that looks at the variable, and the
        second leaves both polls running for the life of the tab. The call is
        recorded instead."""
        self.assertEqual([101, 202], self.result["permanent"]["clearedTimers"])

    def test_the_notice_is_actually_revealed(self):
        """Built and left hidden renders as a blank page. Asserted as
        `hidden === false` AND non-empty, because the stub defaults hidden to
        false and either half alone passes on an untouched element."""
        self.assertTrue(self.result["permanent"]["noticeShown"])

    def test_the_loading_overlay_is_cleared_from_under_it(self):
        """The overlay is fixed-position over everything, so leaving it buries
        the explanation behind a spinner that never stops -- the exact trap
        showAuthNotice's own comment records."""
        self.assertTrue(self.result["permanent"]["loadingCleared"])

    def test_the_reload_button_is_wired(self):
        """It is the only control left on the screen; dead, the reader has a
        dialog with no way out."""
        self.assertTrue(self.result["permanent"]["reloadWired"])

    def test_a_transient_failure_still_retries_and_shows_no_notice(self):
        """The fresh-install case the retry was written for. A fix that turned
        this into a notice would be a worse defect than the one it replaced:
        the page would give up on a database that was about to appear."""
        got = self.result["transient"]
        self.assertEqual([3000], got["armed"])
        self.assertFalse(got["noticeHasTitle"])
        self.assertFalse(got["rescanDisabled"])


class TestTheNoticeIsASiblingNotABranch(unittest.TestCase):
    """A source-level rule, so it is checked at source level -- deliberately.

    AGENTS.md pins showAuthNotice's shared half as having to stay identical on
    every path through it. A different failure with a different remedy gets its
    own function rather than a third `reason` value that would loosen it.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = (Path(__file__).resolve().parent.parent
                   / "web" / "js" / "70-bootstrap.js").read_text(encoding="utf-8")

    def test_the_guard_sits_above_the_timer(self):
        """Below it, the timer is armed before the return and the notice fires
        over a page that is still polling."""
        guard = self.src.index("if (d.permanent) { showDatabaseNotice(); return; }")
        timer = self.src.index("setTimeout(() => {", guard)
        self.assertLess(guard, timer)

    def test_the_reason_rationale_still_heads_the_function_that_has_a_reason(self):
        """Inserting a function above another one silently steals its header.

        That is what happened here: `showDatabaseNotice` was written directly
        above `showAuthNotice`, which put the 18-line block explaining why
        `reason` exists -- and naming the three 403 handlers -- on top of a
        function that has no parameter, no default and no 403 caller, leaving
        the function it documents with no header at all. AGENTS.md calls that
        rationale load-bearing. Nothing failed; JavaScript hoists declarations,
        and no test sliced that region. A reviewer found it, and a first check
        by reading FORWARD from `showAuthNotice` missed it, because the comment
        inside its body is a different comment.
        """
        src = self.src
        auth = src.index("function showAuthNotice(reason")
        header = src[:auth].rstrip().splitlines()[-1]
        self.assertTrue(header.lstrip().startswith("//"),
                        f"showAuthNotice lost its header; found: {header!r}")
        rationale = src.index("`reason` exists because")
        other = src.index("function showDatabaseNotice()")
        self.assertLess(other, rationale,
                        "the reason rationale sits above the wrong function")
        self.assertLess(rationale, auth)

    def test_show_auth_notice_grew_no_database_branch(self):
        self.assertIn("function showDatabaseNotice()", self.src)
        self.assertNotIn("showAuthNotice('database')", self.src)


if __name__ == "__main__":
    unittest.main()
