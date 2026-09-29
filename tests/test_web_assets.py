"""Tests for the extracted web assets and the packaging that must ship them.

The dashboard document used to be one 2,067-line string literal inside
`dashboard.py`. It now lives in `web/index.html` + `web/app.css` + the ordered
parts under `web/js/`, and is reassembled at import. Two things can silently break as a result, and
neither shows up in a normal test run:

1. **Reassembly.** If a placeholder is renamed or a file is emptied, the server
   would happily serve a document missing its stylesheet or its entire
   application, with no error.
2. **Packaging.** The qualified package is copied wholesale by some surfaces,
   while the Docker allowlist and VSIX enumerate its files; browser/vendor
   assets also have explicit pyproject data-file lists. A module or asset added
   to the repo but missing from one required surface produces an install that
   imports fine here and fails only on a user's machine. Each surface is parsed
   into the layout it actually declares — a name found *somewhere* in a file is
   not the same claim as a name that ships.

   `.dockerignore` is the fifth and least obvious of them: it is a deny-all
   allowlist (`*`, then `!` exceptions), so it decides what the *build context*
   contains, and a `COPY` naming a file the context does not carry fails the
   build outright. It drifted behind the module split and the web-asset
   extraction and `docker build` stopped working entirely — no test read the
   file, so nothing noticed.

These tests are cheap and guard every supported install layout, including the
`.vsix`.
"""

import importlib
import re
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import dashboard

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
PACKAGE_DIR = REPO_ROOT / "claude_usage"

# Every package module the app imports at runtime, DISCOVERED FROM DISK rather than
# listed. A hardcoded list only guards the modules it already knows about: a new
# module could be added to the repo, imported by the app, and omitted from an
# explicit surface without failing a hand-maintained-list test. Globbing makes
# the guard bite on the commit that adds the module.
#
# proxy.py is excluded deliberately: it is only used by the Docker loopback proxy
# and is not part of the dashboard or the CLI.
NOT_SHIPPED = {"proxy.py"}
RUNTIME_MODULES = sorted(
    p.relative_to(REPO_ROOT).as_posix() for p in PACKAGE_DIR.glob("*.py")
)
# Discovered from disk, not hardcoded: adding a JS part — or anything else under
# web/ — must fail the packaging checks below until every surface ships it,
# which is the whole point of them.
#
# The TOP-LEVEL half of this glob is new, and its absence is exactly how
# `GET /icon.svg` came to 404 on three of the five delivery surfaces. This list
# used to name `web/index.html` and `web/app.css` BY HAND beside a `web/js/*.js`
# glob, so a top-level asset of any other shape was discovered by no test at
# all: the icon shipped to the checkout and the .vsix (where vsce carries the
# extension's own `resources/` copy) and to nothing else, and the surfaces were
# never told about it because nothing asked them to be.
#
# Dotfiles are excluded rather than required. `web/.env` and a stray `.DS_Store`
# are precisely what `test_the_allowlist_does_not_re_admit_the_whole_web_tree`
# forbids the build context to carry, so requiring them to ship would put the
# two rules in direct contradiction — and the way to satisfy this list is to
# delete the stray, not to admit it.
# RECURSIVE, and `*` rather than `*.js` below the top level. The previous shape
# was `web/*` (files) + `web/js/*.js`, which is one directory deep and .js-only
# under it -- so a `web/css/print.css`, a `web/fonts/x.woff2` or a
# `web/js/models.json` was discovered by NO test and would ship to Homebrew
# alone, whose `libexec.install "web"` takes the directory wholesale, while pip,
# Docker and the .vsix name their files one by one. That is precisely the shape
# `GET /icon.svg` shipped in, one directory higher, and the paragraph above is
# the record of it: widening the glob is what closed it then, and the same
# argument reaches the rest of the tree.
WEB_ASSETS = sorted(
    "/".join(("web",) + p.relative_to(WEB_DIR).parts)
    for p in WEB_DIR.rglob("*")
    if p.is_file()
    and not any(part.startswith(".") for part in p.relative_to(WEB_DIR).parts))


def _dockerignore_patterns(text=None):
    """(exclusions, exceptions) from `.dockerignore`, comments and blanks dropped.

    `text` overrides the file, the way `_formula_install_args` and
    `_copy_python_files` below take theirs, so a spelling this repo must never
    carry can still be run through the real parser instead of asserted about.

    Exceptions come back with the leading `!` stripped and no trailing slash, so
    `!vendor/` and `!vendor` compare equal — Docker treats them the same:
    measured 2026-08-10 on Docker 29.5.2, a context of `*` plus either spelling
    shipped every file beneath `sub/`, two levels of nesting included, on
    BuildKit and on the classic builder. The normalisation is not cosmetic, and
    it is why the guards below can spell the bare form once: measured the same
    day, with the `rstrip` dropped a bare `!vendor/` passes
    `test_the_allowlist_does_not_re_admit_the_whole_vendor_tree` outright — the
    key it looks for is `vendor`, the set holds `vendor/`, and `_context_admits`
    then reports the strays excluded while both builders ship them.
    """
    if text is None:
        text = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    entries = [line.strip() for line in text.splitlines()
               if line.strip() and not line.strip().startswith("#")]
    return ([e for e in entries if not e.startswith("!")],
            {e[1:].rstrip("/") for e in entries if e.startswith("!")})


def _dockerfile_copy_sources():
    """Every source path named by a `COPY` in the Dockerfile.

    Comment lines are dropped and backslash continuations are joined, in that
    order, which is what Docker itself does. The current package COPY fits on
    one line, but the parser must remain correct if a future source list wraps;
    dropping comments first matters because the join word-splits, so a source
    name mentioned only in a comment cannot satisfy a packaging assertion.
    """
    text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    text = "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("#"))
    sources = []
    for line in re.sub(r"\\\n", " ", text).splitlines():
        if not line.strip().startswith("COPY "):
            continue
        words = [w for w in line.split()[1:] if not w.startswith("--")]
        sources.extend(words[:-1])  # the final word is the destination
    return sources


def _context_admits(path, exceptions):
    """Is `path` in the build context, given a deny-all `*` plus `exceptions`?

    Three ways in, all three checked against a real `docker build` rather than
    assumed, on both BuildKit and the classic builder:

    1. An exact exception. Neither builder requires a parent directory to be
       re-included first, so `!web/js/00-core.js` stands on its own.
    2. An exception on an ancestor directory, which re-includes the whole
       subtree beneath it, at every depth: measured 2026-08-10 on Docker 29.5.2,
       `*` plus a bare `!sub/` shipped `sub/a.txt`, `sub/nested/b.txt` and
       `sub/n1/n2/c.txt` alike on BuildKit and on the classic builder. This rule
       models the spelling the class below forbids, not one the repo uses —
       measured the same day, no exception in `.dockerignore` names a directory,
       and `vendor/chart.umd.js` is admitted by rule 1. While that holds, the
       ancestor half of this branch cannot admit a single real path, a proper
       ancestor of one being a directory and no exception being one;
       `test_every_exception_names_a_file_that_is_in_the_repo` is that premise,
       asserted. Modelling it anyway is what makes the forbidding bite, and the
       subject of every number below is THE BRANCH, not that premise test — an
       earlier version of this sentence wrote "it" straight after naming the
       test, which made the one measurement it carried false. Narrow the range
       to an exact match and change nothing else:
       `test_either_spelling_of_a_bare_directory_exception_is_caught` fails
       twice, so the branch is asserted directly. Narrow it and put `!web/`
       back: `test_the_allowlist_does_not_re_admit_the_whole_web_tree` falls
       from 4 failures to 1 — the directory is still reported, every stray
       riding in behind it is not. Delete the premise test and put `!web/` back
       instead, and that same test stays at 4, which is what makes the two
       readings distinguishable. All three measured on this tree, 2026-08-10.
    3. `path` is a directory with something admitted beneath it. A directory is
       not an entry that gets copied so much as a container for the entries that
       are, so it materializes carrying exactly its admitted children. This is
       what lets `COPY web ./web` resolve while `web` itself is never re-included
       wholesale — and it is not a technicality: modelling it as a miss said the
       image could not build, when it demonstrably does.
    """
    parts = path.strip("/").split("/")
    if any("/".join(parts[:i]) in exceptions for i in range(1, len(parts) + 1)):
        return True
    return any(e.startswith("/".join(parts) + "/") for e in exceptions)


def _setuptools_table():
    """`[tool.setuptools]` as setuptools reads it, not as the file spells it.

    Grepping pyproject cannot tell `py-modules` from `keywords`, and the two
    already collide: `keywords = [..., "dashboard"]` makes `"dashboard"` present
    in the text whether or not the module is packaged.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)["tool"]["setuptools"]


def _strip_ruby_comments(text):
    """`text` with `#` comments removed, leaving a `#` inside a string alone.

    Only the quote state matters, so this is a character walk rather than a
    regex: `libexec.install "a#b.py"` must keep its argument, while
    `# "reports.py",` must lose the whole line.
    """
    stripped = []
    for line in text.splitlines():
        in_string = False
        cut = len(line)
        for position, char in enumerate(line):
            if char == '"' and (position == 0 or line[position - 1] != "\\"):
                in_string = not in_string
            elif char == "#" and not in_string:
                cut = position
                break
        stripped.append(line[:cut])
    return "\n".join(stripped)


def _formula_install_args(text):
    """Every path named by a `libexec.install` in a Homebrew formula.

    Ruby continues an argument list across lines on a trailing comma and the
    module list spans six of them, so each call is joined before its string
    literals are read — the same problem the Dockerfile's backslashes pose.

    Comments come off *before* the join, for the same reason the Dockerfile
    parser drops them first: commenting a line out is the ordinary way to stop
    shipping a file, and reading the name back out of the comment is exactly the
    substring behaviour these tests exist to replace. Measured before this was
    written — `# "reports.py",` inside the argument list left the suite green
    while `brew install` would ship no reports.py.
    """
    lines = _strip_ruby_comments(text).splitlines()
    args = []
    for index, line in enumerate(lines):
        if "libexec.install" not in line:
            continue
        call = line
        while call.rstrip().endswith(",") and index + 1 < len(lines):
            index += 1
            call += lines[index]
        args.extend(re.findall(r'"([^"]+)"', call))
    return args


def _copy_python_files(text):
    """The `files` array in the extension's bundler, with comments removed.

    Bounded to the array and stripped of comments on purpose. The bundler
    already refuses to run when a *listed* file is missing, so the direction
    left uncovered is a file present in the repo and absent from the array —
    and `// "reports.py",` left inside the array is the ordinary way to get
    there while the name stays in the file's text.

    Both comment forms come off, block first so a `/* ... */` spanning lines
    cannot survive by hiding its opener behind a `//`. Stripping only `//` was
    not enough: measured before this was written, `/* "reports.py", */` left the
    suite green while `node scripts/copy-python.js` exited 0 having written no
    reports.py into the .vsix.
    """
    body = re.search(r"\bconst files = \[(.*?)\];", text, re.S)
    if body is None:
        return []
    listed = re.sub(r"/\*.*?\*/", "", body.group(1), flags=re.S)
    return re.findall(r'"([^"]+)"', re.sub(r"//[^\n]*", "", listed))


class TestWebAssetsExist(unittest.TestCase):
    def test_the_shell_and_stylesheet_are_present_and_substantial(self):
        for name, floor in (("index.html", 5_000), ("app.css", 10_000)):
            with self.subTest(asset=name):
                path = WEB_DIR / name
                self.assertTrue(path.is_file(), f"{path} is missing")
                self.assertGreater(len(path.read_text(encoding="utf-8")), floor)

    def test_the_asset_list_this_module_guards_is_not_vacuous(self):
        """Guard the glob itself, the way the surface parsers above are guarded.

        Every packaging assertion in this file iterates WEB_ASSETS, so a glob
        that matched nothing would satisfy all of them while checking nothing.
        The names are spelled here and nowhere else: this is the one place a
        hardcoded list is right, because its job is to catch the discovery
        going quiet rather than to be the discovery.
        """
        for required in ("web/index.html", "web/app.css", "web/icon.svg"):
            with self.subTest(asset=required):
                self.assertIn(required, WEB_ASSETS)
        self.assertGreater(len(WEB_ASSETS), 10, "the globs found almost nothing")

    def test_the_js_is_split_into_ordered_parts(self):
        parts = sorted((WEB_DIR / "js").glob("*.js"))
        self.assertGreater(len(parts), 1, "the app JS should be split into parts")
        for part in parts:
            with self.subTest(part=part.name):
                self.assertRegex(
                    part.name, r"^\d{2}-[a-z0-9-]+\.js$",
                    "parts load in filename order and a classic script needs its "
                    "top-level consts defined first, so the numeric prefix is "
                    "load-bearing")
        self.assertGreater(
            sum(len(p.read_text(encoding="utf-8")) for p in parts), 50_000)

    def test_the_parts_concatenate_to_the_script_the_page_inlines(self):
        joined = dashboard.load_app_js(WEB_DIR)
        self.assertIn(joined, dashboard.HTML_TEMPLATE,
                      "the assembled page must contain exactly the concatenation")

    def test_load_order_puts_definitions_before_use(self):
        """PRICING is a top-level const; formatting and filtering read it."""
        names = [p.name for p in sorted((WEB_DIR / "js").glob("*.js"))]
        self.assertLess(names.index("10-pricing.js"), names.index("40-filters.js"))
        self.assertLess(names.index("00-core.js"), names.index("10-pricing.js"))

    def test_the_shell_declares_both_placeholders_exactly_once(self):
        shell = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        for marker in ("__APP_CSS__", "__APP_JS__"):
            self.assertEqual(shell.count(marker), 1,
                             f"{marker} must appear exactly once in index.html")

    def test_the_server_placeholders_survived_the_split(self):
        """The nonce and app-config markers are substituted per request."""
        shell = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        self.assertEqual(shell.count("__CSP_NONCE__"), 2)
        self.assertEqual(shell.count("__APP_CONFIG_JSON__"), 1)


class TestReassembly(unittest.TestCase):
    def test_the_loaded_template_contains_every_part(self):
        template = dashboard.HTML_TEMPLATE
        self.assertNotIn("__APP_CSS__", template, "the stylesheet was not inlined")
        self.assertNotIn("__APP_JS__", template, "the application was not inlined")
        self.assertIn("function calcCost(", template)
        self.assertIn(":root {", template)
        self.assertIn("<!DOCTYPE html>", template)

    def test_reloading_reproduces_the_same_document(self):
        self.assertEqual(dashboard.load_html_template(), dashboard.HTML_TEMPLATE)

    def test_the_document_is_still_one_page_with_one_app_script(self):
        """Delivery shape is contractual: the CSP grants a nonce to inline
        script, and the page ships as a single document rather than extra
        routes. A stray <script> tag would need its own CSP allowance."""
        template = dashboard.HTML_TEMPLATE
        self.assertEqual(len(re.findall(r"<script", template)), 3)
        self.assertEqual(len(re.findall(r"<style", template)), 1)

    def test_a_missing_asset_fails_loudly(self):
        from unittest import mock
        with mock.patch.object(dashboard, "find_web_dir", return_value=None):
            with self.assertRaises(RuntimeError) as caught:
                dashboard.load_html_template()
        self.assertIn("web assets not found", str(caught.exception).lower())


class TestPackagingShipsEveryRuntimeFile(unittest.TestCase):
    """Each surface is parsed into the list it declares, not searched as text.

    What ships is the list; the rest of the file is prose. A substring match
    cannot tell the two apart, and it does not need a contrived edit to be
    fooled — names such as `dashboard` also occur in pyproject's keywords and
    prose. The qualified package ships wholesale in pip, Docker, and Homebrew;
    the VSIX and Docker context intentionally enumerate its files, so those
    declarations are compared with the modules discovered on disk.
    """

    def _assert_ships_everything(self, path, listed, required, label):
        for item in required:
            with self.subTest(surface=label, item=item):
                self.assertIn(
                    item, listed,
                    f"{path.name} does not ship {item}. Package modules are "
                    "explicit in the Docker context and VSIX copy list; web "
                    "and vendor assets are also explicit in pyproject. The "
                    "required surfaces must stay synchronized.",
                )

    def test_checkout_compatibility_names_are_exact_package_aliases(self):
        """Legacy patches must mutate the implementation, not a copied shim."""
        package_names = {p.stem for p in PACKAGE_DIR.glob("*.py")}
        aliases = sorted(
            p.stem for p in REPO_ROOT.glob("*.py")
            if p.stem in package_names
        )
        self.assertGreater(len(aliases), 10, "the alias discovery is vacuous")
        for name in aliases:
            with self.subTest(module=name):
                self.assertIs(
                    importlib.import_module(name),
                    importlib.import_module(f"claude_usage.{name}"),
                )

    def test_pyproject_lists_every_module(self):
        path = REPO_ROOT / "pyproject.toml"
        table = _setuptools_table()
        self.assertEqual(table["packages"], ["claude_usage"])
        self.assertNotIn(
            "py-modules", table,
            "generic flat modules must not be installed into site-packages")

    def test_pyproject_files_every_web_asset_under_the_dir_it_is_read_from(self):
        """The destination key, not the source path, decides where a file lands.

        setuptools copies each source to `<destination>/<basename>` and ignores
        the source's own directory, so a part pasted into the
        `share/claude-usage/web` group five lines above installs beside
        index.html — where `dashboard.app_js_parts`' `web/js/*.js` glob cannot
        see it. `load_app_js` only raises at zero parts, so the page is still
        assembled, minus the one that defines APP_CONFIG, and dies at load in
        the browser with the server reporting nothing. Both groups spell
        `web/...`, so the file's text is identical either way.
        """
        installed = {}
        for destination, sources in _setuptools_table()["data-files"].items():
            for source in sources:
                installed.setdefault(source, []).append(destination)
        for asset in WEB_ASSETS:
            with self.subTest(asset=asset):
                expected = "share/claude-usage/" + asset.rsplit("/", 1)[0]
                self.assertEqual(
                    installed.get(asset), [expected],
                    f"pyproject must install {asset} into {expected}, not "
                    f"{installed.get(asset)}; the destination is the only thing "
                    "that decides where it lands.")

    def test_dockerfile_copies_every_module_and_the_web_dir(self):
        path = REPO_ROOT / "Dockerfile"
        sources = set(_dockerfile_copy_sources())
        self.assertIn("claude_usage", sources)
        self.assertIn("web", sources, "the page is not copied into the image")

    def test_homebrew_formula_installs_every_module_and_the_web_dir(self):
        formulas = list((REPO_ROOT / "Formula").glob("*.rb"))
        self.assertTrue(formulas, "no Homebrew formula found")
        for path in formulas:
            installed = _formula_install_args(path.read_text(encoding="utf-8"))
            self.assertIn("claude_usage", installed)
            self.assertIn("web", installed)

    def test_extension_bundler_copies_every_module_and_asset(self):
        path = REPO_ROOT / "vscode-extension" / "scripts" / "copy-python.js"
        listed = _copy_python_files(path.read_text(encoding="utf-8"))
        self._assert_ships_everything(path, listed, RUNTIME_MODULES, "copy-python.js")
        self.assertIn("cli.py", listed, "the extension launcher is missing")
        self._assert_ships_everything(path, listed, WEB_ASSETS, "copy-python.js")

    def test_vendor_on_disk_reaches_the_pip_install_and_the_vsix(self):
        """PKG-3. The two surfaces nothing drove from `vendor/` on disk.

        The vendor net linked disk to `.dockerignore`
        (`test_vendor_on_disk_and_the_allowlist_name_the_same_files`) and
        pyproject to `.dockerignore`
        (`test_the_context_admits_every_pinned_vendor_asset`), and stopped there.
        So a companion asset — the shape a Chart.js re-pin takes: a date
        adapter, a locale bundle, a `.map` — dropped on disk and named in
        `.dockerignore` passed the ENTIRE suite while being absent from the pip
        install and from the .vsix. It reached the image (`COPY vendor`
        materialises admitted children) and Homebrew (`libexec.install "vendor"`
        takes the directory wholesale), so two of five surfaces carried it,
        three did not, and nothing said which — the same asymmetry that shipped
        `GET /icon.svg` broken on three surfaces.

        The `Dockerfile` and the formula are deliberately not asserted here:
        both take the directory, so they need no per-file line and cannot fall
        behind. These two spell every file by hand, which is exactly why they
        can.

        Equality rather than containment, for the reason its sibling gives: a
        name listed but not on disk is either a dead entry or a file someone
        deleted, and for pyproject it fails the wheel build rather than
        anything a reader sees.
        """
        on_disk = {p.relative_to(REPO_ROOT).as_posix()
                   for p in (REPO_ROOT / "vendor").rglob("*") if p.is_file()}
        self.assertTrue(on_disk, "vendor/ holds no files at all")

        destinations = {}
        for destination, sources in _setuptools_table()["data-files"].items():
            for source in sources:
                if source.startswith("vendor/"):
                    destinations.setdefault(source, []).append(destination)
        self.assertEqual(
            on_disk, set(destinations),
            "vendor/ on disk and pyproject's `data-files` must name exactly the "
            "same files, or the asset is absent from the pip/uv/pipx install "
            "while the Docker image and the Homebrew keg still carry it.")
        for asset, where in sorted(destinations.items()):
            with self.subTest(asset=asset):
                # Same rule as the web assets: the destination key decides where
                # setuptools puts it, and `find_chart_file` looks under vendor/.
                self.assertEqual(
                    where, ["share/claude-usage/vendor"],
                    f"{asset} must install into share/claude-usage/vendor")

        path = REPO_ROOT / "vscode-extension" / "scripts" / "copy-python.js"
        listed = {f for f in _copy_python_files(path.read_text(encoding="utf-8"))
                  if f.startswith("vendor/")}
        self.assertEqual(
            on_disk, listed,
            "vendor/ on disk and copy-python.js's `files` array must name "
            "exactly the same files, or the asset is missing from the .vsix "
            "and a route serving it 404s with the packaging step green.")

    def test_the_hand_written_parsers_read_a_list_and_not_the_whole_file(self):
        """Guard the two sliced parsers, the way the card parser below is guarded.

        A slice that reads *less* than its list fails loudly above — the missing
        names are reported one by one. A slice that runs past the list would
        not: it would pick up every other string in the file and quietly restore
        the substring behaviour these tests exist to replace. Every entry naming
        something that is actually in the repo rules that out, and it is worth
        asserting for its own sake — nothing else checks that what the formula
        installs exists, and `brew install` fails outright when it does not.
        """
        surfaces = [(f"Formula/{p.name}", _formula_install_args(p.read_text(encoding="utf-8")))
                    for p in (REPO_ROOT / "Formula").glob("*.rb")]
        surfaces.append(("copy-python.js", _copy_python_files(
            (REPO_ROOT / "vscode-extension" / "scripts" / "copy-python.js")
            .read_text(encoding="utf-8"))))
        for label, entries in surfaces:
            with self.subTest(surface=label):
                self.assertTrue(entries, f"{label}: the parser found no list at all")
                for entry in entries:
                    self.assertTrue(
                        (REPO_ROOT / entry).exists(),
                        f"{label} names {entry!r}, which is not in the repo")

    def test_the_parsers_do_not_read_a_commented_out_entry_as_shipped(self):
        """Commenting a line out is how you stop shipping a file. Honour it.

        Both parsers slice a list out of a file written in another language, and
        a name that survives only inside a comment restores exactly the
        substring behaviour this class exists to replace: the suite stays green
        while the artifact ships without the file. Both directions were measured
        against the real artifacts before this was written, and both were live:
        `# "reports.py",` inside the formula's argument list, and
        `/* "reports.py", */` inside the bundler's array, each left
        `tests.test_web_assets` entirely green — and the bundler exited 0 having
        written no reports.py at all. The control (deleting the entry outright)
        did fail, so the parsers were reached and it was comment handling alone
        that was missing.
        """
        formula = (
            'def install\n'
            '  libexec.install "cli.py", "scanner.py",\n'
            '                  # "reports.py",\n'
            '                  "web"\n'
            'end\n'
        )
        self.assertEqual(
            _formula_install_args(formula), ["cli.py", "scanner.py", "web"],
            "a commented-out formula entry was read back as if it shipped")

        bundler = (
            'const files = [\n'
            '  "cli.py",\n'
            '  // "scanner.py",\n'
            '  /* "reports.py", */\n'
            '  /* several lines,\n'
            '     "rollups.py",\n'
            '  */\n'
            '  "web/index.html",\n'
            '];\n'
        )
        self.assertEqual(
            _copy_python_files(bundler), ["cli.py", "web/index.html"],
            "a commented-out bundler entry was read back as if it shipped")

    def test_the_formula_parser_keeps_a_hash_that_is_inside_a_string(self):
        """Stripping `#` blindly would silently drop a legitimate path.

        The comment strip is a character walk rather than a regex precisely so
        that a `#` inside a string literal stays an argument. Nothing in the
        repo is named this way today; the test exists so the cheaper regex is
        not substituted later without noticing what it costs.
        """
        formula = 'libexec.install "od#d.py", "web"\n'
        self.assertEqual(_formula_install_args(formula), ["od#d.py", "web"])

    def test_every_runtime_module_actually_exists(self):
        """Guards the list above from drifting away from the repo."""
        for module in RUNTIME_MODULES:
            with self.subTest(module=module):
                self.assertTrue((REPO_ROOT / module).is_file())


class TestDockerBuildContextCarriesWhatTheDockerfileCopies(unittest.TestCase):
    """`.dockerignore` is an executable packaging surface, and once broke the image.

    It is a deny-all allowlist, so it does not merely trim the context — it
    *defines* it. When the modules were split out and the page moved into
    `web/`, the exceptions were not extended, and the context stopped carrying
    eleven of the fifteen modules the Dockerfile names plus the entire `web/`
    tree. `docker build` then failed on its very first COPY with
    `"/transcripts.py": not found`, so the README's Docker path and
    `scripts/run-docker.sh` were both dead.

    Nothing caught it because no test read the file. These tests do, and they
    discover the required package modules and assets from disk rather than
    listing them, so an addition fails here until `.dockerignore` ships it.
    """

    def test_the_context_is_a_deny_all_allowlist(self):
        """One exclusion, everything else an exception.

        With a single `*` there is no later pattern that can re-exclude an
        allowed file, so the `!` lines below can be read as the whole truth.
        A second exclusion would make the last-match-wins ordering load-bearing
        and quietly invalidate every assertion in this class.
        """
        exclusions, exceptions = _dockerignore_patterns()
        self.assertEqual(exclusions, ["*"],
                         "the build context must deny everything by default and "
                         "re-admit named files, with nothing that can re-exclude")
        self.assertTrue(exceptions, "an allowlist with no exceptions ships nothing")

    def test_every_exception_names_a_file_that_is_in_the_repo(self):
        """The general form of the two `assertNotIn`s below, and the reason
        `_context_admits`' second rule cannot admit anything today.

        Those two spell `web` and `vendor` by hand, so a later `!tools/` would
        re-admit its whole subtree with nothing to say so. Requiring every
        exception to name a regular file rules that out for every directory at
        once — an ancestor exception is a directory by definition — which is
        what lets that helper's rule 2 be documented as forbidden rather than
        as how anything ships.

        The existence half is worth its own assertion: a `!` on a path that is
        not in the repo is a typo admitting nothing, or a leftover from a
        deleted file, and `test_vendor_on_disk_and_the_allowlist_name_the_same_files`
        below reaches that only under `vendor/`.
        """
        _, exceptions = _dockerignore_patterns()
        for entry in sorted(exceptions):
            with self.subTest(entry=entry):
                path = REPO_ROOT / entry
                self.assertTrue(path.exists(),
                                f"`!{entry}` names nothing in the repo")
                self.assertFalse(
                    path.is_dir(),
                    f"`!{entry}` re-admits every file beneath {entry}/, now and "
                    "whenever one is dropped there. Name the files instead.")

    def test_the_context_admits_every_runtime_module_and_web_asset(self):
        _, exceptions = _dockerignore_patterns()
        for item in RUNTIME_MODULES + WEB_ASSETS:
            with self.subTest(item=item):
                self.assertTrue(
                    _context_admits(item, exceptions),
                    f".dockerignore does not re-admit {item}, so it is absent from "
                    "the build context. Five surfaces hardcode this list "
                    "(pyproject.toml, Dockerfile, .dockerignore, Formula/*.rb, "
                    "vscode-extension/scripts/copy-python.js) and they must be "
                    "updated together or the install breaks for users only.",
                )

    def test_every_path_the_dockerfile_copies_is_in_the_context(self):
        """Closes the loop between the two files from the other direction.

        The test above is driven by what the repo contains; this one is driven
        by what the Dockerfile asks for. A COPY of something the context lacks
        does not degrade — it aborts the build.
        """
        _, exceptions = _dockerignore_patterns()
        sources = _dockerfile_copy_sources()
        self.assertIn("web", sources, "the page is not copied into the image at all")
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue(
                    _context_admits(source, exceptions),
                    f"Dockerfile copies {source} but .dockerignore excludes it; "
                    f'docker build fails with "/{source}: not found"',
                )

    def test_the_allowlist_does_not_re_admit_the_whole_web_tree(self):
        """`!web/` would work, and would be worse.

        A bare directory exception re-includes everything beneath it, so any
        stray file dropped in `web/` would ride into the context and — because
        the Dockerfile copies the directory wholesale — into the image. Naming
        the assets individually keeps the allowlist an allowlist, and is what
        makes the discovery test above bite instead of passing vacuously.
        """
        _, exceptions = _dockerignore_patterns()
        for directory in ("web", "web/js"):
            with self.subTest(directory=directory):
                self.assertNotIn(directory, exceptions)
        # The property that line actually buys, stated directly: `web` is still
        # reachable for COPY, but only the named assets travel with it.
        self.assertTrue(_context_admits("web", exceptions))
        for stray in ("web/notes.md", "web/js/scratch.js", "web/.env"):
            with self.subTest(stray=stray):
                self.assertFalse(_context_admits(stray, exceptions))

    def test_the_allowlist_does_not_re_admit_the_whole_vendor_tree(self):
        """The same rule, for the other directory the Dockerfile copies whole.

        A bare `!vendor/` sat here and did exactly what the test above forbids
        for `web/`. Re-run 2026-08-10 on Docker 29.5.2 rather than inherited from
        the round that first wrote it: with `!vendor/` standing in for the two
        named lines, a planted `vendor/.env` and `vendor/secrets/creds.json`
        reached /app/vendor on BuildKit and on the classic builder, while the
        same two files planted under `web/` did not, and with the named lines
        left alone none of the four shipped.

        Nothing pinned the tightening once it landed: before this test existed,
        collapsing the two named lines back to `!vendor/`, and adding `!vendor/`
        beside them, each left this file entirely green — the fix could be undone
        in one line and nothing would say so. (That was recorded with a bare
        module-wide test count; the count is dropped rather than corrected,
        because such a count rots and this module has long since outgrown the one
        it carried — see the class at the end of this file.) Measured
        2026-08-10, the collapse now fails this test, the sibling that requires
        every exception to name a real file, and the disk-equality test below.
        """
        _, exceptions = _dockerignore_patterns()
        self.assertNotIn(
            "vendor", exceptions,
            "a bare `!vendor/` re-admits the whole subtree and makes the named "
            "vendor files below it dead")
        # The property those lines actually buy, stated directly: `COPY vendor`
        # still resolves from the named children alone, exactly as web/ does.
        self.assertTrue(_context_admits("vendor", exceptions))
        for stray in ("vendor/stray.txt", "vendor/.env",
                      "vendor/secrets/creds.json"):
            with self.subTest(stray=stray):
                self.assertFalse(_context_admits(stray, exceptions))

    def test_either_spelling_of_a_bare_directory_exception_is_caught(self):
        """`!vendor` and `!vendor/` are one pattern to Docker, so the guard
        above has to reject both — and it names only one because the parser
        strips the trailing slash first.

        Run through the real parser rather than asserted about it, since the
        spelling being modelled is one this repo must never carry. Measured
        2026-08-10 on Docker 29.5.2, a context of `*` plus either spelling
        shipped `sub/a.txt`, `sub/nested/b.txt` and `sub/n1/n2/c.txt` alike on
        BuildKit and on the classic builder, which is the shape a
        `vendor/secrets/creds.json` would travel in. Re-run independently the
        same day rather than carried forward: the round before this one inherited
        this figure without repeating it, and inherited figures are how the
        comment block in `.dockerignore` came to carry one false claim after
        another.
        """
        for spelling in ("!vendor", "!vendor/"):
            with self.subTest(spelling=spelling):
                _, exceptions = _dockerignore_patterns(f"*\n{spelling}\n")
                self.assertIn(
                    "vendor", exceptions,
                    "the guard above looks for one key; both spellings have to "
                    "normalise onto it or half the revert walks past it")
                self.assertTrue(
                    _context_admits("vendor/secrets/creds.json", exceptions),
                    "a bare directory exception re-admits the whole subtree, and "
                    "_context_admits has to model that — otherwise the stray "
                    "assertions above pass while docker build ships the stray")

    def test_the_context_admits_every_pinned_vendor_asset(self):
        """The pyproject side of the same list, which the disk side cannot see.

        The test below drives `vendor/` from disk. This one drives it from
        pyproject's `data-files`, and the two catch different mistakes: an
        asset declared for the wheel but never vendored is present here and
        absent from disk, so only this assertion reaches it. Measured
        2026-08-09 before either existed: deleting `!vendor/chart.umd.js` alone
        left this file entirely green, because `COPY vendor` still
        resolves through LICENSE.chartjs.md — the image builds without Chart.js
        and only `/assets/chart.umd.js` finds out.
        """
        _, exceptions = _dockerignore_patterns()
        pinned = _setuptools_table()["data-files"].get(
            "share/claude-usage/vendor", [])
        self.assertTrue(pinned, "pyproject installs no vendor asset at all")
        for asset in pinned:
            with self.subTest(asset=asset):
                self.assertTrue(
                    _context_admits(asset, exceptions),
                    f".dockerignore does not re-admit {asset}, so it is missing "
                    "from the image while `COPY vendor` still succeeds: the "
                    "build stays green and the runtime does not.")

    def test_vendor_on_disk_and_the_allowlist_name_the_same_files(self):
        """Equality, not containment — which is why this test can exist at all.

        `.dockerignore` and this file both used to say it could not: requiring
        every file on disk to be admitted asserts the opposite of what the
        named form is for, since a stray beside chart.umd.js must NOT ship.
        That is a fair objection to the one-way rule and none at all to the
        equality. A stray fails this too, and the way to make it pass is to
        delete it, not to admit it. What the equality says is that `vendor/`
        holds exactly what the image ships, so both mistakes are loud instead
        of one.

        Both were silent. Measured 2026-08-09 on Docker 29.5.2, on BuildKit and
        on the classic builder: with `vendor/chart.umd.js.map` on disk and named
        in no surface, this file stayed entirely green and `docker build`
        succeeded with the map absent from /app/vendor — the shape a re-pin of
        Chart.js takes, discovered at runtime or not at all. The sibling test
        above catches that only once the asset also reaches pyproject.toml, and
        disk is where it lands first.

        The other direction is the vendor half of
        `test_the_allowlist_does_not_re_admit_the_whole_web_tree`: collapse
        these lines to `!vendor/` and the allowlist stops naming files, which
        is what let `vendor/.env` and `vendor/secrets/creds.json` reach
        /app/vendor on both builders when it last stood here.
        """
        on_disk = {p.relative_to(REPO_ROOT).as_posix()
                   for p in (REPO_ROOT / "vendor").rglob("*") if p.is_file()}
        self.assertTrue(on_disk, "vendor/ holds no files at all")
        _, exceptions = _dockerignore_patterns()
        admitted = {e for e in exceptions
                    if e == "vendor" or e.startswith("vendor/")}
        self.assertEqual(
            on_disk, admitted,
            "vendor/ on disk and the `!vendor/...` lines in .dockerignore must "
            "name exactly the same files. A file on disk that is not admitted "
            "is silently missing from the image and `docker build` still "
            "succeeds; a name admitted that is not on disk is either a dead "
            "line or a directory exception that would ship whatever lands "
            "beneath it. Add the asset to .dockerignore, pyproject.toml and "
            "vscode-extension/scripts/copy-python.js together, or delete it.")

    def test_secrets_and_local_state_stay_out_of_the_context(self):
        """The reason the allowlist exists, asserted rather than assumed."""
        _, exceptions = _dockerignore_patterns()
        for path in (".git/config", ".env", "usage.db", "tests/test_web_assets.py",
                     "node_modules/x/index.js", "vscode-extension/package.json"):
            with self.subTest(path=path):
                self.assertFalse(_context_admits(path, exceptions))


class TestTheIconResolvesInEveryDeliverySurface(unittest.TestCase):
    """`GET /icon.svg` shipped on two surfaces of five, and nothing said so.

    `find_icon_file` searched two module-relative paths inside
    `vscode-extension/resources/`. The checkout has that directory and the
    .vsix has a copy of it (put there by vsce, not by `copy-python.js`), so the
    two surfaces a developer actually looks at both worked. Homebrew installs
    `libexec/{web,vendor}` plus the modules, pip installs
    `share/claude-usage/{web,vendor}`, and the Docker image copies `web` and
    `vendor` — none of the three carries `vscode-extension/` in any form, so on
    all three the route 404'd while the page around it rendered normally.
    `web/app.css` masks `header .header-icon` with the URL unconditionally and
    the markup is a `<span>`, so the symptom was a blank 26px gap in the header
    and nothing else: no console error the server could see, no failing
    request the page reports, no test.

    Every layout below is rebuilt FROM THE SURFACE'S OWN DECLARATION — parsed
    by the same helpers the packaging tests use — and then handed to the real
    `find_icon_file` with `dashboard.__file__` pointed into it. That is the
    difference between this and the check that was missing: asserting the file
    exists in the repo passes on the broken tree, because it always did exist.
    Each layout is driven by a different surface, so each of these tests fails
    for a different missing line:

      pip      → pyproject's `data-files`
      Docker   → `.dockerignore`'s `!` exceptions (through the real admission
                 rule, since `COPY web` materializes only admitted children)
      .vsix    → `copy-python.js`'s `files` array
      Homebrew → `libexec.install "web"`, which needs the file on disk only
      checkout → the tree this suite is running in
    """

    ICON = "web/icon.svg"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        # Resolved, because the resolver resolves: on macOS the temp root is a
        # symlink and the unresolved string never appears in what it returns.
        self.tmp = Path(self._tmpdir.name).resolve()
        self.icon_bytes = (REPO_ROOT / self.ICON).read_bytes()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _place(self, root, relative, source):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    def _resolve_from(self, module_dir, prefix=None):
        """What `find_icon_file` answers for a dashboard.py at `module_dir`.

        `sys.prefix` is pointed at an empty directory unless the layout under
        test is one that installs under a prefix, so the interpreter this suite
        happens to run under cannot answer for somebody else's layout.
        """
        empty_prefix = self.tmp / "unrelated-prefix"
        empty_prefix.mkdir(exist_ok=True)
        with mock.patch.object(dashboard, "__file__",
                               str(module_dir / "dashboard.py")), \
                mock.patch.object(sys, "prefix", str(prefix or empty_prefix)):
            return dashboard.find_icon_file()

    def _assert_serves_the_icon(self, resolved, root):
        """It resolved, inside the rebuilt layout, to the same bytes.

        `root` matters: without it a test could pass by reaching back into the
        checkout this suite runs from — which is the one layout that was never
        broken — and report that a pip install works because the developer's
        clone is sitting a few directories up.
        """
        self.assertIsNotNone(
            resolved, f"nothing under {root} answers GET /icon.svg")
        self.assertTrue(
            resolved.is_relative_to(root),
            f"{resolved} is outside the layout under test ({root}); the icon "
            "was found in the checkout, not in the install")
        self.assertEqual(resolved.read_bytes(), self.icon_bytes)

    # --- the five layouts, each rebuilt from its own surface ----------------

    def _build_pip_layout(self):
        """The package under site-packages, data files under the prefix.

        The destination key decides where a data file lands and the source's
        own directory is ignored, which is the rule
        `test_pyproject_files_every_web_asset_under_the_dir_it_is_read_from`
        pins; this rebuild obeys it rather than re-deriving the path, so an
        icon filed under the wrong group here fails to resolve exactly as it
        would on a real install.
        """
        table = _setuptools_table()
        root = self.tmp / "pip"
        module_dir = root / "lib" / "python3.13" / "site-packages"
        module_dir.mkdir(parents=True)
        self.assertEqual(table["packages"], ["claude_usage"])
        package_dir = module_dir / "claude_usage"
        package_dir.mkdir()
        for module in RUNTIME_MODULES:
            (package_dir / Path(module).name).write_text("", encoding="utf-8")
        for destination, sources in table["data-files"].items():
            for source in sources:
                self._place(root, f"{destination}/{Path(source).name}",
                            REPO_ROOT / source)
        return package_dir, root

    def _build_homebrew_layout(self, formula):
        libexec = self.tmp / f"brew-{formula.stem}" / "libexec"
        libexec.mkdir(parents=True)
        for arg in _formula_install_args(formula.read_text(encoding="utf-8")):
            source = REPO_ROOT / arg
            if source.is_dir():
                shutil.copytree(source, libexec / arg)
            else:
                self._place(libexec, arg, source)
        return libexec / "claude_usage"

    def _build_docker_layout(self):
        """WORKDIR /app, filled by the COPYs through the real admission rule.

        A directory COPY materializes exactly its admitted children, so
        dropping `!web/icon.svg` from `.dockerignore` leaves `COPY web ./web`
        succeeding with the icon simply absent — a green build and a 404.
        """
        app = self.tmp / "docker" / "app"
        app.mkdir(parents=True)
        _, exceptions = _dockerignore_patterns()
        for source in _dockerfile_copy_sources():
            path = REPO_ROOT / source
            if path.is_dir():
                for child in sorted(path.rglob("*")):
                    relative = child.relative_to(REPO_ROOT).as_posix()
                    if child.is_file() and _context_admits(relative, exceptions):
                        self._place(app, relative, child)
            elif path.is_file() and _context_admits(source, exceptions):
                self._place(app, source, path)
        return app / "claude_usage"

    def _build_vsix_layout(self):
        """`copy-python.js` fills `python/`; vsce carries `resources/` itself.

        Returns the module directory and the extension root, because the
        fallback test below needs to reach the copy the bundler did not put
        there.
        """
        extension = self.tmp / "vsix"
        python_dir = extension / "python"
        python_dir.mkdir(parents=True)
        listed = _copy_python_files(
            (REPO_ROOT / "vscode-extension" / "scripts" / "copy-python.js")
            .read_text(encoding="utf-8"))
        for entry in listed:
            self._place(python_dir, entry, REPO_ROOT / entry)
        self._place(extension, "resources/icon.svg",
                    REPO_ROOT / "vscode-extension" / "resources" / "icon.svg")
        return python_dir / "claude_usage", extension

    # --- the five assertions ------------------------------------------------

    def test_a_pip_install_serves_the_icon(self):
        module_dir, root = self._build_pip_layout()
        resolved = self._resolve_from(module_dir, prefix=root)
        self._assert_serves_the_icon(resolved, root)
        self.assertEqual(resolved,
                         root / "share" / "claude-usage" / "web" / "icon.svg")

    def test_a_homebrew_install_serves_the_icon(self):
        formulas = list((REPO_ROOT / "Formula").glob("*.rb"))
        self.assertTrue(formulas, "no Homebrew formula found")
        for formula in formulas:
            with self.subTest(formula=formula.name):
                libexec = self._build_homebrew_layout(formula)
                resolved = self._resolve_from(libexec)
                self._assert_serves_the_icon(resolved, libexec.parent)
                self.assertEqual(resolved, libexec.parent / "web" / "icon.svg")

    def test_the_docker_image_serves_the_icon(self):
        app = self._build_docker_layout()
        resolved = self._resolve_from(app)
        self._assert_serves_the_icon(resolved, app.parent)
        self.assertEqual(resolved, app.parent / "web" / "icon.svg")

    def test_the_vsix_serves_the_icon(self):
        python_dir, extension = self._build_vsix_layout()
        resolved = self._resolve_from(python_dir)
        self._assert_serves_the_icon(resolved, extension)
        self.assertEqual(resolved, python_dir.parent / "web" / "icon.svg")

    def test_the_checkout_serves_the_icon(self):
        """No patching: this is the layout the suite is running in."""
        resolved = dashboard.find_icon_file()
        self._assert_serves_the_icon(resolved, REPO_ROOT)
        self.assertEqual(resolved, REPO_ROOT / "web" / "icon.svg")

    def test_an_older_vsix_still_falls_back_to_the_extension_copy(self):
        """The two candidates this fix did not remove, asserted.

        A .vsix built before `web/icon.svg` was bundled has `resources/icon.svg`
        and no `python/web/icon.svg`, and the sidebar webview loads that copy
        regardless of what the bundler ships. Deleting the extension-relative
        candidates would break that install and nothing else in this class
        would notice, because every layout above resolves through `web/`.
        """
        python_dir, extension = self._build_vsix_layout()
        (python_dir.parent / "web" / "icon.svg").unlink()
        resolved = self._resolve_from(python_dir)
        self._assert_serves_the_icon(resolved, extension)
        self.assertEqual(resolved, extension / "resources" / "icon.svg")

    def test_the_layouts_are_rebuilt_and_not_merely_asserted_about(self):
        """The control: every layout above must be BROKEN by dropping the icon.

        Without this, all five assertions could be passing because some path
        outside the rebuild answers for them, and the class would go on passing
        after the surfaces were emptied again. Each layout is built, its
        `web/icon.svg` removed, and the resolver asked again; the .vsix keeps
        its `resources/` copy, so it is the one layout that legitimately still
        answers and is checked for that instead.
        """
        module_dir, pip_root = self._build_pip_layout()
        brew = self._build_homebrew_layout(REPO_ROOT / "Formula" / "claude-usage.rb")
        docker = self._build_docker_layout()
        vsix_python, vsix_root = self._build_vsix_layout()
        for label, module_root, layout_root, prefix in (
            ("pip", module_dir, pip_root / "share" / "claude-usage", pip_root),
            ("homebrew", brew, brew.parent, None),
            ("docker", docker, docker.parent, None),
        ):
            with self.subTest(layout=label):
                (layout_root / "web" / "icon.svg").unlink()
                self.assertIsNone(
                    self._resolve_from(module_root, prefix=prefix),
                    f"the {label} layout answers /icon.svg from somewhere this "
                    "rebuild did not put it, so its assertion above proves "
                    "nothing about the surface it is supposed to be driving")
        (vsix_python.parent / "web" / "icon.svg").unlink()
        (vsix_root / "resources" / "icon.svg").unlink()
        self.assertIsNone(self._resolve_from(vsix_python))

    def test_the_two_copies_of_the_icon_have_not_drifted(self):
        """`web/icon.svg` and `vscode-extension/resources/icon.svg` are one
        image stored twice, and both have to stay that way.

        The extension's copy cannot move: `package.json` names it as the
        activity-bar icon and `sidebar.ts` hands its `asWebviewUri` to the
        panel's stylesheet, neither of which can read out of `web/`. The web
        copy cannot be dropped either — it is the only one four of the five
        surfaces ship. So the duplication is deliberate, and byte-equality is
        what stops the header and the sidebar quietly showing different art.
        """
        extension_copy = REPO_ROOT / "vscode-extension" / "resources" / "icon.svg"
        self.assertTrue(extension_copy.is_file())
        self.assertEqual(
            extension_copy.read_bytes(), self.icon_bytes,
            "the two copies of the icon differ; re-copy one onto the other "
            "rather than letting the sidebar and the dashboard header drift")

    def test_the_stylesheet_asks_for_the_path_the_server_answers(self):
        """The route and the reference are written in different languages, in
        different files, and nothing but this connects them. `app.css` is
        inlined into the document, so its relative `url("icon.svg")` resolves
        against the page URL rather than the stylesheet's — which is why the
        server route is `/icon.svg` at the root and not `/web/icon.svg`.
        """
        css = (WEB_DIR / "app.css").read_text(encoding="utf-8")
        self.assertIn('url("icon.svg")', css)
        server = (PACKAGE_DIR / "dashboard.py").read_text(encoding="utf-8")
        self.assertIn('path == "/icon.svg"', server)


class TestEveryCardIsCollapsible(unittest.TestCase):
    """Folding a section depends on three separate things lining up in markup.

    A card needs `data-card` (so the toggle finds it and can persist its state),
    a title element that matches the click selector in 70-bootstrap.js, and a
    visible `.card-caret` — the only thing on screen that says the section folds
    at all. Each lives in a different place, none of them errors when absent, and
    a new section that gets two out of three simply does not collapse, silently.

    This matters now rather than later: the page is about to grow sections, and
    a Codex view would be built by copying one of these.
    """

    HTML = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    # Must stay in step with TITLE_SEL in web/js/70-bootstrap.js.
    TITLE_TAGS = ('<h2>', '<h2 ', '<div class="section-title"')

    def _cards(self):
        """(id, attrs, inner-html-up-to-the-next-card) for every [data-card]."""
        starts = [m for m in re.finditer(r'<(?:div|section)([^>]*\bdata-card="[^"]*"[^>]*)>',
                                         self.HTML)]
        cards = []
        for i, m in enumerate(starts):
            end = starts[i + 1].start() if i + 1 < len(starts) else len(self.HTML)
            ident = re.search(r'id="([^"]+)"', m.group(1))
            cards.append((ident.group(1) if ident else "(no id)", m.group(1),
                          self.HTML[m.end():end]))
        return cards

    def test_the_page_has_the_cards_this_test_thinks_it_has(self):
        """Guard the parser itself: a regex that matched nothing would pass every
        assertion below while checking exactly nothing."""
        self.assertGreaterEqual(len(self._cards()), 10)

    def test_every_card_declares_a_persistence_key(self):
        for ident, attrs, _ in self._cards():
            with self.subTest(card=ident):
                key = re.search(r'data-card="([^"]*)"', attrs)
                self.assertTrue(key and key.group(1).strip(),
                                "an empty data-card cannot persist its state")

    def test_every_card_key_is_unique(self):
        keys = [re.search(r'data-card="([^"]*)"', a).group(1) for _, a, _ in self._cards()]
        dupes = {k for k in keys if keys.count(k) > 1}
        self.assertEqual(dupes, set(), "two cards sharing a key fold together")

    def test_every_card_has_a_visible_caret(self):
        for ident, _, body in self._cards():
            with self.subTest(card=ident):
                self.assertIn('class="card-caret"', body,
                              "no caret: nothing tells the reader this section folds")

    def test_every_card_title_matches_the_click_selector(self):
        """The caret is decoration; the title is what actually receives the click."""
        for ident, _, body in self._cards():
            with self.subTest(card=ident):
                head = body[:body.find('class="card-caret"')]
                self.assertTrue(any(tag in head for tag in self.TITLE_TAGS),
                                "the caret is not inside an <h2>/.section-title, so "
                                "clicking the heading would not toggle this card")

    def test_the_caret_does_not_inherit_the_dim_heading_colour(self):
        """It did, and on a #1E1F20 card the control was effectively invisible —
        which is why the fold looked like a feature the page did not have."""
        css = (REPO_ROOT / "web" / "app.css").read_text(encoding="utf-8")
        block = re.search(r"\.card-caret\s*\{(.*?)\}", css, re.S)
        self.assertIsNotNone(block, ".card-caret has no rule at all")
        self.assertNotIn("color: inherit", block.group(1))
        self.assertIn("color:", block.group(1))

    def test_no_card_starts_collapsed(self):
        """Expanded by default; collapsing is the reader's choice and persists."""
        for ident, attrs, _ in self._cards():
            with self.subTest(card=ident):
                self.assertNotIn("collapsed", attrs)


class TestThisModuleQuotesNoRottingTestCount(unittest.TestCase):
    """A bare test count in a docstring is a measurement nobody executes.

    Several stood in this module at once. Each was a count of this module on the
    day it was written, and this module has grown past all of them, so a reader
    taking any one of them for the file's size was reading a number that had
    already moved. What every one of them was actually claiming is that a
    mutation left the checks green — and that claim needs no number, because an
    assertion does not get more convincing for being counted.

    A docstring above already states the rule and drops its own count. Stating
    it was not enough: the round that wrote it added fresh counts of its own and
    walked past the ones it inherited. This is that rule made executable, which
    is the form the prose one failed to be.
    """

    def test_no_docstring_here_quotes_a_module_wide_test_count(self):
        text = " ".join(Path(__file__).read_text(encoding="utf-8").split())
        hits = re.findall(r".{0,70}\b\d+ tests? OK", text)
        self.assertEqual(
            hits, [],
            "write 'left the suite green', not 'left the suite at N tests OK'. "
            "The count is true on the day it is written and wrong by the next "
            "commit that adds a test, and nothing executes it in between.")


class TestTheCopyrightNoticeTravelsWithTheSoftware(unittest.TestCase):
    """MIT requires the notice in "all copies or substantial portions".

    Every delivery surface IS such a copy, and two of the five shipped without
    one: `.dockerignore` re-admitted `LICENSE` but the `Dockerfile` never
    copied it in, and the formula installed every module and `vendor/` and
    `web/` but not the licence. pip carried it through `data-files`, the
    `.vsix` has its own under `vscode-extension/`, and the checkout obviously
    has it — so the gap was exactly the two surfaces nobody reads by hand.

    This matters more than housekeeping for a fork going public: the notice
    names the upstream author, and dropping it is the one licence term this
    MIT-licensed project has to keep.
    """

    def test_every_surface_carries_the_notice_by_its_own_mechanism(self):
        """Each surface ships it differently, so each is checked differently.

        Checking one mechanism across all five is what made the FIRST version of
        this test wrong in both directions at once: a bare `LICENSE` grep was
        satisfied by `vendor/LICENSE.chartjs.md` — a different project's notice
        — in three files, and it then failed pip and the `.vsix`, which really
        do ship the licence, just not by naming it in a file list.

        pip: `license = "MIT"` in `pyproject.toml`, which setuptools pairs with
        the root `LICENSE` into the wheel's `.dist-info`.
        Docker: an explicit `COPY`, plus a `!` line, because the context is a
        deny-all allowlist and a `COPY` it does not admit aborts the build.
        Homebrew: `libexec.install`, since the formula names every file.
        `.vsix`: `vscode-extension/LICENSE`, which vsce picks up from the
        extension root — verified identical to the root one.
        """
        # The ROOT licence, never Chart.js's.
        root_licence = re.compile(r"(?<![\w./-])\.?/?LICENSE(?![\w.-])")

        def names_it(path):
            return any(root_licence.search(line)
                       for line in path.read_text(encoding="utf-8").splitlines()
                       if not line.lstrip().startswith("#"))

        missing = []
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        if not re.search(r'^\s*license\s*=', pyproject, re.M):
            missing.append("pip (pyproject declares no license)")
        if not names_it(REPO_ROOT / "Dockerfile"):
            missing.append("Docker (no COPY of LICENSE)")
        if not names_it(REPO_ROOT / ".dockerignore"):
            missing.append("Docker (LICENSE not in the build context)")
        if not names_it(REPO_ROOT / "Formula" / "claude-usage.rb"):
            missing.append("Homebrew (formula does not install LICENSE)")
        if not (REPO_ROOT / "vscode-extension" / "LICENSE").is_file():
            missing.append(".vsix (no LICENSE at the extension root)")

        self.assertEqual(
            missing, [],
            f"these delivery surfaces ship the software without the MIT "
            f"copyright notice the licence requires: {missing}")

    def test_the_extension_copy_is_the_same_notice(self):
        """Two files, one licence — a drifted copy names the wrong holder."""
        root = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
        ext = (REPO_ROOT / "vscode-extension" / "LICENSE").read_text(encoding="utf-8")
        self.assertEqual(root.strip(), ext.strip())

    def test_the_licence_is_actually_there_to_ship(self):
        """Anti-vacuity: the surfaces could all name a file that does not
        exist, which would satisfy the grep and ship nothing."""
        licence = REPO_ROOT / "LICENSE"
        self.assertTrue(licence.is_file(), "no LICENSE at the repository root")
        text = licence.read_text(encoding="utf-8")
        self.assertIn("MIT", text)
        self.assertIn("Copyright", text)


class TestTheShippedSetIsImportClosed(unittest.TestCase):
    """Nothing shipped may import something deliberately not shipped.

    `NOT_SHIPPED` exists because `proxy.py` is wanted in the Docker image and
    nowhere else — pip, Homebrew and the .vsix all leave it out on purpose. But
    nothing checked the other direction: a shipped module that grew an
    `import proxy` passed the whole suite here in the checkout, where that
    Docker-only module is importable, and then died at import time on three of the
    five delivery surfaces. The failure lands on the user's machine, after a
    green CI run, and names a module the developer never noticed was special.

    Function-local imports count. Package modules such as `dashboard.py` and
    `cli.py` reach siblings from inside functions, so a `.body`-only walk would miss
    exactly the shape this repository already uses.
    """

    def test_no_shipped_module_imports_an_unshipped_one(self):
        import ast
        unshipped = {name[:-3] for name in NOT_SHIPPED}
        offenders = []
        for module in RUNTIME_MODULES:
            tree = ast.parse((REPO_ROOT / module).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    named = ([node.module.split(".")[0]] if node.module else
                             [alias.name.split(".")[0] for alias in node.names])
                elif isinstance(node, ast.Import):
                    named = [a.name.split(".")[0] for a in node.names]
                else:
                    continue
                offenders += [f"{module} imports {n}"
                              for n in named if n in unshipped]
        self.assertEqual(
            offenders, [],
            "a module that ships imports one that does not, so pip, Homebrew "
            "and the .vsix will fail at import time while the suite is green")

    def test_the_walk_would_notice(self):
        """Anti-vacuity: the check passes today because nothing offends, which
        is indistinguishable from a check that cannot fail. Drive it over a
        module that does offend."""
        import ast
        unshipped = {name[:-3] for name in NOT_SHIPPED}
        self.assertTrue(unshipped, "NOT_SHIPPED is empty; this guards nothing")
        target = sorted(unshipped)[0]
        found = []
        for source in (f"import {target}\n",
                       f"from {target} import bridge\n",
                       f"def f():\n    import {target}\n"):
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.ImportFrom):
                    named = ([node.module.split(".")[0]] if node.module else
                             [alias.name.split(".")[0] for alias in node.names])
                elif isinstance(node, ast.Import):
                    named = [a.name.split(".")[0] for a in node.names]
                else:
                    continue
                found += [n for n in named if n in unshipped]
        self.assertEqual(len(found), 3,
                         "the walk must catch top-level, from-, and "
                         "function-local imports alike")


if __name__ == "__main__":
    unittest.main()
