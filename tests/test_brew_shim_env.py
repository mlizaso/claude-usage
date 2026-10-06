"""The Homebrew shim's environment allowlist, against what the product reads.

`Formula/codex-claude-usage.rb` runs the interpreter through `/usr/bin/env -i`, so the
child process sees only the variables the shim names. That stripping is
deliberate (commit 4bd948e, "fix(security): harden private dashboard runtime")
and must stay. What is not deliberate is the *membership* of the list, which was
written when three product variables existed and was never revisited as more
arrived — and a variable missing from it does not fail, it silently does
nothing, which is the worst shape a configuration bug can take.

Measured 2026-08-10 against a keg replica of the shim (the formula's own heredoc,
only the two `#{...}` interpolations substituted), on a one-turn database of
1M in / 1M out at `claude-opus-5` with a `CODEX_CLAUDE_USAGE_RATES` file naming
$1.00/$1.00:

    shim  `stats`, override set : Est. total cost:  $30.0000
    clone `stats`, override set : Est. total cost:  $2.0000
    clone `stats`, no override  : Est. total cost:  $30.0000

The shim's answer is bit-identical to the no-override answer: the file was never
read, nothing said so, and the reported money was 15x. `CODEX_CLAUDE_USAGE_RATES` has
no flag equivalent anywhere in `cli.COMMAND_FLAGS`, so for a Homebrew user the
documented override was not merely dropped, it was unreachable. Two more legs
reproduced the same day: `CODEX_CLAUDE_USAGE_PROJECTS_DIRS` gave "New files: 1" through
the shim against "New files: 2" from a clone, and `TZ=Pacific/Auckland` bucketed a
2026-08-09T14:00:00Z turn on 2026-08-09 through the shim against 2026-08-10 from
a clone (with `TZ=UTC` the clone returns to 2026-08-09, which is the control
proving the clone honours TZ rather than ignoring it too).

So the tables below are the point of this file, and they are hardcoded on BOTH
sides on purpose. A test that *derived* the allowlist from the product's env
reads would be worse than none: written the obvious way it misses
`CODEX_CLAUDE_USAGE_RATES` and `CODEX_CLAUDE_USAGE_PROJECTS_DIRS` outright, because both are
read through a module constant rather than a string literal, and it would have
certified the broken allowlist; written well enough to resolve those constants it
drags in `CODEX_CLAUDE_USAGE_ALLOW_CONTAINER_BIND` — the single gate that lets
`validate_bind_host` return a non-loopback address — and turns "the product reads
it" into a proof obligation that the shim forward it. That inverts the commit the
stripping came from. The derivation below is therefore used only to prove the
tables are COMPLETE, never to populate them: a new variable fails the suite by
appearing in neither table, which forces a human to classify it.

This is the same shape as the packaging surfaces in `tests/test_web_assets.py` —
parse the artefact, compare against a curated expectation — and for the same
reason: a name found somewhere in a file is not the same claim as a name that
takes effect.
"""

import ast
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FORMULA = REPO_ROOT / "Formula" / "codex-claude-usage.rb"

# Forwarded because the product reads them and the reader is meant to set them.
# The reason matters as much as the name: this is the table a maintainer reads
# when deciding where a new variable goes.
FORWARDED = {
    "CODEX_CLAUDE_USAGE_DOCKER": "opts out of automatic local container usage collection",
    "HOST": "the dashboard's bind address, documented beside PORT",
    "PORT": "the dashboard's port, documented beside HOST",
    "CODEX_CLAUDE_USAGE_DB": "relocates usage.db; the only one that was never dropped",
    "CODEX_CLAUDE_USAGE_LIVE_LIMITS": (
        "opts into the live quota query (live_limits.ENABLE_ENV). Forwarded "
        "because it is the switch the reader sets; dropping it would silently "
        "return them to the stale cache they turned this on to escape"
    ),
    "CODEX_CLAUDE_USAGE_LIMITS_URL": (
        "overrides the quota endpoint (live_limits.ENDPOINT_ENV). Forwarded "
        "for the same reason as CODEX_CLAUDE_USAGE_RATES: it names something the "
        "reader chose, and dropping it silently queries somewhere else"
    ),
    "CODEX_CLAUDE_USAGE_THRESHOLDS": (
        "relocates the per-window alert thresholds (limits_core.THRESHOLDS_ENV). "
        "Forwarded for the same reason as CODEX_CLAUDE_USAGE_DB: it names a file the "
        "reader chose, and dropping it silently moves their alert settings to "
        "the default path, where a threshold they set would appear to vanish"
    ),
    "LIMITS_PORT": (
        "the standalone limits server's port, the direct analogue of PORT for "
        "the other server. Dropping it would silently bind 8081 after the "
        "reader asked for something else -- the failure PORT's own entry "
        "exists to prevent"
    ),
    "CODEX_CLAUDE_USAGE_RATES": (
        "user-supplied prices (pricing.RATE_OVERRIDE_ENV). Dropping it reported "
        "$30.0000 where a clone reported $2.0000, and there is no --rates flag "
        "to fall back to"
    ),
    "CODEX_CLAUDE_USAGE_PROJECTS_DIRS": (
        "extra transcript roots (scanner.EXTRA_PROJECTS_DIRS_ENV), named by "
        "README and by the usage banner the brew binary itself prints"
    ),
    "CLAUDE_CONFIG_DIR": "relocates ~/.claude, so the plan panel reads the right install",
    "CODEX_CLAUDE_USAGE_CONFIG": "points at a specific .claude.json, same class as above",
}

# Forwarded although no line of Python reads them: the interpreter, libc and
# SQLite's `localtime` modifier do.
FORWARDED_OS = {
    "LANG": "locale, for the interpreter's own text handling",
    "LANGUAGE": "locale",
    "LC_ALL": "locale",
    "LC_CTYPE": "locale",
    "TZ": (
        "decides the local calendar day every report is bucketed on (invariant "
        "4). No Python line reads it, so no derivation from the source could "
        "ever propose it; libc and SQLite's `localtime` consume it"
    ),
}

# Deliberately withheld. Every one of these IS read by the product; withholding
# them is the decision, and it is recorded here so the next reader does not
# rediscover it as a bug.
WITHHELD = {
    "ProgramFiles": "Windows-only Docker installation root; Homebrew does not use it",
    "CODEX_CLAUDE_USAGE_API_TOKEN": (
        "credential-shaped, and omitted by 4bd948e itself — the commit that "
        "wrote this allowlist, at a tree where the variable already existed. "
        "The product degrades safely without it: dashboard.py mints a fresh "
        "secrets.token_urlsafe(32) and delivers it through the dashboard-url "
        "file that `codex-claude-usage url` reads"
    ),
    "CODEX_CLAUDE_USAGE_HEALTH_TOKEN": "credential-shaped, same commit, same reasoning",
    "CODEX_CLAUDE_USAGE_TOKEN_COMMAND": (
        "names a command that PRINTS a credential (live_limits.TOKEN_COMMAND_ENV), "
        "so forwarding it would let a packaged launcher run an arbitrary command "
        "from the reader's environment and hand the result to a network request. "
        "Withheld for the same reason as the token it produces, and with the "
        "same remedy: export it and run the module directly"
    ),
    "CODEX_CLAUDE_USAGE_OAUTH_TOKEN": (
        "credential-shaped, and the most sensitive variable this product reads "
        "-- it can act as the user against Anthropic. Withheld for the same "
        "reason as the two above: the shim's `env -i` allowlist is the boundary "
        "between the reader's shell and a packaged launcher, and a credential "
        "that crosses it silently is one nobody decided to hand over. Someone "
        "who wants live limits under Homebrew can export it and run the module "
        "directly"
    ),
    "CODEX_CLAUDE_USAGE_ALLOW_CONTAINER_BIND": (
        "the only thing that lets validate_bind_host return a non-loopback "
        "address. Docker-only, injected by scripts/run-docker.sh inside the "
        "container; forwarding it through a HOST shim weakens a bind guard for "
        "no benefit"
    ),
    "CODEX_CLAUDE_USAGE_SUPPRESS_AUTH_URL": (
        "Docker-only, same source. It silences the printed authenticated URL, "
        "which for a brew user is their only on-screen link"
    ),
    "CODEX_CLAUDE_USAGE_DOCKER_CONTAINER": (
        "Docker-only, injected by scripts/run-docker.sh so recovery advice can "
        "enter the managed app container and reach /data. Forwarding a shell "
        "value through Homebrew could only put an unrelated container name in "
        "an otherwise local command"
    ),
    "ANTHROPIC_API_KEY": (
        "a credential. account.py checks it for PRESENCE only and never reads "
        "the value, so the whole cost of stripping it is that an API-key "
        "install renders as a subscription one — a wrong label on a localhost "
        "page, which is cheaper than putting a live key in the child's "
        "environment"
    ),
    "ANTHROPIC_AUTH_TOKEN": "a credential, read the same presence-only way",
}

# Set unconditionally rather than forwarded. PATH is REPLACED (it points at the
# python@3.13 keg), so it is not a passthrough at all and must never be treated
# as one.
UNCONDITIONAL = {"HOME", "PATH", "TMPDIR", "CODEX_CLAUDE_USAGE_INVOKED_AS"}


def _shim_source(text=None):
    """The generated `bin/codex-claude-usage`, rendered from the formula's heredoc.

    `text` overrides the file so a spelling the repo must never carry can still
    be run through the real parser, the way `_formula_install_args` and friends
    in test_web_assets.py take theirs.

    Ruby's `<<~EOS` dedents to the least-indented line, interpolates `#{...}`,
    and joins a trailing backslash to the next line — the formula's `exec` is
    four source lines and one shipped line. All three are reproduced here, and
    `test_the_python_render_matches_ruby` proves the reproduction rather than
    asserting it: measured 2026-08-10, this function's output is byte-identical
    to `ruby -e` evaluating the same heredoc.
    """
    if text is None:
        text = FORMULA.read_text(encoding="utf-8")
    raw = re.search(r'\(bin/"codex-claude-usage"\)\.write <<~EOS\n(.*?)^\s*EOS$',
                    text, re.S | re.M)
    if raw is None:
        raise AssertionError("Formula no longer writes bin/codex-claude-usage from a "
                             "<<~EOS heredoc; this parser must be updated")
    body = textwrap.dedent(raw.group(1)).replace("\\\n", "")
    body = re.sub(r'#\{formula_opt_bin\("([^"]+)"\)\}', r"/OPT/\1", body)
    return body.replace("#{libexec}", "/LIBEXEC")


def _forwarded_names(body=None):
    """(unconditional, conditional) variable names the shim lets through.

    Conditional names come from two places, because one of them cannot live in
    the other: the passthrough list the `for` loop walks, and any `safe_env+=`
    outside that loop — which is where TZ has to sit. See
    `test_an_empty_tz_survives_the_shim` for why it cannot join the loop.
    """
    if body is None:
        body = _shim_source()
    unconditional = set(re.findall(r'^\s*"([A-Z_][A-Z0-9_]*)=', body, re.M))
    conditional = set()
    array = re.search(r"^passthrough=\(\n(.*?)^\)$", body, re.S | re.M)
    if array:
        conditional |= set(re.findall(r"[A-Z_][A-Z0-9_]*", array.group(1)))
    inline = re.search(r"^\s*for name in ([^;]+); do", body, re.M)
    if inline and "passthrough" not in inline.group(1):
        conditional |= set(re.findall(r"[A-Z_][A-Z0-9_]*", inline.group(1)))
    conditional |= set(re.findall(r'safe_env\+=\("([A-Z_][A-Z0-9_]*)=', body))
    return unconditional - conditional, conditional


def _env_names_read(path):
    """Every environment variable name read in one package module.

    Resolving module constants is the whole job. `os.environ.get("HOST")` is the
    easy shape; `environ.get(RATE_OVERRIDE_ENV, "")` and
    `environ.get(name) for name in _API_KEY_ENV_VARS` are the two that a grep for
    string literals cannot see, and between them they hide four names — two of
    which are the ones this file exists for.
    """
    module_name = ("codex_claude_usage" if path.stem == "__init__"
                   else f"codex_claude_usage.{path.stem}")
    module = importlib.import_module(module_name)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {child: node for node in ast.walk(tree)
               for child in ast.iter_child_nodes(node)}

    def resolve(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.Name):
            value = getattr(module, node.id, None)
            if isinstance(value, str):
                return [value]
            if isinstance(value, (tuple, list, set, frozenset)):
                return sorted(value)
            walk = parents.get(node)  # a loop variable: find what it iterates
            while walk is not None:
                if (isinstance(walk, (ast.For, ast.comprehension))
                        and isinstance(getattr(walk, "target", None), ast.Name)
                        and walk.target.id == node.id):
                    return resolve(walk.iter)
                for generator in getattr(walk, "generators", []):
                    if (isinstance(generator.target, ast.Name)
                            and generator.target.id == node.id):
                        return resolve(generator.iter)
                walk = parents.get(walk)
        return None

    names, unresolved = set(), []
    for node in ast.walk(tree):
        argument = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "get" and "environ" in ast.unparse(node.func.value):
                argument = node.args[0] if node.args else None
            elif node.func.attr == "getenv" and node.args:
                argument = node.args[0]
        elif (isinstance(node, ast.Subscript)
              and "environ" in ast.unparse(node.value)):
            argument = node.slice
        if argument is None:
            continue
        found = resolve(argument)
        if found is None:
            unresolved.append(f"{path.name}:{node.lineno}  {ast.unparse(node)}")
        else:
            names.update(found)
    return names, unresolved


def _all_env_names_read():
    names, unresolved = set(), []
    for path in sorted((REPO_ROOT / "codex_claude_usage").glob("*.py")):
        found, bad = _env_names_read(path)
        names |= found
        unresolved += bad
    return names, unresolved


class TestTheAllowlistIsTheProductsAllowlist(unittest.TestCase):
    """The parsed formula against the two curated tables. Runs everywhere."""

    def test_the_shim_forwards_exactly_the_classified_passthroughs(self):
        # Equality, not containment: a name added to the formula and to neither
        # table fails here, which is the half that keeps the tables honest. The
        # reverse half is test_every_variable_the_product_reads_is_classified.
        _, conditional = _forwarded_names()
        self.assertEqual(conditional, set(FORWARDED) | set(FORWARDED_OS))

    def test_the_shim_still_sets_home_path_and_tmpdir_itself(self):
        unconditional, _ = _forwarded_names()
        self.assertEqual(unconditional, UNCONDITIONAL)

    def test_the_shim_forwards_nothing_it_deliberately_withholds(self):
        # The assertion carrying the security value. A future "just derive the
        # list from the source" refactor goes red here instead of silently
        # widening the shim.
        _, conditional = _forwarded_names()
        self.assertEqual(conditional & set(WITHHELD), set())

    def test_every_variable_the_product_reads_is_classified(self):
        names, unresolved = _all_env_names_read()
        self.assertEqual(unresolved, [], "an environment read this test cannot "
                                         "resolve to a name; bind it to a "
                                         "module-level constant")
        self.assertEqual(
            names - set(FORWARDED) - set(FORWARDED_OS) - set(WITHHELD) - UNCONDITIONAL,
            set(),
            "a new environment variable must be classified as forwarded or "
            "withheld in tests/test_brew_shim_env.py, and the Homebrew shim "
            "updated to match",
        )

    def test_the_derivation_sees_the_names_a_literal_grep_would_miss(self):
        # Guards the resolver rather than the formula. Drop the constant
        # resolution and these four disappear, taking the two variables this
        # file exists for with them, and every assertion above still passes.
        names, _ = _all_env_names_read()
        for name in ("CODEX_CLAUDE_USAGE_RATES", "CODEX_CLAUDE_USAGE_PROJECTS_DIRS",
                     "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            self.assertIn(name, names)

    def test_nothing_is_withheld_that_the_product_does_not_read(self):
        # Keeps WITHHELD from accumulating fossils: a variable deleted from the
        # product should leave this table too.
        names, _ = _all_env_names_read()
        self.assertEqual(set(WITHHELD) - names, set())

    def test_every_forwarded_product_variable_is_read_by_the_product(self):
        names, _ = _all_env_names_read()
        self.assertEqual(set(FORWARDED) - names, set())

    def test_the_two_tables_do_not_overlap(self):
        self.assertEqual(set(FORWARDED) & set(WITHHELD), set())
        self.assertEqual(set(FORWARDED_OS) & set(WITHHELD), set())

    def test_every_classification_carries_a_reason(self):
        for table in (FORWARDED, FORWARDED_OS, WITHHELD):
            for name, reason in table.items():
                self.assertTrue(reason.strip(), f"{name} has no recorded reason")


@unittest.skipUnless(os.name == "posix", "the shim is a bash script")
class TestTheShimBehaves(unittest.TestCase):
    """The rendered shim, actually executed. This is what proves the mechanism.

    The formula hardcodes the interpreter's *name* (`python3.13`), so the fake
    keg below is a directory holding a symlink of that name pointing at whatever
    interpreter is running the suite — the test stays correct on 3.11 and 3.12.
    """

    def _run(self, env, body=None):
        """Run the real shim with `env` as the parent environment.

        Returns the child's `os.environ` as a dict. The libexec `cli.py` is a
        stub, because what is under test is which variables survive `env -i`,
        not what the CLI does with them.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            (root / "libexec").mkdir()
            (root / "libexec" / "codex_claude_usage").mkdir()
            (root / "bin" / "python3.13").symlink_to(sys.executable)
            (root / "libexec" / "codex_claude_usage" / "__init__.py").write_text(
                "", encoding="utf-8")
            (root / "libexec" / "codex_claude_usage" / "cli.py").write_text(
                "import json, os\nprint(json.dumps(dict(os.environ)))\n",
                encoding="utf-8")
            shim = _shim_source(body)
            shim = shim.replace("/OPT/python@3.13", str(root / "bin"))
            shim = shim.replace("/LIBEXEC", str(root / "libexec"))
            script = root / "codex-claude-usage"
            script.write_text(shim, encoding="utf-8")
            script.chmod(0o755)
            done = subprocess.run([str(script)], capture_output=True, text=True,
                                  encoding="utf-8", env=env, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            return json.loads(done.stdout)

    def _parent_env(self, **extra):
        base = {"HOME": os.path.expanduser("~"), "PATH": "/usr/bin:/bin",
                "TMPDIR": "/tmp"}
        base.update(extra)
        return base

    def test_every_forwarded_variable_reaches_the_interpreter(self):
        names = sorted(set(FORWARDED) | set(FORWARDED_OS))
        sentinels = {name: f"sentinel-{name}" for name in names}
        child = self._run(self._parent_env(**sentinels))
        missing = {name for name in names if child.get(name) != sentinels[name]}
        self.assertEqual(missing, set())

    def test_every_withheld_variable_is_erased(self):
        sentinels = {name: f"sentinel-{name}" for name in WITHHELD}
        child = self._run(self._parent_env(**sentinels))
        self.assertEqual(sorted(set(WITHHELD) & set(child)), [])

    def test_an_unrelated_variable_is_erased(self):
        # The control. Without it the test above could pass on a shim that
        # forwards everything and merely happens not to be asked.
        child = self._run(self._parent_env(SOME_OTHER_SECRET="hunter2"))
        self.assertNotIn("SOME_OTHER_SECRET", child)

    def test_an_empty_tz_survives_the_shim(self):
        # `TZ=` is not the same as TZ unset: POSIX reads an empty TZ as UTC,
        # measured here as tzname ('UTC','UTC') and
        # date('2026-08-09T23:00:00Z','localtime') = 2026-08-09, against
        # ('CET','CEST') and 2026-08-10 with TZ absent. So TZ has to be
        # forwarded on set-vs-unset; the `[[ -n ... ]]` loop the other
        # passthroughs use drops it and silently turns "force UTC" into "use
        # the machine's zone" — a wrong local-day bucket, which is the very
        # dimension invariant 4 is about.
        child = self._run(self._parent_env(TZ=""))
        self.assertIn("TZ", child)
        self.assertEqual(child["TZ"], "")

    def test_an_unset_tz_is_not_invented(self):
        child = self._run(self._parent_env())
        self.assertNotIn("TZ", child)

    @unittest.skipUnless(shutil.which("ruby"), "ruby not available")
    def test_the_python_render_matches_ruby(self):
        # `_shim_source` reimplements three squiggly-heredoc rules in Python.
        # This is the only thing standing between that reimplementation and
        # every behavioural assertion above quietly testing a shim Homebrew
        # would never write.
        raw = re.search(r'\(bin/"codex-claude-usage"\)\.write <<~EOS\n(.*?)^\s*EOS$',
                        FORMULA.read_text(encoding="utf-8"), re.S | re.M).group(1)
        program = ('def formula_opt_bin(x); "/OPT/" + x; end\n'
                   'def libexec; "/LIBEXEC"; end\n'
                   'print(<<~EOS)\n' + raw + 'EOS\n')
        done = subprocess.run(["ruby", "-e", program], capture_output=True,
                              text=True, encoding="utf-8", timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(_shim_source(), done.stdout)


if __name__ == "__main__":
    unittest.main()
