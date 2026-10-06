"""Regression checks for packaging and CI trust boundaries."""

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent

# Earlier extension identities are frozen migration inputs, not current names.
# v1.6.1 used the private suffix; v1.7.0 removed it before the full project rename.
LEGACY_EXTENSION_IDS = (
    "mlizaso.claude-usage",
    "mlizaso.claude-usage-private",
)
CURRENT_EXTENSION_ID = "mlizaso.codex-claude-usage"


# --------------------------------------------------------------------------
# Comment stripping. An assertion over a script's RAW text is satisfied by a
# commented-out copy of the very line it means to pin, so anything asserting
# "this script does X" reads code lines rather than bytes. There are THREE
# strippers because there are three dialects in this repository's packaging
# surface, and applying the wrong one is worse than applying none: it no-ops
# silently, or mis-lexes silently, while making the assertion LOOK guarded.
# Measured 2026-08-14: the `#` stripper returns
# `// fs.rmSync(targetDir, ...)` unchanged.
#
# The split used to be by COMMENT syntax alone -- one `#` stripper for bash
# and PowerShell together, one `//` stripper for JavaScript -- and that was
# one level too coarse. The three differ in their STRING-ESCAPE syntax as
# well, and a shared escape rule is wrong for two of them:
#
#   bash          `\` escapes inside "..." and unquoted; literal in '...'
#   PowerShell    backtick escapes; `\` is an ordinary path character
#   JavaScript    `\` escapes inside '...', "..." and `...`
#
# and run-docker.sh's `\"` is mis-read without the backslash rule. What the
# split does NOT rest on is install.ps1's Windows paths: measured 2026-08-14,
# both escape rules return byte-identical output (49 lines) over the shipped
# install.ps1, because consuming `\P` appends both characters anyway. The
# rules diverge only where a `\` immediately precedes a closing quote -- a
# line install.ps1 does not have today and could acquire at any time, which is
# why the split is kept and why the self-test pins a synthetic line rather
# than a real one. Block comments are dialect-specific too: `<# ... #>` is
# PowerShell's and bash has nothing of the kind, so admitting it on a bash
# file invents a comment syntax the shell does not have.
# --------------------------------------------------------------------------


# A `#` opens a line comment only where a TOKEN MAY START, in both `#`
# dialects -- it is not a comment in the middle of a word. Measured
# 2026-08-14 against the real parsers: `bash -c 'f() { echo "$#"; }; f docker
# run --name c#1 --cap-drop ALL img'` receives SEVEN arguments with
# `--cap-drop ALL` intact, and `pwsh -c 'Write-Output abc#def'` prints
# `abc#def`. Before this set existed the scan cut at every unquoted `#` and
# silently deleted a real hardening flag from such a line.
#
# The set is bash's METACHARACTERS plus whitespace, and it is deliberately
# generous, because the two error directions are not symmetric: a character
# wrongly IN it makes the scan cut early (TRUNCATING -- fail-closed for the
# required-occurrence counters that consume these strippers), while a
# character wrongly OUT of it hands a real comment back as code (ADDING --
# the dangerous half). Stating the RULE rather than an enumeration is
# deliberate: an enumeration is what rotted here twice, and the rule answers
# the questions the list cannot. `{` and `}` are reserved WORDS in bash, not
# metacharacters, so neither is a member -- which is why `${#arr[@]}` and
# `echo ${HOME}#c` both survive whole. An earlier version of this comment
# claimed `{` was a member, under the words "confirmed against a real
# parser", and thereby re-created the very defect this rule closes (it cut
# `docker run ${#arr[@]} --cap-drop ALL img` down to `docker run ${`).
#
# Two members do NOT open a comment in bash and are here on the truncating
# side of the trade -- do not read the set as "what bash opens a comment
# after", which is what the sentence this replaces said while `\r` sat in it:
#
#   `)` -- bash treats `(echo s)#c` as a comment but `f $(printf q)#c` as one
#     word ending `q#c` (measured 2026-08-14: the arguments arrive as
#     `q#c --cap-drop ALL`), and this scan cannot tell an operator `)` from a
#     substitution's closing one, so it cuts both.
#   `\r` -- measured the same day, `f hi<CR># --cap-drop ALL` passes
#     `--cap-drop ALL` through as arguments, so a mid-line CR does NOT open a
#     comment and this scan cuts where bash does not.
#
# `\r` is KEPT rather than dropped, deliberately and after measuring the
# alternative: removing it moves the scan's only error toward ADDING, which
# is the dangerous half, and its membership is never load-bearing anyway --
# in a CRLF file every `\r` sits immediately before `\n`, so reaching it
# needs a stray MID-LINE CR, and `.gitattributes` pins LF (measured
# 2026-08-14: `scripts/run-docker.sh` and `vscode-extension/scripts/install.sh`
# contain 0 CR bytes each). The members bash genuinely does open a comment
# after are whitespace, `;`, `(`, `|`, `&`, `<` and `>`.
#
# **This set is bash's, and the rule is applied to bash ONLY.** It was briefly
# a union applied to both `#` dialects, which was a NEW ADDING hole rather
# than a fix: PowerShell opens a comment after a closing quote, after `]`, and
# after most operators, so an allow-list of separators leaves real pwsh
# comments looking like code. Measured on pwsh 7.6.4 -- `Write-Output "a"# c`
# prints `a` and the next statement runs, and the same holds for `'a'` and
# `]`. A correct PowerShell rule would have to be the COMPLEMENT ("a `#`
# continues a token only after a word character"), which is a different rule
# and is not attempted; `_powershell_code_lines` therefore keeps its older
# behaviour of cutting at any unquoted `#`, which is TRUNCATING and so
# fail-closed for the required-occurrence callers that consume it. That costs
# the pwsh side of L1-1(a) and it is disclosed rather than papered over.
# (`=` is NOT a pwsh boundary, though a reviewer reported it as one:
# `Write-Output =# c` prints `=#` and `c` as two arguments.)
#
# Neither shipped file carries a `)#` or a `}#` word today (`grep '}#\|)#'`
# over both exits 1, measured 2026-08-14), so the `)` trade costs nothing
# here yet. `test_a_hash_inside_a_word_is_not_a_comment_in_either_dialect`
# drives every member of this set and pins both disagreements above as
# today's wrong answer with two EXPLICIT cases.
#
# **The derived loop by itself cannot catch a wrong member**, and an earlier
# version of this comment claimed it could: it takes its expectation from the
# same set it iterates, so it is self-satisfying. A skeptic planted `]` --
# bash-verified NOT to open a comment -- and the whole module stayed green.
# What bites is the pair of literal `{`/`}` pins beside it. A new member needs
# its own explicit case, not just a line in this set.
COMMENT_BOUNDARY_CHARS = frozenset(" \t\r\n;()|&<>")


def _bash_code_lines(body):
    """bash (`#` comments, `\\` escapes, no block comments): the statements.

    Applied to scripts/run-docker.sh and vscode-extension/scripts/install.sh,
    both of which comment with `#`. It exists for the mutant that comments a
    real statement out, which this module demonstrates end to end.

    **No standing claim is made about where the strings this module pins do
    and do not appear.** The sentence here used to assert that "every string
    this module pins appears in their comment prose as well as in their code",
    and that is false for every one of them: measured 2026-08-14, no string
    this module pins against either bash file's TEXT currently sits in that
    file's comment prose. What does sit there is `LEGACY_EXTENSION_IDS`
    (install.sh:48, the sentence describing the rename) -- pinned against the
    executed stub's call LOG, not against the file -- so the negative absolute
    is false too and must not be written in its place. Both halves are a
    property of files this module does not control: the RAW counts in
    `TestLocalInstallersRetireTheSupersededExtensionId` depend on the first
    one staying true, and one comment mentioning `--uninstall-extension` added
    to install.sh turns that test red at `2 != 1`.

    Three things this must get right, each of which a previous version got
    wrong and each of which was demonstrated rather than imagined:

    * **Comment-ONLY lines are not enough.** `foo=1  # "$code_cli"
      --uninstall-extension ...` survives a first-character test.
    * **A `#` inside a quoted string is literal**, so cutting at the first
      `#` truncates a real statement. This is not hypothetical in this
      repository: scripts/run-docker.sh's launch banner is
      `echo "... http://localhost:${PORT}/#token=${API_TOKEN}"`, and a naive
      cut drops the `/#token=${API_TOKEN}` this module asserts on. The scan
      below tracks quote state, so that line survives whole.
    * **An escaped quote does not change quote state.** `echo "say \\" here"
      # --cap-drop ALL` is one statement and one comment to bash; a scanner
      that treats the escaped `"` as a terminator believes a string is open
      at the `#` and hands the COMMENT back as code. That direction is
      fail-OPEN for a counting caller -- it ADDS an occurrence -- and it was
      demonstrated end to end: with the proxy container's real
      `--cap-drop ALL` deleted and one such decoy line added,
      `TestDockerSecurityTopology` stayed green while the shipped container
      ran unconfined. `escape="\\\\"` is what closes it, and it is bash's
      rule specifically: the same character is NOT an escape in PowerShell
      (where it is an ordinary path separator) and the escape there is the
      backtick.

    **What it does NOT provide.** A lossy, language-approximate stripper is
    unsound in both directions: it can drop text a caller needed, and -- as
    the escape case above proved -- it can add text a caller must not see.
    Which direction is dangerous depends on what the caller asserts, and that
    rule is stated at each counting call site rather than as a blanket
    placement rule here. The blanket version this replaces ("every counting
    assertion in this module is therefore taken on the RAW body ... and the
    strippers feed only the monotone ones") was false when written -- the
    hardening-flag count in `TestDockerSecurityTopology` is taken on stripped
    code, deliberately and correctly.

    **The newline fail-open is CLOSED in BOTH its spellings**, and the
    difference is worth knowing because the first attempt closed only one of
    them and then announced the mechanism closed. The mechanism is the
    per-newline quote RESET: whatever ends the line, the reset closed an open
    string, the next line's quote RE-OPENED one, and the real `#` after it
    came back as code.

    * the `\\<newline>` continuation is consumed by the escape branch;
    * a BARE newline inside a `"` string is carried by `spans_newline`,
      because bash and PowerShell both keep such a string open (measured) --
      and dropping the backslash was all it took to reproduce the identical
      defeat, one character shorter, with a container left unconfined.

    Both are pinned by
    `test_a_continued_string_does_not_swallow_the_comment_after_it`. Single
    quotes are deliberately NOT carried; `_scan`'s docstring says why, and the
    residue below is what that costs.

    **The list below is what has been FOUND, not what exists.** The sentence
    it replaces ("Nothing else below is closed") read as an inventory, and
    round 10 falsified it with the nested-substitution entry now standing
    first -- a residue that had survived two hardening commits while this
    docstring and `TestDockerSecurityTopology`'s comment both presented the
    list as complete. Treat a new construct as undisclosed until measured.

    Residue, each measured 2026-08-14 against real bash 3.2.57 and against
    this stripper, and each labelled with the DIRECTION it fails in -- ADDING
    means it hands comment or data text back as code and therefore defeats a
    required-occurrence count, which is the dangerous half:

    * **ADDING, and the only one of these whose construct is PRESENT in the
      guarded files.** `$( ... )` and backtick substitutions are not modelled
      as NESTED PARSING CONTEXTS: bash restarts quoting and comment
      recognition inside one, so a `#` there opens a comment even when the
      substitution sits inside a double-quoted word, while this scan is still
      in the `"` string and never reaches its comment branch. Measured:
      `X="$(printf %s ok  # --cap-drop ALL<newline>)"` sets `ok` under bash
      (the flag text is discarded) and comes back from here as CODE, flag
      included; the backtick spelling behaves identically in both. `${` is NOT
      part of this -- `"${PORT#x}"` is prefix removal, not a nested context.
      The ADDING face needs the substitution to span a NEWLINE, because on one
      line the comment would eat the closing `)"` and bash would reject the
      file. There is a TRUNCATING face too, which fails closed:
      `X="$(grep " #x" f)" --cap-drop ALL` comes back as `X="$(grep "`,
      dropping a real flag and LOWERING a required-occurrence count.
      Unmodelled nesting is the mechanism; closing `$(` alone would be closing
      one spelling. What guards it instead is a raw-text check on the two
      shipped bash files --
      `TestTheShippedScriptsStayInsideWhatTheseStrippersLex`'s bash half,
      built on `_bash_nested_substitutions` -- covering the ADDING face only,
      the truncating one needing no guard. **Read that helper's docstring
      before relying on it:** it is fail-closed in two named cases and NOT in
      general, and the constructs that would blind it are FORBIDDEN in those
      two files rather than parsed, because two skeptics defeated the lexing
      version end to end.

    * **ADDING.** A bare multi-line SINGLE-quoted string. `'` is not in
      `spans_newline` for the reason `_scan`'s docstring gives, so
      `x='abc<newline>def'  # --cap-drop ALL` returns the comment as code --
      verified against bash 3.2.57, which parses that file and prints
      `abc<newline>def`. This is the residue the double-quote carry does NOT
      buy, and it is named here because an earlier version of this docstring
      disclosed the multi-line-string hole in general and a later one DELETED
      that disclosure while closing only half of it.
    * **ADDING.** `$'...'` ANSI-C quoting. `\\'` does not end the string there
      (`x=$'a\\'b'` sets `a'b`, verified), but this scan closes the string at
      that `'` and re-opens at the next, so
      `x=$'a\\'b'  # --cap-drop ALL` returns the whole line, comment included.
    * **ADDING.** The `: '...'` block-comment idiom, and heredocs in both
      forms (`<<EOF` and `<<'EOF'`). All three are quoted or redirected DATA
      to bash and executable-looking text to this scan, so a body line reading
      `--cap-drop ALL` is counted. The `: '...'` one defeats the hardening
      count on RAW text as well, so it is not a placement question.
    * **CLOSED, and it used to be a TRUNCATING residue here.** A `#` that is
      not a comment because bash requires a word boundary: `echo abc#def
      --keep` prints `abc#def --keep` and `${PORT#x}` is prefix removal,
      while this scan used to cut at the `#` and return `echo abc` /
      `echo ${PORT`, silently deleting a real flag from
      `docker run --name c#1 --cap-drop ALL img`. `COMMENT_BOUNDARY_CHARS`
      above is what closes it, and it is NOT a whitespace test -- `echo hi;#
      c` prints `hi`, so `;` opens a comment too, and a boundary rule that
      misses an operator would turn this into an ADDING residue. What
      SURVIVES is the truncating half of that trade, and it is exactly two
      characters, both named in that comment and both pinned by
      `test_a_hash_inside_a_word_is_not_a_comment_in_either_dialect`: a word
      ending in `)` before a literal `#` (`f $(printf q)#c`), because this
      scan cannot tell an operator `)` from a substitution's closing one, and
      a mid-line `\\r` before one. `}` is NOT among them and this sentence
      said it was: `}` is not in `COMMENT_BOUNDARY_CHARS` at all, and
      `echo ${HOME}#c --cap-drop ALL` is returned WHOLE, which is what bash
      does with it.
    * A blanket "carry quote state across the newline" would close the first
      two and is REFUSED: one stray apostrophe would then hold a string open
      over the rest of the file and hand every later comment back as code,
      which is a strictly worse adding path than the three above.
    * Not this function's to fix at all: `.count()` counts an INERT
      occurrence, so replacing a live flag with `[ -z "--cap-drop ALL" ]`
      keeps the count and drops the flag. That is a property of the caller's
      instrument, not of the lexer.
    """
    return _scan(body, line_comment="#", block=None, quotes="'\"",
                 spans_newline='"', comment_needs_boundary=True,
                 escape="\\", escape_in='"')


def _powershell_code_lines(body):
    """PowerShell (`#` and `<# #>` comments, backtick escapes): the statements.

    Its own function rather than a flag on the bash one, for the reason
    `_js_code_lines` is its own function: the dialects disagree about what a
    character means, and one shared rule silently applies the wrong one.
    Measured 2026-08-14 on pwsh 7.6.4, `"a\\"b"` is a ParserError in
    PowerShell -- so backslash is NOT an escape here -- while the backtick IS,
    and a backtick-escaped quote defeats exactly the assertion the bash escape
    defeats. Note what this does NOT rest on: applying bash's rule to the
    shipped install.ps1 yields byte-identical output, so its Windows paths are
    not the reason. The rules diverge only where a `\\` immediately precedes a
    closing quote.

    Two PowerShell-only rules the bash stripper must not have:

    * **`<# ... #>` is a block comment.** bash has nothing of the kind, and
      admitting it there invents a comment syntax the shell does not have.
    * **A `<#` inside a line comment opens no block.** `# open with <#` is one
      comment and nothing more -- verified against an independent PowerShell
      lexer in the round-7 record. What produces that here is the single-pass
      scan: the `#` starts a line comment which consumes the rest of the line,
      so the `<#` inside it is never examined at all. A `<#.*?#>` regex over
      the whole body did the opposite and let a genuinely executable line be
      swallowed between two ordinary `#` comments.

      Note what is NOT load-bearing, because the obvious reading is wrong:
      the block-opener check sits above the line-comment check, but swapping
      them changes nothing. Both marker pairs are prefix-disjoint (`<#` vs
      `#`, `/*` vs `//`), so at each position at most one can match. Measured
      -- the swap is an equivalent mutant, green on the whole module. The
      order is documentation, not mechanism.

    Residue: `""` doubling inside a double-quoted string is not modelled. It
    happens to be harmless, because an even number of quote characters leaves
    the scan's state where it found it; an ODD one is a PowerShell parse
    error rather than a mutant. Single-quoted `''` doubling is the same
    shape. A string continued with a trailing BACKTICK is handled -- it is the
    same `_scan` branch bash's continuation goes through, and pwsh 7.6.4 was
    the authority: `Write-Output "abc`<nl>def"` prints two lines, so the
    newline survives in the value while the string stays open across it, and
    only the second half is what comment stripping depends on.

    **Here-strings (`@" ... "@`, `@' ... '@`) are not recognised, and the
    single-quoted one is worse than "unrecognised".** The mechanism is not
    the obvious one and was measured, not reasoned about:

    * `@" ... "@` with an EVEN number of `"` in its body is harmless-ish. The
      `@` is an ordinary character, the `"` opens a string that `spans_newline`
      carries, and a `<#` inside it is never examined at all -- the body comes
      back as CODE. That is the ADDING direction, the same one the residue
      above names, not blindness.
    * `@' ... '@` is the dangerous one, because `'` is deliberately NOT in
      `spans_newline`: the quote state resets at every newline, so a `<#`
      anywhere in the body IS examined, outside any string, and opens a block
      comment. Everything up to the next `#>` -- or, with none, the entire
      remainder of the file -- is dropped, and the stripper goes BLIND rather
      than approximate. An odd-`"` `@" ... "@` body reaches the same state.

    Two things bound it, and neither is the old note's bare "install.ps1 has
    none", which is worth nothing for a guard whose job is to notice the file
    acquiring one TOMORROW:

    * `_scan` now RAISES on a body that ends inside a block comment, so the
      swallow-to-end-of-file shape cannot be silent. An unterminated `<#` is
      a hard PowerShell parse error (pwsh 7.6.4: "The terminator '#>' is
      missing from the multiline comment."), so this can never fire on a
      script that actually parses.
    * That guard does NOT catch a body that re-closes with `#>`. Measured
      2026-08-14, `$d = @'<nl><# lead #><nl>'@<nl>$keep = 1` is valid
      PowerShell that pwsh runs, and this scan silently loses the region
      between the two markers while leaving its state clean.
      `TestTheShippedScriptsStayInsideWhatTheseStrippersLex` is what covers
      that: it fails if install.ps1 acquires any of `@"`, `@'`, `"@`, `'@`,
      `<#` or `#>` at all.

    A string continued by a bare newline with no backtick is handled by
    `spans_newline` for `"` only; the single-quoted form is the same residue
    `_bash_code_lines` names, and closing it needs the blanket carry that
    docstring refuses.
    """
    return _scan(body, line_comment="#", block=("<#", "#>"), quotes="'\"",
                 spans_newline='"', comment_needs_boundary=False,
                 escape="`", escape_in='"')


def _js_code_lines(body):
    """`//` and `/* */` JavaScript: the executable statements.

    Its own function because the `#` strippers SILENTLY NO-OP on JavaScript
    -- they return a `// `-prefixed line unchanged -- so routing a JS assertion
    through one of them would leave it exactly as defeatable while
    reading as guarded. That is the fail-open outcome this trio exists to
    avoid, and it is why they are separate functions rather than one with a
    flag.

    JavaScript's escape is `\\`, and unlike bash's it is honoured inside
    single quotes and backticks too, so it is passed for all three quote
    characters.

    Same residue as above, plus: a regex literal containing `//` would be
    mis-stripped. copy-python.js holds none today.

    **The block-comment swallow `_powershell_code_lines` describes is much
    NARROWER here, and the obvious reading is wrong.** A JS template literal
    is multi-line by design and the backtick IS in `spans_newline`, so a `/*`
    inside one is inside a string and is never examined -- measured
    2026-08-14, `const t = \\`<nl>/* lead<nl>\\`; keep();` strips to both
    statements intact. copy-python.js is full of template literals and none of
    them can blind this scan. What remains is the unterminated `/*` outside a
    string, which is a JS syntax error and which `_scan` now RAISES on rather
    than silently dropping the file's tail;
    `TestTheShippedScriptsStayInsideWhatTheseStrippersLex` adds the
    conservative belt (copy-python.js carries no `/*` at all today).

    No `comment_needs_boundary` here: `//` opens a comment anywhere outside a
    string in JavaScript, so the separator allow-list BASH needs would be
    wrong for this one. (This sentence used to say "the two `#` dialects",
    which is the same false instruction `_scan`'s docstring carried:
    `_powershell_code_lines` does not take the flag either, and was measured
    to become an ADDING hole when it did.)
    """
    return _scan(body, line_comment="//", block=("/*", "*/"), quotes="'\"`",
                 spans_newline="`",
                 escape="\\", escape_in="'\"`")


def _scan(body, line_comment, block, quotes, escape=None, escape_in="",
          spans_newline="", comment_needs_boundary=False):
    """Shared state machine. Order of precedence: escape, quote, block, comment.

    `comment_needs_boundary` is BASH's rule and is passed by
    `_bash_code_lines` alone. Read the flag as an ALLOW-LIST of separators
    (`COMMENT_BOUNDARY_CHARS`), not as "the token-boundary idea": that idea is
    true of PowerShell too -- `pwsh -c 'Write-Output abc#def'` prints
    `abc#def` -- but pwsh's correct rule is the COMPLEMENT of this one (a `#`
    continues a token only AFTER a word character; pwsh opens a comment after
    a closing quote, after `]` and after most operators), so passing this
    allow-list there is an ADDING hole rather than a fix, and it is not
    attempted. JavaScript needs no boundary rule at all: `//` opens a comment
    anywhere outside a string. This is a contract a docstring cannot enforce,
    so it is executable --
    `test_a_hash_inside_a_word_is_not_a_comment_in_either_dialect` reds if the
    flag is passed to `_powershell_code_lines`.

    It RAISES rather than returning a truncated list when the body ends inside
    a block comment. Both dialects that pass a `block` treat an unterminated
    opener as a syntax error, so no script that parses can reach it -- but a
    here-string or template literal carrying `<#` / `/*` opens one this scan
    never closes, and the whole remainder of the file is then dropped. Silent
    truncation of the file's tail is the one failure a caller asserting an
    ABSENCE cannot notice, so it is made loud here. It does not cover a body
    that re-closes with `#>` / `*/`; see the strippers' docstrings for the
    test that does.

    `escape` and `escape_in` are the dialect's, not a shared default: the same
    character means "escape" in one language and "path separator" in the next
    (see the three strippers above). `escape_in` names the quote characters
    inside which the escape is honoured -- bash and PowerShell honour theirs
    inside double quotes but not single, JavaScript honours its own inside all
    three. Outside any quote the escape is honoured whenever one is given,
    which is true of all three dialects.

    `spans_newline` names the quote characters whose string survives a BARE
    newline, and it is per-dialect because the dialects genuinely disagree.
    Measured 2026-08-14: `echo "abc<nl>def"` runs under bash 3.2.57 and
    `Write-Output "abc<nl>def"` runs under pwsh 7.6.4, while node raises
    SyntaxError for a `"` spanning a raw newline and accepts it only for a
    backtick template. Without this, closing the ESCAPED continuation alone
    left the identical fail-open one character shorter: drop the backslash and
    the reset below still ended the string at end of line, the next line's
    quote RE-OPENED one, and the real `#` after it came back as code. That was
    demonstrated defeating the hardening count with a container left
    unconfined, and it is why this is a carry rather than an escape rule.

    Single quotes are deliberately NOT carried in bash or PowerShell even
    though their strings do span newlines there. The carry makes an ODD quote
    hold a string open to end of file, and every `#` inside it comes back as
    code -- the ADDING direction. An apostrophe is the realistic odd quote (a
    `: '...'` block, an unrecognised heredoc), whereas an odd `"` is not
    idiomatic in these files. Verified on all four shipped files: each has an
    EVEN number of unquoted `"` per logical line, so the carry propagates
    nowhere today.
    """
    block_open, block_close = block if block else (None, None)
    out, current, quote, in_block = [], [], None, False
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "\n":
            if quote is not None and quote in spans_newline:
                # The string is still open in the real parser, so the logical
                # line is not over. Keep the newline in `current` so a joined
                # statement cannot fuse two tokens into one.
                current.append(ch)
                i += 1
                continue
            statement = "".join(current).strip()
            if statement:
                out.append(statement)
            current, quote = [], None
            i += 1
            continue
        if in_block:
            if body.startswith(block_close, i):
                in_block = False
                i += len(block_close)
                continue
            i += 1
            continue
        # The escaped character is consumed WITH its escape and never
        # examined, so an escaped quote cannot open or close a string and an
        # escaped comment marker cannot start a comment.
        #
        # An escape immediately before a NEWLINE is the dialect's line
        # continuation, and both characters are consumed together WITHOUT
        # ending the logical line and WITHOUT resetting quote state. That is
        # the whole of the newline-continuation fix: the branch used to
        # decline (`following != "\n"`) and leave the newline to the reset
        # branch below, which closed an open string at end of line so that the
        # NEXT line's closing quote RE-OPENED one and swallowed the real `#`
        # comment after it -- handing that comment back as code, the ADDING
        # direction. Demonstrated end to end before the fix, and pinned by
        # `test_a_continued_string_does_not_swallow_the_comment_after_it`.
        #
        # Verified against the real parsers 2026-08-14 rather than reasoned
        # about, because the three dialects agree on continuation for
        # different reasons: `echo "abc\<nl>def"` prints `abcdef` under bash
        # 3.2.57 and `const s = "abc\<nl>def"` is `abcdef` under node, while
        # `Write-Output "abc`<nl>def"` prints TWO lines under pwsh 7.6.4 --
        # PowerShell keeps the newline in the value. All three agree on the
        # only thing this scan needs: the string stays OPEN across the break,
        # so the quote that follows is a closing one and the `#` after it is
        # a comment.
        #
        # Joining is the safe direction here and cannot add comment text: the
        # branch fires only where the escape is honoured (outside quotes, or
        # inside `escape_in` ones), so bash's `'a\<nl>b'` -- where the
        # backslash is literal -- still falls through to the reset below.
        if escape is not None and ch == escape and (
                quote is None or quote in escape_in):
            following = body[i + 1: i + 2]
            if following == "\n":
                i += 2
                continue
            if following:
                current.append(ch)
                current.append(following)
                i += 2
                continue
        if quote is not None:
            current.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in quotes:
            quote = ch
            current.append(ch)
            i += 1
            continue
        # Both marker pairs are prefix-disjoint, so at most one of the next
        # two can match at any position and this order is documentation
        # rather than mechanism -- swapping them is an equivalent mutant
        # (measured). What makes `# open with <#` a comment and nothing more
        # is the line-comment branch consuming to end of line.
        if block_open is not None and body.startswith(block_open, i):
            in_block = True
            i += len(block_open)
            continue
        if body.startswith(line_comment, i) and (
                not comment_needs_boundary
                or i == 0
                or body[i - 1] in COMMENT_BOUNDARY_CHARS):
            while i < n and body[i] != "\n":
                i += 1
            continue
        current.append(ch)
        i += 1
    if in_block:
        raise ValueError(
            "the body ends inside an unterminated %r block, so this scan "
            "dropped everything after it -- see _scan's docstring"
            % (block_open,))
    statement = "".join(current).strip()
    if statement:
        out.append(statement)
    return out


def _bash_nested_substitutions(body):
    """Every `$( )` / backtick substitution opened INSIDE a double-quoted word.

    Returns `(text, spans_newline, carries_hash)` per opener, on RAW bash
    text. It exists because `_scan` does not model a substitution as a NESTED
    PARSING CONTEXT: bash restarts comment recognition inside one, so
    `X="$(cmd  # --cap-drop ALL<newline>)"` is a comment to bash and CODE to
    the stripper -- the ADDING direction, which defeats a required-occurrence
    count. See `_bash_code_lines`' residue list.

    Three deliberate choices, each measured rather than reasoned about:

    * **RAW placement.** This detects a VIOLATION, and by this module's own
      polarity rule a violation is counted on raw text: feeding it the
      stripper's output would let the blindness it guards lower what it sees.
      That costs a small outer-level lexer here (quotes, the bash escape, and
      outer `#` comments), because install.sh carries 10 backtick CHARACTERS
      across 6 comment lines and every one of them is inside comment PROSE --
      a detector that did not skip comments would report them. Both figures
      re-derived 2026-08-14; the version this replaces said "10 backticks"
      where a reader took it for 10 comments, so both units are named now.
    * **`$(` and the BACKTICK, never `${`.** Both substitution spellings have
      the identical defect (verified under bash 3.2.57: both decoys evaluate
      to `ok`, discarding the flag text). `${` is parameter expansion, not a
      nested context -- pushing on it would flag `"${PORT#x}"`, which is
      prefix removal and which the stripper already returns whole.
    * **Two narrow fail-closed cases, and NOT a general guarantee.** An
      opener with no close before end of body, and a region whose quotes do
      not balance, are each reported as both spanning and hash-carrying. An
      earlier version of this bullet claimed the general form -- "a construct
      this scan does not understand reds the guard rather than passing it" --
      and two skeptics refuted it independently, each defeating the guard end
      to end with the proxy container left unconfined. What they found is
      what is now written down:

      - a quoted `)` inside the region closed it early, so the scan resumed
        past the real `#` with the outer `"` still open and reported the
        substitution CLEAN. Closed: the region is lexed for quotes now.
      - `$'...'` ANSI-C quoting, and a heredoc BODY, invert this scan's OUTER
        quote parity against bash and silence it for the whole REST OF THE
        FILE -- measured at 17 substitutions found dropping to 1 on the clean
        run-docker.sh. **That one is not lexed. It is FORBIDDEN**, by
        `test_the_shipped_bash_avoids_what_blinds_the_substitution_scan`,
        which is fail-closed by construction rather than by out-lexing bash.

      The general lesson is in the code because it keeps being relearned: a
      lexer-shaped belt can always be defeated by a construct its author did
      not model, so where the construct is absent from the guarded files the
      honest guard forbids it rather than parsing it.

    What it does NOT cover: the TRUNCATING face of the same unmodelled
    nesting (`X="$(grep " #x" f)" --cap-drop ALL` -> `X="$(grep "`). That one
    lowers a required-occurrence count and therefore fails closed at the
    counter; it needs no guard here. Nor is this a lexer: an inner quoted `#`
    inside a substitution is reported like any other, which is fail-closed
    noise a maintainer resolves by teaching the stripper.
    """
    found = []
    i, n, quote = 0, len(body), None
    while i < n:
        ch = body[i]
        if ch == "\\" and quote != "'":
            i += 2
            continue
        if quote is None:
            if ch in "'\"":
                quote = ch
            elif ch == "#" and (i == 0 or body[i - 1] in COMMENT_BOUNDARY_CHARS):
                while i < n and body[i] != "\n":
                    i += 1
                continue
            i += 1
            continue
        if ch == quote:
            quote = None
            i += 1
            continue
        if quote == '"' and (body.startswith("$(", i) or ch == "`"):
            opener = "$(" if ch == "$" else "`"
            j, depth = i + len(opener), 1
            spans_newline = carries_hash = closed = False
            # Quote state INSIDE the substitution. Counting parens blindly
            # let a quoted `)` close the region early, after which the scan
            # resumed past the real `#` with the outer `"` still open and
            # reported the substitution CLEAN -- the M1-1 defeat surviving the
            # fix written to close it. Measured on the shipped files THROUGH
            # THIS HELPER, so the figure is re-derivable: 14 of
            # run-docker.sh's 17 substitution regions and 3 of install.sh's 4
            # contain a quote character, so "flag any region with a quote"
            # would fire on 17 of 21 and is not available; the region has to
            # be lexed.
            inner_quote = None
            while j < n:
                inner = body[j]
                if inner == "\\" and inner_quote != "'":
                    j += 2
                    continue
                if inner == "\n":
                    spans_newline = True
                    j += 1
                    continue
                if inner_quote is not None:
                    if inner == inner_quote:
                        inner_quote = None
                    j += 1
                    continue
                if inner in "'\"":
                    inner_quote = inner
                elif inner == "#":
                    carries_hash = True
                elif opener == "$(" and inner == "(":
                    depth += 1
                elif opener == "$(" and inner == ")":
                    depth -= 1
                    if depth == 0:
                        closed = True
                        break
                elif opener == "`" and inner == "`":
                    closed = True
                    break
                j += 1
            # An unbalanced quote inside the region means the lexing above is
            # not trustworthy for it, so it is reported rather than cleared.
            if inner_quote is not None:
                spans_newline = carries_hash = True
            if not closed:
                spans_newline = carries_hash = True
            else:
                # An early close cannot be recognised, so do not try. After the
                # region closes, look for a `#` to the end of the PHYSICAL LINE
                # -- no quote lexing, no stopping at a quote -- and if one is
                # there, report the region as both spanning and hash-carrying.
                #
                # WHY NOT SOMETHING SMARTER. Round 12 lexed this tail as if it
                # were still inside the enclosing double-quoted word, and two
                # reviewers defeated it with one extra `"`: after an early close
                # bash is still INSIDE the substitution, where a `"` opens a
                # nested word rather than ending the outer one. A judge then
                # defeated the obvious repair too -- "require the outer closing
                # quote to be adjacent" -- with
                #
                #     Z="$(case $y in a)"printf" ok  # --cap-drop ALL
                #
                # which puts the quote exactly where that rule wants it. The
                # conclusion is the one to keep: AFTER AN EARLY CLOSE THIS SCAN
                # DOES NOT KNOW WHAT CONTEXT BASH IS IN, so every rule about
                # where the word ends is a guess and the attacker picks the
                # shape that beats it. This rule makes no such guess.
                #
                # THE COST, STATED RATHER THAN HIDDEN. It flags a legitimate
                # trailing comment on a substitution line --
                # `X="$(printf ok)"  # an ordinary comment` -- because that `#`
                # is indistinguishable, to a scanner that will not guess where
                # the word ends, from the decoy above. Measured on the shipped
                # files: 0 of run-docker.sh's 17 regions and 0 of install.sh's 4
                # are flagged today, so the cost is zero now and the failure
                # mode is a LOUD red rather than a silent blinding -- the same
                # trade the `<<` literal ban makes. A maintainer who hits it
                # moves the comment to its own line.
                #
                # It also fixes the OTHER direction round 12 got wrong, in two
                # ways. A `#` on a LATER line of a multi-line double-quoted word
                # no longer fires, because the search stops at the newline. And
                # the `#` must sit at a TOKEN BOUNDARY -- the module's own
                # `COMMENT_BOUNDARY_CHARS` rule, which is bash's -- so a URL
                # fragment does not fire: `URL="http://$(hostname):8080/#x"` is
                # clean, and that shape is not hypothetical, since
                # scripts/run-docker.sh already carries a `/#token=` fragment in
                # a double-quoted word.
                #
                # What DOES still false-positive is a trailing comment on a
                # substitution line: `X="$(printf ok)"  # note`. That `#` is at
                # a boundary and is genuinely indistinguishable from the decoy
                # unless the scan guesses where the word ends, which is the
                # guess this whole rule exists to refuse. Zero occurrences
                # today; a maintainer who hits it moves the comment to its own
                # line.
                k = j + 1
                while k < n and body[k] != "\n":
                    if body[k] == "#" and body[k - 1] in COMMENT_BOUNDARY_CHARS:
                        spans_newline = carries_hash = True
                        break
                    k += 1
            found.append((body[i:min(j + 1, n)], spans_newline, carries_hash))
            i = j + 1
            continue
        i += 1
    return found


def _fully_quoted_list(group, quotes="\"'"):
    """Every comma-separated item of `group`, or [] if ANY item is not a literal.

    ALL-OR-NOTHING on purpose. The previous version of the caller below
    harvested double-quoted names with `re.findall` and returned whatever it
    found, so a single item respelled with single quotes silently produced a
    SMALLER list -- and a smaller list is a smaller sandbox, with the executed
    harness then reaching a real `code-insiders.cmd` on PATH while still
    passing. Anything this cannot fully account for -- a variable, a
    subexpression, a splat, a concatenation, an escape -- yields [], which the
    caller turns into a loud failure. Never a best effort.
    """
    items = []
    for part in group.split(","):
        part = part.strip()
        if len(part) < 2 or part[0] not in quotes or part[-1] != part[0]:
            return []
        inner = part[1:-1]
        if part[0] in inner or "`" in inner or "\\" in inner or "$" in inner:
            return []
        items.append(inner)
    return items


def _code_cli_candidate_names(powershell_installer):
    """The names install.ps1's `Find-CodeCli` asks `Get-Command` for.

    Read out of the script rather than hand-listed, so the executed harness's
    sandbox cannot drift away from the loop it has to capture. Returns [] --
    which the caller turns into a loud failure rather than a silently smaller
    sandbox -- on anything it does not fully understand.

    Two things it now gets right that a bare `re.search` over the whole file
    did not, both verified rather than reasoned about:

    * **It is ANCHORED inside `Find-CodeCli`'s body.** Unanchored, the search
      took the FIRST `foreach ($name in @(...))` anywhere in the file, so an
      unrelated loop added above the function would hand back a non-empty list
      containing none of the CLI names -- a sandbox built out of names the
      installer never asks for, passing the caller's `assertTrue`.
    * **The `@(...)` group must parse ENTIRELY as quoted literals**, either
      quote character, via `_fully_quoted_list`. Single-quoting one of the
      four names used to drop it silently.
    """
    function = re.search(
        r"function\s+Find-CodeCli\s*\{(.*?)\n\}", powershell_installer, re.S)
    if not function:
        return []
    match = re.search(
        r"foreach\s*\(\s*\$name\s+in\s+@\(([^()]*)\)\s*\)", function.group(1))
    if not match:
        return []
    return _fully_quoted_list(match.group(1))


def _shell_code_cli_candidate_names(shell_installer):
    """The names install.sh's `find_code_cli` asks `command -v` for.

    The twin of the function above, and it exists because the pwsh half was
    hardened alone for a round: `_run_install_sh` hard-coded a single stub
    named `code` and read nothing out of install.sh, so its sandbox was
    hermetic only because `code` happens to come FIRST in
    `for name in code code-insiders`. Reordering that one loop was measured to
    send the real ambient CLI both `--uninstall-extension` and
    `--install-extension` while the shipped test stayed green.

    Same all-or-nothing rule: anchored inside the function, and every word of
    the `for` list must be a bare literal name. A variable, a glob, a command
    substitution or a quote yields [], which the caller fails loudly on.

    **Reading the names is only half of hermeticity, and the other half is
    `_shell_code_cli_producers_above_the_name_loop` beside this.** `find_code_cli`
    has a SECOND producer -- a `for path in "/Applications/Visual Studio
    Code.app/.../bin/code" ...` loop whose absolute paths no PATH stubbing can
    intercept.
    """
    function = re.search(
        r"find_code_cli\s*\(\s*\)\s*\{(.*?)\n\}", shell_installer, re.S)
    if not function:
        return []
    match = re.search(r"for\s+name\s+in\s+([^;\n]*?)\s*;?\s*do\b",
                      function.group(1))
    if not match:
        return []
    names = match.group(1).split()
    if not names or not all(
            re.fullmatch(r"[A-Za-z0-9._-]+", name) for name in names):
        return []
    return names


def _shell_code_cli_producers_above_the_name_loop(shell_installer):
    """Lines in `find_code_cli` that emit a CLI path before the name loop runs.

    Returns the offending lines, so `[]` means the PATH-stubbable `for name in
    ... command -v` loop is the function's first producer and the harness's
    sandbox is hermetic BY CONSTRUCTION rather than by an unpinned ordering.

    `find_code_cli` ends with a second producer the sandbox cannot reach: two
    hard-coded absolute paths under `/Applications/Visual Studio Code*.app`.
    `_run_install_sh` prepends a directory of stubs to PATH, which is
    structurally incapable of redirecting an absolute path, and the first of
    those two paths EXISTS and is executable on an ordinary developer machine
    -- it is the real Microsoft launcher on this one. Today the name loop runs
    first and always succeeds against the stubs, so the path loop is
    unreachable; nothing in the script, the extractor or (until now) this
    module pinned that ordering.

    **This is a PRE-EXECUTION refusal, and the ordering of the two halves is
    the whole value.** Reordering the two loops does already red the executed
    test -- an ambient CLI never writes `$STUB_LOG`, so its `assertIn`s fail
    on an empty log -- but only AFTER the run has sent this developer's real
    VS Code `--uninstall-extension mlizaso.claude-usage-private` and
    `--install-extension <junk>.vsix --force`. A post-hoc red is worth
    nothing here; refusing before `subprocess.run` is what prevents the side
    effect. Deliberately NOT done: asserting the `/Applications` paths do not
    exist on the host, which would red the suite on every contributor machine
    with VS Code installed, this one included.
    """
    function = re.search(
        r"find_code_cli\s*\(\s*\)\s*\{(.*?)\n\}", shell_installer, re.S)
    if not function:
        return ["find_code_cli could not be read out of install.sh"]
    body = function.group(1)
    loop = re.search(r"for\s+name\s+in\s+[^;\n]*?\s*;?\s*do\b", body)
    if not loop:
        return ["find_code_cli has no `for name in ... do` loop"]
    return [line.strip() for line in body[: loop.start()].splitlines()
            if re.search(r"\b(echo|printf|return)\b", line)
            and not line.lstrip().startswith("#")]


# PowerShell predicates whose truth value differs between Windows and the
# POSIX hosts this suite can execute. See
# `test_install_ps1_has_no_platform_conditional`.
#
# **These are bare NAMES, deliberately carrying no `$`.** The `$` was an
# anchor, and every scope-qualified spelling of the same automatic variable
# walked around it: measured 2026-08-14 on pwsh 7.6.4, `$global:IsWindows`,
# `$script:IsMacOS`, `${global:IsWindows}` and `$private:IsLinux` all read the
# genuine automatic variable (they print `False`/`True` on this POSIX host,
# not empty), so `if (-not $global:IsWindows) { ...removal... }` is alive here
# and dead on Windows -- and matched nothing while the list held `$IsWindows`.
# Dropping the anchor catches every qualifier, the braced forms, and a
# backtick inside the QUALIFIER (`$gl`+backtick+`obal:IsWindows`), with no
# enumeration of scopes to keep in sync. `env:os` keeps its provider prefix
# because it is not a scope and cannot be qualified: `$global:env:OS` prints
# empty on pwsh 7.6.4, so that one spelling is not an evasion at all.
#
# `IsWindows` does not travel alone. PowerShell Core defines `IsLinux` and
# `IsMacOS` beside it, and the sibling spelling is the DANGEROUS one: measured
# 2026-08-14, wrapping the whole removal block in
# `if ($IsLinux -or $IsMacOS) { ... }` left this module fully green while
# `if (-not $IsWindows)` around the same block turned this test red -- the
# executed twin runs the branch on POSIX and sees the removal work, while on
# Windows, the only platform install.ps1 targets, it is dead. **That
# experiment is no longer reproducible and the sentence is kept as history**:
# it was measured on a module whose list held `IsWindows` without its
# siblings, and with all five listed the sibling mutation now reds here too.
# No test count is quoted, here or below: the two that were ("GREEN at 29
# tests", written when 29 was exact) rotted across the next two commits, and
# the criterion was always the claim that mattered. Anything added here must
# bring its whole family.
POWERSHELL_PLATFORM_PREDICATES = (
    "env:OS", "IsWindows", "IsLinux", "IsMacOS", "PSVersionTable",
)


class TestExtensionSecurityManifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = json.loads(
            (ROOT / "vscode-extension" / "package.json").read_text(
                encoding="utf-8"
            )
        )

    def test_private_identity_and_local_execution(self):
        self.assertEqual(self.package["name"], "codex-claude-usage")
        self.assertIs(self.package["private"], True)
        self.assertEqual(self.package["publisher"], "mlizaso")
        self.assertEqual(
            self.package["publisher"] + "." + self.package["name"],
            CURRENT_EXTENSION_ID)
        self.assertEqual(self.package["extensionKind"], ["ui"])

    def test_process_settings_are_machine_scoped(self):
        properties = self.package["contributes"]["configuration"]["properties"]
        for name in (
            "codexClaudeUsage.pythonPath",
            "codexClaudeUsage.cliPath",
            "codexClaudeUsage.port",
        ):
            self.assertEqual(properties[name]["scope"], "machine")

    def test_all_contribution_identifiers_use_the_new_name(self):
        contributes = self.package["contributes"]
        self.assertEqual(
            {command["command"] for command in contributes["commands"]},
            {"codexClaudeUsage." + suffix
             for suffix in ("open", "rescan", "restart", "showLogs")})
        self.assertEqual(
            [container["id"]
             for container in contributes["viewsContainers"]["activitybar"]],
            ["codexClaudeUsageSidebar"])
        self.assertEqual(set(contributes["views"]), {"codexClaudeUsageSidebar"})
        self.assertEqual(
            [view["id"] for view in contributes["views"]["codexClaudeUsageSidebar"]],
            ["codexClaudeUsage.dashboard"])
        self.assertEqual(
            set(contributes["configuration"]["properties"]),
            {"codexClaudeUsage." + suffix
             for suffix in ("pythonPath", "cliPath", "port")})

    def test_package_has_no_publish_script(self):
        self.assertNotIn("publish", self.package["scripts"])

    def test_package_recreates_generated_outputs(self):
        scripts = self.package["scripts"]
        self.assertIn("npm run clean", scripts["vscode:prepublish"])
        self.assertIn("npm run copy-python", scripts["vscode:prepublish"])

        # Read the CODE, not the file. `// fs.rmSync(targetDir, ...)` left
        # this test green while the packager no longer removed the stale
        # tree, and `// if (targetStat.isSymbolicLink() ...` left it green
        # with the symlink refusal -- a security guard on a packaging surface
        # AGENTS.md names -- commented out. Both measured 2026-08-14.
        # `_js_code_lines`, not `_hash_code_lines`: this file is JavaScript,
        # and the `#` stripper returns a `// `-prefixed line unchanged.
        copy_script = "\n".join(_js_code_lines(
            (ROOT / "vscode-extension" / "scripts" / "copy-python.js")
            .read_text(encoding="utf-8")
        ))
        self.assertIn("fs.rmSync(targetDir", copy_script)
        self.assertIn("isSymbolicLink()", copy_script)

    def test_local_installers_always_rebuild_locked_dependencies(self):
        """The supply-chain commands must be code, not prose about code.

        All six of these assertions used to read the installers' raw text, so
        a `# ` in front of any one of the three commands, in either installer,
        left the whole module green -- measured six ways, one mutation at a
        time. `npm ci --ignore-scripts ...` is the consequential one: without
        it `npm run package` builds the .vsix against whatever node_modules
        happens to be on disk, which is precisely what the comment beside it
        and the `assertNotIn` below exist to forbid.

        Nothing else covers these: this module is the only reader of either
        installer in `tests/`, and BOTH executed installer tests pass an
        explicit .vsix, so the no-argument branch holding all four commands
        never runs in either of them. (This paragraph used to end "install.ps1
        has no executed test at all (no `pwsh` on this host)", which the
        executed twin below falsified on 2026-08-14.)
        """
        scripts_dir = ROOT / "vscode-extension" / "scripts"
        shell_installer = (scripts_dir / "install.sh").read_text(encoding="utf-8")
        powershell_installer = (scripts_dir / "install.ps1").read_text(
            encoding="utf-8"
        )
        command = "npm ci --ignore-scripts --omit=optional --no-audit --no-fund"
        # Each installer gets ITS dialect's stripper. Sharing one is what let
        # a backtick-escaped quote in install.ps1 turn the `npm ci` line's
        # trailing comment into code -- measured, and green.
        for name, raw, strip in (
            ("install.sh", shell_installer, _bash_code_lines),
            ("install.ps1", powershell_installer, _powershell_code_lines),
        ):
            with self.subTest(installer=name):
                code = "\n".join(strip(raw))
                self.assertIn(command, code)
                self.assertIn("npm audit signatures", code)
                self.assertIn(
                    "npm audit --ignore-scripts --audit-level=low", code)
        # Deliberately RAW. A negative assertion cannot be satisfied by adding
        # a comment -- a commented-out `[ -d node_modules ] ||` still fails it
        # -- so stripping would only weaken it, by letting the forbidden guard
        # hide inside a `<# ... #>` block or behind a quoted `#`.
        self.assertNotIn("[ -d node_modules ] ||", shell_installer)


class TestLocalInstallersRetireTheSupersededExtensionIds(unittest.TestCase):
    """`--force` overwrites the same id; it does not remove a different one.

    Both earlier extension ids must be retired when installing the renamed
    extension. Installing the new .vsix alone leaves old extensions installed
    and able to start their own dashboard servers. The new contribution ids
    use codexClaudeUsage; the old ids here are intentional migration fixtures.

    A VS Code manifest cannot declare that it supersedes a previous id, so the
    installers have to do it. The removal must be best-effort: a new user does
    not have the old extension, and `code` exits non-zero when asked to
    uninstall one it cannot find.
    """

    @classmethod
    def setUpClass(cls):
        cls.scripts_dir = ROOT / "vscode-extension" / "scripts"
        cls.shell_installer = (cls.scripts_dir / "install.sh").read_text(
            encoding="utf-8"
        )
        cls.powershell_installer = (cls.scripts_dir / "install.ps1").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _code_cli_fixture(uninstall_exits):
        # The stub is keyed by frozen old ids, independently of the script's
        # loop. A wrong or repeated id therefore cannot silently succeed.
        return (
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$STUB_LOG"\n'
            'case "$1:$2" in\n'
            f"  --uninstall-extension:{LEGACY_EXTENSION_IDS[0]}) "
            f"exit {uninstall_exits[0]} ;;\n"
            f"  --uninstall-extension:{LEGACY_EXTENSION_IDS[1]}) "
            f"exit {uninstall_exits[1]} ;;\n"
            "  --install-extension:*) exit 0 ;;\n"
            "  *) exit 99 ;;\n"
            "esac\n"
        )

    def _assert_retirement_result(self, installer, completed, log, uninstall_exits):
        self.assertEqual(
            completed.returncode, 0,
            f"{installer} exited {completed.returncode}: "
            f"{completed.stdout}{completed.stderr}")
        calls = log.splitlines()
        expected_removals = [
            f"--uninstall-extension {extension_id}"
            for extension_id in LEGACY_EXTENSION_IDS
        ]
        self.assertEqual(
            expected_removals,
            [line for line in calls if "--uninstall-extension" in line],
            f"{installer} must remove each earlier id once, and never the current id")
        self.assertNotIn(f"--uninstall-extension {CURRENT_EXTENSION_ID}", calls)
        self.assertEqual(expected_removals, calls[:2])
        self.assertEqual(3, len(calls), f"{installer} must install after both removals")
        self.assertTrue(calls[2].startswith("--install-extension "))
        self.assertTrue(calls[2].endswith(" --force"))
        for extension_id, exit_code in zip(LEGACY_EXTENSION_IDS, uninstall_exits):
            self.assertEqual(
                exit_code == 0,
                f"Removed the superseded extension {extension_id}." in completed.stdout,
                f"{installer}'s removal notice must follow that id's exit code")

    def test_both_installers_uninstall_both_legacy_ids(self):
        """The flag and the id must be tied together, not merely both present.

        Each script loops over a frozen list, so pinning it takes linked
        assertions: the uninstall call must pass the iterator, and its loop
        must take exactly the two earlier ids -- both in code, not comments.
        Checking the two substrings independently over the whole file let
        install.ps1's id be mutated to `mlizaso.codex-claude-usage-WRONG-ID`, or to
        `mlizaso.codex-claude-usage` (which would make the script uninstall the very
        extension it is about to install, reinstating the defect on Windows),
        with the whole module staying green.

        **Two counts are taken on the RAW body, and that placement is the
        point.** They count a VIOLATION -- they fail on too MUCH, not too
        little -- so feeding them stripped text is
        fail-OPEN by construction: any lossy stripper can only lower a count,
        and lowering it hides the violation. Both mutants that exploited that
        are dead here because a count over raw bytes cannot be lowered by
        hiding a line from a stripper: a second call behind
        `Write-Output "cleanup #1"; ` (a `#` inside double quotes is literal to
        bash and PowerShell alike), and a second call bracketed by prose lines
        carrying `<#` and `#>`. The second was reproduced on install.sh too,
        through this module's OWN executed harness -- the script ran and
        uninstalled the extension it had installed one line earlier, and the
        module stayed green -- so it was never a PowerShell problem.

        **This is the placement rule for a count of a VIOLATION only, and the
        module holds a count of the other kind.** `TestDockerSecurityTopology`
        counts REQUIRED occurrences on stripped code, which is the stronger
        placement there for the reason its own comment measures: on raw text a
        commented-out `--cap-drop ALL` still counts. The blanket sentence this
        pair of docstrings used to carry -- "they are the module's only
        non-monotone assertions", "every counting assertion in this module is
        taken on the RAW body" -- was false in both halves.

        **The shape rule below is a lexical approximation and does not settle
        reachability.** It exists because `assertIn(invoker, calls[0])` was a
        bare substring test wearing a semantic claim: a line that merely
        MENTIONS the invoker inside a quoted string satisfied it. Requiring
        the statement to BEGIN with the invoker -- or to be an `if` whose
        command is the invoker, which is how install.sh writes it -- kills the
        three mutants that had the invoker in the wrong syntactic position.

        **What survives THIS test, and what now catches it.** UNREACHABILITY
        is invisible to every rule here: moving the whole removal block into a
        balanced `function Remove-Legacy { ... }` that nothing ever calls
        leaves the uninstall statement byte-identical, so every assertion in
        this test still passes. Reachability is not answerable by any lexical
        rule and this test should never be read as closure for install.ps1.

        It is no longer the last word, though. That mutant is now killed by
        `test_a_missing_legacy_extension_does_not_abort_install_ps1`, which
        runs the script: measured 2026-08-14 on pwsh 7.6.4, the uncalled-
        function mutant comes back with an EMPTY stub call log
        (`['--uninstall-extension mlizaso.claude-usage-private'] != []`).
        The asymmetry with install.sh that used to be the argument for
        building that twin is therefore closed: both installers are now
        executed, and this static test is the half that still runs where
        `pwsh` is absent.
        """
        for name, body, strip, var, assignment, assign_prefix, loop, invoker in (
            ("install.sh", self.shell_installer, _bash_code_lines,
             "$legacy_extension_id",
             "for legacy_extension_id in %s; do" % " ".join(LEGACY_EXTENSION_IDS),
             "for legacy_extension_id in", None, '"$code_cli"'),
            ("install.ps1", self.powershell_installer, _powershell_code_lines,
             "$LegacyExtensionId",
             '$LegacyExtensionIds = @("%s", "%s")' % LEGACY_EXTENSION_IDS,
             "$LegacyExtensionIds =",
             "foreach ($LegacyExtensionId in $LegacyExtensionIds) {",
             "& $CodeCli"),
        ):
            with self.subTest(installer=name):
                # RAW, and fail-closed: see the docstring. Both tokens occur
                # exactly once in each installer at HEAD, comments included --
                # the prose describes the rename without spelling the flag,
                # and uses of the variable carry no `=`.
                self.assertEqual(
                    body.count("--uninstall-extension"), 1,
                    "%s should have one uninstall loop body; a second call anywhere "
                    "in the file, however it is dressed up, is a second "
                    "removal" % name)
                self.assertEqual(
                    body.count(assign_prefix), 1,
                    "%s must define the legacy id list exactly once" % name)
                self.assertNotIn(
                    "legacy_extension_id=" if name == "install.sh"
                    else "$LegacyExtensionId =", body,
                    "%s must not replace the iterator with another id" % name)

                code = strip(body)
                calls = [l for l in code if "--uninstall-extension" in l]
                self.assertEqual(
                    len(calls), 1,
                    "%s should have one uninstall loop body in code, "
                    "not in a comment: %r" % (name, calls))
                self.assertIn(
                    var, calls[0],
                    "%s calls --uninstall-extension without passing %s, so "
                    "which id it removes is unpinned: %r"
                    % (name, var, calls[0]))
                self.assertTrue(
                    calls[0].startswith(invoker)
                    or calls[0].startswith("if " + invoker),
                    "%s's uninstall statement does not START with the VS Code "
                    "CLI (%s) and is not an `if` whose command it is, so the "
                    "invoker is merely mentioned -- in a string, in a "
                    "Write-Output, or inside some other construct -- rather "
                    "than run: %r" % (name, invoker, calls[0]))
                self.assertIn(
                    assignment, code,
                    "%s must iterate over exactly the two old ids in code" % name)
                if loop is not None:
                    self.assertIn(loop, code)
                    self.assertEqual(body.count(loop), 1)

    def test_the_harness_refuses_a_find_code_cli_it_cannot_sandbox(self):
        """Non-vacuity for the ordering guard `_run_install_sh` runs first.

        The shipped script passes (asserted implicitly by the executed tests
        above, which would refuse otherwise), so the check is driven over the
        two shapes it exists to catch: the real function with its two loops
        SWAPPED, and one that gains a third producer above the name loop.
        Measured 2026-08-14 on a scratch copy with install.sh's loops actually
        swapped, `subprocess.run` replaced by a raising stub: both subTests of
        `test_a_missing_legacy_extension_does_not_abort_install_sh` fail with
        this guard's message and the stub is never reached, so nothing runs
        against the host's real CLI. Without the guard the same tree runs
        install.sh, sends the ambient CLI `--uninstall-extension` and
        `--install-extension --force`, and only then fails on an empty log.
        """
        self.assertEqual(
            [], _shell_code_cli_producers_above_the_name_loop(
                self.shell_installer),
            "the shipped find_code_cli must keep its `command -v` name loop "
            "first, or this class's sandbox is not hermetic")
        swapped = (
            'find_code_cli() {\n'
            '    for path in "/Applications/x/code"; do\n'
            '        [ -x "$path" ] && { echo "$path"; return 0; }\n'
            '    done\n'
            '    for name in code code-insiders; do\n'
            '        command -v "$name" >/dev/null 2>&1 && { echo "$name"; return 0; }\n'
            '    done\n'
            '}\n')
        self.assertTrue(_shell_code_cli_producers_above_the_name_loop(swapped))
        # The name loop still first, but a third producer added above it.
        third = swapped.replace(
            '    for path in "/Applications/x/code"; do\n'
            '        [ -x "$path" ] && { echo "$path"; return 0; }\n'
            '    done\n', "")
        self.assertEqual(
            [], _shell_code_cli_producers_above_the_name_loop(third))
        self.assertTrue(_shell_code_cli_producers_above_the_name_loop(
            third.replace("find_code_cli() {\n",
                          'find_code_cli() {\n    echo "$CODE_OVERRIDE"; return 0\n')))
        # Unreadable is refused too, rather than passing as "no producers".
        self.assertTrue(_shell_code_cli_producers_above_the_name_loop("x=1\n"))

    def test_powershell_relaxes_its_stop_preference_around_the_removal(self):
        """The relaxation around the removal must be present, and restored after.

        Some PowerShell builds turn a native command's non-zero exit into a
        terminating error under `$ErrorActionPreference = "Stop"`, which would
        abort the install before it began when the legacy extension is absent
        -- the common case for a new user.

        **Which builds is version-dependent, and this docstring used to state
        it wrongly as a flat "7.4+".** `$PSNativeCommandUseErrorActionPreference`
        was experimental, on by default in 7.4, and off again on current
        builds. Measured 2026-08-14 on the pwsh 7.6.4 this suite can now
        execute: it reads False, and a native non-zero exit under Stop does not
        throw -- bare, and through the `2>&1 | Out-Null` pipeline the script
        uses. So the guard is inert on 7.6.4, load-bearing on 7.4 and wherever
        the preference is switched on, and irrelevant on Windows PowerShell
        5.1, which never had the behaviour. It is pinned here because it must
        survive for the builds that need it, not because every build does.

        This half is static, and it is no longer the only thing standing:
        `test_a_missing_legacy_extension_does_not_abort_install_ps1` below now
        EXECUTES the script. Earlier versions of this docstring said the
        executed twin was deliberately unshipped because `pwsh` was absent
        here; PowerShell 7.6.4 was installed on 2026-08-14 and the twin was
        written, watched to fail against three mutants, and shipped. What
        remains true is that this static half runs everywhere while the twin
        skips where `pwsh` is missing OR the host is not POSIX, so the two are
        complementary rather than redundant.

        Do not restate the skip as a fact about CI: the GitHub-hosted
        `ubuntu-latest` and `macos-latest` images this project's matrix uses do
        ship PowerShell 7, so the twin executes on them. It does NOT execute on
        the matrix's `windows-latest` leg, which ships pwsh but cannot run the
        twin's POSIX stub.

        **This test is not the only reader of install.ps1 there**, and an
        earlier version of this sentence said it was. NO COUNT IS QUOTED, and
        that is the correction: the count here has been wrong twice, most
        recently as "SIX ... enumerated by walking this module's ast for tests
        that touch `powershell_installer`", a walk that does not return six
        because it was run WITHOUT dropping docstrings --
        `test_a_dialects_escape_is_not_shared_with_the_others` mentions
        install.ps1 in its prose and reads neither the file nor the variable.
        The criterion is what survives a rename or an added test, so state
        that instead: **every unguarded reader of install.ps1 in this module
        is LEXICAL.** It opens the file or takes `powershell_installer`, it
        matches strings, and no test in this repository EXECUTES install.ps1
        on the platform install.ps1 is written for. To re-derive the set, walk
        the module's ast for undecorated tests whose body -- docstring
        DROPPED -- names `powershell_installer` or the literal `install.ps1`.

        One shape here is FLAGGED, not fixed: `removal.split("finally", 1)[0]`
        is non-monotone in the other direction -- a stripper that drops a line
        carrying `finally` WIDENS the region the following `assertIn` searches.
        No mutant was built through that path, so this is a note about a shape,
        not a claim about a hole.
        """
        code = "\n".join(_powershell_code_lines(self.powershell_installer))
        self.assertIn(
            '$ErrorActionPreference = "Continue"', code,
            "the relaxation is not in code; a copy of it in a comment proves "
            "nothing about what the script does")
        removal = code.split('$ErrorActionPreference = "Continue"', 1)[1]
        self.assertIn("--uninstall-extension", removal.split("finally", 1)[0])
        self.assertIn(
            "$ErrorActionPreference = $PreviousErrorAction", code,
            "the Stop preference is never restored in code, so the rest of "
            "the install would run relaxed")

    @unittest.skipUnless(os.name == "posix", "runs install.sh under bash")
    def test_a_missing_legacy_extension_does_not_abort_install_sh(self):
        """Executed, not read: the script runs under `set -euo pipefail`.

        A stub `code` on PATH stands in for the VS Code CLI and exits 1 on
        --uninstall-extension, which is what the real CLI does when the
        extension is not installed -- the common case for a new user. The
        script must still reach --install-extension and exit 0.

        The exact-removal check on the log is the lexer-independent half of the
        legacy-id guard. Its
        three original assertions are all monotone `assertIn`s, so this
        harness watched a mutated install.sh uninstall the extension it had
        just installed and passed -- the extra call was right there in the
        stub log. Counting it costs nothing, because the log is already
        captured.
        """
        for uninstall_exits in ((1, 1), (0, 0), (1, 0), (0, 1)):
            with self.subTest(uninstall_exits=uninstall_exits):
                completed, log = self._run_install_sh(uninstall_exits)
                self._assert_retirement_result(
                    "install.sh", completed, log, uninstall_exits)

    def _run_install_sh(self, uninstall_exits):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            log = tmp / "code-calls.log"
            # HERMETIC BY NAME, exactly as the pwsh twin below is, and for the
            # same measured reason. This wrote ONE stub named `code` and read
            # nothing out of install.sh, so it was hermetic only because
            # `code` comes first in `for name in code code-insiders`:
            # reordering that loop to `code-insiders code` sent this host's
            # real CLI both `--uninstall-extension` and `--install-extension`
            # while the test stayed green. The names come from the script.
            #
            # By NAME is the whole reach of the mechanism, and `find_code_cli`
            # has a second producer it cannot touch: after the name loop it
            # falls back to two hard-coded absolute paths under
            # `/Applications/Visual Studio Code*.app/.../bin/`, and a
            # PATH-prepended stub cannot intercept an absolute path. The pwsh
            # twin discloses its own `$env:LOCALAPPDATA` fallbacks the same
            # way. That fallback is unreachable today only because the name
            # loop runs first and always finds a stub -- so the ordering is
            # asserted HERE, before `subprocess.run`, rather than left to the
            # executed assertions, which do red on a reorder but only after
            # the real VS Code on this machine has been sent an uninstall.
            names = _shell_code_cli_candidate_names(self.shell_installer)
            self.assertTrue(
                names,
                "could not read find_code_cli's name list out of install.sh, "
                "so the sandbox cannot be made hermetic against it")
            self.assertEqual(
                [], _shell_code_cli_producers_above_the_name_loop(
                    self.shell_installer),
                "find_code_cli produces a CLI path before its PATH-stubbable "
                "`for name in ... command -v` loop, so this harness would run "
                "install.sh against a CLI it cannot intercept -- on a "
                "developer machine that is the real VS Code, and it would be "
                "sent --uninstall-extension and --install-extension before "
                "any assertion below ran")
            for name in names:
                stub = tmp / name
                stub.write_text(
                    self._code_cli_fixture(uninstall_exits),
                    encoding="utf-8",
                )
                stub.chmod(0o755)
            vsix = tmp / "codex-claude-usage-0.0.0.vsix"
            vsix.write_bytes(b"not a real vsix")

            env = dict(os.environ)
            env["PATH"] = f"{tmp}{os.pathsep}{env.get('PATH', '')}"
            env["STUB_LOG"] = str(log)
            completed = subprocess.run(
                ["bash", str(self.scripts_dir / "install.sh"), str(vsix)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=20,
            )
            return completed, log.read_text(encoding="utf-8") if log.exists() else ""

    def test_install_ps1_has_no_platform_conditional(self):
        """A cheap, honestly LEXICAL guard on the one mutant class the twin cannot see.

        The executed twin below runs install.ps1 under pwsh on a POSIX host,
        so it reads the POSIX branch of a script that only ever runs on
        Windows. A one-line `if ($env:OS -ne "Windows_NT") { ... }` around the
        whole removal block is therefore invisible to it and to every lexical
        rule in this class -- the call line stays byte-identical -- while the
        removal is DEAD on the only platform install.ps1 targets. Reproduced,
        with the mutation `diff`-proved to have landed.

        install.ps1 needs no platform branch at all: it is the Windows
        installer, and every path in it is a Windows path. So the guard is to
        assert it contains none.

        **It kills the SPELLINGS it lists, not the mutant class**, and the
        difference has now bitten three times. This docstring used to claim it
        killed "this mutant class for both polarities", while
        `POWERSHELL_PLATFORM_PREDICATES` held `$IsWindows` without its
        siblings. Measured 2026-08-14, `if ($IsLinux -or $IsMacOS) { ... }`
        around the whole removal block left this module fully green -- alive
        under the executed twin on POSIX, dead on Windows -- while
        `if (-not $IsWindows)` around the same block turned this test red.
        Both siblings are listed now. Then the BRACE spelling walked through
        the same door: `${env:OS}` and `${ENV:OS}` are the same variable and
        were not matched, so `if (${env:OS} -ne "Windows_NT") { ...removal... }`
        left the module green. Then the SCOPE QUALIFIER walked through the
        door the brace fix had just closed: `$global:IsWindows`,
        `$script:IsWindows` and `${global:IsWindows}` are that same automatic
        variable (verified on pwsh 7.6.4 -- they print `False` on POSIX, not
        empty), and every one of them missed a list anchored on a literal `$`.
        The list is now bare NAMES, which closes all three families at once
        and needs no enumeration of scopes; `POWERSHELL_PLATFORM_PREDICATES`
        says why `env:OS` keeps its provider prefix. The `${...}`
        normalisation that closed the brace pair is gone with the anchor --
        with no `$` to restore, `${IsWindows}` and `${env:OS}` already contain
        their names -- and it was never implicated in the scope hole: measured
        both ways, `${global:IsWindows}` missed with it and without it.

        **What no amount of listing can reach, so that the next reader does
        not have to rediscover it.** PowerShell also honours a backtick inside
        the braces, and it resolves to the SAME variable: measured on pwsh
        7.6.4, `$env:FOO`, `${env:FOO}` and `` ${env:F`OO} `` all read `bar`.
        A backtick inside the NAME therefore still walks through -- bare names
        close a backtick in the QUALIFIER (`$gl`+backtick+`obal:IsWindows`
        still contains `iswindows`) and cannot close one that splits the name
        itself. That is not a spelling to add to the list: a substring test
        over literal spellings cannot become a test
        for a VARIABLE in a language with brace and escape syntax, so this
        stays a list of spellings permanently. (`${ env:OS }` with spaces is a
        different variable in PowerShell and returns empty, so it is not an
        evasion.) Nor can it see a platform test written any other way (a
        `Get-CimInstance` probe, an `$env:SystemRoot` existence check, a
        comparison hidden behind a variable), and it says nothing about
        whether the script WORKS on Windows. Per AGENTS.md, do not describe
        Windows as tested until a run passes; none has.

        **The search is taken on the RAW body, and that placement is the
        point.** This is a FORBIDDEN-occurrence assertion -- it fails on too
        MUCH -- so feeding it stripped text is fail-OPEN by construction: any
        lossy stripper can only lower what it sees, and a here-string carrying
        `<#` used to swallow the whole rest of install.ps1 and hand this test
        an empty region to search. Raw text cannot be lowered that way. It is
        safe here rather than merely correct in principle: measured
        2026-08-14 with the anchor GONE, each of the five bare names occurs 0
        times in the whole of the clean install.ps1, comments included (the
        file's one `$global:LASTEXITCODE = 0` contains none of them), so there
        is no false positive to absorb, and a future comment merely MENTIONING
        one of them fails CLOSED -- noise a maintainer resolves, not a hole. Do not read the previous round's
        opposite conclusion for `test_both_installers_uninstall_the_legacy_id`
        as covering this: that is a REQUIRED-occurrence count, where raw is the
        weaker placement. The polarity decides, every time.
        """
        # Casefolded because PowerShell is case-INSENSITIVE: `$ENV:OS` is the
        # same variable as `$env:OS`, and a case-sensitive `in` missed it while
        # the docstring above claimed the class was closed. Bare names, with no
        # `$` anchor, for the reason `POWERSHELL_PLATFORM_PREDICATES` gives:
        # the anchor was what `$global:IsWindows` walked around, and it also
        # made the `${...}` fold necessary, which is why that fold is gone.
        # None of this is "the difference between listing spellings and listing
        # variables", which is what the comment here used to claim and what no
        # substring test can deliver: see the docstring for the backtick-in-the-
        # name form that still walks straight through.
        # PowerShell reaches the SAME variable by several path spellings, and
        # each repair here has so far closed one of them. Enumerated on pwsh
        # 7.6.4 by reading each and comparing to `$env:OS`, all returning
        # `Windows_NT`:
        #
        #     Env:\OS            drive path, separator `\`
        #     Env:/OS            drive path, separator `/`
        #     Environment::OS    PROVIDER-qualified path
        #     Env::OS            errors -- not a spelling to worry about
        #
        # The `\`/`/` fold closed the first two and left the third, which two
        # reviewers found independently. `environment::` is folded to `env:`
        # so all three collapse onto the bare name, rather than adding a
        # fourth literal to chase. Note this is still a substring test over
        # text: the backtick-in-the-name form documented in the docstring
        # walks through all of it, and no fold repairs that.
        low = (self.powershell_installer.lower()
               .replace("\\", "").replace("/", "")
               .replace("environment::", "env:"))
        found = [p for p in POWERSHELL_PLATFORM_PREDICATES if p.lower() in low]
        self.assertEqual(
            [], found,
            "install.ps1 gained a platform predicate (%r). It is the Windows "
            "installer, so it should have none -- and no test in this "
            "repository executes it on Windows, so a branch that is alive on "
            "POSIX and dead on Windows would pass every check here while "
            "being dead where it matters" % (found,))

    @unittest.skipUnless(
        os.name == "posix" and shutil.which("pwsh"),
        "runs install.ps1 under pwsh with a POSIX stub CLI",
    )
    def test_a_missing_legacy_extension_does_not_abort_install_ps1(self):
        """The executed twin: it closes REACHABILITY, as pwsh-on-POSIX reads the script.

        Every static guard in this class is a lexical approximation, and three
        consecutive review rounds each defeated the previous one with a
        mutation its author had not imagined. The last survivor was
        UNREACHABILITY -- moving the removal into a balanced
        `function Remove-Legacy { ... }` that nothing calls leaves every
        asserted string byte-identical -- and reachability is not answerable
        by any lexical rule. Running the script answers it for free.

        Measured 2026-08-14 on pwsh 7.6.4, each mutation `diff`-proved to have
        landed before the run (a reviewer in the round that produced this test
        published green results from a regex that silently matched nothing):

          pristine before this rename    -> 1 uninstall, of the legacy id
          the whole removal in an uncalled `function Remove-Legacy {}`
                                         -> 0 uninstalls   <- lexically invisible
          a later `$LegacyExtensionId = "mlizaso.claude-usage"`
                                         -> 1 uninstall, of the WRONG id
          the call rewritten as a string assignment
                                         -> 0 uninstalls

        The middle one is the mutant a judge proved static analysis cannot
        kill without lexing PowerShell in Python: its call line is
        byte-identical to the genuine one and the corruption lives in a later
        assignment.

        **What it does NOT close, stated plainly because this docstring used
        to say "it is what closes install.ps1".** THE PLATFORM IS A STAND-IN
        AS MUCH AS THE CLI IS. install.ps1 is the Windows installer and this
        runs it under pwsh on POSIX, so anything whose truth value differs
        between the two is outside what any test here observes: a platform
        conditional (covered separately, and only lexically, by
        `test_install_ps1_has_no_platform_conditional`), `Find-CodeCli`'s
        `code.cmd` / `code-insiders.cmd` candidates, its two
        `$env:LOCALAPPDATA` fallbacks, and PATHEXT-dependent resolution. What
        it does close is REACHABILITY as pwsh-on-POSIX interprets the script,
        which is the one thing no lexical rule can answer.

        The guard matches its install.sh twin -- `os.name == "posix"` AND the
        interpreter -- because the harness's stub is a `#!/bin/sh` file made
        runnable with `Path.chmod(0o755)`, and neither the shebang nor the
        mode bit means anything on Windows. The matrix's five legs include
        `windows-latest`, which ships pwsh, so gating on `shutil.which("pwsh")`
        alone SCHEDULED this POSIX-only harness to run there. What it would
        have done there is not asserted here: two different landing points
        were reproduced by two reviewers and neither could execute Windows.
        Every leg whose `os.name` is posix executes it and `windows-latest`
        skips -- on today's matrix that is the three `ubuntu-latest` Python
        versions and `macos-latest`. The predicate is the durable half: the
        sentence here said "the three legs that do execute it", which was
        wrong because `tests.yml` has FOUR non-windows legs, and any bare leg
        count is falsified the day a Python version is added.
        """
        for native_errors in (False, True):
            for uninstall_exits in ((1, 1), (0, 0), (1, 0), (0, 1)):
                with self.subTest(
                        native_errors=native_errors, uninstall_exits=uninstall_exits):
                    completed, log = self._run_install_ps1(
                        uninstall_exits, native_errors)
                    self._assert_retirement_result(
                        "install.ps1", completed, log, uninstall_exits)

    def _run_install_ps1(self, uninstall_exits, native_errors):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            log = tmp / "code-calls.log"
            # HERMETIC BY NAME, not by PATH position. `Find-CodeCli` iterates
            # NAMES in the outer loop and asks `Get-Command` for each, so PATH
            # precedence does not protect a stub named `code`: a `code.cmd`
            # anywhere on PATH is tried FIRST and wins. Measured 2026-08-14 on
            # this host with a `code` stub in PATH entry 1 and a `code.cmd`
            # decoy in entry 2 -- the loop resolved the decoy, and the real
            # script sent it both `--uninstall-extension` and
            # `--install-extension`. That is not a Windows-only hazard (VS
            # Code's `bin` on PATH is simply where a `code.cmd` usually is);
            # it is a sandbox that held here only because this machine has no
            # `code.cmd`, while it DOES have a real `code` at
            # /opt/homebrew/bin/code. So the stub is written under EVERY name
            # the loop searches, taken from install.ps1 itself rather than
            # hand-listed, and no ambient CLI can be reached whatever is
            # installed.
            names = _code_cli_candidate_names(self.powershell_installer)
            self.assertTrue(
                names,
                "could not read Find-CodeCli's name list out of install.ps1, "
                "so the sandbox cannot be made hermetic against it")
            for name in names:
                stub = tmp / name
                stub.write_text(
                    self._code_cli_fixture(uninstall_exits),
                    encoding="utf-8",
                )
                stub.chmod(0o755)
            vsix = tmp / "codex-claude-usage-0.0.0.vsix"
            vsix.write_bytes(b"not a real vsix")

            env = dict(os.environ)
            env["PATH"] = f"{tmp}{os.pathsep}{env.get('PATH', '')}"
            env["STUB_LOG"] = str(log)
            # Running the test suite must not opt the developer into anybody's
            # telemetry. Without these, `pwsh` initialises Microsoft's telemetry
            # on first start and writes a persistent UUID under the user's home
            # -- a side effect of `python -m unittest` that nothing in this
            # repository asks for and nothing tells the reader about.
            env["POWERSHELL_TELEMETRY_OPTOUT"] = "1"
            env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
            env["STUB_INSTALLER"] = str(self.scripts_dir / "install.ps1")
            env["STUB_VSIX"] = str(vsix)
            completed = subprocess.run(
                [
                    "pwsh", "-NoProfile", "-NonInteractive", "-Command",
                    "$PSNativeCommandUseErrorActionPreference = "
                    + ("$true; " if native_errors else "$false; ")
                    + "& $env:STUB_INSTALLER -Vsix $env:STUB_VSIX",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=20,
            )
            return completed, log.read_text(encoding="utf-8") if log.exists() else ""


@unittest.skipUnless(
    os.name == "posix" and shutil.which("pwsh"),
    "runs the PowerShell installer with POSIX command fixtures",
)
class TestPowerShellInstallerFailures(unittest.TestCase):
    STAGES = ["ci", "signatures", "audit", "package", "name", "version",
              *("uninstall:" + extension_id for extension_id in LEGACY_EXTENSION_IDS),
              "install"]

    def test_required_command_failure_stops_before_later_steps(self):
        for native_errors in (False, True):
            for stage in self.STAGES:
                if stage.startswith("uninstall:"):
                    continue  # Removing an absent earlier extension is best-effort.
                with self.subTest(stage=stage, native_errors=native_errors):
                    completed, calls = self._run_installer(stage, native_errors)
                    self.assertNotEqual(completed.returncode, 0, completed.stdout)
                    self.assertNotIn("Done. Reload VS Code", completed.stdout)
                    self.assertEqual(calls, self.STAGES[:self.STAGES.index(stage) + 1])

    def test_success_still_installs_after_a_missing_legacy_extension(self):
        for native_errors in (False, True):
            with self.subTest(native_errors=native_errors):
                completed, calls = self._run_installer("", native_errors)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("Done. Reload VS Code", completed.stdout)
                self.assertEqual(calls, self.STAGES)

    def _run_installer(self, failed_stage, native_errors):
        source = (ROOT / "vscode-extension/scripts/install.ps1").read_text(
            encoding="utf-8")
        code_names = _code_cli_candidate_names(source)
        self.assertTrue(code_names, "every possible VS Code CLI must be stubbed")
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            scripts = tmp / "extension" / "scripts"
            scripts.mkdir(parents=True)
            installer = scripts / "install.ps1"
            installer.write_text(source, encoding="utf-8")
            # A stale package must not let a failed audit or build reach install.
            (scripts.parent / "fixture-1.0.0.vsix").write_bytes(b"stale package")
            commands = tmp / "bin"
            commands.mkdir()
            log = tmp / "calls.log"
            check = (
                'printf "%s\\n" "$stage" >> "$STUB_LOG"\n'
                '[ "$stage" != "$STUB_FAIL_STAGE" ] || exit 17\n'
            )
            fixtures = {
                "npm": (
                    'case "$*" in\n'
                    '  ci*) stage=ci ;;\n'
                    '  "audit signatures") stage=signatures ;;\n'
                    '  audit*) stage=audit ;;\n'
                    '  "run package") stage=package ;;\n'
                    '  *) exit 98 ;;\nesac\n' + check
                ),
                "node": (
                    'case "$*" in\n'
                    '  *.name) stage=name; value=fixture ;;\n'
                    '  *.version) stage=version; value=1.0.0 ;;\n'
                    '  *) exit 98 ;;\nesac\n' + check
                    + 'printf "%s\\n" "$value"\n'
                ),
            }
            code_fixture = (
                'case "$1" in\n'
                '  --uninstall-extension) stage="uninstall:$2" ;;\n'
                '  --install-extension) stage=install ;;\n'
                '  *) exit 98 ;;\nesac\n' + check
                + 'case "$stage" in uninstall:*) exit 1 ;; esac\n'
            )
            fixtures.update({name: code_fixture for name in code_names})
            for name, body in fixtures.items():
                command = commands / name
                command.write_text("#!/bin/sh\n" + body, encoding="utf-8")
                command.chmod(0o755)
            env = dict(os.environ)
            env.update({
                "PATH": f"{commands}{os.pathsep}{env.get('PATH', '')}",
                "STUB_LOG": str(log),
                "STUB_FAIL_STAGE": failed_stage,
                "STUB_INSTALLER": str(installer),
                "POWERSHELL_TELEMETRY_OPTOUT": "1",
                "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            })
            completed = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-Command",
                 "$PSNativeCommandUseErrorActionPreference = "
                 + ("$true; " if native_errors else "$false; ")
                 + "& $env:STUB_INSTALLER"],
                capture_output=True, text=True, encoding="utf-8", env=env,
                timeout=20,
            )
            calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            return completed, calls


class TestTheCommentStrippersAreNotVacuous(unittest.TestCase):
    """Drive the strippers themselves; every caller above only reads them.

    A stripper that quietly stopped stripping would make every assertion fed
    by it satisfiable by a comment again, and a stripper that quietly started
    over-stripping would truncate a real statement. Neither shows up in the
    callers -- the first leaves them green on prose, the second leaves them
    red for the wrong reason. So the properties are spelled out here.
    """

    def test_bash_stripper_drops_comments_and_keeps_quoted_hashes(self):
        source = (
            '#!/usr/bin/env bash\n'
            '# a whole-line comment\n'
            'real_one="kept"   # trailing comment\n'
            'echo "http://h/#token=${T}"\n'
            'foo=1  # "$code_cli" --uninstall-extension $legacy\n'
            'kept_after_a_block_marker  # bash has no <# ... #>\n'
        )
        self.assertEqual(
            _bash_code_lines(source),
            [
                'real_one="kept"',
                'echo "http://h/#token=${T}"',
                'foo=1',
                'kept_after_a_block_marker',
            ],
        )

    def test_powershell_stripper_drops_comments_and_block_comments(self):
        source = (
            '# a whole-line comment\n'
            '$real_one = "kept"   # trailing comment\n'
            'Write-Output "http://h/#token=$T"\n'
            '$null = 1  # & $CodeCli --uninstall-extension $Legacy\n'
            '<#\n'
            'swallowed_by_a_real_block\n'
            '#>\n'
            '# open with <#\n'
            'not_swallowed_by_a_line_comment\n'
            '# ...and close with #>\n'
        )
        self.assertEqual(
            _powershell_code_lines(source),
            [
                '$real_one = "kept"',
                'Write-Output "http://h/#token=$T"',
                '$null = 1',
                'not_swallowed_by_a_line_comment',
            ],
        )

    def test_each_dialects_escape_keeps_a_comment_out_of_the_code(self):
        """The escape rules, driven directly -- these had no self-test at all.

        An escaped quote must not change quote state, or the scanner believes
        a string is open at the `#` and hands the COMMENT back as code. That
        is fail-OPEN for a counting caller, and it was demonstrated end to end
        against `TestDockerSecurityTopology` with a real `--cap-drop ALL`
        deleted. One line per dialect, each using ITS escape character.
        """
        self.assertEqual(
            _bash_code_lines('echo "say \\" here"  # --cap-drop ALL\n'),
            ['echo "say \\" here"'],
        )
        self.assertEqual(
            _powershell_code_lines(
                'Write-Output "note `" x"  # npm ci --ignore-scripts\n'),
            ['Write-Output "note `" x"'],
        )
        self.assertEqual(
            _js_code_lines('const s = "a \\" b";  // fs.rmSync(targetDir)\n'),
            ['const s = "a \\" b";'],
        )

    def test_a_continued_string_does_not_swallow_the_comment_after_it(self):
        """The newline-continuation fail-open, closed and pinned.

        `_scan` used to reset quote state at every newline, so a string
        carried across a `<escape><newline>` continuation was CLOSED at the
        line break and the next line's closing quote RE-OPENED one -- leaving
        the scan inside a string at the real `#`, which it then handed back as
        code. That is the ADDING direction, and it was demonstrated end to end
        rather than imagined: with scripts/run-docker.sh's proxy container's
        real `--cap-drop ALL` deleted (which alone turns
        `TestDockerSecurityTopology` RED) and the bash line below appended,
        that test went GREEN with one container running unconfined. Re-run
        2026-08-14 against the fix: the same two edits leave it RED.

        Each line uses ITS dialect's escape, and each was run through the real
        parser first -- `echo "abc\\<nl>def"` prints `abcdef` under bash
        3.2.57 (and `bash -n` accepts the whole decoy file), `const s =
        "abc\\<nl>def"` is `abcdef` under node, and
        `Write-Output "abc`<nl>def"` prints two lines under pwsh 7.6.4,
        because PowerShell keeps the newline in the value. The dialects
        disagree about the VALUE and agree about the only thing this scan
        needs: the string is still open on the second line, so the `#` after
        its closing quote is a comment.
        """
        for strip, source, joined in (
            (_bash_code_lines,
             'echo "abc\\\ndef"  # --cap-drop ALL\n', 'echo "abcdef"'),
            (_powershell_code_lines,
             'Write-Output "abc`\ndef"  # --cap-drop ALL\n',
             'Write-Output "abcdef"'),
            (_js_code_lines,
             'const s = "abc\\\ndef";  // --cap-drop ALL\n',
             'const s = "abcdef";'),
            # The BARE-newline spelling, which the first fix left open. It is
            # the same mechanism one character shorter, and it defeated the
            # hardening count exactly as the escaped one did. bash 3.2.57 and
            # pwsh 7.6.4 both keep a `"` string open across a raw newline
            # (measured), so both must carry it. JavaScript is deliberately
            # absent: node raises SyntaxError for a `"` spanning a raw
            # newline, so there is no valid input to pin, and `_js_code_lines`
            # must NOT carry `"` or it would diverge from its own parser.
            (_bash_code_lines,
             'echo "abc\ndef"  # --cap-drop ALL\n', 'echo "abc\ndef"'),
            (_powershell_code_lines,
             'Write-Output "abc\ndef"  # --cap-drop ALL\n',
             'Write-Output "abc\ndef"'),
        ):
            with self.subTest(strip=strip.__name__):
                code = strip(source)
                self.assertEqual(code, [joined])
                self.assertNotIn(
                    "--cap-drop ALL", "\n".join(code),
                    "the comment after a continued string came back as code")

    def test_an_unquoted_continuation_joins_and_keeps_every_flag(self):
        """The shape scripts/run-docker.sh actually has, 45 times at HEAD.

        Consuming the continuation joins two physical lines into one logical
        statement, which is what bash does. It is asserted here because the
        hardening map counts flags over `"\\n".join(...)` of exactly this
        output, and a join that dropped or fused a flag would move those
        counts -- so the count is taken here too, not just the text.
        """
        source = (
            'docker run --rm -d \\\n'
            '  --read-only \\\n'
            '  --cap-drop ALL \\\n'
            '  "$IMAGE"  # trailing prose naming --cap-drop ALL\n'
        )
        code = "\n".join(_bash_code_lines(source))
        self.assertEqual(
            code, 'docker run --rm -d   --read-only   --cap-drop ALL   "$IMAGE"')
        self.assertEqual(code.count("--cap-drop ALL"), 1)
        self.assertEqual(code.count("--read-only"), 1)

    def test_the_continuation_rule_is_not_applied_where_the_escape_is_dead(self):
        """A named RESIDUE, executable so it cannot be forgotten.

        Inside bash's single quotes the backslash is a literal character, not
        an escape, so the continuation branch deliberately does not fire and
        the pre-fix per-newline reset still applies. Real bash keeps that
        string open across the newline; this scan does not, and the comment
        after the closing quote therefore still comes back as code. Left open
        rather than closed: carrying quote state across a newline as a blanket
        rule is what would fix it, and that opens a WORSE adding path, because
        one stray apostrophe -- in a heredoc body, in `it's` inside a `: '...'`
        block -- would then hold a string open over the rest of the file and
        hand every later comment back as code.

        This asserts today's wrong answer on purpose. If it goes red because
        the residue was closed, check that the fix did not buy it with a
        blanket carry, then update the expectation.
        """
        code = _bash_code_lines("echo 'a\\\nb'  # --cap-drop ALL\n")
        self.assertEqual(code, ["echo 'a\\", "b'  # --cap-drop ALL"])
        self.assertIn("--cap-drop ALL", "\n".join(code))

    def test_a_dialects_escape_is_not_shared_with_the_others(self):
        """Why the escape is a parameter and not one shared rule.

        Backslash is bash's and JavaScript's escape and is NOT PowerShell's:
        measured 2026-08-14 on pwsh 7.6.4, `"a\\"b"` is a ParserError there,
        while install.ps1 legitimately carries
        `"$env:LOCALAPPDATA\\Programs\\Microsoft VS Code\\bin\\code.cmd"`,
        whose backslashes are ordinary path characters.

        That real line is NOT the discriminator, and an earlier version of this
        docstring said it was. Measured: both rules return byte-identical
        output (49 lines) over the shipped install.ps1, because consuming `\\P`
        appends both characters anyway. The rules diverge only where a `\\`
        immediately precedes a CLOSING quote -- which is what the synthetic
        line below pins, and which install.ps1 could acquire at any time. A
        shared backtick rule, meanwhile, leaves bash's `\\"` open, and that one
        does bite on a shipped file. The split is kept for the divergence that
        exists, not for the one that was asserted.
        """
        windows_path = (
            '$c = "$env:LOCALAPPDATA\\Programs\\Microsoft VS Code\\bin"'
            '  # not a comment marker in sight\n')
        self.assertEqual(
            _powershell_code_lines(windows_path),
            ['$c = "$env:LOCALAPPDATA\\Programs\\Microsoft VS Code\\bin"'],
        )
        # bash's rule applied to the same line eats the `\P`, `\M` and `\b`
        # pairs -- harmless here only because nothing follows; it is the
        # instrument that is wrong, which is why it is not the one used.
        self.assertNotEqual(
            _bash_code_lines('$x = "a\\"  # b\n'),
            _powershell_code_lines('$x = "a\\"  # b\n'),
        )
        # And the reverse: a backtick is an ordinary character to bash.
        self.assertEqual(
            _bash_code_lines('echo "a`b"  # c\n'), ['echo "a`b"'])

    def test_a_hash_inside_a_word_is_not_a_comment_in_either_dialect(self):
        """The token-boundary rule, checked against both real parsers.

        Measured 2026-08-14: `bash -c 'f() { echo "$#"; }; f docker run --name
        c#1 --cap-drop ALL img'` receives SEVEN arguments, and `pwsh -c
        'Write-Output abc#def'` prints `abc#def`. Before this rule the scan
        cut at the `#` and returned `docker run --name c`, DELETING a real
        `--cap-drop ALL` from the hardening map that
        `TestDockerSecurityTopology` counts on stripped code.

        **The rule is bash's and is applied to bash only.** It was briefly
        applied to PowerShell too, which was a new ADDING hole rather than a
        fix: pwsh opens a comment after a closing quote and after `]`, so an
        allow-list of separators hands real pwsh comments back as code.
        `_powershell_code_lines` keeps cutting at any unquoted `#`, which is
        TRUNCATING and therefore fail-closed for its callers, and the pwsh half
        of this finding is disclosed as open rather than claimed closed. The
        two pwsh assertions below pin that decision so it cannot drift back.

        `{` and `}` are deliberately ABSENT from `COMMENT_BOUNDARY_CHARS` and
        are asserted NOT to be boundaries, because a previous version of this
        rule listed `{` under the words "confirmed against a real parser" and
        thereby re-created the flag-deleting defect on `${#arr[@]}`, and two
        prose sites then claimed `}` was a member it never was.

        **The separator loop is DERIVED from `COMMENT_BOUNDARY_CHARS`, not
        hand-written.** The hand-written six it replaces covered six of the
        set's eleven members, so `\\r`, `\\n`, `)`, `<` and `>` were asserted
        by nothing at all -- which is how a false member list shipped under a
        "confirmed against a real parser" sentence.

        **What the derived loop does NOT do is catch a wrong member**, and the
        sentence that used to sit here said it did. It asserts the scan's own
        behaviour for each member, taking its expectation from the same set it
        iterates, so it is self-satisfying: a skeptic planted `]` -- verified
        under bash NOT to open a comment -- and the whole module stayed green.
        The literal `{` and `}` cases below are what bite. Add a member, add
        its case.
        """
        flagged = "docker run --name c#1 --cap-drop ALL img\n"
        self.assertEqual(_bash_code_lines(flagged), [flagged.strip()])
        self.assertEqual(_bash_code_lines("echo ${PORT#x}\n"), ["echo ${PORT#x}"])
        # bash 3.2.57: `echo hi{# x` prints `hi{# x`, and this is the shape
        # that deletes a hardening flag if `{` is treated as a boundary.
        self.assertEqual(
            _bash_code_lines("docker run ${#arr[@]} --cap-drop ALL img\n"),
            ["docker run ${#arr[@]} --cap-drop ALL img"])
        self.assertEqual(
            _bash_code_lines("echo hi{# --cap-drop ALL\n"),
            ["echo hi{# --cap-drop ALL"])
        # `}` likewise. Measured 2026-08-14: `f ${HOME}#c --cap-drop ALL`
        # passes `/Users/<user>#c --cap-drop ALL` as arguments, so restoring
        # `}` to the set -- which two comments in this file used to invite --
        # would silently delete a hardening flag here.
        self.assertEqual(
            _bash_code_lines("echo ${HOME}#c --cap-drop ALL\n"),
            ["echo ${HOME}#c --cap-drop ALL"])
        # Today's WRONG answer, on purpose, and the only surviving truncating
        # residue of the boundary trade: bash passes `q#c --cap-drop ALL` as
        # arguments (measured the same day), while this scan cannot tell an
        # operator `)` from a substitution's closing one and cuts. Correct
        # would be the whole line. Flip this when the scan learns the
        # difference; do not delete it.
        self.assertEqual(
            _bash_code_lines("f $(printf q)#c --cap-drop ALL\n"),
            ["f $(printf q)"])

        for separator in sorted(COMMENT_BOUNDARY_CHARS):
            with self.subTest(separator=separator):
                self.assertEqual(
                    _bash_code_lines("echo hi%s# --cap-drop ALL\n" % separator),
                    [("echo hi" + separator).strip()])
        # The second of the two members bash does NOT open a comment after,
        # pinned as today's wrong answer beside `)` above: measured
        # 2026-08-14, `f hi<CR># --cap-drop ALL` passes the flag through as
        # arguments. It stays in the set because dropping it moves the scan's
        # error toward ADDING; both shipped bash files hold 0 CR bytes.
        self.assertIn("\r", COMMENT_BOUNDARY_CHARS)

        # PowerShell: cuts at any unquoted `#`. Measured on pwsh 7.6.4 --
        # `Write-Output "a"# c` prints `a` and the next statement runs, so a
        # boundary allow-list would have returned that comment as CODE.
        self.assertEqual(
            _powershell_code_lines('Write-Output "a"# --uninstall-extension\n'),
            ['Write-Output "a"'])
        self.assertEqual(
            _powershell_code_lines("Write-Output abc#def\n"), ["Write-Output abc"])

    def test_js_stripper_drops_comments_and_keeps_quoted_slashes(self):
        source = (
            '// a whole-line comment\n'
            'fs.rmSync(targetDir);   // trailing comment\n'
            'const u = "https://example/x";\n'
            '/*\n'
            'swallowed_by_a_real_block\n'
            '*/\n'
            '// open with /*\n'
            'not_swallowed_by_a_line_comment;\n'
            '// ...and close with */\n'
        )
        self.assertEqual(
            _js_code_lines(source),
            [
                'fs.rmSync(targetDir);',
                'const u = "https://example/x";',
                'not_swallowed_by_a_line_comment;',
            ],
        )

    def test_the_hash_strippers_are_the_wrong_instrument_for_javascript(self):
        """Why there are three functions rather than one with flags.

        Applying either `#` stripper to JavaScript no-ops: the assertion stays
        exactly as defeatable while READING as guarded, which is worse than
        leaving it on raw text, because the next reader stops looking. Pinned
        so nobody consolidates the trio.
        """
        line = "  // fs.rmSync(targetDir, { recursive: true, force: true });"
        self.assertEqual(_bash_code_lines(line), [line.strip()])
        self.assertEqual(_powershell_code_lines(line), [line.strip()])
        self.assertEqual(_js_code_lines(line), [])

    def test_both_strippers_leave_real_code_on_the_shipped_files(self):
        """Non-vacuity in the other direction: they must not eat everything.

        Each of these is a file some assertion above reads through a stripper,
        so a stripper returning `[]` would make that assertion fail loudly --
        but a stripper returning almost nothing would not, and that is the
        state worth catching early.
        """
        scripts = ROOT / "vscode-extension" / "scripts"
        for path, strip in (
            (scripts / "install.sh", _bash_code_lines),
            (scripts / "install.ps1", _powershell_code_lines),
            (ROOT / "scripts" / "run-docker.sh", _bash_code_lines),
            (scripts / "copy-python.js", _js_code_lines),
        ):
            with self.subTest(path=path.name):
                raw = path.read_text(encoding="utf-8")
                code = strip(raw)
                self.assertGreater(len(code), 20, "stripper ate the file")
                self.assertLess(
                    len(code), len(raw.splitlines()),
                    "stripper returned every line; it is not stripping")
                self.assertFalse(
                    [l for l in code if l.startswith(("#", "//"))],
                    "a comment survived as code")


class TestTheShippedScriptsStayInsideWhatTheseStrippersLex(unittest.TestCase):
    """The strippers' "the shipped file has none" mitigations, made executable.

    `_powershell_code_lines` can be BLINDED rather than merely approximated:
    a `@' ... '@` here-string body carrying `<#` opens a block comment the
    scan never closes -- the quote state resets at each newline for `'`, so
    unlike the `@" ... "@` form the marker IS examined -- and everything up to
    the next `#>`, or the whole rest of the file, is dropped. `_scan` raises
    when a body ENDS inside such a block, which is the unterminated case and a
    hard syntax error in both block dialects. It does NOT catch a body that
    re-closes with `#>`, which loses the region between the two markers and
    leaves the scan's state clean.

    Two docstrings above answered that with "install.ps1 has none" /
    "copy-python.js holds none today", which is worth nothing for a guard
    whose whole job is to notice a file acquiring something TOMORROW. This is
    the same sentence with a test behind it: a future edit that introduces a
    construct these strippers cannot lex turns red here, at the construct,
    rather than silently blinding an assertion elsewhere.

    The JavaScript half is the conservative one and is labelled as such. A
    template literal containing `/*` was CHECKED and does not blind the scan
    (the backtick spans newlines, so the marker sits inside a string);
    copy-python.js carries template literals throughout and no `/*` at all,
    and forbidding that sequence outright is cheaper than deciding, lexically,
    which occurrence of it the scan would have seen from outside a string.

    It is deliberately a FORBIDDEN-occurrence check on RAW text -- the only
    placement that cannot be defeated by the very blindness it guards -- and
    it is therefore fail-CLOSED: a here-string added for a legitimate reason
    reds this test, and the answer is to teach the stripper, not to delete the
    line.

    **The two BASH files were outside this class until round 10, and that is
    how a live ADDING residue went undisclosed.** `_bash_code_lines` does not
    model `$( )` / backtick substitution as a nested parsing context, so a
    comment inside one that sits inside a double-quoted word comes back as
    CODE -- reproduced end to end against the counter this module calls
    "losing one leaves that container unconfined": delete the proxy
    container's real `--cap-drop ALL` (which alone reds
    `TestDockerSecurityTopology`), plant one such decoy, and the whole module
    goes green with the shipped proxy container genuinely unconfined. Unlike
    every other bash residue this file discloses, its construct is PRESENT in
    the guarded files -- measured 2026-08-14, `$'` occurs 0 times, `: '` 0
    times and `<<` 0 times across both, while this class's own scan finds 17
    double-quoted substitutions in run-docker.sh and 4 in install.sh -- so
    ordinary maintenance drift is enough to blind the counter. The bash half
    below is the same belt shape as the two above, at the construct.
    """

    def test_the_shipped_powershell_has_no_here_string_or_block_comment(self):
        source = (ROOT / "vscode-extension" / "scripts" / "install.ps1").read_text(
            encoding="utf-8"
        )
        for marker in ('@"', "@'", '"@', "'@", "<#", "#>"):
            with self.subTest(marker=marker):
                self.assertNotIn(
                    marker, source,
                    "install.ps1 gained %r. `_powershell_code_lines` does not "
                    "lex here-strings, and a here-string body carrying `<#` "
                    "opens a block comment that swallows the rest of the file "
                    "-- every stripper-fed assertion above then searches a "
                    "truncated region" % (marker,))

    def test_the_shipped_bash_opens_no_substitution_the_scan_misreads(self):
        """The bash half: no nested substitution the stripper reads wrongly.

        Both conditions are forbidden because both are outside what `_scan`
        lexes, and each is checked separately so the failure names which one
        landed. A substitution that SPANS A NEWLINE is what the ADDING face
        needs (on one line the inner comment would swallow the closing `)"`
        and bash would reject the file, so `bash -n` already catches it); a
        substitution CARRYING A `#` is the payload itself. Measured
        2026-08-14, the shipped files hold 21 such substitutions between them
        and none satisfies either condition, so this is a boundary held by
        construction rather than a repair of a live defect.
        """
        for name, path in (
            ("scripts/run-docker.sh", ROOT / "scripts" / "run-docker.sh"),
            ("vscode-extension/scripts/install.sh",
             ROOT / "vscode-extension" / "scripts" / "install.sh"),
        ):
            found = _bash_nested_substitutions(path.read_text(encoding="utf-8"))
            with self.subTest(script=name):
                self.assertEqual(
                    [], [text for text, spans, _ in found if spans],
                    "%s gained a double-quoted `$( )` or backtick "
                    "substitution spanning a newline. `_bash_code_lines` does "
                    "not restart comment recognition inside one, so a `#` "
                    "there is a comment to bash and CODE to the stripper -- "
                    "which ADDS text to the hardening counts in "
                    "TestDockerSecurityTopology. Teach the stripper or keep "
                    "the substitution on one line" % (name,))
                self.assertEqual(
                    [], [text for text, _, hashed in found if hashed],
                    "%s gained a `#` inside a double-quoted `$( )` or "
                    "backtick substitution, which this scan reads as string "
                    "content rather than as the comment bash sees" % (name,))

    def test_the_shipped_bash_avoids_what_blinds_the_substitution_scan(self):
        """The constructs that flip `_bash_nested_substitutions`' quote parity.

        Its outer lexer models plain `'` and `"` and nothing else. Two bash
        constructs invert its parity relative to the shell and thereby silence
        it for the WHOLE REST OF THE FILE, not just one line:

        * `$'...'` ANSI-C quoting, where `\'` does NOT end the string for bash
          but does for this lexer;
        * a heredoc BODY, which the lexer reads as ordinary code, so a lone
          apostrophe in prose (`usage: it's a note`) opens a string that never
          closes.

        A skeptic measured the consequence on the clean `run-docker.sh`: one
        such line near the top takes the scan from 17 substitutions found to
        1, retiring the guard silently. So this is NOT lexed -- it is
        FORBIDDEN, which is the same shape the PowerShell test above uses and
        is fail-closed by construction rather than by out-lexing bash.

        Both constructs are absent from both files today (measured 0 and 0),
        so this costs nothing now and reds the commit that would blind the
        scan. If one is ever genuinely needed, the scan must be taught it
        first -- that ordering is the point.
        """
        for name, path in (
            ("run-docker.sh", ROOT / "scripts" / "run-docker.sh"),
            ("install.sh", ROOT / "vscode-extension" / "scripts" / "install.sh"),
        ):
            with self.subTest(script=name):
                body = path.read_text(encoding="utf-8")
                # Line continuations are removed FIRST, because bash joins them
                # before it tokenises: `x=$\<newline>'a'` is ANSI-C quoting to
                # the shell and contains no literal `$'` for a substring test
                # to find. Verified -- that spelling runs and prints `ab`.
                joined = re.sub(r"\\\n", "", body)
                self.assertNotIn(
                    "$'", joined,
                    "%s gained an ANSI-C quoted string. It inverts "
                    "_bash_nested_substitutions' quote parity and silences the "
                    "substitution guard for the rest of the file; teach the "
                    "scan before using it." % name)
                # A LITERAL check, deliberately, and not a spelling list. The
                # version this replaces matched `<<-?\s*['\"]?\w`, which three
                # reviewers independently defeated: it misses `<<\EOF`,
                # `<< \EOF`, `<<-\EOF`, `<<""`, `<<''`, `<<"$X"` and `<<$X` --
                # all real heredocs on bash 3.2.57 -- while OVER-firing on the
                # arithmetic shift `X=$(( 1 << 2 ))`. Measured: planting
                # `cat <<\USAGE` with an apostrophe in its body takes
                # run-docker.sh from 17 substitutions found to 0.
                #
                # That was this campaign's failure shape 3 committed inside the
                # function written to stop committing it: the FORBIDDING was
                # itself a lexer-shaped partial match. `<<` needs no spelling
                # list, catches `<<<` here-strings for free, and both files
                # hold 0 today, so the cost is nothing and the failure mode is
                # a loud red rather than a silent blinding. If a shipped script
                # ever legitimately needs `<<`, teach `_bash_nested_substitutions`
                # first -- that ordering is the whole point.
                self.assertNotIn(
                    "<<", joined,
                    "%s gained a `<<`. A heredoc body is read as code by "
                    "_bash_nested_substitutions, so one apostrophe in it "
                    "silences the substitution guard for the rest of the "
                    "file. Do not narrow this check to the spelling you "
                    "happen to have used." % name)

    def test_the_bash_substitution_scan_is_not_vacuous(self):
        """It fires on both spellings of the real decoy, and on nothing near it.

        Ground truth, bash 3.2.57 on 2026-08-14: both decoy scripts print
        `ok` -- the `--cap-drop ALL` text is DISCARDED by the shell while
        `_bash_code_lines` hands it back as code. The three negatives are the
        constructs a coarser rule would have swept up: `${` expansion, which
        is not a nested context; a substitution opened OUTSIDE a double quote,
        which the stripper already reads correctly; and a `#` comment
        mentioning either construct in prose, of which install.sh has ten.
        """
        decoy = 'DOCKER_CTX="$(printf %s ok  # --cap-drop ALL\n)"\n'
        self.assertIn("--cap-drop ALL", "\n".join(_bash_code_lines(decoy)))
        self.assertEqual(
            [(True, True)],
            [(spans, hashed) for _, spans, hashed
             in _bash_nested_substitutions(decoy)])
        self.assertEqual(
            [(True, True)],
            [(spans, hashed) for _, spans, hashed in _bash_nested_substitutions(
                'X="`printf %s ok  # --cap-drop ALL\n`"\n')])
        self.assertEqual(
            [], _bash_nested_substitutions('echo "${PORT#x}"\n'))
        self.assertEqual(
            [], _bash_nested_substitutions('X=$(printf %s ok  # --cap-drop ALL\n)\n'))
        self.assertEqual(
            [], _bash_nested_substitutions('# `code` and "$(x)" in prose\n'))
        # NEGATIVE legs for the tail scan, both from the round that found it
        # firing on legitimate bash. A `#` that is not at a token boundary is
        # not a comment to bash, and scripts/run-docker.sh already carries a
        # `/#token=` fragment in a double-quoted word, so this shape is real
        # rather than hypothetical. A newline on a LATER line of a multi-line
        # word is likewise none of this scan's business.
        self.assertEqual(
            [("$(hostname)", False, False)],
            _bash_nested_substitutions('URL="http://$(hostname):8080/#x"\n'),
            "a URL fragment is not a comment and must not red the guard")
        self.assertEqual(
            [('$(basename "$0")', False, False)],
            _bash_nested_substitutions(
                'B="$(basename "$0") starts\nsecond line"\n'),
            "a newline later in the enclosing word is literal string content")
        # Fail-closed on an opener with no close: reported as both.
        self.assertEqual(
            [(True, True)],
            [(spans, hashed) for _, spans, hashed
             in _bash_nested_substitutions('X="$(printf %s ok\n')])

        # An EARLY close, which is what the scan cannot recognise and must not
        # need to. Each of these closes the region before the real `#` because
        # the `)` is not a nesting close at all; the tail scan is what catches
        # them, and disabling it leaves every one of these green.
        #
        # They are ordinary bash on the shell that runs run-docker.sh: under
        # bash 5.2.32 the first evaluates and the shell DISCARDS the comment
        # (`RESULT=[ok]`, measured in a container). Under this host's bash
        # 3.2.57 it is a syntax error -- 3.2 counts parens naively exactly as
        # this scan did, so the macOS oracle AGREED WITH THE BUG. That is why
        # these are pinned here rather than left to a "verified against a real
        # parser" sentence.
        for label, source in (
            ("case pattern", 'Z="$(case $y in a) printf %s ok'
                             '  # --cap-drop ALL\n;; esac)"\n'),
            ("${y:-)} default", 'Z="$(echo ${y:-)} ok  # --cap-drop ALL\n)"\n'),
            ("${y%)} suffix", 'Z="$(echo ${y%)} ok  # --cap-drop ALL\n)"\n'),
            # The three above were pinned by round 12 and its tail scan was
            # then defeated by adding ONE character to each: a `"` in the tail
            # made the scan believe the enclosing word had ended. The four
            # below are those defeats, verified to run under bash 5.2.37 with
            # the flag text discarded by the shell. The last is a judge's, and
            # is the one that also defeats the obvious repair ("require the
            # outer closing quote to be adjacent") -- it puts the quote exactly
            # where that rule wants it.
            ("early close then quote",
             'Z="$(case $y in a)"printf" %s ok  # --cap-drop ALL\n;; esac)"\n'),
            ("${y:-)} then quote",
             'Z="$(echo ${y:-)}"x" ok  # --cap-drop ALL\n)"\n'),
            ("${y%)} then quote",
             'Z="$(echo ${y%)}"x" ok  # --cap-drop ALL\n)"\n'),
            ("single-quoted backslash in the tail",
             'Z="$(case $y in a) printf \'\\\\\'  # --cap-drop ALL\n;; esac)"\n'),
        ):
            with self.subTest(early_close=label):
                self.assertEqual(
                    [(True, True)],
                    [(spans, hashed) for _, spans, hashed
                     in _bash_nested_substitutions(source)],
                    "an unquoted `)` closed the region early and the tail was "
                    "never scanned, so the real `#` went unseen")

    def test_the_shipped_javascript_carries_no_block_comment_marker(self):
        """Conservative, and honest about being conservative.

        A template literal containing `/*` does NOT blind `_js_code_lines`;
        that was asserted in the round that wrote this class and refuted by
        running it. copy-python.js is full of template literals, so a test
        forbidding backticks would have been red on the shipped file from
        birth. What is forbidden instead is the marker itself, which the file
        does not carry today. A legitimate `/* */` comment added tomorrow reds
        this deliberately: the answer then is to confirm no multi-line string
        in the file carries the sequence, not to delete the check.
        """
        source = (ROOT / "vscode-extension" / "scripts" / "copy-python.js").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(
            "/*", source,
            "copy-python.js gained a block-comment marker. `_js_code_lines` "
            "sees one only from outside a string, and its string tracking is "
            "approximate, so the sequence is kept out of the file rather than "
            "adjudicated -- `test_package_recreates_generated_outputs`, which "
            "asserts the stale-tree removal and the symlink refusal are CODE, "
            "reads this file through that scan")

    def test_the_unterminated_block_is_loud_rather_than_silent(self):
        """Non-vacuity for `_scan`'s raise, beside the residue it does NOT close.

        The three raising cases are the ones measured 2026-08-14 to reach a
        block opener from OUTSIDE a string. Note which spelling is which: the
        `@' ... '@` here-string reaches it because `'` is not carried across
        newlines, while the `@" ... "@` form with balanced quotes does not
        reach it at all and hands its body back as code -- the ADDING residue,
        not blindness. Without the raise the first case returns `["$d = @'"]`
        and everything after it is silently gone.
        """
        with self.assertRaises(ValueError):
            _powershell_code_lines("$d = @'\n<# lead\n'@\n$keep = 1\n")
        with self.assertRaises(ValueError):
            _powershell_code_lines("$d = 1\n<# open\n$keep = 1\n")
        with self.assertRaises(ValueError):
            _js_code_lines("a();\n/* open\nkeep();\n")

        # The `@" ... "@` form: no raise, body returned AS CODE.
        self.assertIn(
            "<# lead",
            "\n".join(_powershell_code_lines('$d = @"\n<# lead\n"@\n$keep = 1\n')))

        # The residue the raise does NOT close: re-closing with `#>` leaves
        # the scan's state clean and loses the region in silence. Valid
        # PowerShell -- pwsh 7.6.4 runs it. Today's wrong answer, on purpose.
        reclosed = _powershell_code_lines("$d = @'\n<# lead #>\n'@\n$keep = 1\n")
        self.assertNotIn("<# lead #>", "\n".join(reclosed))
        self.assertIn("$keep = 1", reclosed)


class TestPythonDistributionIdentity(unittest.TestCase):
    def test_distribution_name_and_cli_command(self):
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        self.assertEqual(project["name"], "codex-claude-usage")
        self.assertEqual(
            project["scripts"], {"codex-claude-usage": "codex_claude_usage.cli:main"})


class TestVendoredAssetPinParity(unittest.TestCase):
    """The Chart.js pin is duplicated across the runtime and the packager.

    dashboard.py refuses to serve an asset whose digest doesn't match its pin,
    and copy-python.js refuses to bundle one whose digest doesn't match its own.
    If the two constants drift, the .vsix is built from an asset the dashboard
    will then reject at runtime — every chart silently disappears in the
    extension. Keep them equal, in the spirit of the PRICING parity test.
    """

    def test_packager_and_dashboard_pin_the_same_digest(self):
        import dashboard

        script = (
            ROOT / "vscode-extension" / "scripts" / "copy-python.js"
        ).read_text(encoding="utf-8")
        match = re.search(
            r"expectedChartSha256\s*=\s*[\"']([0-9a-f]{64})[\"']", script
        )
        self.assertIsNotNone(match, "copy-python.js must pin a Chart.js sha256")
        self.assertEqual(match.group(1), dashboard.CHART_JS_SHA256)

    def test_pin_matches_the_vendored_file(self):
        import dashboard

        digest = hashlib.sha256(
            (ROOT / "vendor" / "chart.umd.js").read_bytes()
        ).hexdigest()
        self.assertEqual(digest, dashboard.CHART_JS_SHA256)


class TestDockerSecurityTopology(unittest.TestCase):
    # The launcher starts TWO containers -- the app and the loopback proxy --
    # and three of the hardening flags therefore have to appear twice. A bare
    # `assertIn` per flag is count-BLIND: deleting one of the two
    # `--cap-drop ALL` lines left this module green (measured), which is one
    # unconfined container. So every entry carries the number of code
    # occurrences it must have, and the mapping is what makes a deletion loud.
    # This is a different defect from an assertion a comment satisfies, and it
    # needs a different instrument -- exact counts, not comment stripping --
    # though the counts are taken over code lines so both are covered at once.
    #
    # **These counts are on STRIPPED code deliberately, and moving them to the
    # raw body would make this guard strictly weaker.** Measured 2026-08-14 by
    # commenting out the proxy container's real `--cap-drop ALL`: the stripped
    # count drops to 1 and this test fails, while `raw.count` still reads 2
    # and a raw-placed assertion would have passed with one container
    # unconfined. The directional rule, which the module used to state as a
    # blanket "every counting assertion is taken on the RAW body":
    #
    #   counting a VIOLATION (a second `--uninstall-extension` call) -> RAW,
    #     because a lossy stripper can only LOWER a count and lowering it
    #     hides the violation;
    #   counting a REQUIRED occurrence (these hardening flags) -> STRIPPED,
    #     because a lossy stripper lowers the count and that fails CLOSED,
    #     while the raw body cannot tell a live flag from a commented-out one.
    #
    # Residue, so this is not read as closure: a stripper that ADDS text
    # defeats a required-occurrence count wherever it sits. Two of the known
    # ways are now closed and self-tested -- an escaped quote (the dialect
    # escape rules above) and the backslash-newline CONTINUATION inside a
    # double-quoted string, whose fix is in `_scan` and whose end-to-end
    # attack is reproduced below. Measured 2026-08-14: with the proxy
    # container's real `--cap-drop ALL` deleted and
    # `echo "abc\<newline>def"  # --cap-drop ALL` appended (bash prints
    # `abcdef` and `bash -n` accepts the file, so the tail is genuinely a
    # comment), this test used to go from RED on the deletion alone to GREEN;
    # against the fix the same two edits leave it RED.
    #
    # Known-open ADDING residues, none of them reachable by the escape rules:
    # bash's `$'...'` ANSI-C quoting, its `: '...'` block idiom (which defeats
    # raw and stripped alike), heredocs in both forms, and -- found in round
    # 10, after two commits had already been written to close this counter --
    # a comment inside a `$( )` or backtick substitution that sits inside a
    # double-quoted word. That last one defeated this exact map end to end:
    # the proxy container's `--cap-drop ALL` deleted, one decoy planted, whole
    # module green, container unconfined. It is the only one of the five whose
    # construct is actually present in the guarded files, and
    # `TestTheShippedScriptsStayInsideWhatTheseStrippersLex` now holds the
    # boundary for it at the construct.
    #
    # **That list is what has been found, not what exists.** This comment used
    # to open "What is STILL open, all of it" and close by calling
    # `_bash_code_lines` "the full list"; both were exhaustiveness claims
    # standing on the counter this residue defeats, and both were false when
    # written. `_bash_code_lines` carries the direction each fails in and why
    # the blanket "carry quote state across the newline" that would close two
    # of them is refused -- read it as a record, not an inventory. Separately,
    # and not a lexer question at all: `.count()` counts an INERT occurrence,
    # so replacing a real flag with `[ -z "--read-only" ]` keeps the count and
    # drops the flag.
    HARDENING = {
        "--internal --ipv6=false": 1,
        "gateway_mode_ipv4=isolated": 1,
        "host_binding_ipv4=127.0.0.1": 1,
        'PRIVATE_DRIVER" != "bridge': 1,
        'PROXY_DRIVER" != "bridge': 1,
        "--read-only": 2,
        "--cap-drop ALL": 2,
        "--security-opt no-new-privileges": 2,
        "--memory 512m": 1,
        "--memory 128m": 1,
        "od -An -N32 -tx1 /dev/urandom": 1,
        # NAME only, never `NAME=value`: see the sibling test below, which is
        # what actually guards this. The key here just pins that the variable is
        # still handed to the container at all.
        "--env CODEX_CLAUDE_USAGE_API_TOKEN": 1,
        "--env CODEX_CLAUDE_USAGE_SUPPRESS_AUTH_URL=1": 1,
        "/#token=${API_TOKEN}": 1,
        "dst=/home/codexclaudeusage/.claude/projects,readonly": 1,
        # No leading space: the stripper hands back the statement stripped of
        # its indentation, and the space this key used to carry was only ever
        # standing in for "a flag, not a suffix of a longer one".
        '--env PORT': 1,
        '--env CODEX_CLAUDE_USAGE_INVOKED_AS': 1,
        '--env CODEX_CLAUDE_USAGE_DOCKER_CONTAINER': 1,
        '-p "127.0.0.1:$PORT:$PORT"': 1,
        '--listen-port "$PORT"': 1,
        '--target-port "$PORT"': 1,
    }

    def test_the_bearer_token_never_travels_in_a_command_line(self):
        """argv is world-readable on Linux; the launcher's environment is not.

        `--env CODEX_CLAUDE_USAGE_API_TOKEN="$API_TOKEN"` put the whole 64-hex bearer
        token in the `docker run` process's argv, and /proc/<pid>/cmdline is
        mode 444 with no `hidepid` on mainstream distributions -- so any other
        local user sweeping the process table while the launcher runs captured
        it, which an ordinary `sh` polling loop was shown to win. No duration is
        quoted: it is a property of the reader's machine and their Docker, and
        `tests/test_comment_benchmarks.py` rejects exactly that kind of figure.
        That token is the whole of the authentication: the
        Origin check passes when Origin is absent (correct for non-browser
        clients), so a raw local socket with the token reads /api/data in full
        -- every project name, session topic, branch and cost in the database --
        and can POST /api/rescan.

        The name-only form travels in the launcher's own environment instead,
        which is /proc/<pid>/environ, mode 0400 and owner-only.
        `vscode-extension/src/server-manager.ts` already passed this same secret
        by environment rather than argv; the Docker launcher was the one surface
        that did not.

        Written as a search for the ASSIGNMENT form rather than an equality on
        the whole line, so that any future `--env FOO="$SECRET"` is caught too,
        not only the one that was wrong.
        """
        import re
        raw = (ROOT / "scripts" / "run-docker.sh").read_text(encoding="utf-8")
        code = "\n".join(_bash_code_lines(raw))
        offenders = re.findall(r"--env\s+[A-Za-z_][A-Za-z0-9_]*=\S*\$", code)
        self.assertEqual(
            offenders, [],
            "a secret is being passed to docker in argv, where every local "
            "user can read it; pass the NAME only and export the value")

    def test_launcher_fails_closed_around_networks_and_mounts(self):
        raw = (ROOT / "scripts" / "run-docker.sh").read_text(encoding="utf-8")
        code = "\n".join(_bash_code_lines(raw))
        # `/#token=${API_TOKEN}` is why the stripper has to know about quotes:
        # it lives inside the double-quoted echo at run-docker.sh:192, and the
        # naive trailing-`#` cut this module used to carry would have deleted
        # it along with the rest of that line.
        self.assertEqual(
            {flag: code.count(flag) for flag in self.HARDENING},
            dict(self.HARDENING),
            "a hardening flag moved, was deleted, or was commented out; the "
            "two-count entries are the app and proxy containers, and losing "
            "one leaves that container unconfined",
        )

    def test_thresholds_live_on_the_writable_data_mount(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            "ENV CODEX_CLAUDE_USAGE_THRESHOLDS=/data/limit-thresholds.json",
            dockerfile,
            "the read-only image acknowledged threshold writes but had no "
            "writable persistence path",
        )

    def test_public_authority_port_reaches_the_dashboard_unchanged(self):
        """The raw TCP proxy must not make the dashboard reject browser Host.

        The browser sends the published host port and the proxy deliberately
        forwards bytes unchanged.  Therefore the proxy listener, upstream
        dashboard and host mapping must all use the selected port.  Keeping
        fixed internal 8080/8081 ports made every Docker request answer 421
        after the dashboard began validating an explicit Host port.
        """
        raw = (ROOT / "scripts" / "run-docker.sh").read_text(encoding="utf-8")
        code = "\n".join(_bash_code_lines(raw))
        for fragment in (
            "--env PORT",
            '-p "127.0.0.1:$PORT:$PORT"',
            '--listen-port "$PORT"',
            '--target-port "$PORT"',
            "http://127.0.0.1:$PORT/healthz",
        ):
            self.assertIn(fragment, code)

        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("port=os.environ['PORT']", dockerfile)
        self.assertIn("http://127.0.0.1:{port}/healthz", dockerfile)

    @unittest.skipUnless(os.name == "posix", "Docker shell launcher needs a POSIX runtime")
    def test_launcher_rejects_ambiguous_or_unprivileged_ports_before_docker(self):
        script = ROOT / "scripts" / "run-docker.sh"
        for port in ("80", "1023", "01024", "077777", "00099999", "65536"):
            with self.subTest(port=port):
                env = dict(os.environ, CODEX_CLAUDE_USAGE_DOCKER_PORT=port)
                got = subprocess.run(
                    ["bash", str(script)], cwd=ROOT, env=env,
                    capture_output=True, text=True, encoding="utf-8",
                    check=False,
                )
                self.assertEqual(got.returncode, 1)
                self.assertIn("decimal integer from 1024 to 65535", got.stderr)

    def test_launcher_waits_for_the_proxy_before_reporting_success(self):
        raw = (ROOT / "scripts" / "run-docker.sh").read_text(encoding="utf-8")
        code = "\n".join(_bash_code_lines(raw))
        self.assertLess(code.index('docker exec "$CREATED_PROXY_CONTAINER_ID"'),
                        code.index('echo "✅  Running at'))
        self.assertIn("Dashboard containers started but did not become ready", code)

    @unittest.skipUnless(os.name == "posix", "Docker shell launcher needs a POSIX runtime")
    def test_failed_launch_removes_only_artifacts_it_created(self):
        """A proxy-create failure must not strand the app or new networks."""
        script = ROOT / "scripts" / "run-docker.sh"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            claude = tmp / "claude"
            (claude / "projects").mkdir(parents=True)
            data = tmp / "data"
            docker_log = tmp / "docker.log"
            fake_docker = tmp / "docker"
            fake_docker.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$DOCKER_TEST_LOG\"\n"
                "case \"$1 $2\" in\n"
                "  'version --format') echo 28.0.0 ;;\n"
                "  'container inspect'|'network inspect') exit 1 ;;\n"
                "  'network create') printf '%s-id\\n' \"${!#}\" ;;\n"
                "  'run --rm') echo app-id ;;\n"
                "  'create --rm') exit 42 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o700)
            env = dict(
                os.environ,
                PATH=f"{tmp}{os.pathsep}{os.environ['PATH']}",
                CLAUDE_CONFIG_DIR=str(claude),
                CODEX_CLAUDE_USAGE_DOCKER_DATA_DIR=str(data),
                DOCKER_TEST_LOG=str(docker_log),
            )

            got = subprocess.run(
                ["bash", str(script)], cwd=ROOT, env=env,
                capture_output=True, text=True, encoding="utf-8", check=False,
            )

            self.assertEqual(got.returncode, 42, got.stderr)
            calls = docker_log.read_text(encoding="utf-8").splitlines()
            failed = next(i for i, call in enumerate(calls)
                          if call.startswith("create --rm"))
            cleanup = calls[failed + 1:]
            self.assertEqual(
                cleanup,
                [
                    'container inspect --format {{.Id}} {{ index .Config.Labels '
                    '"com.codex-claude-usage.launch" }} codex-claude-usage-proxy',
                    "rm --force app-id",
                    "network rm codex-claude-usage-loopback-id",
                    "network rm codex-claude-usage-private-id",
                ],
            )

    @unittest.skipUnless(os.name == "posix", "Docker shell launcher needs POSIX signals")
    def test_launcher_signal_uses_the_launch_label_when_id_capture_is_cut_off(self):
        """TERM on the launcher after create cannot strand that object."""
        script = ROOT / "scripts" / "run-docker.sh"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            claude = tmp / "claude"
            (claude / "projects").mkdir(parents=True)
            docker_log = tmp / "docker.log"
            docker_state = tmp / "network-owner"
            launcher_pid = tmp / "launcher.pid"
            fake_docker = tmp / "docker"
            fake_docker.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$DOCKER_TEST_LOG\"\n"
                "case \"$1 $2\" in\n"
                "  'version --format') echo 28.0.0 ;;\n"
                "  'container inspect') exit 1 ;;\n"
                "  'network inspect')\n"
                "    if [[ \"$*\" == *'--format'* && -f \"$DOCKER_TEST_STATE\" ]]; then\n"
                "      printf 'interrupted-network-id '\n"
                "      command cat \"$DOCKER_TEST_STATE\"\n"
                "    else\n"
                "      exit 1\n"
                "    fi\n"
                "    ;;\n"
                "  'network create')\n"
                "    for arg in \"$@\"; do\n"
                "      case \"$arg\" in\n"
                "        com.codex-claude-usage.launch=*)\n"
                "          printf '%s\\n' \"${arg#*=}\" > \"$DOCKER_TEST_STATE\" ;;\n"
                "      esac\n"
                "    done\n"
                "    kill -TERM \"$(command cat \"$DOCKER_TEST_LAUNCHER_PID\")\"\n"
                "    sleep 0.2\n"
                "    ;;\n"
                "  'network rm')\n"
                "    kill -TERM \"$(command cat \"$DOCKER_TEST_LAUNCHER_PID\")\"\n"
                "    sleep 0.2\n"
                "    ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o700)
            env = dict(
                os.environ,
                PATH=f"{tmp}{os.pathsep}{os.environ['PATH']}",
                CLAUDE_CONFIG_DIR=str(claude),
                CODEX_CLAUDE_USAGE_DOCKER_DATA_DIR=str(tmp / "data"),
                DOCKER_TEST_LOG=str(docker_log),
                DOCKER_TEST_STATE=str(docker_state),
                DOCKER_TEST_LAUNCHER_PID=str(launcher_pid),
            )

            got = subprocess.run(
                [
                    "bash", "-c",
                    'printf "%s\\n" "$$" > "$1"; exec bash "$2"',
                    "docker-signal-test", str(launcher_pid), str(script),
                ], cwd=ROOT, env=env,
                capture_output=True, text=True, encoding="utf-8", check=False,
            )

            self.assertEqual(got.returncode, 143, got.stderr)
            calls = docker_log.read_text(encoding="utf-8").splitlines()
            created = next(i for i, call in enumerate(calls)
                           if call.startswith("network create"))
            self.assertEqual(
                calls[created + 1:],
                [
                    'network inspect --format {{.Id}} {{ index .Labels '
                    '"com.codex-claude-usage.launch" }} codex-claude-usage-private',
                    "network rm interrupted-network-id",
                ],
            )
            self.assertIn("Removing Docker artifacts", got.stderr)

    @unittest.skipUnless(os.name == "posix", "Docker shell launcher needs a POSIX runtime")
    def test_an_image_named_like_the_app_is_not_treated_as_a_container(self):
        """The persistent image must not block the next ordinary launch."""
        script = ROOT / "scripts" / "run-docker.sh"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            claude = tmp / "claude"
            (claude / "projects").mkdir(parents=True)
            docker_log = tmp / "docker.log"
            fake_docker = tmp / "docker"
            fake_docker.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$DOCKER_TEST_LOG\"\n"
                "case \"$1 $2\" in\n"
                "  'version --format') echo 28.0.0 ;;\n"
                "  'container inspect') exit 1 ;;\n"
                "  'inspect --format') echo image-id ;;\n"
                "  'network inspect') exit 1 ;;\n"
                "  'network create') exit 42 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o700)
            env = dict(
                os.environ,
                PATH=f"{tmp}{os.pathsep}{os.environ['PATH']}",
                CLAUDE_CONFIG_DIR=str(claude),
                CODEX_CLAUDE_USAGE_DOCKER_DATA_DIR=str(tmp / "data"),
                DOCKER_TEST_LOG=str(docker_log),
            )

            got = subprocess.run(
                ["bash", str(script)], cwd=ROOT, env=env,
                capture_output=True, text=True, encoding="utf-8", check=False,
            )

            self.assertEqual(got.returncode, 42, got.stderr)
            self.assertNotIn("Refusing to remove unrelated container", got.stderr)
            calls = docker_log.read_text(encoding="utf-8").splitlines()
            self.assertIn(
                'container inspect --format {{.Id}} {{ index .Config.Labels '
                '"com.codex-claude-usage.managed" }} codex-claude-usage',
                calls,
            )

    @unittest.skipUnless(os.name == "posix", "Docker shell launcher needs a POSIX runtime")
    def test_stopping_a_prior_managed_container_removes_its_captured_id(self):
        """A same-name replacement after inspection must not be the target."""
        script = ROOT / "scripts" / "run-docker.sh"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            claude = tmp / "claude"
            (claude / "projects").mkdir(parents=True)
            docker_log = tmp / "docker.log"
            fake_docker = tmp / "docker"
            fake_docker.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$DOCKER_TEST_LOG\"\n"
                "case \"$1 $2\" in\n"
                "  'version --format') echo 28.0.0 ;;\n"
                "  'container inspect')\n"
                "    if [[ \"$*\" == *'{{.Id}}'* ]]; then\n"
                "      echo 'prior-proxy-id true'\n"
                "    else\n"
                "      echo true\n"
                "    fi\n"
                "    ;;\n"
                "  'rm --force') exit 73 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o700)
            env = dict(
                os.environ,
                PATH=f"{tmp}{os.pathsep}{os.environ['PATH']}",
                CLAUDE_CONFIG_DIR=str(claude),
                CODEX_CLAUDE_USAGE_DOCKER_DATA_DIR=str(tmp / "data"),
                DOCKER_TEST_LOG=str(docker_log),
            )

            got = subprocess.run(
                ["bash", str(script)], cwd=ROOT, env=env,
                capture_output=True, text=True, encoding="utf-8", check=False,
            )

            self.assertEqual(got.returncode, 73, got.stderr)
            calls = docker_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("rm --force prior-proxy-id", calls)
            self.assertNotIn("rm --force codex-claude-usage-proxy", calls)

    @unittest.skipUnless(os.name == "posix", "Docker shell launcher needs a POSIX runtime")
    def test_reused_networks_and_proxy_target_use_launch_identity(self):
        """Mutable Docker names cannot redirect attachments or proxy traffic."""
        script = ROOT / "scripts" / "run-docker.sh"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            claude = tmp / "claude"
            (claude / "projects").mkdir(parents=True)
            docker_log = tmp / "docker.log"
            fake_docker = tmp / "docker"
            fake_docker.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$DOCKER_TEST_LOG\"\n"
                "case \"$1 $2\" in\n"
                "  'version --format') echo 28.0.0 ;;\n"
                "  'container inspect'|'inspect --format') exit 1 ;;\n"
                "  'network inspect')\n"
                "    if [[ \"${3-}\" != --format ]]; then exit 0; fi\n"
                "    format=$4; target=${!#}\n"
                "    private=false\n"
                "    [[ \"$target\" == codex-claude-usage-private || \"$target\" == private-existing-id ]] && private=true\n"
                "    case \"$format\" in\n"
                "      *'.Id'*) [[ \"$private\" == true ]] && echo private-existing-id || echo proxy-existing-id ;;\n"
                "      *'.Driver'*) echo bridge ;;\n"
                "      *'gateway_mode_ipv4'*) echo isolated ;;\n"
                "      *'host_binding_ipv4'*) echo 127.0.0.1 ;;\n"
                "      *'.EnableIPv6'*) echo false ;;\n"
                "      *'.Internal'*) [[ \"$private\" == true ]] && echo true || echo false ;;\n"
                "      *'len .Containers'*) echo 0 ;;\n"
                "    esac\n"
                "    ;;\n"
                "  'run --rm') echo app-id ;;\n"
                "  'create --rm') echo proxy-id ;;\n"
                "  'network connect') exit 55 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o700)
            env = dict(
                os.environ,
                PATH=f"{tmp}{os.pathsep}{os.environ['PATH']}",
                CLAUDE_CONFIG_DIR=str(claude),
                CODEX_CLAUDE_USAGE_DOCKER_DATA_DIR=str(tmp / "data"),
                DOCKER_TEST_LOG=str(docker_log),
            )

            got = subprocess.run(
                ["bash", str(script)], cwd=ROOT, env=env,
                capture_output=True, text=True, encoding="utf-8", check=False,
            )

            self.assertEqual(got.returncode, 55, got.stderr)
            calls = docker_log.read_text(encoding="utf-8").splitlines()
            run = next(call for call in calls if call.startswith("run --rm"))
            create = next(call for call in calls if call.startswith("create --rm"))
            self.assertIn("--network private-existing-id", run)
            self.assertIn("--network proxy-existing-id", create)
            self.assertIn("network connect private-existing-id proxy-id", calls)
            run_args = run.split()
            create_args = create.split()
            app_alias = run_args[run_args.index("--network-alias") + 1]
            proxy_target = create_args[create_args.index("--target-host") + 1]
            self.assertRegex(app_alias, r"^app-[0-9a-f]{48}$")
            self.assertEqual(proxy_target, app_alias)
            self.assertNotEqual(proxy_target, "codex-claude-usage")

    @unittest.skipUnless(os.name == "posix", "Docker shell launcher needs a POSIX runtime")
    def test_failed_launch_uses_ids_not_names_that_can_be_replaced(self):
        """A same-name replacement must survive cleanup of this invocation."""
        script = ROOT / "scripts" / "run-docker.sh"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            claude = tmp / "claude"
            (claude / "projects").mkdir(parents=True)
            docker_log = tmp / "docker.log"
            fake_docker = tmp / "docker"
            fake_docker.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$DOCKER_TEST_LOG\"\n"
                "case \"$1 $2\" in\n"
                "  'version --format') echo 28.0.0 ;;\n"
                "  'container inspect'|'network inspect') exit 1 ;;\n"
                "  'network create') printf '%s-id\\n' \"${!#}\" ;;\n"
                "  'run --rm') echo app-id ;;\n"
                "  'create --rm') echo proxy-id ;;\n"
                "  'network connect') exit 55 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o700)
            env = dict(
                os.environ,
                PATH=f"{tmp}{os.pathsep}{os.environ['PATH']}",
                CLAUDE_CONFIG_DIR=str(claude),
                CODEX_CLAUDE_USAGE_DOCKER_DATA_DIR=str(tmp / "data"),
                DOCKER_TEST_LOG=str(docker_log),
            )

            got = subprocess.run(
                ["bash", str(script)], cwd=ROOT, env=env,
                capture_output=True, text=True, encoding="utf-8", check=False,
            )

            self.assertEqual(got.returncode, 55, got.stderr)
            calls = docker_log.read_text(encoding="utf-8").splitlines()
            failed = next(i for i, call in enumerate(calls)
                          if call.startswith("network connect"))
            self.assertEqual(
                calls[failed + 1:],
                [
                    "rm --force proxy-id",
                    "rm --force app-id",
                    "network rm codex-claude-usage-loopback-id",
                    "network rm codex-claude-usage-private-id",
                ],
            )

    def test_image_build_context_is_allowlisted_and_digest_pinned(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertIsNotNone(
            re.search(r"^FROM \S+@sha256:[0-9a-f]{64}$", dockerfile, re.M)
        )
        self.assertTrue(dockerignore.startswith("*\n"))
        # Whole lines, not substrings. `"!vendor/" in dockerignore` was true
        # only because `!vendor/chart.umd.js` contains it, so the assertion
        # could not tell the named children from the bare directory exception
        # they replaced — the single revert it exists to catch. A bare
        # `!vendor/` re-admits the whole subtree: re-run 2026-08-10 on Docker
        # 29.5.2 rather than inherited, while it stands there a planted
        # vendor/.env and vendor/secrets/creds.json reach /app/vendor on BuildKit
        # and on the classic builder, and with the named lines left alone neither
        # does (tests/test_web_assets.py carries that measurement and the two-way
        # disk check). The other four entries are genuine whole-line
        # exceptions, checked against the real file rather than assumed.
        # Compared against the exception lines rather than the whole text, so a
        # failure prints the allowlist instead of sixty lines of prose.
        admitted = {
            line for line in dockerignore.splitlines() if line.startswith("!")
        }
        for path in ("codex_claude_usage/cli.py", "codex_claude_usage/scanner.py",
                     "codex_claude_usage/dashboard.py", "proxy.py",
                     "vendor/chart.umd.js"):
            self.assertIn("!" + path, admitted)
        for bare in ("!vendor", "!vendor/"):
            self.assertNotIn(
                bare, admitted,
                f"{bare} ships every stray beside chart.umd.js; name the files",
            )


class TestWorkflowSupplyChain(unittest.TestCase):
    ACTION_PINS = {
        "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-node": "820762786026740c76f36085b0efc47a31fe5020",
        "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
        "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    }

    def test_all_actions_are_pinned_to_commit_sha(self):
        workflows = ROOT / ".github" / "workflows"
        uses = []
        for path in workflows.glob("*.yml"):
            uses.extend(
                re.findall(
                    r"^\s*-?\s*uses:\s*(\S+)",
                    path.read_text(encoding="utf-8"),
                    re.M,
                )
            )
        self.assertTrue(uses)
        for action in uses:
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")
            name, sha = action.rsplit("@", 1)
            self.assertIn(name, self.ACTION_PINS)
            self.assertEqual(sha, self.ACTION_PINS[name])

    def test_extension_workflows_use_supported_lts_node(self):
        for name in ("extension-ci.yml", "tag-on-merge.yml"):
            body = (ROOT / ".github" / "workflows" / name).read_text(
                encoding="utf-8"
            )
            self.assertIn('node-version: "24.18.0"', body)
            self.assertNotIn('node-version: "20.', body)

    def test_release_build_isolated_from_write_token(self):
        body = (ROOT / ".github" / "workflows" / "tag-on-merge.yml").read_text(
            encoding="utf-8"
        )
        prepare_start = body.index("  prepare:\n")
        release_start = body.index("  release:\n")
        prepare = body[prepare_start:release_start]
        release = body[release_start:]

        self.assertIn("contents: read", prepare)
        self.assertNotIn("contents: write", prepare)
        self.assertIn("npm run package", prepare)
        self.assertIn("contents: write", release)
        self.assertNotIn("npm run", release)
        self.assertNotIn("github.event.repository.private", prepare)
        self.assertNotIn("Require a private repository", prepare)
        self.assertIn("Release from main only", prepare)

    def test_release_inherits_only_a_green_applicable_extension_run(self):
        """A changelog-only push must not turn a prior red run into absence."""
        body = (ROOT / ".github" / "workflows" / "tag-on-merge.yml").read_text(
            encoding="utf-8"
        )
        gate = body[body.index("          runs_for() {"):
                    body.index("      # Build under a read-only token")]

        self.assertIn("extension_run_for()", gate)
        self.assertIn("git log -1 --format=%H", gate)
        self.assertIn("actions/workflows/extension-ci.yml/runs", gate)
        self.assertIn("--paginate --slurp", gate)
        self.assertEqual(gate.count("git merge-base --is-ancestor"), 2)
        self.assertIn('ext=$(check ".github/workflows/extension-ci.yml" extension)',
                      gate)
        self.assertNotIn("optional ] && echo \"ok\"", gate)


# --------------------------------------------------------------------------
# The implicit-encoding guard. See TestSuiteIsIndependentOfTheRunnerEncoding.
# --------------------------------------------------------------------------

# name -> (mode positional index, encoding positional index, default mode).
# The indices are the real stdlib signatures, so a call that passes `mode` or
# `encoding` positionally is read correctly rather than mistaken for an
# omission. `None` means the parameter does not exist positionally.
_OPENERS = {
    "open": (1, 3, "r"),                    # open(file, mode, buffering, enc)
    "fdopen": (1, 3, "r"),                  # forwards to open()
    "read_text": (None, 0, "r"),            # Path.read_text(encoding, ...)
    "write_text": (None, 1, "w"),           # Path.write_text(data, encoding)
    "NamedTemporaryFile": (0, 2, "w+b"),    # mode defaults to BINARY
    "TemporaryFile": (0, 2, "w+b"),         # ditto
    "SpooledTemporaryFile": (1, 3, "w+b"),  # (max_size, mode, buffering, enc)
}

# subprocess entry points. `encoding=` is keyword-only on all of them, and
# `text=`/`universal_newlines=` are what turn the pipes into text.
_SPAWNERS = ("run", "Popen", "check_output", "check_call", "call")


def _called_name(node):
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _keyword(call, name):
    for kw in call.keywords:
        if kw.arg == name:
            return kw
    return None


def _passes_an_encoding(call, encoding_index):
    """True when the call names an encoding at all — positionally or by name.

    `encoding=None` counts as NOT passing one: it is spelled out but still asks
    for `locale.getencoding()`, which is the whole defect.
    """
    kw = _keyword(call, "encoding")
    if kw is not None:
        return not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
    if encoding_index is not None and len(call.args) > encoding_index:
        return True
    return False


def _mode_of(call, mode_index, default):
    """The literal mode string, or None when it cannot be read statically."""
    kw = _keyword(call, "mode")
    if kw is not None:
        node = kw.value
    elif mode_index is not None and len(call.args) > mode_index:
        node = call.args[mode_index]
    else:
        return default
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def implicit_encoding_calls(source, filename="<source>"):
    """Every call in `source` that would decode with the runner's codepage.

    Returns `[(lineno, description), ...]`, sorted, so a failure can name the
    exact line rather than the file.

    Module level, and the string-taking front door onto `_offending_calls`,
    deliberately — the same reason `_stranded_definitions` in
    tests/test_suite_hygiene.py is module level: the guard below runs that walk
    over the real `tests/` directory (from already-parsed trees, so the
    directory is read and parsed once), and the meta-test below that drives
    *this* function over source known to break the rule. A scan for *absence*
    reports success just as happily when it has quietly stopped matching
    anything, so the meta-test has to reach the real walk rather than a copy of
    its body.
    """
    return _offending_calls(ast.parse(source, filename=filename))


def _offending_calls(tree):
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        if name is None:
            continue
        # `os.open` is not `open`. `_called_name` reads an Attribute call as its
        # bare attribute, so `os.open(path, os.O_WRONLY)` arrived here spelled
        # "open" and was reported as an implicit-encoding offender -- but it
        # returns a FILE DESCRIPTOR and has no `encoding` parameter to pass. The
        # first `os.open` written under tests/ (the guard probe in
        # test_real_data_guard_is_live.py) failed this check with no way to
        # satisfy it. `os.fdopen` is deliberately NOT excluded: that one wraps
        # the descriptor in a text stream and does take an encoding.
        if (name == "open" and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"):
            continue
        # `**kwargs` may carry the encoding; nothing static can tell.
        if any(kw.arg is None for kw in node.keywords):
            continue

        if name in _SPAWNERS:
            if _passes_an_encoding(node, None):
                continue
            for kw in node.keywords:
                if kw.arg not in ("text", "universal_newlines"):
                    continue
                explicitly_off = (isinstance(kw.value, ast.Constant)
                                  and not kw.value.value)
                if not explicitly_off:
                    found.append((
                        node.lineno,
                        f"subprocess.{name}({kw.arg}=...) with no encoding=",
                    ))
                break
            continue

        if name not in _OPENERS:
            continue
        mode_index, encoding_index, default = _OPENERS[name]
        if _passes_an_encoding(node, encoding_index):
            continue
        mode = _mode_of(node, mode_index, default)
        if mode is not None and "b" in mode:
            continue  # binary: there is no encoding to get wrong
        unreadable = "" if mode is not None else " (mode is not a literal)"
        found.append((node.lineno, f"{name}() with no encoding={unreadable}"))
    return sorted(found)


_WATCHED = frozenset(_OPENERS) | frozenset(_SPAWNERS)


def _watched_call_count(tree):
    """How many calls the walk above even looked at. Vacuity fuel."""
    return sum(1 for node in ast.walk(tree)
               if isinstance(node, ast.Call)
               and _called_name(node) in _WATCHED)


_BREAKS_THE_RULE = '''
import subprocess, sys, tempfile
from pathlib import Path
Path("a").read_text()
Path("a").write_text("x")
open("a").close()
open("a", "w").close()
tempfile.NamedTemporaryFile("w", delete=False).close()
subprocess.run([sys.executable, "-c", "pass"], text=True)
subprocess.Popen([sys.executable], universal_newlines=True)
Path("a").read_text(encoding=None)
'''

_KEEPS_THE_RULE = '''
import subprocess, sys, tempfile
from pathlib import Path
Path("a").read_text(encoding="utf-8")
Path("a").read_text("utf-8")
Path("a").write_text("x", encoding="utf-8")
Path("a").write_text("x", "utf-8")
open("a", encoding="utf-8").close()
open("a", "rb").close()
open("a", mode="rb").close()
tempfile.NamedTemporaryFile(delete=False).close()
tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8").close()
tempfile.NamedTemporaryFile("w+b", delete=False).close()
subprocess.run([sys.executable], text=True, encoding="utf-8")
subprocess.run([sys.executable], capture_output=True)
subprocess.run([sys.executable], text=False)
subprocess.run([sys.executable], text=True, **kwargs)
'''


class TestSuiteIsIndependentOfTheRunnerEncoding(unittest.TestCase):
    """No test under `tests/` may decode with the interpreter's default encoding.

    `Path.read_text()` and `subprocess.run(text=True)` with no `encoding=` fall
    back to `locale.getencoding()`, which is cp1252 on a windows-latest runner.
    `scripts/run-docker.sh` carries U+274C, so the read above raised
    `UnicodeDecodeError` there and the whole `Tests` workflow concluded failure
    — which `tag-on-merge.yml` reads as a refusal to tag or publish. Product
    code is already explicit everywhere or opens binary; the harness was the
    outlier.

    This used to guard **only this module**, because its target list was built
    from this module's own `globals()`. Every other file in `tests/` was held to
    the rule by convention, which is precisely how the offenders accumulated in
    the first place. It is now two halves, and neither is a list:

    * **Static** — an AST walk over every `tests/*.py` found on disk. It sees
      lines no test run reaches, so a branch that only executes on one platform
      is still checked, and it names `file:line`. It cannot see a call it
      cannot resolve statically: one made through `getattr`, one whose kwargs
      arrive as `**kwargs`, or a text read performed for us inside a helper
      that lives outside `tests/`.
    * **Dynamic** — one child under PEP 597's `-X warn_default_encoding`, which
      imports every discovered test module and then runs this module's own
      cases. It sees the real call however it was spelled, so it catches the
      aliased and `getattr`-ed ones the walk cannot resolve — but only along
      the code paths it executes: every module body, and the bodies of this
      module's tests. Running all of `tests/` here would double the suite.

    So neither half is complete on its own, and the residue is stated rather
    than papered over: a call that is *both* statically unresolvable *and*
    inside another module's test body is caught by neither. Verified by
    mutation, not by reading — planting `op = open; op(path, "a")` inside a
    method of tests/test_rate_limits.py leaves this module green, while the
    same two lines at that module's top level go red on the dynamic half
    alone.

    The child **warns rather than errors**, and only warnings naming a file
    under `tests/` count. `-W error::EncodingWarning` is deliberately wrong
    here on both counts: it aborts the child at the first warning, so the guard
    loses the one thing that makes it useful — naming the offending line — and
    any `EncodingWarning` raised inside the stdlib would become a Windows-only
    red build, which is the failure mode this guard exists to remove.
    """

    @classmethod
    def setUpClass(cls):
        """Discover and parse tests/ ONCE. Discovery is a glob, never a list.

        Both directory-wide tests below read the same parsed trees, so the
        directory is read and compiled a single time — three passes over ~50
        files, two of them multi-thousand-line, cost seconds rather than the
        fractions this module used to take.
        """
        cls.sources = sorted(TESTS_DIR.glob("*.py"))
        cls.trees = [
            (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
            for path in cls.sources
        ]
        cls.modules = sorted(
            f"tests.{p.stem}" for p in TESTS_DIR.glob("test_*.py"))

    def test_os_open_is_not_mistaken_for_the_builtin(self):
        """`os.open` has no encoding parameter; flagging it is unsatisfiable."""
        self.assertEqual(
            [], _offending_calls(ast.parse(
                "import os\nos.open(p, os.O_WRONLY | os.O_CREAT)\n")))

    def test_the_exclusion_did_not_disarm_the_check(self):
        """Three neighbours of that exclusion must still be caught: the builtin
        `open`, an `os.fdopen` (which DOES take an encoding), and an `open`
        reached as some other object's attribute."""
        for source in ("open(p)\n",
                       "import os\nos.fdopen(fd)\n",
                       "thing.open(p)\n"):
            with self.subTest(source=source.strip()):
                self.assertNotEqual([], _offending_calls(ast.parse(source)),
                                    f"no longer flagged: {source!r}")

    def test_no_call_under_tests_falls_back_to_the_locale_codepage(self):
        offenders = []
        for path, tree in self.trees:
            for lineno, why in _offending_calls(tree):
                offenders.append(
                    f"{path.relative_to(ROOT).as_posix()}:{lineno}: {why}")
        self.assertEqual(
            offenders, [],
            "these calls decode with the runner's codepage — cp1252 on "
            'windows-latest. Pass encoding="utf-8"; for a Python child, set '
            "PYTHONIOENCODING=utf-8 on it as well. Never errors=.",
        )

    def test_the_static_check_sees_source_that_breaks_the_rule(self):
        """Drive the checker itself, or `offenders == []` proves nothing.

        A scan for absence stays green when it stops matching. These two
        constants are the only place the rule is spelled out as code.
        """
        broken = implicit_encoding_calls(_BREAKS_THE_RULE, "<broken>")
        self.assertEqual(
            [lineno for lineno, _ in broken],
            [4, 5, 6, 7, 8, 9, 10, 11],
            f"the checker stopped recognising an offender: {broken}",
        )
        self.assertEqual(
            implicit_encoding_calls(_KEEPS_THE_RULE, "<clean>"), [],
            "the checker flags compliant code, so its verdict means nothing",
        )

    def test_the_discovery_this_guard_rests_on_is_not_vacuous(self):
        """Guard the glob, the way tests/test_web_assets.py guards its own.

        Both halves iterate what the globs return, so a glob that matched
        nothing would satisfy them while checking nothing. These names are
        spelled out here and nowhere else: the job of this test is to catch the
        discovery going quiet, not to be the discovery.
        """
        names = {p.name for p in self.sources}
        for required in (Path(__file__).name, "test_scanner.py",
                         "test_dashboard_js.py"):
            self.assertIn(required, names, "the tests/ glob went quiet")
        self.assertGreater(len(names), 20, "the tests/ glob found almost nothing")
        self.assertEqual(len(self.trees), len(self.sources))
        self.assertIn(f"tests.{Path(__file__).stem}", self.modules)

        # And that the walk had calls to look at. Zero would mean the names in
        # _OPENERS/_SPAWNERS no longer describe how this suite opens files.
        looked_at = sum(_watched_call_count(tree) for _path, tree in self.trees)
        self.assertGreater(
            looked_at, 50,
            "the checker matched almost no calls; _OPENERS/_SPAWNERS have "
            "probably drifted from how the suite actually opens files",
        )

    def test_pep_597_reports_no_implicit_encoding_from_tests(self):
        modules = self.modules
        here = f"tests.{Path(__file__).stem}"
        cases = sorted(
            f"{here}.{name}"
            for name, obj in globals().items()
            if isinstance(obj, type)
            and issubclass(obj, unittest.TestCase)
            and obj is not type(self)  # running ourselves would recurse
        )
        program = (
            "import importlib, sys, unittest\n"
            "mods = sys.argv[1].split(',')\n"
            "for name in mods:\n"
            "    importlib.import_module(name)\n"
            "print('IMPORTED', len(mods))\n"
            "suite = unittest.TestLoader().loadTestsFromNames(\n"
            "    sys.argv[2].split(','))\n"
            "result = unittest.TextTestRunner(verbosity=0).run(suite)\n"
            "print('RAN', result.testsRun, len(result.failures),\n"
            "      len(result.errors))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-X", "warn_default_encoding",
             "-W", "always::EncodingWarning", "-c", program,
             ",".join(modules), ",".join(cases)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            # Both ends of the pipe: the child is Python, so `encoding=` alone
            # would decode a cp1252 stdout as UTF-8 — a second mismatch, and a
            # worse one, since cp1252 decoding never raises while UTF-8 does.
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            timeout=300,
        )
        # The child has to have actually done the work, or the scan below
        # proves nothing: a crashed import or an empty target list would leave
        # stderr clean for entirely the wrong reason.
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"IMPORTED {len(modules)}", proc.stdout, proc.stderr)
        ran = re.search(r"^RAN (\d+) (\d+) (\d+)$", proc.stdout, re.M)
        self.assertIsNotNone(ran, proc.stdout + proc.stderr)
        self.assertGreater(int(ran.group(1)), 0, "the child ran no cases")
        self.assertEqual((ran.group(2), ran.group(3)), ("0", "0"), proc.stderr)

        # Only warnings naming a file under tests/ count. A stdlib
        # EncodingWarning must never turn one platform red on its own.
        offenders = []
        for line in proc.stderr.splitlines():
            match = re.match(r"^(.*?):(\d+): EncodingWarning", line)
            if match is None:
                continue
            path = Path(match.group(1))
            if not path.is_absolute():
                path = ROOT / path
            try:
                path.resolve().relative_to(TESTS_DIR)
            except ValueError:
                continue
            offenders.append(line)
        self.assertEqual(
            offenders, [],
            "PEP 597 saw these run with no encoding= — cp1252 on "
            "windows-latest",
        )


if __name__ == "__main__":
    unittest.main()
