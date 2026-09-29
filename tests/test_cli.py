"""Tests for cli.py - pricing, formatting, and cost calculation."""

import collections
import contextlib
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
import cli
from cli import get_pricing, calc_cost, fmt, fmt_cost, PRICING


CliRun = collections.namedtuple("CliRun", "code out err calls")


def run_cli(argv, printing=None):
    """Drive `cli.main()` with every command stubbed out, both streams captured.

    Stubbing is what makes an ACCEPTED invocation assertable: the claim is which
    command ran with which arguments, not what a real scan would print — and an
    unstubbed `scan` or `dashboard` would walk the developer's own transcripts
    and bind a socket.

    Two streams because `main`'s diagnostics and its report output are no longer
    the same stream, and one of the two tells a caller apart from a shell.
    `printing` lets a stub emit a line on stdout, so a test can assert exactly
    what `$(claude-usage url)` substitutes.
    """
    calls = []

    def record(name):
        def stub(*args, **kwargs):
            calls.append((name, kwargs))
            if printing and name in printing:
                print(printing[name])
        return stub

    out, err = io.StringIO(), io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(
            cli.COMMANDS, {name: record(name) for name in cli.COMMANDS}))
        # `main` calls these three by name rather than through COMMANDS.
        for name in ("cmd_dashboard", "cmd_scan", "cmd_url"):
            stack.enter_context(mock.patch.object(cli, name, record(name)))
        stack.enter_context(mock.patch.object(cli.sys, "argv", ["cli.py"] + argv))
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        try:
            cli.main()
            code = 0
        except SystemExit as exit_code:
            code = exit_code.code or 0
    return CliRun(code, out.getvalue(), err.getvalue(), calls)


class TestGetPricing(unittest.TestCase):
    def test_exact_model_match(self):
        p = get_pricing("claude-opus-4-6")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_all_known_models_have_pricing(self):
        for model in ("claude-fable-5", "claude-mythos-5",
                       "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
                       "claude-sonnet-4-7", "claude-sonnet-4-6", "claude-sonnet-4-5",
                       "claude-haiku-4-7", "claude-haiku-4-6", "claude-haiku-4-5"):
            p = get_pricing(model)
            self.assertGreater(p["input"], 0, f"Missing input price for {model}")
            self.assertGreater(p["output"], 0, f"Missing output price for {model}")

    def test_fable_and_mythos_have_explicit_entries(self):
        """Regression guard for #136/#137 — Fable 5 and Mythos 5 must be priced
        explicitly at 2x Opus, not fall through to $0/n/a or an Opus rate."""
        for model in ("claude-fable-5", "claude-mythos-5"):
            self.assertIn(model, PRICING)
            p = get_pricing(model)
            self.assertEqual(p["input"], 10.00, f"{model} input price wrong")
            self.assertEqual(p["output"], 50.00, f"{model} output price wrong")
            self.assertEqual(p["cache_read"], 1.00, f"{model} cache_read wrong")
            self.assertEqual(p["cache_write"], 12.50, f"{model} cache_write wrong")

    def test_fable_date_suffix_matches(self):
        """JSONL model strings may carry a date suffix."""
        p = get_pricing("claude-fable-5-20260601")
        self.assertEqual(p["input"], 10.00)
        self.assertEqual(p["output"], 50.00)

    def test_substring_match_fable_and_mythos(self):
        """Unknown future fable/mythos variants resolve to Fable pricing,
        not the generic opus/sonnet/haiku rates or n/a."""
        for model in ("some-fable-variant", "internal-mythos-test"):
            p = get_pricing(model)
            self.assertEqual(p["input"], 10.00, f"{model} should map to Fable pricing")
            self.assertEqual(p["output"], 50.00, f"{model} should map to Fable pricing")

    def test_opus_4_8_has_explicit_entry(self):
        """Regression guard for issue #133 — Opus 4.8 must be present, not just
        resolved via the generic 'opus' substring fallback."""
        self.assertIn("claude-opus-4-8", PRICING)
        p = get_pricing("claude-opus-4-8")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_opus_4_7_has_explicit_entry(self):
        """Regression guard for issue #61 — Opus 4.7 must be present."""
        p = get_pricing("claude-opus-4-7")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_opus_4_7_with_date_suffix(self):
        """Model strings from JSONL often have date suffixes."""
        p = get_pricing("claude-opus-4-7-20260215")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_prefix_match(self):
        # A model name with a suffix should still match the base
        p = get_pricing("claude-sonnet-4-6-20260401")
        self.assertEqual(p["input"], 3.00)
        self.assertEqual(p["output"], 15.00)

    def test_substring_match_opus(self):
        p = get_pricing("new-opus-5-model")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_substring_match_sonnet(self):
        p = get_pricing("custom-sonnet-variant")
        self.assertEqual(p["input"], 3.00)
        self.assertEqual(p["output"], 15.00)

    def test_substring_match_haiku(self):
        p = get_pricing("experimental-haiku-fast")
        self.assertEqual(p["input"], 1.00)
        self.assertEqual(p["output"], 5.00)

    def test_substring_match_case_insensitive(self):
        p = get_pricing("Claude-Opus-Next")
        self.assertEqual(p["input"], 5.00)

    def test_prefix_takes_precedence_over_substring(self):
        # Exact prefix match should win over substring fallback
        p = get_pricing("claude-opus-4-6-preview")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_unknown_model_returns_none(self):
        self.assertIsNone(get_pricing("glm-5.1"))
        self.assertIsNone(get_pricing("gpt-4o"))
        self.assertIsNone(get_pricing("some-unknown-model"))

    def test_none_model_returns_none(self):
        self.assertIsNone(get_pricing(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(get_pricing(""))


class TestCalcCost(unittest.TestCase):
    def test_basic_cost_calculation(self):
        # 1M input tokens of Sonnet at $3/MTok = $3.00
        cost = calc_cost("claude-sonnet-4-6", 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(cost, 3.00)

    def test_output_tokens(self):
        # 1M output tokens of Sonnet at $15/MTok = $15.00
        cost = calc_cost("claude-sonnet-4-6", 0, 1_000_000, 0, 0)
        self.assertAlmostEqual(cost, 15.00)

    def test_cache_read_discount(self):
        # Cache read = 10% of input price
        # 1M cache_read of Opus at $5 * 0.10 = $0.50
        cost = calc_cost("claude-opus-4-6", 0, 0, 1_000_000, 0)
        self.assertAlmostEqual(cost, 0.50)

    def test_cache_creation_premium(self):
        # Cache creation = 125% of input price
        # 1M cache_creation of Opus at $5 * 1.25 = $6.25
        cost = calc_cost("claude-opus-4-6", 0, 0, 0, 1_000_000)
        self.assertAlmostEqual(cost, 6.25)

    def test_combined_cost(self):
        cost = calc_cost("claude-haiku-4-5",
                         inp=500_000, out=100_000,
                         cache_read=200_000, cache_creation=50_000)
        expected = (
            500_000 * 1.00 / 1_000_000 +   # input
            100_000 * 5.00 / 1_000_000 +    # output
            200_000 * 1.00 * 0.10 / 1_000_000 +  # cache read
            50_000 * 1.00 * 1.25 / 1_000_000     # cache creation
        )
        self.assertAlmostEqual(cost, expected)

    def test_zero_tokens(self):
        cost = calc_cost("claude-opus-4-6", 0, 0, 0, 0)
        self.assertEqual(cost, 0.0)

    def test_unknown_model_costs_zero(self):
        cost = calc_cost("glm-5.1", 1_000_000, 500_000, 100_000, 50_000)
        self.assertEqual(cost, 0.0)

    def test_non_anthropic_model_costs_zero(self):
        cost = calc_cost("gpt-4o", 1_000_000, 500_000, 0, 0)
        self.assertEqual(cost, 0.0)


class TestFmt(unittest.TestCase):
    def test_millions(self):
        self.assertEqual(fmt(1_500_000), "1.50M")
        self.assertEqual(fmt(1_000_000), "1.00M")

    def test_thousands(self):
        self.assertEqual(fmt(1_500), "1.5K")
        self.assertEqual(fmt(1_000), "1.0K")

    def test_small_numbers(self):
        self.assertEqual(fmt(999), "999")
        self.assertEqual(fmt(0), "0")


class TestFmtCost(unittest.TestCase):
    def test_formatting(self):
        self.assertEqual(fmt_cost(3.0), "$3.0000")
        self.assertEqual(fmt_cost(0.0001), "$0.0001")
        self.assertEqual(fmt_cost(0), "$0.0000")


class TestPricingConsistency(unittest.TestCase):
    """Ensure CLI pricing matches known Anthropic API rates."""

    def test_opus_pricing(self):
        for model in ("claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5"):
            p = get_pricing(model)
            self.assertEqual(p["input"], 5.00, f"{model} input price wrong")
            self.assertEqual(p["output"], 25.00, f"{model} output price wrong")

    def test_sonnet_pricing(self):
        for model in ("claude-sonnet-4-7", "claude-sonnet-4-6", "claude-sonnet-4-5"):
            p = get_pricing(model)
            self.assertEqual(p["input"], 3.00, f"{model} input price wrong")
            self.assertEqual(p["output"], 15.00, f"{model} output price wrong")

    def test_haiku_pricing(self):
        for model in ("claude-haiku-4-7", "claude-haiku-4-6", "claude-haiku-4-5"):
            p = get_pricing(model)
            self.assertEqual(p["input"], 1.00, f"{model} input price wrong")
            self.assertEqual(p["output"], 5.00, f"{model} output price wrong")


class TestDashboardNoBrowser(unittest.TestCase):
    """The VS Code extension passes --no-browser; CLI users get a browser."""

    def test_no_browser_suppresses_webbrowser(self):
        with mock.patch.object(cli, "cmd_scan"), \
             mock.patch("dashboard.serve") as mock_serve, \
             mock.patch("webbrowser.open") as mock_open, \
             redirect_stdout(io.StringIO()):
            cli.cmd_dashboard(host="127.0.0.1", port=9999, no_browser=True)
            mock_open.assert_not_called()
            mock_serve.assert_called_once()


class TestDashboardArgumentsAreRejectedLikeEveryOther(unittest.TestCase):
    """`dashboard`'s own three values used to leave as a Python traceback.

    Every other bad argument prints one line and exits 1 — `--source must be one
    of: claude, codex, all`, ``unknown argument for `today`: --port`` — because
    `main` catches the ValueError `validate_flags` and `validate_source` raise.
    `cmd_dashboard` raised the same kind of ValueError from outside that
    boundary, so `dashboard --port 8O80` answered with a 17-line stack trace
    naming internal files, and the out-of-range ports were not checked here at
    all: they reached `bind()` as an OverflowError *after* "Scanning in the
    background..." had printed, which reads as a scan failure rather than a typo.
    """

    def _run(self, argv, environ=None):
        """Drive `cli.main()` with the dashboard stubbed out.

        Returns (exit code, printed output, [(name, kwargs)]). The stub is what
        makes "the command never ran" assertable, and it stands in for the
        background scan thread and the blocking server, neither of which a test
        may start. HOST/PORT are cleared unless the case is about them — they
        are the defaults these arguments fall back to, and a developer with
        either exported would otherwise get different results than CI.
        """
        calls = []

        def stub(*args, **kwargs):
            calls.append(("cmd_dashboard", kwargs))

        buffer = io.StringIO()
        # stderr is merged into the same buffer below: these assertions are
        # about the MESSAGE, not the stream it arrives on, and the CLI's
        # diagnostics moved to stderr when `url`'s stdout was narrowed to a URL
        # or nothing. Tests that ARE about the stream capture the two streams
        # separately -- see tests/test_cli_streams.py.
        with mock.patch.dict(os.environ, environ or {}), \
             mock.patch.object(cli, "cmd_dashboard", stub), \
             mock.patch.object(cli.sys, "argv", ["cli.py"] + argv), \
             redirect_stdout(buffer), redirect_stderr(buffer):
            for name in ("HOST", "PORT"):
                if name not in (environ or {}):
                    os.environ.pop(name, None)
            try:
                cli.main()
                code = 0
            except SystemExit as exit_code:
                code = exit_code.code or 0
        return code, buffer.getvalue(), calls

    def test_a_mistyped_port_is_one_line_rather_than_a_traceback(self):
        """The finding's own scenario: `--port 8O80`, letter O for zero."""
        code, output, calls = self._run(["dashboard", "--port", "8O80", "--no-browser"])
        self.assertEqual(code, 1)
        self.assertEqual(len(output.strip().splitlines()), 1,
                         "more than the one line every other bad argument gets")
        self.assertIn("--port", output, "the message never named the flag")
        self.assertIn("8O80", output)
        self.assertEqual(calls, [], "the dashboard started anyway")

    def test_a_non_loopback_host_is_one_line_rather_than_a_traceback(self):
        code, output, calls = self._run(["dashboard", "--host", "8.8.8.8", "--no-browser"])
        self.assertEqual(code, 1)
        self.assertEqual(len(output.strip().splitlines()), 1, output)
        self.assertIn("Refusing non-loopback", output)
        self.assertEqual(calls, [])

    def test_an_unknown_surface_is_one_line_rather_than_a_traceback(self):
        code, output, calls = self._run(["dashboard", "--surface", "bogus", "--no-browser"])
        self.assertEqual(code, 1)
        self.assertEqual(len(output.strip().splitlines()), 1, output)
        self.assertIn("surface must be one of", output)
        self.assertEqual(calls, [])

    def test_a_port_outside_the_range_is_rejected_before_anything_runs(self):
        """These never reached a check at all — `bind()` raised OverflowError,
        which is not a ValueError, so translating `cmd_dashboard`'s exception
        would not have covered them either. `--port -1` cannot be spelled with a
        space (a value starting with `-` is a dangling flag), hence the `=` form.
        """
        for argv in (["dashboard", "--port=99999", "--no-browser"],
                     ["dashboard", "--port=-1", "--no-browser"],
                     ["dashboard", "--port", "65536", "--no-browser"]):
            with self.subTest(argv=argv):
                code, output, calls = self._run(argv)
                self.assertEqual(code, 1, output)
                self.assertIn("65535", output)
                self.assertEqual(calls, [], "the background scan thread started")

    def test_port_zero_is_rejected_rather_than_quietly_meaning_8080(self):
        """`dashboard.serve` does `port = port or int(os.environ.get("PORT", …))`
        and 0 is falsy, so `--port 0` never asked the kernel for an ephemeral
        port — it listened on 8080 (verified: with PORT=8199 exported, `--port 0`
        printed "Dashboard listening at http://127.0.0.1:8199"). Rejecting it is
        the honest answer; silently serving somewhere else is not."""
        code, output, calls = self._run(["dashboard", "--port", "0", "--no-browser"])
        self.assertEqual(code, 1, output)
        self.assertIn("--port", output)
        self.assertEqual(calls, [])

    def test_the_rejection_precedes_the_scan_root_warnings(self):
        """The port is judged before anything prints, so the one line is the
        whole output — the tracebacks used to arrive underneath the scan-root
        warnings and the background scan's own chatter."""
        code, output, calls = self._run(
            ["dashboard", "--projects-dir", "/definitely/not/here", "--port", "abc"])
        self.assertEqual(code, 1)
        self.assertEqual(len(output.strip().splitlines()), 1, output)
        self.assertNotIn("Warning", output)
        self.assertEqual(calls, [])

    def test_a_bad_environment_default_names_the_variable_not_the_flag(self):
        """`PORT` is where the value came from when no `--port` was given, and a
        message blaming a flag the user never typed sends them looking in the
        wrong place."""
        code, output, calls = self._run(["dashboard", "--no-browser"],
                                        environ={"PORT": "abc"})
        self.assertEqual(code, 1, output)
        self.assertIn("PORT", output)
        self.assertNotIn("--port", output)
        self.assertEqual(calls, [])

    def test_the_message_cannot_carry_terminal_escapes(self):
        """The value is echoed back, and it is the user's own argv — but every
        other message on this path goes through `terminal_safe`, and one that
        does not is where a pasted argument gets to move the cursor."""
        code, output, _ = self._run(["dashboard", "--port=\x1b[31m9", "--no-browser"])
        self.assertEqual(code, 1)
        self.assertNotIn("\x1b", output)
        self.assertIn("\\x1b", output)

    def test_every_shipped_dashboard_invocation_still_starts(self):
        """Over-rejection breaks the image and the extension on the next release:
        Dockerfile's CMD and the VS Code extension's spawn args both come
        through here, and 65535 is a legal port that an off-by-one would refuse.
        """
        for argv in (["dashboard"],
                     ["dashboard", "--no-browser"],
                     ["dashboard", "--no-browser", "--host", "127.0.0.1",
                      "--port", "8080", "--surface", "vscode"],
                     ["dashboard", "--host", "localhost", "--port", "65535"],
                     ["dashboard", "--port=1"]):
            with self.subTest(argv=argv):
                code, output, calls = self._run(argv)
                self.assertEqual(code, 0, output)
                self.assertEqual(len(calls), 1, output)

    def test_the_library_contract_still_raises_before_anything_starts(self):
        """The translation belongs to `main`, not to `cmd_dashboard`: a caller
        importing it gets the exception, not a printed line and an exit.

        The scan and the server are stubbed because the check being absent is
        exactly what this asserts — an unvalidated `cmd_dashboard` would start a
        real background scan of the developer's own transcripts and bind a real
        socket, so the failure mode has to be a failed assertion, not a test that
        writes to `~/.claude/usage.db`. `serve` never being called is the other
        half of the claim: nothing runs before the arguments are judged."""
        with mock.patch.object(cli, "cmd_scan"), \
             mock.patch("dashboard.serve") as mock_serve, \
             redirect_stdout(io.StringIO()):
            for kwargs in ({"port": "abc"}, {"port": 99999}, {"port": 0},
                           {"host": "8.8.8.8"}, {"surface": "bogus"}):
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(ValueError):
                        cli.cmd_dashboard(no_browser=True, **kwargs)
            mock_serve.assert_not_called()

    def test_the_validator_returns_what_the_dashboard_should_bind(self):
        """One definition, used by `main` for the message and by `cmd_dashboard`
        for the values — `localhost` resolves to the numeric address here for
        the same reason `validate_bind_host` does it."""
        self.assertEqual(cli.validate_dashboard_args(host="localhost", port="8123"),
                         ("127.0.0.1", 8123, None))
        with mock.patch.dict(os.environ, {"HOST": "127.0.0.1", "PORT": "9001"}):
            self.assertEqual(cli.validate_dashboard_args(surface="vscode"),
                             ("127.0.0.1", 9001, "vscode"))


class TestPerCommandHelpIsAnswered(unittest.TestCase):
    """`cli.py <command> --help` is the first thing a newcomer types.

    It used to exit 1 with ``unknown argument for `scan`: --help``, because
    `--help` appears in no command's COMMAND_FLAGS entry and `validate_flags`
    correctly judged it unknown. The top-level `cli.py --help` worked, so the
    gap was invisible to anyone who already knew the tool — and it is exactly
    the reader a public repository gets most of.

    The pair of assertions here is the whole point: answering `--help` must not
    have widened the door. An argument the command genuinely does not read is
    still an error, including a bare positional, which is the shape that once
    printed a complete, correctly formatted *Claude* report under
    `today codex` and exited 0.
    """

    def _run(self, argv):
        buffer = io.StringIO()
        # stderr is merged into the same buffer below: these assertions are
        # about the MESSAGE, not the stream it arrives on, and the CLI's
        # diagnostics moved to stderr when `url`'s stdout was narrowed to a URL
        # or nothing. Tests that ARE about the stream capture the two streams
        # separately -- see tests/test_cli_streams.py.
        with mock.patch.object(cli.sys, "argv", ["cli.py"] + argv), \
             redirect_stdout(buffer), redirect_stderr(buffer):
            code = 0
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code or 0
        return code, buffer.getvalue()

    def test_every_command_answers_help_with_the_banner(self):
        for command in sorted(cli.COMMANDS):
            for flag in ("--help", "-h", "help"):
                with self.subTest(command=command, flag=flag):
                    code, out = self._run([command, flag])
                    self.assertEqual(code, 0, f"{command} {flag} exited {code}")
                    self.assertIn("Claude Code Usage Dashboard", out)

    def test_an_unknown_argument_is_still_rejected(self):
        for argv in (["today", "--sourcex", "codex"], ["scan", "--bogus"],
                     ["today", "codex"], ["dashboard", "9000"]):
            with self.subTest(argv=argv):
                code, out = self._run(argv)
                self.assertEqual(code, 1, f"{argv} exited {code}: {out[:80]}")

    def test_help_is_not_in_any_command_flag_table(self):
        """The table keeps meaning "flags this command READS".

        Answering --help by adding it to all six entries would make the table
        claim each command reads it, and an unknown flag is an error precisely
        because the table is exhaustive about what is read.
        """
        for command, flags in cli.COMMAND_FLAGS.items():
            self.assertNotIn("--help", flags, command)
            self.assertNotIn("-h", flags, command)


class TestAHelpWordGivenAsAValueIsAValue(unittest.TestCase):
    """`main` scanned every token for a help word, with no notion of which had
    already been consumed as a flag's value.

    So a value that happened to spell one printed the banner and exited 0 with
    the command never run — the silent-drop-at-exit-0 class the rest of that file
    legislates against. `scan --projects-dir help` reported success having
    scanned nothing, which a wrapper passing `--projects-dir "$DIR"` cannot tell
    from a scan, and `today --source help` did the same rather than reaching
    `validate_source` — while `today --source=help` correctly exited 1. Two
    spellings of one flag, two answers.

    `flag_positions` is now the single definition of which token is a value,
    shared with `validate_flags`, so the help check and the flag check can no
    longer disagree about it.
    """

    def test_a_help_word_in_a_value_position_is_judged_as_a_value(self):
        run = run_cli(["today", "--source", "help"])
        self.assertEqual(run.code, 1, run.out + run.err)
        # stderr: a rejected argument is a diagnostic, and this command's
        # stdout is its report. `run_cli` captures the two separately, so
        # naming the stream here is the assertion, not an accident.
        self.assertIn("--source must be one of", run.err)
        self.assertEqual(run.out, "", "an error is not report output")
        self.assertEqual(run.calls, [], "the report ran anyway")

    def test_the_two_spellings_of_one_flag_agree(self):
        """The equals form was always right; this is the space form catching up,
        asserted as equality so neither can drift alone."""
        space = run_cli(["today", "--source", "help"])
        equals = run_cli(["today", "--source=help"])
        self.assertEqual((space.code, space.out, space.err),
                         (equals.code, equals.out, equals.err))

    def test_a_repeatable_flags_value_reaches_the_command(self):
        """`help` is a directory name like any other, and the scan is what has
        to decide it does not exist."""
        run = run_cli(["scan", "--projects-dir", "help"])
        self.assertEqual(run.code, 0, run.out + run.err)
        self.assertEqual([name for name, _ in run.calls], ["cmd_scan"], run.out)

    def test_a_help_word_after_another_flag_is_still_answered(self):
        """The landmine for the narrow fix: recognising help only in the first
        token answers every one of these with `unknown argument` instead. It is
        what a reader types after already starting to build the command line."""
        for argv in (["dashboard", "--no-browser", "--help"],
                     ["scan", "--projects-dir", "/tmp", "-h"],
                     ["url", "--open", "help"]):
            with self.subTest(argv=argv):
                run = run_cli(argv)
                self.assertEqual(run.code, 0, run.out + run.err)
                self.assertIn("Claude Code Usage Dashboard", run.out)
                self.assertEqual(run.calls, [], "the command ran anyway")


class TestTheScanRootWarningReachesOnlyTheCommandsThatScan(unittest.TestCase):
    """`main` resolved the scan roots for all six commands, on stdout.

    Four of them never read a transcript directory, and on one the warning broke
    a caller rather than merely puzzling one: `url`'s stdout is a single URL, and
    it is documented as the way back in after losing the link, so
    `open "$(claude-usage url)"` received the warning, a newline, then the link.
    A root named in CLAUDE_USAGE_PROJECTS_DIRS that is not currently mounted is
    the ordinary way it fires, and it needs no flag — the four read commands do
    not accept `--projects-dir` at all.
    """

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        # Inside a directory that exists, so its absence is this test's doing
        # rather than a guess about the filesystem it runs on.
        self.missing = os.path.join(holder.name, "not-mounted")

    def _run(self, argv, **kwargs):
        with mock.patch.dict(os.environ,
                             {"CLAUDE_USAGE_PROJECTS_DIRS": self.missing}):
            return run_cli(argv, **kwargs)

    def test_a_read_command_warns_about_scan_roots_on_neither_stream(self):
        for argv in (["url"], ["today"], ["week"], ["stats"]):
            with self.subTest(argv=argv):
                run = self._run(argv)
                self.assertEqual(run.code, 0, run.out + run.err)
                self.assertNotIn(self.missing, run.out)
                self.assertNotIn(self.missing, run.err)

    def test_the_url_a_shell_substitutes_is_exactly_the_url(self):
        """The reported break, at the boundary that broke."""
        link = "http://127.0.0.1:21754/#token=0123456789abcdef"
        run = self._run(["url"], printing={"cmd_url": link})
        self.assertEqual(run.code, 0, run.err)
        self.assertEqual(run.out.splitlines(), [link])

    def test_the_scanning_commands_keep_the_warning_on_stderr(self):
        """It is about their own arguments, so they keep it — on stderr, where
        `require_db`'s empty-database notice already goes, because a diagnostic
        is not report output."""
        for argv in (["scan"], ["dashboard", "--no-browser"]):
            with self.subTest(argv=argv):
                run = self._run(argv)
                self.assertEqual(run.code, 0, run.out + run.err)
                self.assertIn(self.missing, run.err)
                self.assertIn("not found, skipping", run.err)
                self.assertNotIn(self.missing, run.out)


class TestTheCacheTierClampCannotCreditMoneyBack(unittest.TestCase):
    """`calc_cost_parts` clamps the 1-hour slice to its own total, in Python.

    Cache writes bill at two rates and `turns.cache_creation_1h_tokens` stores
    only the 1-hour part, so the 5-minute part is the remainder. The two columns
    are summed independently by SQL, so a 1-hour figure exceeding its own total
    makes that remainder negative — and a negative quantity at a positive rate
    credits money back.

    This existed only in JavaScript. `test_dashboard_js.py` guards the JS twin
    with `TestCacheWriteTiersArePricedSeparately`, and its Python parity cases
    all pass a 1-hour slice at or below the total, so no case discriminated
    the clamp on this side: a mutation
    audit removed it and the whole 1709-test suite still passed. Same rule, two
    implementations, one test — this is the other one.
    """

    #: The audit's own case. The clamp is what keeps this at the 1-hour rate for
    #: the 1000 tokens that exist, rather than billing 999999 of them and
    #: crediting back a negative 5-minute remainder.
    MODEL, CACHE_CREATION, CACHE_1H = "claude-opus-5", 1000, 999999

    def test_a_1h_slice_larger_than_its_own_total_does_not_credit_money_back(self):
        from pricing import calc_cost_parts
        parts = calc_cost_parts(self.MODEL, 0, 0, 0, self.CACHE_CREATION, self.CACHE_1H)
        for key, value in parts.items():
            with self.subTest(part=key):
                self.assertGreaterEqual(value, 0.0, "a cost part went negative")
        # 1000 tokens, all of them long-lived: the 1h rate and nothing else.
        expected = self.CACHE_CREATION * get_pricing(self.MODEL)["cache_write_1h"] / 1_000_000
        self.assertAlmostEqual(parts["cache_creation"], expected, places=12)

    def test_the_clamped_total_agrees_with_calc_cost(self):
        """The parts and the single-figure total must not disagree about it."""
        from pricing import calc_cost_parts
        parts = calc_cost_parts(self.MODEL, 0, 0, 0, self.CACHE_CREATION, self.CACHE_1H)
        total = calc_cost(self.MODEL, 0, 0, 0, self.CACHE_CREATION, self.CACHE_1H)
        self.assertAlmostEqual(sum(parts.values()), total, places=12)

    def test_an_ordinary_split_is_untouched_by_the_clamp(self):
        """The control. Without it this class would pass against a clamp that
        zeroed every cache figure, which would be a far worse defect."""
        from pricing import calc_cost_parts
        rates = get_pricing(self.MODEL)
        parts = calc_cost_parts(self.MODEL, 0, 0, 0, 1000, 400)
        expected = (600 * rates["cache_write"] + 400 * rates["cache_write_1h"]) / 1_000_000
        self.assertAlmostEqual(parts["cache_creation"], expected, places=12)


if __name__ == "__main__":
    unittest.main()
