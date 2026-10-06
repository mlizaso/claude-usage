"""
cli.py - Command-line interface for the Codex / Claude Usage dashboard.

`USAGE` below is the one prose copy of the command list; running with no
arguments (or `-h`) prints it. There was a second list here and it named four of
the six — it was written when four was all there was, and `week` and `url` each
arrived in a commit that updated `COMMANDS` and `USAGE` together without ever
touching it. Correcting it would only have made a third copy to keep equal, so
it is gone; do not let another one accrete.
"""

import contextlib
import os
import sys
import sqlite3
from pathlib import Path
from datetime import datetime, date, timedelta

from . import scanner
from .scanner import VERSION, invocation, terminal_safe
# Re-exported so `cli.PRICING` and `from cli import calc_cost` keep working;
# pricing.py is the source of truth.
from .pricing import PRICING, calc_cost, fmt, fmt_cost, get_pricing
from .reports import hr, resolve_source, _cmd_stats, _cmd_today, _cmd_week

DB_PATH = Path(os.environ.get("CODEX_CLAUDE_USAGE_DB", Path.home() / ".claude" / "usage.db"))

# Which assistant the reports cover. `all` is the only way to get a blended
# figure, and it says so in its own output — see reports.BLEND_NOTE.
VALID_SOURCES = ("claude", "codex", "all")


def validate_source(source):
    """Reject a --source the reports could not honour.

    An unrecognised name would otherwise scope every query to nothing and print
    a confident, empty report — a typo answering "you used nothing" instead of
    "there is no such source".
    """
    if source is not None and source not in VALID_SOURCES:
        raise ValueError(
            "--source must be one of: " + ", ".join(VALID_SOURCES))
    return source


def validate_dashboard_args(host=None, port=None, surface=None):
    """Resolve and check `dashboard`'s own three values before anything runs.

    Every other bad argument prints one line and exits 1; these three left as a
    Python traceback, because `main` translated `validate_flags` and
    `validate_source` and nothing else. Two of them cost more than tidiness. The
    port range was checked nowhere, so `--port 99999` reached `bind()` and
    surfaced as an OverflowError — not even a ValueError — *after* "Scanning in
    the background..." had printed, reading as a scan failure rather than a typo.
    And 0 is falsy in `dashboard.serve`'s `port = port or ...`, so `--port 0`
    never asked for the ephemeral port it looks like it asks for: it listened on
    8080 (or on `PORT`) without a word.

    One definition, called from both sides: `main` for the message,
    `cmd_dashboard` for the contract — a library caller still gets the
    ValueError rather than a printed line and an exit. Returns the resolved
    (host, port, surface).
    """
    from .dashboard import VALID_SURFACES, validate_bind_host

    host = validate_bind_host(host or os.environ.get("HOST", "localhost"))
    # `is None`, not `or`: 0 has to reach the range check below instead of
    # falling through to the default, which is how it silently became 8080.
    given = port if port is not None else os.environ.get("PORT", "8080")
    named = "--port" if port is not None else "PORT"
    try:
        port = int(given)
    except (TypeError, ValueError):
        raise ValueError(f"{named} must be a whole number: {terminal_safe(given)}")
    if not 1 <= port <= 65535:
        raise ValueError(f"{named} must be between 1 and 65535: {port}")
    if surface and surface not in VALID_SURFACES:
        raise ValueError("surface must be one of: " + ", ".join(sorted(VALID_SURFACES)))
    return host, port, surface


def _unusable_database_message(exc):
    """The three lines for a database SQLite cannot read.

    One definition because it is printed from two places, and they cover two
    halves of one command. `require_db` catches what `init_db` raises -- a file
    that is not a database at all, whose very first `stored_tables` fails --
    while `main` catches what the REPORT raises, because a file whose page 1 is
    intact and whose later pages are damaged opens fine and only fails on the
    first read past the corruption. That is the commonest shape of the two
    (an interrupted write, a cloud-sync clobber, a bad sector) and it escaped
    the handler entirely: the guard ended one statement before the failure it
    was written for, so `stats` still produced the six-frame traceback.
    """
    return (f"Not a usable usage database: {terminal_safe(str(DB_PATH))}\n"
            f"  SQLite could not read it: {terminal_safe(str(exc))}\n"
            f"  Move or delete that file, then run: {invocation()} scan")


def _locked_database_message(exc, action):
    """The four lines for a database SQLite could not use *right now*.

    `action` is the half of the command that failed -- "open" or "read" -- and
    it is the only thing that legitimately differs between the two call sites.
    There were two copies of this, written in the same commit that extracted the
    sibling above so that there would be one, and the second was three lines
    where this is four: the line it dropped was the diagnosis. A reader who hits
    the lock one statement later than the other one was told to try again
    without being told the file is intact or what is likely holding it.

    "scanning or rebuilding" rather than "scanning", because the read path
    reaches this with `no such table: turns` as well -- a reader inside another
    process's rebuild window, which `main`'s comment describes at length.

    The read-only mount stays in the shared sentence, and that was the argued
    half: it cannot be the cause of a failure met mid-report, since `init_db`
    has already written by then. It is a hedged possibility standing beside
    SQLite's own words on the line above, and the alternative on offer was two
    copies again -- which is how the diagnosis went missing in the first place.
    """
    return (f"Could not {action} the usage database right now: "
            f"{terminal_safe(str(DB_PATH))}\n"
            f"  SQLite said: {terminal_safe(str(exc))}\n"
            f"  The file itself looks fine. Another codex-claude-usage process may "
            f"be scanning or rebuilding it, or it may be on a read-only mount.\n"
            f"  Try again in a moment.")


def database_refusal(exc):
    """The message for a database this build will not touch, or `None`.

    One definition because THREE commands open the database and only the read
    ones ever answered these conditions. Against a foreign file, `stats` exited 1
    with the refusal on stderr -- SEVEN lines, not the "one line" an earlier
    version of this docstring claimed, because the remedy is deliberately
    multi-line; `scan`, the command that message names, raised a bare `sqlite3`
    traceback on the very same file instead; and `dashboard`'s background thread
    folded the whole remedy onto one `\\x0a`-escaped line of STDOUT and went on
    serving, with stderr empty and exit 0.

    No frame count is quoted for that traceback, deliberately. The figure this
    docstring used to give was wrong when it was written -- "eight" against a
    measured seven -- and it is zero on every path now, because this function is
    what removed the tracebacks. A depth that moves whenever a caller is added is
    a measurement, not a description.

    **The order is load-bearing and cannot be re-derived by reading**:
    `sqlite3.OperationalError` is a SUBCLASS of `sqlite3.DatabaseError`, so
    testing the parent first makes the specific branch unreachable and answers
    `database is locked` with "move or delete that file" -- destructive advice
    about an intact file. That was a real defect, fixed once in `require_db`'s
    except clauses and reintroducible here the moment these two `isinstance`
    calls swap places.

    `None` for anything else, so a caller can re-raise what this does not know
    rather than mislabel it.
    """
    from .db import ForeignDatabaseError, UnsafeDatabasePathError
    if isinstance(exc, ForeignDatabaseError):
        # As-is: `db` escaped the path and the table names individually, and
        # routing the whole thing through `terminal_safe` here would fold its
        # newlines to `\x0a` and print the remedy on one line -- the exact trap
        # `port_in_use_lines` and this repository's own tests document.
        return str(exc)
    if isinstance(exc, sqlite3.OperationalError):
        # `database is locked`, `attempt to write a readonly database` and
        # `unable to open database file` all arrive here, and none of them means
        # the database is damaged: the first is the concurrency this product
        # creates by design, and the second is a read-only mount or a directory
        # the user cannot write.
        return _locked_database_message(exc, "open")
    if isinstance(exc, sqlite3.DatabaseError):
        # What is left after the subclass above: a file that is not a SQLite
        # database at all, or one whose pages are damaged. The one state
        # declare-and-rebuild has no answer for, because `stored_tables` raises
        # before `schema_mismatches` can be asked anything. Nothing here can
        # repair it either: a rebuild needs a schema to read and there is none,
        # so the remedy has to be the user's.
        return _unusable_database_message(exc)
    if isinstance(exc, UnsafeDatabasePathError):
        # The PATH, not the contents -- and until 2026-08-16 all four of these
        # came out as a raw traceback from `today`/`week`/`stats` and from
        # `scan`, which is the one class of database problem this contract was
        # written for: a mis-pointed `CODEX_CLAUDE_USAGE_DB` is a typo, not a crash.
        # Reproduced for each of the four on a copy of the tree as it stood.
        #
        # The guard's own sentence is quoted rather than re-worded, so there is
        # still exactly one statement of what it refuses and why, in `db`.
        return (f"Cannot use that usage database path: "
                f"{terminal_safe(str(DB_PATH))}\n"
                f"  {terminal_safe(str(exc))}\n"
                f"  Point CODEX_CLAUDE_USAGE_DB at a regular file you own, or repair "
                f"that path.")
    return None


def _refusable():
    """Catch database failures; callers re-raise anything we cannot classify.

    Both explicit database refusal types subclass RuntimeError. Unrelated
    runtime and OS errors retain their original traceback via ``_refused``.
    """
    return (RuntimeError, OSError, sqlite3.DatabaseError)


def _is_a_lock(exc):
    """Whether `exc` is the one refusal that clears itself without the user.

    Deliberately narrower than `isinstance(exc, sqlite3.OperationalError)`, and
    the difference is the whole point: that family also carries `attempt to
    write a readonly database` and `unable to open database file`, and a
    read-only mount does not become writable while the process waits.

    Matched on SQLite's own words rather than on the class, because SQLite is
    the only thing that knows which of them it means and there is no distinct
    exception type for the lock. `database is locked` and `database table is
    locked` are the two it emits.

    This is a different question from `database_refusal`'s, which is why it is
    not folded into it: that function decides what to PRINT, and all three
    messages get the same four lines because the remedy really is the same
    ("try again in a moment, or fix the mount"). This decides whether the
    dashboard beside the failure can still answer, and only the lock can.

    It is also a different question from `db.a_retry_could_succeed`, which the
    page uses, and the two disagree on purpose. This one asks whether the data
    is intact NOW; that one asks whether a later request will succeed. A
    rebuild window answers no here and yes there -- the tables really are
    dropped, and the rebuild really does finish. See that docstring; do not
    replace this call with it.
    """
    return (isinstance(exc, sqlite3.OperationalError)
            and "locked" in str(exc).lower())


class DatabaseRefused(SystemExit):
    """`exit(1)` for a refused database, carrying whether it may clear itself.

    A `SystemExit` subclass carrying the same exit code, so `except SystemExit`
    still catches it, the terminal still sees 1, and every existing caller and
    test is unaffected. What it adds is the one bit `cmd_dashboard`'s background
    thread cannot recover otherwise: `cmd_scan` prints the reason and exits, so
    by the time the thread sees anything the exception that explained it is
    gone, and the thread has to say something about a server that is still bound
    and serving.

    `transient` is true only for a LOCK. A locked database is expected to become
    readable -- it is the concurrency this product creates by design, and the
    file behind the page is intact the moment the other process lets go. A
    foreign file, a damaged one, a refused path and a read-only mount will not
    clear themselves, and every data request will fail for the life of the
    process.

    This used to read "the lock family" and be implemented as
    `isinstance(exc, sqlite3.OperationalError)`, which is a wider set than its
    own sentence: `attempt to write a readonly database` and `unable to open
    database file` are in that family and neither becomes readable on its own.
    A read-only `~/.claude` therefore took the reassuring branch and told the
    reader the dashboard was "still serving whatever the database already held",
    while `/api/data` and `/api/sources` answered `500 {"error": "Failed to read
    the usage database"}` on every request -- reintroducing, for the read-only
    case, the false claim round 19 removed for the foreign one.
    """

    def __init__(self, exc):
        super().__init__(1)
        self.transient = _is_a_lock(exc)


def _refused(exc):
    """Print what `database_refusal` says about `exc`, then exit 1.

    Returns False, having printed nothing, for an exception it cannot label, so
    that a caller re-raises with a bare `raise` -- keeping the original
    traceback rather than answering a full disk with a database remedy.
    """
    message = database_refusal(exc)
    if message is None:
        return False
    print(message, file=sys.stderr)
    raise DatabaseRefused(exc)


def require_db():
    """An open connection for a read command, or exit 1 with a reason.

    Two different conditions, deliberately answered differently, because
    conflating them broke a contract this repository already had.

    **A rebuild is an EVENT and exits 1.** `init_db` matched the stored schema
    or dropped it and started over, so on a read path a rebuild means the data
    the user asked about was destroyed a microsecond ago. Reporting `Total
    turns: 0`, `Est. total cost: $0.0000` and exit 0 under that is the defect
    this branch exists for.

    **An empty database is a STATE and reports normally**, with a notice on
    stderr. `TestCliReportsWithNoData` has always said "an empty (but valid) DB
    must report emptiness, not crash or invent rows", and that is right: zeroes
    are the *correct* answer for a machine with no transcripts, so refusing
    outright would break a working install to fix a broken one.

    The notice is what closes the gap between them, and it is why it is
    unconditional rather than tied to the rebuild. Keyed to the rebuild alone,
    the warning was exactly one invocation wide: the next `stats` found a
    matching, empty database and printed a confident `$0.0000` in silence. Two
    independent reviewers reproduced that. Now every invocation against an empty
    database says so, for as long as it stays empty — so a user whose history
    has just been rebuilt away is told on the second command as clearly as on
    the first.

    stdout stays exactly what it was, which is what keeps a piped report
    honest; the notice goes to stderr with the rebuild announcement.
    """
    from .db import BUSY_TIMEOUT_MS, connect_existing_db
    from .scanner import init_db
    # The path itself, BEFORE the existence probe, and inside a handler --
    # both of those are the fix for one thing. `Path.exists()` does not swallow
    # a `PermissionError`: a database inside a directory this user cannot stat
    # made the probe itself raise, one line above the first handler in the
    # function. Asking the guard first answers that case as the path refusal it
    # is, and costs nothing on the ordinary missing-file path -- with
    # `create=False` the guard returns quietly for a file that is not there.
    try:
        conn = connect_existing_db(DB_PATH)
    except _refusable() as exc:
        if not _refused(exc):
            raise
    if conn is None:
        # Naming the file is what tells a typo'd `CODEX_CLAUDE_USAGE_DB` apart from a
        # machine that has never scanned. Every sibling refusal here names it;
        # this one did not, and the instruction it gives -- `cli.py scan` --
        # then CREATES a second database at the typo'd path, leaving the real
        # history untouched and unreachable with nothing naming either file.
        print(f"Database not found: {terminal_safe(str(DB_PATH))}\n"
              f"  Run: {invocation()} scan", file=sys.stderr)
        sys.exit(1)
    ready = False
    try:
        conn.row_factory = sqlite3.Row
        # A read path that WRITES: `init_db` below can drop and recreate every
        # table, which takes the write lock. sqlite3's default 5-second timeout is
        # shorter than one `/api/data` rollup on a real database, and this product
        # creates concurrent writers by design (a dashboard per VS Code window,
        # each with a background scan, plus `cli.py scan` in a terminal) -- so
        # without this the losing reader does not wait, it raises `database is
        # locked`. Same value and same reason as `db.get_db`.
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        # Ensure the schema is current before querying: the read commands touch
        # the `agents` table and the `is_subagent`/`agent_id` columns, so a
        # database written by a different version would otherwise raise "no such
        # column". **This call can DESTROY the database** -- see the docstring.
        # A file that is not ours at all is refused rather than rebuilt, and the
        # refusal is a message and an exit rather than a traceback -- it is a
        # mis-pointed `CODEX_CLAUDE_USAGE_DB`, which is a typo, not a crash.
        #
        # Three conditions and three different messages, but ONE definition of
        # which is which: `database_refusal`, which `cmd_scan` and
        # `cmd_dashboard`'s background thread now share. It carries the subclass
        # ordering that used to be spelt out in this clause list, so a reader
        # wanting that history should look there. `_refusable` is what those
        # three commands catch; its own docstring says why it is a function and
        # why it is deliberately wider than what `database_refusal` can label.
        # The reason given here until 2026-08-16 -- that it must be a helper so
        # `db` is not imported at `cli` import time -- was true of an earlier
        # shape and is not of this one, which names no exception of ours and
        # imports nothing.
        try:
            rebuilt = init_db(conn, DB_PATH)
        except _refusable() as exc:
            if not _refused(exc):
                raise
        empty = conn.execute(
            "SELECT NOT EXISTS(SELECT 1 FROM turns)").fetchone()[0]
        if empty:
            # Unconditional, and that is the whole of the repair -- see the
            # docstring. It follows the rebuild announcement on stderr when
            # there was one, and stands alone on every later invocation.
            print("This usage database holds no turns. If you expected history "
                  f"here, run: {invocation()} scan", file=sys.stderr)
        if rebuilt:
            sys.exit(1)
        ready = True
        return conn
    finally:
        if not ready:
            conn.close()


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_scan(projects_dirs=None, verbose=True):
    """Scan the transcript roots. `verbose` prints one line per file.

    Verbose is right for the command the reader typed: `cli.py scan` is
    supposed to show its work, and the paths it names are the reader's own.

    It is wrong for `cmd_dashboard`'s BACKGROUND scan, which is where this
    parameter came from. That thread shares stdout with the authenticated URL
    the reader is told to copy, so a cold walk of `~/.claude/projects` buried
    the one line that matters under thousands of absolute paths -- each
    carrying the account name, on the stream most likely to be pasted into a
    terminal share or a bug report. `dashboard._background_scan` had already
    passed `verbose=False`; the two entry points simply disagreed.
    """
    from .scanner import scan
    # include_defaults: on the CLI, --projects-dir means "also scan here". Your
    # own ~/.claude/projects is never dropped from a scan just because an extra
    # root was named — a database missing its primary history is worse than one
    # with too much in it.
    #
    # The same three database refusals the read commands answer, because this is
    # the command their message SENDS the reader to: "Move or delete that file,
    # then run: python cli.py scan" was followed literally, without moving the
    # file, and answered with a bare `sqlite3.DatabaseError` traceback.
    # A foreign file did the same through `db.ForeignDatabaseError`. Both are a
    # mis-pointed `CODEX_CLAUDE_USAGE_DB` — a typo, not a crash — and a typo that
    # tracebacks from one command and prints one line from another teaches the
    # reader that the two disagree about the file.
    try:
        scan(projects_dirs=[Path(d) for d in (projects_dirs or [])] or None,
             include_defaults=True, include_docker=True, verbose=verbose)
    except _refusable() as exc:
        # `get_db` runs the same path guard `require_db` does, so the four path
        # conditions reach this command too -- and reached it as tracebacks
        # until 2026-08-16, from inside `db.get_db`. `_refused` re-narrows the
        # widened tuple: anything it cannot label is re-raised untouched.
        if not _refused(exc):
            raise


def cmd_today(source=None):
    validate_source(source)
    with contextlib.closing(require_db()) as conn:
        _cmd_today(conn, resolve_source(conn, source))


def cmd_week(source=None):
    validate_source(source)
    with contextlib.closing(require_db()) as conn:
        _cmd_week(conn, resolve_source(conn, source))


def cmd_stats(source=None):
    validate_source(source)
    with contextlib.closing(require_db()) as conn:
        _cmd_stats(conn, resolve_source(conn, source))


def cmd_url(open_browser=False):
    """Print (or open) the authenticated URL of a running dashboard.

    The way back in after losing the link. The token is deliberately absent from
    the page itself — another local account could otherwise recover it by
    requesting `/` — so a browser sitting at the bare address cannot get itself
    authorized, and before this the only recovery was to restart the server.
    """
    from .dashboard import read_url_file, remove_url_file, url_is_live
    url = read_url_file()
    # The file surviving is not evidence the server is: a crash, a SIGTERM or a
    # closed terminal never runs the cleanup. Printing it unchecked hands over a
    # link that silently does nothing, and `--open` launches a browser at it.
    #
    # But a probe that did not finish is not evidence either, and this file is
    # the only way back into a dashboard that may well still be serving. So the
    # unlink waits for proof (`is False`); an unproven answer keeps the file and
    # still refuses to print or open it, because unverified is not live either.
    if url:
        live = url_is_live(url)
        if live is None:
            print("Could not reach the dashboard, so its link was left alone.", file=sys.stderr)
            print(f"Try again in a moment:  {invocation()} url", file=sys.stderr)
            sys.exit(1)
        if not live:
            # Guarded on the file still naming what was probed, the same rule
            # `serve()`'s shutdown uses. The read above and this verdict are a
            # network round trip apart, and a dashboard that took the file over
            # inside that window owns it now: unlinking on the strength of a
            # probe of a DIFFERENT url spends a link that was never ours, and
            # `write_url_file` runs once, at startup, so it never comes back.
            remove_url_file(only_if=url)
            # It returns nothing, so ask the file rather than assume. A url that
            # is still there and no longer ours means the guard bit — saying
            # "no running dashboard" then would be the opposite of the truth.
            replacement = read_url_file()
            if replacement and replacement != url:
                print("The dashboard link changed while it was being checked.", file=sys.stderr)
                print(f"Try again in a moment:  {invocation()} url", file=sys.stderr)
                sys.exit(1)
            url = None
    if not url:
        # stderr, all of it. `url` exists to be substituted --
        # `open "$(cli.py url)"` -- so its stdout is the URL or nothing.
        # These lines used to go to stdout, which handed a launcher two
        # lines of English where it expected a link, and `2>/dev/null`
        # hid nothing because there was nothing on stderr to hide.
        print("No running dashboard found.", file=sys.stderr)
        print(f"Start one with:  {invocation()} dashboard", file=sys.stderr)
        sys.exit(1)
    print(url)
    if open_browser:
        import webbrowser
        webbrowser.open(url)


def cmd_dashboard(projects_dirs=None, host=None, port=None, no_browser=False, surface=None):
    import threading
    import time

    from .dashboard import authenticated_dashboard_url, serve, start_scan_thread

    host, port, surface = validate_dashboard_args(host, port, surface)

    # Bind and serve the port *first*, then scan in the background. A cold scan
    # over a large ~/.claude/projects backlog can take well over a minute, and
    # the VS Code extension kills the process if /healthz does not answer
    # within 20s (see vscode-extension/src/server-manager.ts). Serving up front
    # means the port is live immediately. The page can render its shell at once,
    # but now keeps a full progress state over provisional figures and waits for
    # this tracked scan to finish before it reads sources and usage data.
    #
    # Capture cmd_scan into a local so the background thread closes over the
    # current binding — keeps the test suite's mock.patch(cli.cmd_scan) effective
    # and prevents the thread from ever touching the real DB after a patch lifts.
    scan = cmd_scan

    def background_scan():
        print("Scanning in the background...")
        try:
            # Deliberately NOT under `dashboard.RESCAN_LOCK` -- see the comment
            # in `dashboard._background_scan`, which measured what taking it
            # would do to the Rescan button.
            scan(projects_dirs=projects_dirs, verbose=False)
        except SystemExit as exc:
            # `cmd_scan` answers a database it will not touch with the same
            # message and exit code the read commands give, and has already
            # written it to stderr as LINES. Raised on a daemon thread it ends
            # the thread and nothing else, so all that is left here is to say
            # which half of the process stopped.
            #
            # **A stated limit, not a fix.** The server is already bound and
            # keeps serving. Tearing it down from this thread was considered and
            # not done: the port is held, the VS Code extension's 20s
            # `/healthz` deadline is counting, and a process that vanishes
            # mid-startup trades one unexplained state for another. What the
            # reader gets instead is the whole remedy, on stderr, on separate
            # lines.
            #
            # **What it says depends on which refusal it was**, because the
            # reassurance below is true of exactly one of them. Until
            # 2026-08-16 one line was printed for all three, and it said the
            # dashboard was "still serving whatever the database already held".
            # Measured that day against a foreign `CODEX_CLAUDE_USAGE_DB`, through a
            # real server and a real authenticated client: `GET /api/data` and
            # `GET /api/sources` both answered `500 {"error": "Failed to read
            # the usage database"}`, and answered it again on the next request,
            # while `/` and `/healthz` returned 200. A reader acting on that
            # line opens the URL on stdout and finds a page that loads and can
            # never fill.
            #
            # A lock is the one refusal the sentence fits: the file behind the
            # page is intact and readable the moment the other process lets go.
            if getattr(exc, "transient", False):
                print("Background scan stopped. The dashboard is still serving "
                      "whatever the database already held.", file=sys.stderr)
            else:
                print("Background scan stopped. The dashboard is still serving "
                      "the page, but it reads the database that was just "
                      "refused, so every data request will fail the same way "
                      "until that is fixed.", file=sys.stderr)
            return False
        except Exception as exc:
            # A daemon thread that dies takes the only ingestion path with it and
            # the dashboard goes on serving stale data with no visible signal.
            # On stderr: it is a diagnostic, and this command's stdout carries
            # the URL a reader is expected to copy.
            print(f"Background scan failed: {terminal_safe(exc)}",
                  file=sys.stderr)
            return False
        print("Background scan complete.")
        return True

    def open_browser():
        # Imported in the thread that uses it, so the --no-browser path the VS
        # Code extension takes still never loads it.
        import webbrowser
        time.sleep(1.0)
        webbrowser.open(authenticated_dashboard_url(host, port))

    def on_ready():
        """Everything that only makes sense once the port is actually ours.

        Both of these used to start *before* `serve()`, which was harmless on
        the happy path and wrong on the one that fails: a busy port then printed
        its complaint underneath however many lines the scan had already
        emitted, and a browser was launched at a dashboard that did not exist.
        `serve` calls this after the bind, so a failed start now does nothing
        but explain itself.
        """
        start_scan_thread(background_scan, threading.Thread)
        # Open a browser for users running this as a script (see README). The VS
        # Code extension passes --no-browser since it embeds the dashboard in a
        # webview.
        if not no_browser:
            threading.Thread(target=open_browser, daemon=True).start()

    serve(host=host, port=port, surface=surface, projects_dirs=projects_dirs,
          on_ready=on_ready)


# ── Entry point ───────────────────────────────────────────────────────────────

USAGE = """
Codex / Claude Usage Dashboard

Usage:
  python cli.py scan [--projects-dir PATH ...]
                                             Scan JSONL files and update database.
                                             --projects-dir may be repeated and is scanned
                                             IN ADDITION to the default locations; set
                                             CODEX_CLAUDE_USAGE_PROJECTS_DIRS for the same effect.
  python cli.py today [--source SOURCE]      Show today's usage summary
  python cli.py week  [--source SOURCE]      Show last 7 days (per-day + by-model)
  python cli.py stats [--source SOURCE]      Show all-time statistics
                                             --source is claude (default), codex, or all.
                                             One assistant at a time, like the dashboard:
                                             Claude bills per token, a Codex plan is a
                                             subscription, so `all` sums unlike figures
                                             and labels itself as doing so. A database
                                             holding only one assistant reports that one
                                             without the flag.
  python cli.py dashboard [--projects-dir PATH ...] [--host LOOPBACK_HOST] [--port PORT] [--no-browser] [--surface SURFACE]
                                                 Scan + start dashboard (opens a browser unless --no-browser)
  python cli.py url [--open]                 Print the authenticated URL of a running
                                             dashboard (--open launches the browser).
                                             Use this if you lost the link.
  python cli.py --version                    Print the version and exit
"""


def usage_text():
    """`USAGE` spelled for the surface the reader is actually on.

    The template says `python cli.py`, which is the checkout's and the Docker
    image's spelling and exists nowhere a pip, Homebrew or .vsix user can reach
    -- there the tool is `codex-claude-usage` and the help text named a command that
    does not exist. Substituted at call time rather than baked into the constant
    because `invocation()` reads `sys.argv[0]`, which is not known at import.

    Every line carries the same prefix, so replacing it shifts the description
    column by one character on all of them equally and they stay aligned with
    each other. `USAGE` itself stays a module constant: it is what the tests
    read, and it is the one prose copy of the command list.
    """
    return USAGE.replace("python cli.py", invocation())

COMMANDS = {
    "url": cmd_url,
    "scan": cmd_scan,
    "today": cmd_today,
    "week": cmd_week,
    "stats": cmd_stats,
    "dashboard": cmd_dashboard,
}

# Which flags each command actually reads, and how each one is spelled:
#   "value"    — takes the next token, or --flag=VALUE
#   "repeated" — the same, and may appear more than once
#   "flag"     — a bare switch, tested with `in rest`
#
# Per command rather than one global set, because a flag a command does not read
# is the same silent drop as one that is misspelt: `today --port 9000` and
# `scan --source codex` both parsed, both did something other than what was
# asked, and both exited 0. Keep this table equal to COMMANDS — a command
# missing from it accepts no flags at all.
COMMAND_FLAGS = {
    "url": {"--open": "flag"},
    "scan": {"--projects-dir": "repeated"},
    "today": {"--source": "value"},
    "week": {"--source": "value"},
    "stats": {"--source": "value"},
    "dashboard": {
        "--projects-dir": "repeated",
        "--host": "value",
        "--port": "value",
        "--surface": "value",
        "--no-browser": "flag",
    },
}


# The three spellings that ask for the banner. Recognised only at a position
# `flag_positions` yields — see `main`.
HELP_TOKENS = ("-h", "--help", "help")


def flag_positions(command, args):
    """The indices of `args` read as flags rather than as another flag's value.

    ONE definition of "which token is a value", used by `validate_flags` for the
    walk below and by `main` for the help check. `main` scanned every token for
    a help word with no notion of what had already been consumed, so a value
    that happened to spell one printed the banner and exited 0 with the command
    never run: `scan --projects-dir help` reported success having scanned
    nothing, and `today --source help` did the same rather than reaching
    `validate_source` — while `today --source=help` correctly exited 1, so the
    two spellings of one flag disagreed.

    A yielded token is a flag, or the leftover `validate_flags` raises on; a
    skipped one belongs to the flag before it. An unrecognised name consumes
    nothing, because nothing here knows whether it takes a value —
    `validate_flags` rejects it either way.
    """
    known = COMMAND_FLAGS.get(command, {})
    index = 0
    while index < len(args):
        yield index
        name, equals, _value = args[index].partition("=")
        if (known.get(name) in ("value", "repeated") and not equals
                and index + 1 < len(args) and not args[index + 1].startswith("-")):
            index += 2
        else:
            index += 1


def validate_flags(command, args):
    """Reject an argument this command would otherwise drop without a word.

    `validate_source` only ever sees the VALUE of `--source`, so a typo in the
    flag NAME bypassed it entirely: `today --sourcex codex` printed a complete,
    correctly formatted *Claude* report and exited 0, and only the one-line
    `Source:` header said the request had not been honoured. So did
    `--source=codex`, the equals form — which is parsed here rather than
    rejected, since it is what a reader who knows argparse types.

    Values are consumed along with their flag by `flag_positions` — the space
    form by skipping the next index, the equals form inside the token itself —
    so nothing already consumed reaches this loop, and a path or a host is never
    mistaken for a flag. That is what lets a leftover be rejected outright
    rather than skipped: it cannot be a value, and no command takes a
    positional. One that genuinely starts with `-` still has the `--flag=VALUE`
    spelling. Raises ValueError.
    """
    known = COMMAND_FLAGS.get(command, {})
    seen = set()
    for index in flag_positions(command, args):
        token = args[index]
        if not token.startswith("-"):
            # A stray word, and only ever that — see above. Skipping it was the
            # same silent drop as a misspelt flag, one token shape over:
            # `today codex` printed a complete, correctly formatted *Claude*
            # report at exit 0, exactly as `today --sourcex codex` did.
            # `repr` for the empty string, which `terminal_safe` renders as no
            # characters at all and would leave naming nothing after the colon.
            raise ValueError(
                f"unexpected argument for `{command}`: "
                f"{terminal_safe(token) if token else repr(token)}")
        name, equals, value = token.partition("=")
        kind = known.get(name)
        if kind is None:
            raise ValueError(
                f"unknown argument for `{command}`: {terminal_safe(name)}")
        if name in seen and kind != "repeated":
            # parse_named_arg returns the FIRST match, so `--source codex
            # --source claude` silently meant codex.
            raise ValueError(f"{name} was given more than once")
        seen.add(name)
        if kind == "flag":
            if equals:
                raise ValueError(f"{name} takes no value")
        elif equals:
            if not value:
                raise ValueError(f"{name} needs a value")
        elif index + 1 < len(args) and not args[index + 1].startswith("-"):
            if not args[index + 1]:
                # The equals spelling refuses an empty value just above, and the
                # space spelling has to agree: `Path("")` is `Path(".")` and
                # `resolve_scan_roots` accepts it, so `--projects-dir ""` — a
                # wrapper passing an unset variable — made the whole working
                # directory a scan root and reported a normal scan at exit 0.
                # `CODEX_CLAUDE_USAGE_PROJECTS_DIRS` has always dropped blanks.
                # Emptiness, not blankness: `Path(" ")` is a directory someone
                # may legitimately have, and one that does not exist already
                # gets the "not found, skipping" warning.
                raise ValueError(f"{name} needs a value")
        else:
            # A dangling flag read as "not supplied", so `today --source` scoped
            # the report to the default; `dashboard --host --port` was worse
            # still — `--port` became the host.
            raise ValueError(f"{name} needs a value")


def parse_named_arg(args, flag):
    """Extract a --flag VALUE (or --flag=VALUE) pair from an argument list."""
    for value in _named_values(args, flag):
        return value
    return None

def parse_repeated_arg(args, flag):
    """Extract every --flag VALUE pair, in the order given."""
    return list(_named_values(args, flag))

def _named_values(args, flag):
    """Every value given for `flag`, in either spelling."""
    joined = flag + "="
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            yield args[i + 1]
        elif arg.startswith(joined):
            yield arg[len(joined):]


def scan_roots_for(rest):
    """The directories this invocation will walk, warning about absent ones.

    Repeatable: --projects-dir A --projects-dir B scans A, B *and* the defaults.
    Resolved once per invocation so the background scan, the Rescan button and
    the startup log all agree on the same list.

    Called from the two branches that walk a directory, because it was called
    for all six: `url`, `today`, `week` and `stats` never read a transcript, and
    an absent `CODEX_CLAUDE_USAGE_PROJECTS_DIRS` root (an unmounted drive is the
    ordinary reason) still printed its warning at the top of their output. On
    stdout, which is the half that broke a caller rather than merely puzzling
    one — `url`'s stdout is a single URL, and `open "$(codex-claude-usage url)"` got
    the warning, a newline, then the link. The warning goes to stderr for the
    same reason `require_db`'s empty-database notice does: a diagnostic is not
    report output.
    """
    extra_dirs = parse_repeated_arg(rest, "--projects-dir")
    roots, missing = scanner.resolve_scan_roots(
        projects_dirs=extra_dirs or None, include_defaults=True)
    for d in missing:
        print(f"  Warning: transcript directory not found, skipping: {terminal_safe(d)}",
              file=sys.stderr)
    return roots


def main():
    """Console entry point (``codex-claude-usage``) and ``python cli.py`` dispatch."""
    if len(sys.argv) >= 2 and sys.argv[1] in ("--version", "-V", "version"):
        print(VERSION)
        sys.exit(0)

    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        # Asking for the banner is not a failure, and `brew test` runs the
        # no-argument form under an exit-0-expecting shell_output.
        print(usage_text())
        sys.exit(0)

    if sys.argv[1] not in COMMANDS:
        # Same silent drop as an unknown flag, one token earlier: `cli.py todya`
        # printed the banner and exited 0, which a wrapper script cannot tell
        # apart from a report.
        print(f"unknown command: {terminal_safe(sys.argv[1])}", file=sys.stderr)
        print(usage_text(), file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]
    rest = sys.argv[2:]
    # `cli.py <command> --help` is the first thing a newcomer types, and it used
    # to be REJECTED — `--help` is in no command's COMMAND_FLAGS entry, so
    # validate_flags called it an unknown argument and exited 1. The banner is
    # the same one the bare form prints; it is answered here rather than added to
    # every table entry so that the table keeps meaning "flags this command
    # reads", which is what makes an unknown one an error.
    #
    # Only at a position `flag_positions` yields, so a help word handed over as
    # a VALUE stays a value — see that function.
    if any(rest[index] in HELP_TOKENS for index in flag_positions(command, rest)):
        print(usage_text())
        sys.exit(0)
    # Before anything runs or prints: an argument this command does not read is
    # dropped in silence, and `validate_source` never sees a misspelt flag NAME.
    try:
        validate_flags(command, rest)
        if command == "dashboard":
            # Its own three values judged at the same boundary and by the same
            # rule. `cmd_dashboard` raises them for a library caller; only here
            # do they become the one line every other bad argument gets, and
            # only here are they judged before the background scan starts.
            validate_dashboard_args(
                host=parse_named_arg(rest, "--host"),
                port=parse_named_arg(rest, "--port"),
                surface=parse_named_arg(rest, "--surface"),
            )
    except ValueError as exc:
        print(terminal_safe(exc), file=sys.stderr)
        sys.exit(1)

    # User-supplied rates (CODEX_CLAUDE_USAGE_RATES), before any command runs. Without
    # this the documented override changed nothing on the terminal reports at
    # all: the only other caller is dashboard.py, which `today` / `week` /
    # `stats` never import. Loaded here rather than at module import so `import
    # cli` has no side effect on the shared price table, and it is idempotent —
    # `cmd_dashboard` imports dashboard, which loads the same file again.
    from .pricing import load_rate_overrides
    load_rate_overrides()

    if command == "dashboard":
        # Translated here for the same reason the ValueErrors above are: the
        # library contract raises, and only this boundary turns it into
        # something a reader can act on. Printed line by line rather than
        # through `terminal_safe`, which escapes Cc — and a newline is Cc, so it
        # would fold the whole remedy onto one line. Safe because every value in
        # those lines is this process's own; see `port_in_use_lines`.
        from .dashboard import PortInUseError
        try:
            cmd_dashboard(
                projects_dirs=scan_roots_for(rest),
                host=parse_named_arg(rest, "--host"),
                port=parse_named_arg(rest, "--port"),
                no_browser="--no-browser" in rest,
                surface=parse_named_arg(rest, "--surface"),
            )
        except PortInUseError as exc:
            print("\n".join(exc.lines), file=sys.stderr)
            sys.exit(1)
    elif command == "scan":
        cmd_scan(projects_dirs=scan_roots_for(rest))
    elif command == "url":
        cmd_url(open_browser="--open" in rest)
    else:
        # Only the report commands take --source, so only they reject a bad one.
        # A typo must not reach reports.py: an unrecognised name scopes every
        # query to nothing, and the report would answer "you used nothing"
        # instead of "there is no such source".
        try:
            source = validate_source(parse_named_arg(rest, "--source"))
        except ValueError as exc:
            print(terminal_safe(exc), file=sys.stderr)
            sys.exit(1)
        # The read itself, not just the open. `require_db` catches what
        # `init_db` raises, which only covers a file whose FIRST page is
        # unreadable. A database whose page 1 is intact and whose later pages
        # are damaged opens cleanly and fails on the first read past the
        # corruption -- inside the report, one statement beyond that handler --
        # so `stats` still produced the traceback the handler was written to
        # remove. Same three lines, and for this state the same remedy is the
        # right one.
        #
        # `OperationalError` IS caught here, with its own wording, and the
        # sentence that stood here said the opposite -- "deliberately NOT
        # caught: a lock taken mid-report is `require_db`'s message" -- three
        # lines above the clause that catches it. Both halves were false.
        # `require_db` can only answer what `init_db` raises, and it has
        # returned by the time a report runs, so anything the report itself
        # hits reaches this boundary and nowhere else.
        #
        # Ordinary writer contention can still begin after `require_db` returns,
        # and later-page corruption can first surface inside the report. The
        # exception ordering matters because `OperationalError` is a SUBCLASS
        # of `DatabaseError`: this clause first keeps a lock from being answered
        # with the destructive "move or delete that file" remedy.
        try:
            COMMANDS[command](source=source)
        except sqlite3.OperationalError as exc:
            # The same four lines `database_refusal` gives for the open, with
            # the verb as the only difference. This was a second, three-line
            # copy, written in the commit that extracted the sibling message so
            # that there would be one of it -- and the line it dropped was the
            # diagnosis, so a lock met one statement later than the other was
            # answered with "try again" and nothing about the file being intact.
            print(_locked_database_message(exc, "read"), file=sys.stderr)
            sys.exit(1)
        except sqlite3.DatabaseError as exc:
            print(_unusable_database_message(exc), file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
