"""The chrome around the figures: what the page says about itself.

Every class here exists because the page previously said something untrue or
nothing at all:

* a first load showed a blank page for its whole duration, because the only
  loading affordance was dimming content that did not exist yet;
* the footer named Anthropic's rate card and Anthropic's model keywords while
  Codex was on screen, where none of it applies;
* there was no way to use the dashboard on a light desktop;
* the header went on promising "Auto-refresh every 15m" after the chosen range
  had switched the polling off;
* the Cost by Model totals row called a published list price an average;
* and "This Week" being the Monday-to-Sunday week was asserted nowhere, so one
  character could move every weekly figure the tool reports.
"""

import json
import os
import re
import subprocess
import tempfile
import unittest
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path

from tests.test_dashboard_js import emit, run_js, requires_node

WEB = Path(__file__).resolve().parent.parent / "web"
CSS = (WEB / "app.css").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")

# HTML elements that carry no end tag, so they never open a nesting level.
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
         "link", "meta", "source", "track", "wbr"}


class _DirectChildren(HTMLParser):
    """The direct children of one element id, in document order.

    A regex cannot do this: `#filter-bar` nests a whole dropdown panel and two
    `<select>`s, and it is the *top level* of that tree that becomes the grid.
    """

    def __init__(self, wanted_id):
        super().__init__(convert_charrefs=True)
        self.wanted, self.depth, self.children = wanted_id, None, []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)   # never opens a level

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.depth is None:
            if attrs.get("id") == self.wanted:
                self.depth = 0
            return
        if self.depth == 0:
            self.children.append({
                "tag": tag, "id": attrs.get("id"),
                "classes": (attrs.get("class") or "").split(),
                "hidden": "hidden" in attrs,
            })
        if tag not in _VOID:
            self.depth += 1

    def handle_endtag(self, tag):
        if self.depth is None or tag in _VOID:
            return
        if self.depth == 0:
            self.depth = None       # the element itself closed
        else:
            self.depth -= 1


def direct_children(element_id, markup=HTML):
    parser = _DirectChildren(element_id)
    parser.feed(markup)
    return parser.children


class TestTheLoadingOverlayIsOutsideTheDimmedContainer(unittest.TestCase):
    """The structural rule the whole indicator depends on.

    `.container.loading` drops to `opacity: 0.35`. A spinner rendered inside that
    element is therefore dimmed to 35% along with everything else — least visible
    at the one moment it is the only thing worth looking at. It has to be a
    sibling, and that is invisible in a screenshot review, so it is pinned here.
    """

    def test_the_container_is_still_dimmed_while_loading(self):
        self.assertRegex(CSS, r"\.container\.loading\s*\{[^}]*opacity:\s*0\.35")

    def test_the_overlay_is_not_inside_the_container(self):
        start = HTML.index('<div class="container">')
        end = HTML.index("<footer>")
        self.assertNotIn('id="load-overlay"', HTML[start:end],
                         "the overlay sits inside .container, so .container.loading "
                         "would dim it to 35% exactly when it must be seen")
        self.assertIn('id="load-overlay"', HTML)

    def test_the_overlay_paints_above_the_page(self):
        block = re.search(r"#load-overlay\s*\{([^}]*)\}", CSS)
        self.assertIsNotNone(block, "no #load-overlay rule")
        body = block.group(1)
        self.assertIn("position: fixed", body)
        self.assertRegex(body, r"z-index:\s*\d+")

    def test_it_starts_hidden(self):
        self.assertRegex(HTML, r'id="load-overlay"[^>]*\bhidden\b')

    def test_the_spinner_animates_and_respects_reduced_motion(self):
        self.assertIn("@keyframes load-spin", CSS)
        reduced = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n  \}",
                            CSS, re.S)
        self.assertIsNotNone(reduced, "no reduced-motion block")
        self.assertIn("load-spinner", reduced.group(1))


@requires_node
class TestTheOverlayIsShownAndCleared(unittest.TestCase):
    def _drive(self, script):
        return run_js(emit("(() => {"
                           "  const overlay = { hidden: true };"
                           "  const text = { textContent: '' };"
                           "  const container = { _c: new Set(), classList: {"
                           "    add(c){ container._c.add(c); },"
                           "    remove(c){ container._c.delete(c); },"
                           "    contains(c){ return container._c.has(c); } },"
                           "    setAttribute(k,v){ container[k] = String(v); },"
                           "    removeAttribute(k){ delete container[k]; } };"
                           "  const els = { 'load-overlay': overlay, 'load-text': text,"
                           "                'meta': {}, 'auth-notice': null };"
                           "  document.getElementById = id => (id in els ? els[id] :"
                           "    { style:{}, classList:{add(){},remove(){},toggle(){}}, appendChild(){} });"
                           "  document.querySelector = s => (s === '.container' ? container : null);"
                           + script +
                           "})()"))

    def test_loading_shows_it_and_names_the_source(self):
        r = self._drive("showLoading('Codex');"
                        "return { shown: !overlay.hidden, text: text.textContent,"
                        "         dimmed: container.classList.contains('loading'),"
                        "         busy: container['aria-busy'] };")
        self.assertTrue(r["shown"])
        self.assertIn("Codex", r["text"])
        self.assertTrue(r["dimmed"], "the dim is still wanted for a source SWITCH, "
                                     "where there is existing content to mark stale")
        self.assertEqual(r["busy"], "true")

    def test_clearing_hides_it_again(self):
        r = self._drive("showLoading('Claude Code'); clearLoading();"
                        "return { hidden: overlay.hidden,"
                        "         undimmed: !container.classList.contains('loading'),"
                        "         busy: container['aria-busy'] || null };")
        self.assertTrue(r["hidden"])
        self.assertTrue(r["undimmed"])
        self.assertIsNone(r["busy"])

    def test_the_auth_notice_clears_it(self):
        """`loadData` returns on 403 without rendering. The overlay is fixed over
        the whole viewport, so leaving it up buries the explanation behind a
        spinner that never stops — a recoverable problem made to look like a
        hang."""
        r = self._drive("showLoading('Claude Code'); showAuthNotice();"
                        "return { hidden: overlay.hidden };")
        self.assertTrue(r["hidden"])


class TestTheFooterNamesTheRightRateCard(unittest.TestCase):
    def test_the_paragraph_is_rendered_not_hardcoded(self):
        """It used to be a literal sentence in the markup, which is why it could
        not follow the source."""
        self.assertIn('id="footer-pricing"', HTML)
        self.assertNotIn("Cost estimates based on Anthropic API pricing", HTML)


@requires_node
class TestFooterPricingNote(unittest.TestCase):
    def _note(self, source):
        return run_js(emit("pricingNoteHTML(" + repr(source).replace("'", '"') + ")"))

    def test_codex_cites_openai_and_never_anthropic(self):
        note = self._note("codex")
        self.assertIn("openai.com/api/pricing", note)
        self.assertNotIn("claude.com", note)

    def test_codex_does_not_list_claude_model_keywords(self):
        """The old text said only models containing fable/mythos/opus/sonnet/haiku
        are costed. Not one of those matches a Codex model, so under Codex the
        sentence claimed nothing on screen was priced."""
        note = self._note("codex")
        for keyword in ("fable", "mythos", "opus", "sonnet", "haiku"):
            self.assertNotIn(keyword, note)

    def test_codex_says_the_figure_is_not_a_bill(self):
        """A Codex plan is a subscription with no per-token price, so the money
        shown is what the API would have charged. Saying so is the difference
        between an estimate and a false invoice."""
        self.assertIn("not a bill", self._note("codex"))

    def test_codex_names_the_models_whose_rates_are_guesses(self):
        listed = run_js(emit("ESTIMATED_RATE_MODELS.slice()"))
        note = self._note("codex")
        self.assertTrue(listed, "no estimated-rate models to name")
        for model in listed:
            self.assertIn(model, note)

    def test_claude_cites_anthropic_and_never_openai(self):
        note = self._note("claude")
        self.assertIn("claude.com/pricing#api", note)
        self.assertNotIn("openai.com", note)

    def test_claude_still_lists_its_model_keywords(self):
        note = self._note("claude")
        for keyword in ("fable", "mythos", "opus", "sonnet", "haiku"):
            self.assertIn(keyword, note)


class TestTheLightPaletteExists(unittest.TestCase):
    """The palette is defined twice on purpose — see the comment in app.css.

    The `@media` copy serves a reader who has expressed no preference and needs no
    JavaScript, so an OS-light reader never sees a dark frame paint first. The
    attribute copy is the explicit choice and must win in both directions. Losing
    either one is a real regression that no visual check would catch, because
    each covers a case the other does not.
    """

    THEMED = ["--bg", "--card", "--border", "--text", "--muted", "--axis",
              "--accent", "--purple", "--raised", "--selected", "--scroll-track",
              "--shadow"]

    def _block(self, pattern):
        m = re.search(pattern, CSS, re.S)
        self.assertIsNotNone(m, "missing CSS block: " + pattern)
        return m.group(1)

    def test_the_os_default_needs_no_javascript(self):
        block = self._block(r"@media \(prefers-color-scheme: light\)\s*\{\s*"
                            r":root:not\(\[data-theme=\"dark\"\]\)\s*\{(.*?)\}")
        for name in self.THEMED:
            self.assertIn(name + ":", block, name + " missing from the OS-default palette")

    def test_the_explicit_choice_overrides_in_both_directions(self):
        block = self._block(r":root\[data-theme=\"light\"\]\s*\{(.*?)\}")
        for name in self.THEMED:
            self.assertIn(name + ":", block, name + " missing from the explicit palette")

    def test_the_dark_default_still_defines_every_token(self):
        block = self._block(r":root\s*\{(.*?)\}")
        for name in self.THEMED:
            self.assertIn(name + ":", block, name + " missing from the dark palette")

    def test_the_two_light_palettes_are_identical(self):
        """The cost of writing it twice is that the copies can drift, and drift
        here is invisible: you would only see it by loading the page on a light OS
        *and* on a dark one with the toggle pressed, and comparing. Pin them equal
        so an edit to one that misses the other fails here instead."""
        os_default = self._block(r"@media \(prefers-color-scheme: light\)\s*\{\s*"
                                 r":root:not\(\[data-theme=\"dark\"\]\)\s*\{(.*?)\}")
        explicit = self._block(r":root\[data-theme=\"light\"\]\s*\{(.*?)\}")
        def tokens(block):
            return dict(re.findall(r"(--[a-z-]+):\s*([^;]+);", block))
        self.assertEqual(tokens(os_default), tokens(explicit))

    def test_every_light_colour_is_readable_on_its_own_background(self):
        """A light theme is not done when it exists, only when it can be read.
        WCAG AA for normal text is 4.5:1; these are the foregrounds that carry
        small text, including 10-11px chart axis labels."""
        block = self._block(r":root\[data-theme=\"light\"\]\s*\{(.*?)\}")
        palette = dict(re.findall(r"(--[a-z-]+):\s*(#[0-9A-Fa-f]{6})", block))

        def luminance(value):
            channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                        for c in channels]
            return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

        def contrast(a, b):
            la, lb = luminance(a), luminance(b)
            return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

        for name in ("--text", "--muted", "--accent", "--blue", "--green",
                     "--red", "--purple"):
            with self.subTest(token=name):
                self.assertGreaterEqual(
                    round(contrast(palette[name], palette["--bg"]), 2), 4.5,
                    name + " is below WCAG AA on --bg")

    def test_no_tag_colour_is_a_literal_hex_the_theme_cannot_reach(self):
        """`.model-tag` names --blue, `.stop-tag` --green, `.stop-tag.truncated`
        --red — and `.effort-tag` alone carried a literal #9B7EC7, the dark
        palette's purple. It was the only foreground hex left in the stylesheet
        apart from two #fff-on---accent pairs, and a hex cannot follow a theme:
        on the light theme that chip rendered at 2.90:1 against its own tint
        while its three siblings sat at 4.70, 4.83 and 4.94."""
        rules = re.findall(r"([^{}]*-tag[^{}]*)\{([^}]*)\}", CSS)
        self.assertTrue(rules, "no tag rules found — the selector convention moved")
        for selector, body in rules:
            with self.subTest(selector=selector.strip().splitlines()[-1].strip()):
                self.assertNotRegex(body, r"color:\s*#[0-9A-Fa-f]{3,8}",
                                    "a tag colour written as a hex cannot follow "
                                    "the theme; name a palette token instead")
        self.assertIn("var(--purple)", CSS, "--purple is defined but nothing uses it")

    def test_the_scrollbars_follow_the_theme(self):
        """They were hardcoded hex, so a light page kept a dark gutter."""
        self.assertNotIn("scrollbar-color: #", CSS)
        self.assertIn("scrollbar-color: var(--scroll-thumb) var(--scroll-track)", CSS)


@requires_node
class TestThemePrecedence(unittest.TestCase):
    """Three states, not two: system (the default), explicit light, explicit dark."""

    def _drive(self, script):
        return run_js(emit("(() => {"
                           "  let store = {};"
                           "  globalThis.localStorage = { getItem: k => (k in store ? store[k] : null),"
                           "                              setItem: (k,v) => { store[k] = String(v); } };"
                           "  globalThis.__osLight = false;"
                           "  globalThis.matchMedia = q => ({ matches: globalThis.__osLight && q.includes('light'),"
                           "                                  addEventListener(){}, addListener(){} });"
                           "  const attrs = {};"
                           "  document.documentElement = { setAttribute: (k,v) => { attrs[k] = v; },"
                           "                               removeAttribute: k => { delete attrs[k]; } };"
                           + script +
                           "})()"))

    def test_with_no_choice_it_follows_the_operating_system(self):
        r = self._drive("const dark = activeTheme();"
                        "globalThis.__osLight = true;"
                        "return { dark, light: activeTheme() };")
        self.assertEqual(r["dark"], "dark")
        self.assertEqual(r["light"], "light")

    def test_an_explicit_choice_beats_the_operating_system_both_ways(self):
        r = self._drive("globalThis.__osLight = true; toggleTheme();"
                        "const darkOnLightOs = activeTheme();"
                        "globalThis.__osLight = false; toggleTheme();"
                        "return { darkOnLightOs, lightOnDarkOs: activeTheme() };")
        self.assertEqual(r["darkOnLightOs"], "dark")
        self.assertEqual(r["lightOnDarkOs"], "light")

    def test_the_choice_persists_and_survives_the_os_flipping(self):
        r = self._drive("globalThis.__osLight = true; toggleTheme();"
                        "globalThis.__osLight = false;"
                        "return { after: activeTheme(), stored: localStorage.getItem('codex-claude-usage-theme') };")
        self.assertEqual(r["after"], "dark")
        self.assertEqual(r["stored"], "dark")

    def test_choosing_sets_the_attribute_the_css_keys_on(self):
        """Writing `data-theme` is the ONLY thing that makes an explicit choice
        take effect — the CSS keys the explicit palette on
        `:root[data-theme="light"]` and the OS one on
        `:root:not([data-theme="dark"])` — so the write is what this asserts.

        It used to report `document.documentElement.setAttribute ? 'set' : 'missing'`,
        which is a property of the stub three lines above rather than of the
        product, and the `attrs` recorder was never read: deleting both attribute
        writes from applyTheme left the whole suite green. The clearing check is
        deliberately the LAST step of one sequence rather than its own test — on
        its own it passes trivially against an applyTheme that never writes.
        """
        r = self._drive("globalThis.__osLight = false; toggleTheme();"
                        "const light = attrs['data-theme'] || null;"
                        "const theme = activeTheme();"
                        "globalThis.__osLight = true; toggleTheme();"
                        "const dark = attrs['data-theme'] || null;"
                        "applyTheme(null);"
                        "return { light, dark, theme, cleared: !('data-theme' in attrs) };")
        self.assertEqual(r["theme"], "light")
        self.assertEqual(r["light"], "light",
                         "Light chosen on a dark OS never reached the DOM, so the "
                         "button relabels itself and nothing else changes")
        self.assertEqual(r["dark"], "dark",
                         "Dark chosen on a light OS never reached the DOM")
        self.assertTrue(r["cleared"],
                        "going back to following the OS must remove the attribute, "
                        "not leave the last explicit choice pinned")

    def test_changing_the_theme_re_reads_the_palette_and_repaints(self):
        """applyTheme's other two jobs, and neither is observable in the markup.

        The charts hold colours copied at construction time, so a theme change
        has to re-read them AND redraw. Dropping the re-read leaves the charts on
        the old palette; dropping the redraw leaves them painted for the theme
        the page has just left. Both mutations were silent before this."""
        r = self._drive("const calls = [];"
                        "syncChartColors = () => { calls.push('sync'); };"
                        "applyFilter = () => { calls.push('repaint'); };"
                        "rawData = { daily_by_model: [] };"
                        "toggleTheme();"
                        "return { calls };")
        self.assertEqual(r["calls"], ["sync", "repaint"])

    def test_the_button_offers_the_other_theme_rather_than_naming_this_one(self):
        """A control labelled with the current state reads as a status display and
        gets ignored; it has to say what pressing it will do."""
        r = self._drive("const btn = { textContent: '', title: '' };"
                        "document.getElementById = id => (id === 'theme-btn' ? btn : null);"
                        "globalThis.__osLight = false; renderThemeButton();"
                        "const onDark = btn.textContent;"
                        "globalThis.__osLight = true; renderThemeButton();"
                        "return { onDark, onLight: btn.textContent };")
        self.assertIn("Light", r["onDark"], "on a dark theme the button must offer Light")
        self.assertIn("Dark", r["onLight"], "on a light theme the button must offer Dark")

    def test_storage_being_unavailable_is_not_fatal(self):
        """Private-mode browsers throw on localStorage access. The theme is a
        convenience; losing it must not take the page down with it."""
        r = self._drive("globalThis.localStorage = { getItem(){ throw new Error('denied'); },"
                        "                            setItem(){ throw new Error('denied'); } };"
                        "return { theme: activeTheme(), toggled: (toggleTheme(), true) };")
        self.assertIn(r["theme"], ("light", "dark"))
        self.assertTrue(r["toggled"])


@requires_node
class TestChartsFollowTheTheme(unittest.TestCase):
    def test_the_chart_palette_is_read_from_css_not_hand_copied(self):
        """`C` used to be a hand-maintained copy of the CSS variables, with a
        comment saying so. A second palette would have had to be copied twice and
        kept in step by memory."""
        r = run_js(emit("(() => {"
                        "  document.documentElement = {};"
                        "  globalThis.getComputedStyle = () => ({"
                        "    getPropertyValue: name => ({ '--text': '#111111', '--axis': '#222222',"
                        "                                 '--border': '#333333', '--card': '#ffffff' })[name] || '' });"
                        "  syncChartColors();"
                        "  return { text: C.text, axis: C.axis, border: C.border, card: C.card };"
                        "})()"))
        self.assertEqual(r, {"text": "#111111", "axis": "#222222",
                             "border": "#333333", "card": "#ffffff"})

    def test_a_missing_variable_keeps_the_compiled_in_default(self):
        """An empty read means no such variable (or a harness with no CSS).
        Writing it through would hand Chart.js an empty colour, which renders as
        transparent — an invisible axis rather than an obvious error."""
        r = run_js(emit("(() => {"
                        "  document.documentElement = {};"
                        "  globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });"
                        "  const before = C.text;"
                        "  syncChartColors();"
                        "  return { before, after: C.text };"
                        "})()"))
        self.assertEqual(r["before"], r["after"])
        self.assertTrue(r["after"])


@requires_node
class TestASourceSwitchNeverStrandsTheOverlayOrThePayload(unittest.TestCase):
    """Two fetches, one screen. The response that comes back last must not win.

    Each source is fetched on entry, so pressing the other one while a fetch is
    still in flight leaves an abandoned response on the way. It arrives holding a
    complete, valid payload — for the source nobody is looking at any more. The
    guard that drops it used to sit *below* the assignments it was guarding, so
    the abandoned payload had already replaced `rawData` and rebuilt the model
    filter by the time it was rejected, and the `return` then skipped the
    `clearLoading()` on the line under it.
    """

    # A switch back to an already-fetched source takes loadSource's cached branch,
    # which does no network work at all — so it is the one place that has to take
    # down an overlay it did not raise, and restore the header line showLoading
    # overwrote.
    STUBS = """
      const overlay = { hidden: true };
      const container = { _c: new Set(), classList: {
        add(c){ container._c.add(c); }, remove(c){ container._c.delete(c); },
        contains(c){ return container._c.has(c); } },
        setAttribute(k,v){ container[k] = String(v); },
        removeAttribute(k){ delete container[k]; } };
      // textContent and innerHTML are one buffer, as they are on a real node:
      // showLoading writes one and updateMetaNote the other.
      const meta = { _t: '', get textContent(){ return this._t; },
                     set textContent(v){ this._t = String(v); },
                     get innerHTML(){ return this._t; },
                     set innerHTML(v){ this._t = String(v); } };
      const els = { 'load-overlay': overlay, 'load-text': { textContent: '' }, 'meta': meta };
      const inert = document.getElementById;
      document.getElementById = id => (id in els ? els[id] : inert(id));
      document.querySelector = s => (s === '.container' ? container : inert(s));

      // The race is in loadData's ordering, not in what it renders.
      applyFilter = () => {};
      buildFilterUI = () => {};
      mergeNewlySeenModels = () => {};
      renderSourceSwitch = () => {};
      updateURL = () => {};

      const payload = (src) => ({ generated_at: src + '-generated', from: src,
                                  all_models: [], daily_by_model: [], sessions_all: [] });
      // Every fetch parks until the driver releases it by name, so the two
      // responses can be made to land in either order. `bodies` lets a sequence
      // answer one source with something other than a payload.
      const gate = {};
      const bodies = {};
      apiFetch = async (path) => {
        const src = /source=([a-z]+)/.exec(path)[1];
        await new Promise(go => { gate[src] = go; });
        return { ok: true, status: 200, json: async () => (bodies[src] || payload(src)) };
      };
      const settle = () => new Promise(r => setTimeout(r, 0));

      const state = (tag) => ({ tag,
        overlayHidden: overlay.hidden,
        dimmed: container.classList.contains('loading'),
        selectedSource,
        rawFrom: rawData && rawData.from,
        filterBuiltFor,
        cached: [...loadedSources.keys()].sort(),
        metaText: meta.textContent,
        metaSaysLoading: meta.textContent.indexOf('Loading') !== -1 });

      availableSources = ['claude', 'codex'];
      selectedSource = 'claude';
      const steps = [];
    """

    # The sequence the class is named for: the reader walks away from a fetch,
    # and it lands anyway.
    SEQUENCE = """
      const first = loadSource('claude');
      await settle(); gate.claude(); await first;
      steps.push(state('claude on screen'));

      const switched = setSource('codex');          // uncached: overlay goes up
      await settle();
      steps.push(state('codex fetch in flight'));

      await setSource('claude');                    // cached: the reader stops waiting
      steps.push(state('switched back to the cached claude'));

      gate.codex(); await switched;                 // the abandoned response lands
      steps.push(state('the stale codex payload landed'));
    """

    def _run(self, sequence=None):
        return run_js("(async () => {" + self.STUBS + (sequence or self.SEQUENCE)
                      + "console.log(JSON.stringify(steps)); })();")

    def test_the_abandoned_payload_does_not_become_the_page(self):
        landed = self._run()[-1]
        self.assertEqual(landed["selectedSource"], "claude")
        self.assertEqual(landed["rawFrom"], "claude",
                         "the abandoned Codex response replaced the payload the "
                         "reader is looking at")
        self.assertEqual(landed["filterBuiltFor"], "claude",
                         "the model filter was rebuilt from the models of the "
                         "source that is no longer on screen")

    def test_the_abandoned_payload_is_still_cached_for_a_later_switch(self):
        """Dropping it is a rendering decision, not a fetching one — it was
        fetched, it is correct for its own source, and a switch to that source
        must not have to ask for it again."""
        self.assertEqual(self._run()[-1]["cached"], ["claude", "codex"])

    def test_switching_back_to_a_loaded_source_takes_the_overlay_down(self):
        back = self._run()[2]
        self.assertTrue(back["overlayHidden"],
                        "the overlay raised for the fetch the reader walked away "
                        "from is never lowered")
        self.assertFalse(back["dimmed"])
        self.assertFalse(back["metaSaysLoading"],
                         "the header still reads 'Loading Codex usage…' over "
                         "Claude's data with nothing in flight")

    def test_the_overlay_is_still_up_while_the_fetch_it_belongs_to_runs(self):
        """The other half of the same rule: it comes down when the reader stops
        waiting, not merely whenever anything finishes."""
        inflight = self._run()[1]
        self.assertFalse(inflight["overlayHidden"])
        self.assertTrue(inflight["dimmed"])

    def test_the_cached_branch_dates_the_payload_it_is_actually_showing(self):
        """"Updated: …" is a claim about what is on screen. The cached branch has
        to restore that line because showLoading wrote over it, and the value it
        restores is the freshness of the payload it just put back — not whichever
        source happened to render last. Both are in `loadedSources`, each with its
        own `generated_at`."""
        steps = self._run("""
          const a = loadSource('claude');
          await settle(); gate.claude(); await a;
          const b = setSource('codex');
          await settle(); gate.codex(); await b;
          steps.push(state('codex rendered, so it dated the header'));
          await setSource('claude');            // cached: no fetch at all
          steps.push(state('back on the cached claude'));
        """)
        dated = [s["metaText"].split("<br>")[0] for s in steps]
        self.assertEqual(dated[0], "Updated: codex-generated")
        self.assertEqual(dated[1], "Updated: claude-generated",
                         "the header dated Claude's data with the moment Codex's "
                         "payload was built")

    def test_a_stale_error_response_does_not_take_over_the_visible_source(self):
        """The `d.error` branch is the same race one level down. It writes the
        header and takes the overlay down, and it did both for whichever response
        arrived — so a "no database yet" answer for a source the reader had
        already left cleared the overlay belonging to the fetch they were waiting
        for, wrote its retry notice over the top, and (on a first load, which is
        when that error happens) re-armed itself every three seconds."""
        steps = self._run("""
          bodies.claude = { error: 'no database yet' };
          const a = loadSource('claude');       // first load: rawData is null
          await settle();
          const b = setSource('codex');         // uncached: its own overlay goes up
          await settle();
          gate.claude(); await a;               // the abandoned answer is an error
          steps.push(state('the stale error landed'));
        """)
        landed = steps[-1]
        self.assertFalse(landed["overlayHidden"],
                         "the overlay for the Codex fetch still in flight was "
                         "taken down by the other source's error")
        self.assertTrue(landed["dimmed"])
        self.assertNotIn("no database yet", landed["metaText"],
                         "the abandoned source's error was written over the "
                         "header of the source on screen")

    def test_a_stale_network_error_does_not_clear_the_visible_source_overlay(self):
        steps = self._run("""
          apiFetch = async (path) => {
            const src = /source=([a-z]+)/.exec(path)[1];
            await new Promise(go => { gate[src] = go; });
            if (src === 'claude') throw new Error('old request disconnected');
            return { ok: true, status: 200, json: async () => payload(src) };
          };
          const a = loadSource('claude');
          await settle();
          const b = setSource('codex');
          await settle();
          gate.claude(); await a;
          steps.push(state('the stale network error landed'));
        """)
        landed = steps[-1]
        self.assertFalse(landed["overlayHidden"],
                         "an abandoned request cleared the overlay owned by "
                         "the visible source")
        self.assertTrue(landed["dimmed"])
        self.assertNotIn("old request", landed["metaText"])

    def test_the_newest_request_wins_when_the_same_source_loads_twice(self):
        steps = self._run("""
          const releases = [];
          apiFetch = () => new Promise(resolve => {
            releases.push(marker => resolve({ ok: true, status: 200,
              json: async () => Object.assign(payload('claude'), {
                from: marker, generated_at: marker + '-generated',
              }) }));
          });
          const old = loadData('claude');
          await settle();
          const newest = loadData('claude');
          await settle();
          releases[1]('new'); await newest;
          steps.push(state('new response rendered'));
          releases[0]('old'); await old;
          steps.push(state('old response landed last'));
        """)
        self.assertEqual(steps[0]["rawFrom"], "new")
        self.assertEqual(steps[-1]["rawFrom"], "new",
                         "an older same-source response replaced the newest one")
        self.assertEqual(steps[-1]["metaText"].split("<br>")[0],
                         "Updated: new-generated")

    def test_a_source_error_keeps_the_previous_sources_figures_obscured(self):
        steps = self._run("""
          const nativeTimeout = globalThis.setTimeout;
          const retryDelays = [];
          globalThis.setTimeout = (fn, ms) => {
            if (ms === 0) return nativeTimeout(fn, ms);
            retryDelays.push(ms); return 1;
          };
          const a = loadSource('claude');
          await settle(); gate.claude(); await a;
          bodies.codex = { error: 'codex database unavailable' };
          const b = setSource('codex');
          await settle(); gate.codex(); await b;
          steps.push(Object.assign(state('codex failed'), { retryDelays }));
        """)
        failed = steps[-1]
        self.assertEqual(failed["selectedSource"], "codex")
        self.assertEqual(failed["rawFrom"], "claude",
                         "the control fixture must still hold Claude's payload")
        self.assertFalse(failed["overlayHidden"],
                         "Claude's figures were revealed under the Codex title")
        self.assertTrue(failed["dimmed"])
        self.assertIn(3000, failed["retryDelays"])


@requires_node
class TestTheQuotaPanelStatesTheAgeOfTheReadingItIsShowing(unittest.TestCase):
    """"As of 2 min ago" is a claim about the wire, not about the last repaint.

    The reading is stamped with its arrival time and the panel counts from that
    stamp. Stamping it in the *renderer* instead meant every range button, model
    toggle and table sort restamped it — `applyFilter` re-renders the panel from
    the same object — so a reading hours old went back to reporting the age the
    server had computed for it when the page first loaded. Codex is the worst
    case: its quota arrives only with /api/data, and auto-refresh is off by
    default, so that payload is never fetched again.
    """

    PAYLOAD = "{\"sources\": [{\"source\": \"claude\", \"turns\": 1}, {\"source\": \"codex\", \"turns\": 1}], \"all_models\": [\"claude-opus-5\", \"gpt-5.6-sol\"], \"generated_at\": \"x\", \"daily_by_model\": [{\"day\": \"2026-08-05\", \"source\": \"claude\", \"model\": \"claude-opus-5\", \"input\": 100, \"output\": 10, \"cache_read\": 0, \"cache_creation\": 0, \"cache_creation_1h\": 0, \"turns\": 1}, {\"day\": \"2026-08-05\", \"source\": \"codex\", \"model\": \"gpt-5.6-sol\", \"input\": 700, \"output\": 70, \"cache_read\": 900, \"cache_creation\": 0, \"cache_creation_1h\": 0, \"turns\": 1}], \"hourly_by_model\": [], \"sessions_all\": [], \"top_dispatches\": [], \"subagent_by_type\": [], \"project_by_day_model\": [], \"limit_incidents\": [], \"subscription_limits\": {\"available\": true, \"plan_type\": \"max\", \"age_seconds\": 60, \"windows\": [{\"kind\": \"5h\", \"group\": \"5h\", \"percent\": 40, \"severity\": \"normal\", \"resets_at\": \"2099-01-01T00:00:00Z\", \"scope\": \"\", \"is_active\": true, \"expired\": false}]}, \"codex_limits\": {\"available\": true, \"source\": \"codex\", \"plan_type\": \"pro\", \"age_seconds\": 30, \"windows\": [{\"kind\": \"10080m\", \"group\": \"10080m\", \"percent\": 83, \"severity\": \"normal\", \"resets_at\": \"2099-01-01T00:00:00Z\", \"scope\": \"\", \"is_active\": true, \"expired\": false}]}}"

    # A clock the driver moves by hand, and a renderer that records the age the
    # panel would print. Everything else is the real page.
    PRELUDE = """
      let now = 1786000000000;
      Date.now = () => now;
      const ages = [];
      renderPlanLimits = (info) => { ages.push(planSampleAge(info)); };
      renderStats = () => {};
      scheduleAutoRefresh = () => {}; startPlanLimitsPoll = () => {};
      globalThis.history = { replaceState: () => {} };
      const payload = """ + PAYLOAD + """;
      const scoped = (src) => Object.assign({}, payload, {
        daily_by_model: payload.daily_by_model.filter(r => r.source === src),
        all_models: payload.daily_by_model.filter(r => r.source === src).map(r => r.model),
      });
      apiFetch = async (path) => {
        if (path === '/api/sources') return { ok: true, json: async () => ({ sources: payload.sources }) };
        if (path === '/api/limits') return { ok: true, json: async () =>
          ({ available: true, plan_type: 'max', age_seconds: 60,
             windows: payload.subscription_limits.windows }) };
        const src = /source=([a-z]+)/.exec(path)[1];
        return { ok: true, status: 200, json: async () => scoped(src) };
      };
      const HOUR = 3600 * 1000;
    """

    def _drive(self, script):
        return run_js("(async () => {" + self.PRELUDE + script + "})();")

    def test_a_filter_click_does_not_make_an_old_reading_look_new(self):
        got = self._drive("""
          availableSources = ['claude']; selectedSource = 'claude';
          await loadData();
          const onArrival = ages[ages.length - 1];
          now += 3 * HOUR;
          applyFilter();                      // any client-side interaction
          console.log(JSON.stringify({ onArrival, afterAClick: ages[ages.length - 1] }));
        """)
        self.assertEqual(got["onArrival"], 60, "the server's own age, on arrival")
        self.assertEqual(got["afterAClick"], 60 + 3 * 3600,
                         "clicking a filter restamped the reading as freshly received")

    def test_a_reading_parked_while_the_other_source_is_up_keeps_its_true_age(self):
        """/api/limits reports Claude's quota, so a poll that lands while Codex is
        on screen is kept rather than applied. It is still a reading that arrived
        *then*, and the switch that finally shows it — minutes or hours later, off
        the cached payload with no network at all — must not re-date it."""
        got = self._drive("""
          availableSources = ['claude', 'codex']; selectedSource = 'claude';
          await loadSource('claude');
          await setSource('codex');
          await refreshPlanLimits();          // parked in lastClaudeLimits, not shown
          now += 3 * HOUR;
          await setSource('claude');          // cached: no fetch, straight to render
          console.log(JSON.stringify({ shown: ages[ages.length - 1] }));
        """)
        self.assertEqual(got["shown"], 60 + 3 * 3600,
                         "the parked reading was stamped when it was finally "
                         "rendered, three hours after it arrived")

    def test_a_genuinely_new_reading_still_reports_its_own_server_side_age(self):
        """The stamp must not freeze the panel either: a fresh poll is a fresh
        object and starts counting from its own arrival."""
        got = self._drive("""
          availableSources = ['claude']; selectedSource = 'claude';
          await loadData();
          now += 3 * HOUR;
          await refreshPlanLimits();
          console.log(JSON.stringify({ afterAPoll: ages[ages.length - 1] }));
        """)
        self.assertEqual(got["afterAPoll"], 60)


@requires_node
class TestTheTitleNamesTheAssistantItIsShowing(unittest.TestCase):
    """The project brand must always retain the selected assistant's scope.

    This includes single-source installs and startup before data arrives. The
    fixed brand names both assistants, so exact source suffixes are essential:
    checking only for the word "Codex" would pass on a Claude-selected view.
    """

    PROBE = """
      const seen = {};
      const heading = { dataset: {}, title: '',
        classList: { toggle: (c, on) => { seen.switchable = on; } },
        setAttribute: (k, v) => { seen[k] = v; },
        removeAttribute: (k) => { delete seen[k]; },
        set textContent(v) { seen.h1 = v; } };
      document.getElementById = () => heading;
    """

    def _title(self, sources, selected):
        return run_js(emit("(() => {" + self.PROBE
                           + "availableSources = " + repr(list(sources)).replace("'", '"') + ";"
                           + "selectedSource = " + '"' + selected + '"' + ";"
                           "renderSourceTitle();"
                           "return { h1: seen.h1, doc: document.title,"
                           "         switchable: !!seen.switchable,"
                           "         role: seen.role || null,"
                           "         switchTo: heading.dataset.switchTo || null }; })()"))

    def test_a_codex_only_machine_names_codex_as_the_selected_assistant(self):
        got = self._title(["codex"], "codex")
        self.assertEqual(got["h1"], "Codex / Claude Usage · Codex")
        self.assertEqual(got["doc"], "Codex / Claude Usage Dashboard — Codex",
                         "the browser tab names the vendor too")

    def test_a_claude_only_machine_names_claude_code_as_the_selected_assistant(self):
        got = self._title(["claude"], "claude")
        self.assertEqual(got["h1"], "Codex / Claude Usage · Claude Code")
        self.assertEqual(got["doc"], "Codex / Claude Usage Dashboard — Claude Code")

    def test_a_single_source_title_is_still_not_a_control(self):
        """Naming the source is not the same as offering a choice: with one
        assistant there is nothing to switch to, so the heading keeps no role, no
        tab stop and no target."""
        got = self._title(["codex"], "codex")
        self.assertFalse(got["switchable"])
        self.assertIsNone(got["role"])
        self.assertIsNone(got["switchTo"])

    def test_two_sources_still_name_the_selected_one_and_offer_the_other(self):
        got = self._title(["claude", "codex"], "codex")
        self.assertEqual(got["h1"], "Codex / Claude Usage · Codex")
        self.assertTrue(got["switchable"])
        self.assertEqual(got["switchTo"], "claude")

    def test_the_loading_window_already_names_the_right_assistant(self):
        """`start()` renders the chrome before it knows which source it is about
        to load — it has to, because the two-source path shows the chooser from
        there — so the heading and the footer spent the whole multi-second first
        fetch naming Claude on a Codex-only machine. That is exactly the window
        the overlay exists for, not a flicker."""
        got = run_js(
            "(async () => {\n"
            "  const seen = {};\n"
            "  const heading = { dataset: {}, title: '',\n"
            "    classList: { toggle: () => {} },\n"
            "    setAttribute: () => {}, removeAttribute: () => {},\n"
            "    set textContent(v) { seen.h1 = v; } };\n"
            "  const footer = { set innerHTML(v) { seen.footer = v; } };\n"
            "  const loadText = { textContent: '' };\n"
            "  const inert = document.getElementById;\n"
            "  const els = { 'app-title': heading, 'footer-pricing': footer,\n"
            "                'load-text': loadText };\n"
            "  document.getElementById = id => (id in els ? els[id] : inert(id));\n"
            "  globalThis.history = { replaceState: () => {} };\n"
            "  apiFetch = async (path) => {\n"
            "    if (path === '/api/scan-status') return { ok: true, status: 200,\n"
            "      json: async () => ({ state: 'idle', generation: 0 }) };\n"
            "    if (path === '/api/sources') return { ok: true, status: 200,\n"
            "      json: async () => ({ sources: [{ source: 'codex', turns: 5 }] }) };\n"
            "    await new Promise(() => {});      // the payload never lands\n"
            "  };\n"
            "  start();\n"
            "  await new Promise(r => setTimeout(r, 0));\n"
            "  console.log(JSON.stringify({ h1: seen.h1, footer: seen.footer,\n"
            "                               loading: loadText.textContent }));\n"
            "})();")
        self.assertIn("Codex", got["loading"], "the overlay named the source already")
        self.assertEqual(got["h1"], "Codex / Claude Usage · Codex",
                         "the heading spent the whole fetch naming the other vendor")
        self.assertIn("openai.com", got["footer"])
        self.assertNotIn("claude.com", got["footer"])


# The three filter-bar children that belong to the source switch and are only on
# screen together. Shared by the markup tests and the node test below, which is
# what makes the pair airtight: one proves the layout is right for this exact
# group, the other proves `renderSourceSwitch` toggles this exact group.
SOURCE_FILTER_GROUP = ("source-filter-label", "source-switch", "source-filter-sep")


class TestTheSourceFilterIsOnScreenWhole(unittest.TestCase):
    """The SOURCE heading and its separator belong to the switch, not to the bar.

    `renderSourceSwitch` hid only the control, so the common install — one
    assistant — opened with a heading labelling a control that is not there. At
    desktop widths that is 50px of stray grey text and a divider. Below 640px,
    where `#filter-bar` becomes the two-column grid whose whole purpose is that
    "a two-column grid keeps each pair together", the orphan label offsets every
    remaining cell by one. Measured in headless Chrome against the unfixed tree,
    one source at 390px: `cols=170px 180px`, Source|Models, model-select|Range,
    range-select|Refresh, refresh-select|— . The whole bar mis-paired.

    The three stay FLAT siblings rather than being gathered into a wrapper for
    exactly that reason: a wrapper is ONE grid item, which moves the identical
    mis-pairing onto the machine that has both assistants (measured, two sources
    at 390px: `cols=216px 134px`, source-group|Models, model-select|Range, …).
    """

    def _grid_items(self, showing_source):
        """The bar's grid items below 640px, in DOM order.

        `#filter-bar .filter-sep { display: none }` there, so a separator is
        never a grid item; every other child that is not `hidden` is one.
        """
        return [child for child in direct_children("filter-bar")
                if "filter-sep" not in child["classes"]
                and (showing_source or child["id"] not in SOURCE_FILTER_GROUP)]

    def test_the_grid_pairs_every_label_with_its_control_in_both_states(self):
        for showing in (False, True):
            with self.subTest(sources=2 if showing else 1):
                items = self._grid_items(showing)
                self.assertTrue(items)
                self.assertEqual(len(items) % 2, 0,
                                 "an odd number of grid items leaves one cell "
                                 "unpaired: " + repr([c["id"] or c["classes"]
                                                      for c in items]))
                for index, child in enumerate(items):
                    what = child["id"] or " ".join(child["classes"])
                    with self.subTest(cell=index, element=what):
                        self.assertEqual("filter-label" in child["classes"],
                                         index % 2 == 0,
                                         what + " lands in column "
                                         + str(index % 2 + 1) + ", so the bar's "
                                         "labels and controls are off by a cell")

    def test_the_three_source_elements_are_addressable(self):
        """The fix hides them individually, so each needs a stable id."""
        ids = {child["id"] for child in direct_children("filter-bar")}
        for element_id in SOURCE_FILTER_GROUP:
            self.assertIn(element_id, ids)

    def test_they_start_hidden_together(self):
        """`renderSourceSwitch` only runs once `/api/sources` has answered, and
        the filter bar is on screen behind the loading overlay until then."""
        hidden = {child["id"] for child in direct_children("filter-bar")
                  if child["id"] in SOURCE_FILTER_GROUP and child["hidden"]}
        self.assertEqual(hidden, set(SOURCE_FILTER_GROUP),
                         "these are on screen before anything knows whether "
                         "there is a second source to switch to")

    def test_nothing_else_in_the_bar_is_hidden(self):
        """`_grid_items` models every other child as always on screen; a new
        `hidden` one would make its pairing assertion quietly wrong."""
        for child in direct_children("filter-bar"):
            if child["id"] not in SOURCE_FILTER_GROUP:
                self.assertFalse(child["hidden"], repr(child))

    def test_the_phone_layout_this_models_is_still_the_one_in_the_stylesheet(self):
        phone = re.search(r"@media \(max-width: 640px\) \{(.*?)\n  \}", CSS, re.S)
        self.assertIsNotNone(phone, "no 640px block to model")
        self.assertRegex(phone.group(1),
                         r"#filter-bar \{[^}]*display: grid;[^}]*"
                         r"grid-template-columns: auto minmax\(0, 1fr\)")
        self.assertRegex(phone.group(1), r"#filter-bar \.filter-sep \{ display: none")

    def test_the_hidden_attribute_alone_still_takes_them_off_the_grid(self):
        """No CSS backs this fix, and that is only safe while no author rule
        gives these elements a `display`: an author declaration beats the UA
        `[hidden] { display: none }` however specific. That is precisely why
        `.source-switch[hidden]` has to be spelled out — `.source-switch` does
        set `display`. Adding `display: inline-block` to `.filter-label` would
        put the orphan heading back with nothing else changing.
        """
        for token in (".filter-label", ".filter-sep"):
            pattern = re.compile(r"(?m)^[ \t]*([^{}@\n]*" + re.escape(token)
                                 + r"\b[^{}\n]*)\{([^{}]*)\}")
            found = list(pattern.finditer(CSS))
            self.assertTrue(found, "no rule for " + token)
            for rule in found:
                for value in re.findall(r"display:\s*([a-z-]+)", rule.group(2)):
                    self.assertEqual(value, "none",
                                     rule.group(1).strip() + " sets display: "
                                     + value + ", which overrides [hidden]")


@requires_node
class TestTheSourceSwitchTakesItsHeadingWithIt(unittest.TestCase):
    """The other half: `renderSourceSwitch` toggles all three, not just the one."""

    def _flags(self, sources):
        return run_js(emit(
            "(() => {"
            "  const flags = {};"
            "  const inert = document.getElementById;"
            "  const els = {};"
            "  for (const id of " + json.dumps(list(SOURCE_FILTER_GROUP)) + ")"
            "    els[id] = { set hidden(v) { flags[id] = v; }, set innerHTML(v) {} };"
            "  document.getElementById = id => (id in els ? els[id] : inert(id));"
            "  availableSources = " + json.dumps(sources) + ";"
            "  selectedSource = " + json.dumps(sources[0]) + ";"
            "  renderSourceSwitch();"
            "  return flags; })()"))

    def test_one_assistant_hides_the_heading_and_the_separator_too(self):
        self.assertEqual(self._flags(["claude"]),
                         {"source-filter-label": True, "source-switch": True,
                          "source-filter-sep": True})

    def test_two_assistants_put_all_three_back(self):
        self.assertEqual(self._flags(["claude", "codex"]),
                         {"source-filter-label": False, "source-switch": False,
                          "source-filter-sep": False})


class TestTheStylesheetDoesNotContradictTheDeliveryItStyles(unittest.TestCase):
    """`.quota-alert`'s comment was the last copy of a claim the code never made.

    It read "The in-page fallback, for when the browser will not show a
    notification". `deliverAlert` shows the banner for EVERY alert and explains
    at length why: `Notification.permission === 'granted'` says the user once
    clicked Allow, not that the toast was seen — Do Not Disturb, a backgrounded
    tab and `tag` collapsing a repeat all swallow it silently. The JS comment and
    the README were corrected when that was established; the stylesheet was not,
    which left it the only place in the shipped source still stating the old
    either/or — read by exactly the person about to gate the banner on
    `Notification.permission` and undo the fix.

    Deliberately scoped to this one comment: "fallback" is ordinary CSS
    vocabulary (font and colour fallbacks), so banning it across the stylesheet
    would be a brittle guard over a cosmetic surface.
    """

    CONDITIONAL_WORDING = ("fallback", "fall back", "falls back", "otherwise",
                           "when not", "will not show")

    def _comment(self):
        anchor = CSS.index("  .quota-alert {")
        before = CSS[:anchor]
        end = before.rindex("*/")
        self.assertEqual(before[end + 2:].strip(), "",
                         ".quota-alert no longer carries a comment of its own")
        return before[before.rindex("/*", 0, end):end + 2].lower()

    def test_it_does_not_make_the_banner_conditional(self):
        comment = self._comment()
        for wording in self.CONDITIONAL_WORDING:
            with self.subTest(wording=wording):
                self.assertNotIn(wording, comment,
                                 "the stylesheet makes the banner conditional "
                                 "on the notification, which deliverAlert has "
                                 "never done")

    def test_it_still_says_what_the_banner_guarantees(self):
        """The other half — a comment that simply stopped mentioning the
        guarantee would satisfy the check above while telling the next reader
        nothing. It names its authority rather than restating the contract,
        which already lives in the JS comment, the README and the CHANGELOG."""
        comment = self._comment()
        self.assertIn("every", comment)
        self.assertIn("deliveralert", comment,
                      "the comment must point at the function that decides this")


@requires_node
class TestNoNumberFollowsTheViewersLocale(unittest.TestCase):
    """web/js/20-format.js pins every formatter to en-US, and says why: on the
    browser locale a de-DE reader saw "$1.500,0000" in a table, "$1500.00" on the
    axis beside it and "1500.0000" in the export of the same figure.

    The rule was enforced on the formatters and nowhere else, so the one tile
    that formats its own number — Sessions, which must print the exact count
    rather than fmt()'s abbreviated form — went on following the browser. The
    guard that existed asserted fmtCost and fmtCostBig in isolation, which is
    neither the call site that broke nor the formatters that could break next.
    """

    # The exact count, not fmt()'s "1.23M": that is why the tile does not go
    # through fmt(), and it is the property that must survive any fix.
    RENDER = """
      const captured = {};
      document.getElementById = (id) => ({ set innerHTML(v) { captured[id] = v; },
                                           closest: () => null });
      renderStats({ sessions: 1234567, turns: 1234567, input: 1234567, output: 1234567,
                    cache_read: 0, cache_creation: 0, subagent_tokens: 0,
                    cost: 1500, billable: true }, 'All Time');
      const values = [...captured['stats-row'].matchAll(/class="value"[^>]*>([^<]*)</g)]
                       .map(m => m[1]);
    """

    def test_the_sessions_tile_is_grouped_the_same_way_as_the_tile_beside_it(self):
        """Driven by overriding the DEFAULT locale rather than by LANG, because
        node on Windows ignores LANG and the guard would pass vacuously there.
        The page's own formatters name 'en-US' at construction, so they are
        untouched by this — exactly as they would be in a German browser."""
        for locale in ("de-DE", "ar-EG"):
            with self.subTest(locale=locale):
                got = run_js(emit("(() => {"
                                  "  const Base = Intl.NumberFormat;"
                                  "  Number.prototype.toLocaleString = function (l, o) {"
                                  "    return new Base(l || " + repr(locale).replace("'", '"')
                                  + ", o).format(this); };"
                                  + self.RENDER +
                                  "  return { sessions: values[0], cost: values[values.length - 1],"
                                  "           control: (1234567).toLocaleString() };"
                                  "})()"))
                self.assertNotEqual(got["control"], "1,234,567",
                                    "the harness did not change the default locale, so "
                                    "this test would pass without formatting anything")
                self.assertEqual(got["sessions"], "1,234,567")
                self.assertEqual(got["cost"], "$1,500.00",
                                 "the tile beside it, for comparison")

    def test_no_formatter_silently_loses_its_pinned_locale(self):
        """The formatters are built from `new Intl.NumberFormat('en-US')` at load,
        so dropping the argument can only be caught by running the whole harness
        under another locale — a re-run in a subprocess, the way the money guard
        in test_remaining_findings.py does it. The rendered tile is checked here
        too: the defect this file was extended for was a call site that bypassed
        the formatters entirely, which formatter-level assertions cannot see."""
        from tests.test_dashboard_js import NODE, _DOM_STUB, extract_app_script
        probe = emit("(() => {" + self.RENDER +
                     "  return { sessions: values[0], fmt: fmt(999.5),"
                     "           rate: fmtRate(0.125), pct: fmtPct(1234.5, 10000),"
                     "           cost: fmtCost(1500.5), big: fmtCostBig(1500.5) };"
                     "})()")
        source = _DOM_STUB + "\n" + extract_app_script() + "\n" + probe
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.cjs"
            harness.write_text(source, encoding="utf-8")
            env = dict(os.environ, LANG="de_DE.UTF-8", LC_ALL="de_DE.UTF-8")
            # node writes UTF-8 whatever LC_ALL says; without encoding= the
            # parent would decode with locale.getencoding() — cp1252 on
            # windows-latest — which is how an en dash became mojibake.
            proc = subprocess.run([NODE, str(harness)], capture_output=True,
                                  text=True, encoding="utf-8", timeout=120,
                                  env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        got = json.loads(proc.stdout)
        self.assertEqual(got, {"sessions": "1,234,567", "fmt": "999.5",
                               "rate": "$0.125/M", "pct": "12.3%",
                               "cost": "$1,500.5000", "big": "$1,500.50"})


@requires_node
class TestARejectedTokenStopsInsteadOfRetryingForever(unittest.TestCase):
    """The notice's own neighbouring comment says a rejected token "is not a
    transient failure — retrying it every three seconds forever cannot succeed".

    The screen said so and then did exactly that: `showAuthNotice` is re-entered
    from `loadData`, `autoRefreshTick`, `start` and the bootstrap, and it left
    both interval timers armed — so the tab went on POSTing /api/rescan and
    GETting /api/limits at a server that answers 403, for as long as it was open
    — while attaching one more `hashchange` closure on every pass.
    """

    def _drive(self, script, notice="null"):
        return run_js(emit("(() => {"
                           # addEventListener dedupes on (type, callback), so a
                           # stub that stores by type alone cannot see the leak.
                           "  const live = [];"
                           "  window.addEventListener = (ev, fn) => {"
                           "    if (!live.some(l => l.ev === ev && l.fn === fn)) live.push({ ev, fn });"
                           "  };"
                           "  const armed = new Map();"
                           "  let nextId = 0;"
                           "  globalThis.setInterval = (fn, ms) => { armed.set(++nextId, ms); return nextId; };"
                           "  globalThis.clearInterval = (id) => { armed.delete(id); };"
                           "  const inert = document.getElementById;"
                           "  const els = { 'auth-notice': " + notice + " };"
                           "  document.getElementById = id => (id in els ? els[id] : inert(id));"
                           + script +
                           "})()"))

    def test_the_hashchange_watcher_is_registered_once_however_often_it_re_renders(self):
        """Pasting an authenticated URL into this tab is a same-document
        navigation, so the watcher is what makes the page recover — but one per
        403 is 240 an hour at the 15s refresh setting."""
        r = self._drive("for (let i = 0; i < 5; i++) showAuthNotice();"
                        "return { hashchange: live.filter(l => l.ev === 'hashchange').length };",
                        notice="{ innerHTML: '', hidden: true }")
        self.assertEqual(r["hashchange"], 1)

    def test_the_polls_that_can_only_be_refused_are_stopped(self):
        """Both are cleared above the `if (!notice) return`, so stopping them does
        not depend on the notice element being in the document."""
        r = self._drive("refreshSeconds = 15; selectedRange = 'today';"
                        "scheduleAutoRefresh(); startPlanLimitsPoll();"
                        "const before = [...armed.values()].sort((a, b) => a - b);"
                        "showAuthNotice();"
                        "return { before, after: [...armed.values()] };")
        self.assertEqual(r["before"], [15000, 30000], "both polls were armed")
        self.assertEqual(r["after"], [])

    def test_it_is_still_safe_to_call_before_any_timer_is_armed(self):
        """The no-token boot path reaches it from the top level, before
        `scheduleAutoRefresh()` has run."""
        r = self._drive("autoRefreshTimer = null; planPollTimer = null;"
                        "showAuthNotice();"
                        "return { armed: [...armed.values()] };")
        self.assertEqual(r["armed"], [])


class TestDimmedTextIsNotDimmedBelowItsOwnPalette(unittest.TestCase):
    """`opacity` composites text with whatever is behind it, so a rule that dims
    a palette token renders at a contrast the palette never approved — and no
    palette test can see it, because the token itself is fine.

    `.cell-cost` (the money printed under every token count in Cost by Model,
    Cost by Project and Cost by Project & Branch) carried `opacity: 0.72` over
    `var(--green)`: 5.18:1 as a token on a light card, 3.03:1 as rendered. The
    unit price nested inside it, `.cell-rate`, is inside that same opacity group
    and inherited the dimming, landing at 1.64:1 on a dark card — and its whole
    purpose is that the multiplication printed there can be *checked*.

    The size step (11px money under a 13px count) is what makes the money
    secondary; the opacity only made it hard to read.
    """

    SURFACES = ("--card", "--bg")   # what a table cell rests on; `tr:hover td`
                                    # raises it to --raised, where the light
                                    # --green token is 4.31:1 on its own — a
                                    # palette question this rule cannot answer.

    def _palette(self, pattern):
        m = re.search(pattern, CSS, re.S)
        self.assertIsNotNone(m, "missing CSS block: " + pattern)
        return dict(re.findall(r"(--[a-z-]+):\s*(#[0-9A-Fa-f]{6})", m.group(1)))

    def _rule(self, selector):
        m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS, re.S)
        self.assertIsNotNone(m, "missing CSS rule: " + selector)
        return m.group(1)

    @staticmethod
    def _luminance(value):
        channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                    for c in channels]
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    @classmethod
    def _contrast(cls, a, b):
        la, lb = cls._luminance(a), cls._luminance(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    @staticmethod
    def _composite(fg, bg, alpha):
        mix = [round(alpha * int(fg[i:i + 2], 16) + (1 - alpha) * int(bg[i:i + 2], 16))
               for i in (1, 3, 5)]
        return "#" + "".join("%02X" % c for c in mix)

    def _rendered(self, selector, palette, surface, inherited=1.0):
        """The colour that actually reaches the screen: the token the rule names,
        composited with every `opacity` between it and the page."""
        body = self._rule(selector)
        token = re.search(r"color:\s*var\((--[a-z-]+)\)", body)
        self.assertIsNotNone(token, selector + " no longer names a palette token")
        alpha = re.search(r"opacity:\s*([0-9.]+)", body)
        effective = inherited * (float(alpha.group(1)) if alpha else 1.0)
        return self._composite(palette[token.group(1)], palette[surface], effective)

    def _both_palettes(self):
        return {
            "dark": self._palette(r":root\s*\{(.*?)\}"),
            "light": self._palette(r":root\[data-theme=\"light\"\]\s*\{(.*?)\}"),
        }

    def test_the_money_under_a_token_count_is_readable_in_both_themes(self):
        for name, palette in self._both_palettes().items():
            for surface in self.SURFACES:
                with self.subTest(theme=name, surface=surface):
                    rendered = self._rendered(".cell-cost", palette, surface)
                    self.assertGreaterEqual(
                        round(self._contrast(rendered, palette[surface]), 2), 4.5,
                        ".cell-cost renders " + rendered + " on " + surface)

    def test_the_unit_price_is_not_dimmed_below_the_token_it_names(self):
        """`.cell-rate` is a child of `.cell-cost`, so any opacity on the parent
        applies to it too. Its own token is `--muted`, which the dark palette
        deliberately keeps low for labels everywhere; what this rule must not do
        is push it *below* that."""
        for name, palette in self._both_palettes().items():
            for surface in self.SURFACES:
                with self.subTest(theme=name, surface=surface):
                    parent = re.search(r"opacity:\s*([0-9.]+)", self._rule(".cell-cost"))
                    rendered = self._rendered(".cell-rate", palette, surface,
                                              float(parent.group(1)) if parent else 1.0)
                    self.assertEqual(rendered.lower(), palette["--muted"].lower(),
                                     ".cell-rate renders " + rendered + " rather than "
                                     "its own token " + palette["--muted"])


@requires_node
class TestThisWeekIsTheISOWeekHoldingToday(unittest.TestCase):
    """"This Week" runs Monday to Sunday, and one character is the whole of it.

    This Week uses an ISO Monday start, including when viewed on Sunday.
    A Sunday-start calculation would exclude the six preceding days.

    The clock is frozen one day at a time and swept through a year, so nothing
    here depends on the weekday the suite happens to run on and the sweep meets
    every daylight-saving transition the machine's own timezone has.
    """

    # A year and a week of "todays": every weekday, every DST change.
    DAYS = [(date(2026, 1, 1) + timedelta(days=n)).isoformat() for n in range(372)]

    SWEEP = """
      (() => {
        const Real = Date;
        const rows = [];
        for (const iso of days) {
          // getRangeBounds reads `new Date()` with no arguments; freeze exactly
          // that, at local NOON so no DST shift can land on the instant itself.
          globalThis.Date = class extends Real {
            constructor(...a) { if (a.length === 0) super(iso + 'T12:00:00'); else super(...a); }
            static now() { return new Real(iso + 'T12:00:00').getTime(); }
          };
          try {
            const b = getRangeBounds(range);
            // Parsed at LOCAL midnight. `new Date('2026-08-03')` is read as UTC
            // and reports Sunday everywhere west of Greenwich, so the bare form
            // would re-introduce bug #151 inside the test that pins its fix.
            const start = new Real(b.start + 'T00:00:00');
            const end = new Real(b.end + 'T00:00:00');
            const dayAfter = new Real(end.getFullYear(), end.getMonth(), end.getDate() + 1);
            rows.push({ today: iso, start: b.start, end: b.end,
                        startDay: start.getDay(), endDay: end.getDay(),
                        dayAfterEnd: dayAfter.getDate(),
                        // Rounded: a week holding a DST change is 6 days plus or
                        // minus an hour, and an exact comparison would go red
                        // once or twice a year in most timezones.
                        spanDays: Math.round((end - start) / 86400000),
                        holdsToday: b.start <= iso && iso <= b.end });
          } finally {
            globalThis.Date = Real;
          }
        }
        return rows;
      })()
    """

    @classmethod
    def setUpClass(cls):
        cls.weeks = run_js(emit(cls.SWEEP, days=cls.DAYS, range="week"))
        cls.months = run_js(emit(cls.SWEEP, days=cls.DAYS, range="month"))

    def test_the_week_starts_on_monday_and_ends_on_sunday(self):
        """The only day-independent statement of the convention.

        Neither "it spans 6 days" nor "it contains today" discriminates: the
        Sunday-start window is also 6 days long and also holds today.
        """
        for row in self.weeks:
            with self.subTest(today=row["today"]):
                self.assertEqual(row["startDay"], 1,
                                 "This Week starts " + row["start"] + ", not a Monday")
                self.assertEqual(row["endDay"], 0,
                                 "This Week ends " + row["end"] + ", not a Sunday")

    def test_the_week_is_seven_days_long_and_holds_today(self):
        """Weaker than the weekday pair and kept for the case it does catch: a
        window that has slipped off today entirely draws no bars at all, because
        dailyFillSpan clamps its end to today and then has nothing left."""
        for row in self.weeks:
            with self.subTest(today=row["today"]):
                self.assertEqual(row["spanDays"], 6,
                                 row["start"] + ".." + row["end"] + " is not 7 days")
                self.assertTrue(row["holdsToday"],
                                "today " + row["today"] + " is outside "
                                + row["start"] + ".." + row["end"])

    def test_this_month_ends_on_the_last_day_of_the_month(self):
        """The other end of the pair `test_previous_month_ends_the_day_before_this_month_starts`
        already half-covers: it pins both starts and their ordering, and nothing
        pins that the month's end is the month's end."""
        for row in self.months:
            with self.subTest(today=row["today"]):
                self.assertEqual(row["dayAfterEnd"], 1,
                                 "This Month ends " + row["end"] + ", which is not "
                                 "the last day of its month")
                self.assertTrue(row["holdsToday"])


@requires_node
class TestChangingTheRangeRestatesWhetherThePageIsPolling(unittest.TestCase):
    """The header must not describe a timer the range has just switched off.

    `refreshIntervalMs` returns 0 for a range that cannot gain rows, and
    `updateMetaNote` carries a branch saying exactly that. `setRange` armed and
    disarmed the timer without ever calling it, so the note kept whatever the last
    caller had left: "Auto-refresh every 15m" over a dead timer, beside an
    "Updated:" stamp that never moves again — which is what a hung dashboard looks
    like — and, going the other way, no note at all over a live one. With polling
    off nothing runs to repair it, so it stands until the reader touches something
    else.
    """

    def _drive(self, script):
        return run_js(emit("(() => {"
                           "  const meta = { innerHTML: '', textContent: '' };"
                           "  const inert = document.getElementById;"
                           "  const els = { 'meta': meta, 'range-select': { value: '' },"
                           "                'refresh-select': { value: '' } };"
                           "  document.getElementById = id => (id in els ? els[id] : inert(id));"
                           "  globalThis.history = { replaceState: () => {} };"
                           "  updateURL = () => {};"
                           "  applyFilter = () => {};"
                           "  const armed = new Map();"
                           "  let nextId = 0;"
                           "  globalThis.setInterval = (fn, ms) => { armed.set(++nextId, ms); return nextId; };"
                           "  globalThis.clearInterval = (id) => { armed.delete(id); };"
                           "  lastGeneratedAt = '2026-08-09 01:49:03';"
                           "  selectedRange = '30d'; refreshSeconds = 0;"
                           "  updateMetaNote();"
                           + script +
                           "})()"))

    def test_a_historical_range_stops_the_header_claiming_to_poll(self):
        got = self._drive("setRefreshSeconds(900);"
                          "const before = meta.innerHTML;"
                          "setRange('prev-month');"
                          "return { before, after: meta.innerHTML,"
                          "         intervalMs: refreshIntervalMs(),"
                          "         armed: [...armed.values()] };")
        self.assertIn("Auto-refresh every 15m", got["before"],
                      "the note has to be there before it can go stale")
        self.assertEqual(got["intervalMs"], 0)
        self.assertEqual(got["armed"], [], "nothing is polling any more")
        self.assertNotIn("Auto-refresh", got["after"],
                         "the header still promises a poll the range has "
                         "switched off, and nothing will run to correct it")

    def test_going_back_to_a_live_range_says_it_is_polling_again(self):
        """The same omission pointed the other way, and the reason the call
        belongs after `scheduleAutoRefresh()` rather than inside the historical
        branch: here a real timer is armed under a header showing no note at
        all, which self-heals only at the first tick — up to a full interval."""
        got = self._drive("setRange('prev-month'); setRefreshSeconds(900);"
                          "const before = meta.innerHTML;"
                          "setRange('30d');"
                          "return { before, after: meta.innerHTML,"
                          "         armed: [...armed.values()] };")
        self.assertNotIn("Auto-refresh", got["before"])
        self.assertEqual(got["armed"], [900000], "the timer really is running")
        self.assertIn("Auto-refresh every 15m", got["after"],
                      "a live poll under a header that mentions none")


@requires_node
class TestTheModelTotalsSayAverageOnlyWhenTheRateIsOne(unittest.TestCase):
    """`avg` is a claim about the printed RATE, not about how many models fed it.

    PRICING lists claude-opus-5, -4-8, -4-7, -4-6 and -4-5 as five separate
    literals holding identical numbers, so a machine that upgraded from one Opus
    build to the next carries two ids in one view. The totals row counted
    contributing models rather than distinct prices, and marked the published
    $5.00/M, $25.00/M and $0.50/M `avg` — sending the reader to look for a blend
    that is not there — while the Cost by reasoning effort totals directly below
    printed the same tokens, the same rates and the same dollars unmarked.

    `columnRate` was written for that other card for precisely this reason; this
    is the sibling path the fix did not reach.
    """

    CAPTURE = r"""
      const captured = {};
      document.getElementById = (id) => ({
        set innerHTML(v) { captured[id] = v; },
        closest: () => null, rows: null, cells: null,
      });
    """

    def _row(self, model, inp=0, out=0, read=0, write=0, write_1h=0):
        return {"model": model, "turns": 1, "input": inp, "output": out,
                "cache_read": read, "cache_creation": write,
                "cache_creation_1h": write_1h}

    def _totals(self, rows):
        """The rendered totals `<td>`s, in column order."""
        html = run_js(emit("(() => {" + self.CAPTURE +
                           "renderModelCostTable(rows);"
                           "return captured['model-cost-total']; })()", rows=rows))
        return ["<td" + part for part in html.split("<td")[1:]]

    # Column order: label, turns, input, output, cache_read, cache_creation.
    INPUT, OUTPUT, READ, WRITE = 2, 3, 4, 5

    def test_two_ids_of_one_model_print_the_published_price_unmarked(self):
        cells = self._totals([
            self._row("claude-opus-5", inp=1_000_000, out=1_000_000,
                      read=1_000_000, write=1_000_000),
            self._row("claude-opus-4-8", inp=1_000_000, out=1_000_000,
                      read=1_000_000, write=1_000_000),
        ])
        for column, rate in ((self.INPUT, "$5.00/M"), (self.OUTPUT, "$25.00/M"),
                             (self.READ, "$0.50/M"), (self.WRITE, "$6.25/M")):
            with self.subTest(rate=rate):
                self.assertIn(rate, cells[column])
                self.assertNotIn("avg", cells[column],
                                 rate + " is the published Opus rate, not a blend")

    def test_two_rates_are_still_an_average(self):
        """The case the model count and the rate set agree on, kept so the fix
        cannot be "simplified" into never marking anything."""
        cells = self._totals([self._row("claude-opus-5", inp=1_000_000),
                              self._row("claude-sonnet-5", inp=1_000_000)])
        self.assertIn("$3.50/M avg", cells[self.INPUT])

    def test_a_column_fed_only_by_an_unpriced_model_is_still_marked(self):
        """An unpriced model's tokens land in the count and not in the money, so
        the column derives $0.00/M — and `$0.00` asserts the usage was free,
        which is a different claim from "not priced". The rate set holds exactly
        one entry here, `null`, so size alone would drop the marker."""
        cells = self._totals([self._row("claude-opus-5", out=1_000_000),
                              self._row("gemma-3", read=1_000_000)])
        self.assertIn("$0.00/M avg", cells[self.READ])
        self.assertNotIn("avg", cells[self.OUTPUT], "output came from opus alone")

    def test_a_totals_row_spanning_both_write_tiers_is_still_marked(self):
        """One row, one model, one rate in the set — and a cache-write figure on
        no price list, because 800k at $6.25 and 200k at $10.00 average $7.00.
        The tier split is a property of the row, not of the models, so it is
        `mixedTiers` and nothing else that catches it."""
        cells = self._totals([self._row("claude-opus-5", write=1_000_000,
                                        write_1h=200_000)])
        self.assertIn("$7.00/M avg", cells[self.WRITE])


class TestTheQuotaCardsReadTrueOnEitherAssistant(unittest.TestCase):
    """Plan Limits and Rate Limits are shown on both sources; their prose was
    written for one.

    Three strings in `web/index.html` are static markup that nothing rewrites
    per source, so with Codex selected the page said, ~200px below a `#plan-note`
    reading "the newest quota figure Codex recorded in its transcripts … Rescan
    to bring it forward", that the same figures "come from Claude Code's own
    cache and are only as fresh as the last time Claude Code refreshed it". Two
    contradictory answers to "why is this number old", and only one of them
    names an action that works.

    The two cards need OPPOSITE treatments, which is why one class asserts both:

    * Plan Limits renders either assistant's windows from the same projected
      shape, so its tooltip may name neither — the source-aware `#plan-note`
      underneath is what states the provenance.
    * Rate Limits reconstructs a notice only Claude Code writes
      (`route_limit_records` files a record as a notice iff it carries an
      `event_uuid`, and `codex_transcripts._limit_snapshot` never emits one), so
      its prose must say so. Its summary otherwise renders "0 / Times limited"
      for a source that cannot record one — "not recorded" printed as a
      measurement, the distinction AGENTS.md keeps `n/a` apart from `$0.00` for.

    Same defect and same remedy as the hourly card's peak-hour wording
    (`TestThePeakHourWordingNamesNoVendor` in tests/test_dashboard_js.py): say
    what the thing is, and where a signal really is one assistant's, say that
    too rather than implying its absence elsewhere is a zero.

    The Rate Limits half of this class now lives in
    `TestTheRateLimitCopyFollowsTheAssistantOnScreen` below, because that copy
    stopped being static markup: naming Claude Code unconditionally fixed the
    Codex page by breaking the Claude one, which is the common install. What
    stays here is the Plan Limits tooltip, which really is static and really
    must name nobody, plus a guard that the Rate Limits copy is not ALSO frozen
    into the markup — two copies of a per-source string is how one of them gets
    updated alone.
    """

    #: Assistants and providers named elsewhere on the page (title, footer rate
    #: card, source switch). In the Plan Limits tooltip any of them is the bug.
    VENDORS = ("Anthropic", "OpenAI", "Claude", "Codex")

    def _tooltip(self, aria_label):
        found = re.search(r'aria-label="' + aria_label + r'" title="([^"]*)"', HTML)
        self.assertIsNotNone(found, f'no info-icon titled "{aria_label}" in '
                                    "index.html — the card lost its explanation")
        return found.group(1)

    def test_the_plan_tooltip_names_no_assistant(self):
        """One panel, both sources, one projected window shape — so a vendor in
        the tooltip is a claim the panel does not make on half its renders."""
        tip = self._tooltip("About plan limits")
        for vendor in self.VENDORS:
            with self.subTest(vendor=vendor):
                self.assertNotIn(
                    vendor, tip,
                    f"the Plan Limits tooltip says {vendor!r} ({tip!r}) about a "
                    "panel that renders either assistant's windows — on the "
                    "other one it names the wrong vendor and the wrong store")

    def test_the_plan_tooltip_still_says_what_the_reading_is(self):
        """Vendor-neutral must not mean contentless."""
        tip = self._tooltip("About plan limits")
        for phrase in ("5-hour", "weekly", "as of", "API-key"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, tip)

    def test_the_rate_limit_copy_is_not_also_frozen_into_the_markup(self):
        """The renderer owns it now. A second copy in `index.html` is the one
        that gets left behind — which is exactly how this card came to name
        Claude Code on a page titled "Codex / Claude Usage · Codex" in the first place."""
        for element, pattern in (
                ("the Rate Limits tooltip",
                 r'aria-label="About rate limits" title="([^"]*)"'),
                ("the .limits-note paragraph",
                 r'<p class="limits-note"[^>]*>(.*?)</p>')):
            found = re.search(pattern, HTML, re.S)
            with self.subTest(element=element):
                self.assertIsNotNone(
                    found, f"{element} is gone from index.html; the renderer "
                           "addresses it by id, so removing it silently drops "
                           "the card's explanation")
                self.assertEqual(
                    "", found.group(1).strip(),
                    f"{element} carries text in the markup as well as in "
                    "renderLimitsCopy(); whichever of the two a later editor "
                    "finds first is the one that gets fixed")


@requires_node
class TestTheRateLimitCopyFollowsTheAssistantOnScreen(unittest.TestCase):
    """The Rate Limits card explains itself differently per source, and its
    summary must not print a zero it never measured.

    This is the half of `TestTheQuotaCardsReadTrueOnEitherAssistant` that used
    to read static markup; those three assertions are carried forward here
    (`..._says_whose_notices_it_reads`, `..._does_not_contradict_the_panel...`,
    `..._keeps_the_caveats_the_older_suite_pins`) and made stronger rather than
    dropped — each now runs against the string the browser would actually show,
    on BOTH sources, instead of the one string the markup held for both.

    Why the change of intended behaviour: naming Claude Code unconditionally is
    true on a Codex page but hands the Claude-only reader — the common install —
    a clause about an assistant they do not have. `renderStopReasonNote` already
    solved the identical problem for the twin card by branching on
    `selectedSource`, so this follows it rather than inventing a fourth pattern.

    The summary is the other half. `route_limit_records` files a record as a
    notice only when it carries an `event_uuid` and `codex_transcripts` never
    emits one, so `limit_incidents` is structurally empty for Codex — and
    "0 / Times limited · 0.0m / Time blocked" printed that absence as a
    measurement. A Codex user who really was throttled last week still read 0.
    """

    #: One real-shaped incident, from tests/test_rate_limits.py's fixture.
    INCIDENT = {"day": "2026-07-30", "started": "2026-07-30 05:18",
                "blocked_min": 23.2, "notices": 109,
                "projects": ["acme/web-storefront"], "reset_hint": "7:10am",
                "reset_zone": "Europe/Madrid", "status": 429}

    #: Drives the real `renderLimits` once per step, in order, against one set
    #: of elements — so a step sees whatever the previous one left behind. That
    #: is the dual-source install: the same DOM, re-rendered on a switch.
    _RENDER = """(() => {
      const html = {}, titles = {};
      const els = new Map();
      document.getElementById = (id) => {
        if (!els.has(id)) {
          const el = stubEl();
          Object.defineProperty(el, 'innerHTML', {
            get() { return html[id] === undefined ? '' : html[id]; },
            set(v) { html[id] = v; },
          });
          Object.defineProperty(el, 'title', {
            get() { return titles[id] === undefined ? '' : titles[id]; },
            set(v) { titles[id] = v; },
          });
          els.set(id, el);
        }
        return els.get(id);
      };
      return steps.map(step => {
        selectedSource = step.source;
        renderLimits(step.incidents);
        // `?? ''` so a string the renderer never writes fails as an assertion
        // about empty copy, not as a KeyError about a missing JSON key.
        return { source: step.source,
                 summary: html['limits-summary'] ?? '',
                 body: html['limits-body'] ?? '',
                 note: html['limits-note'] ?? '',
                 tip: titles['limits-info'] ?? '' };
      });
    })()"""

    @classmethod
    def setUpClass(cls):
        # One node run for the whole class: the harness concatenates and parses
        # the entire app on every call, and this walks four states.
        cls.views = run_js(emit(cls._RENDER, steps=[
            {"source": "claude", "incidents": []},
            {"source": "claude", "incidents": [cls.INCIDENT]},
            {"source": "codex", "incidents": []},
            {"source": "claude", "incidents": [cls.INCIDENT]},
        ]))
        cls.claude_empty, cls.claude, cls.codex, cls.switched_back = cls.views

    def test_the_codex_summary_does_not_print_a_zero_it_never_measured(self):
        """Nothing writes a Codex limit notice, so 0 is not a count of them."""
        summary = self.codex["summary"]
        self.assertNotIn(
            "<b>0</b>", summary,
            f"the Codex summary still counts notices ({summary!r}); Codex "
            "records no notice at all, so 0 is 'not recorded' printed as a "
            "measurement — the n/a-vs-$0.00 distinction AGENTS.md calls "
            "load-bearing")
        self.assertNotIn("0.0m", summary, "same for the blocked-time tile")
        self.assertEqual(3, summary.count("<b>—</b>"),
                         f"all three tiles should read '—' ({summary!r})")
        # The labels stay: the reader still needs to know what is not recorded.
        for label in ("Times limited", "Time blocked", "Most recent"):
            with self.subTest(label=label):
                self.assertIn(label, summary)

    def test_the_claude_summary_still_reports_its_true_zero(self):
        """A Claude reader with a clean range was really not limited, and that
        zero is a measurement. Gating on `incidents.length` instead of on the
        source would have taken it away from them."""
        self.assertIn("<b>0</b>", self.claude_empty["summary"])
        self.assertIn("0.0m", self.claude_empty["summary"])
        self.assertIn("No usage limits reached in this range.",
                      self.claude_empty["body"])
        self.assertIn("<b>1</b>", self.claude["summary"])
        self.assertIn("23.2m", self.claude["summary"])

    def test_the_empty_table_says_which_of_the_two_emptinesses_it_is(self):
        """The empty-table line reads "No usage limits reached in this range",
        which is a false claim on a source whose limits are never written down
        in the first place — it is the table that is silent, not the quota."""
        body = self.codex["body"]
        self.assertNotIn("No usage limits reached", body, body)
        self.assertIn("Not recorded", body, body)
        self.assertIn("Codex", body, body)

    def test_the_rate_limit_prose_says_whose_notices_it_reads(self):
        """Carried forward from TestTheQuotaCardsReadTrueOnEitherAssistant, and
        now asserted on the page that actually needs it: an empty table has to
        read as "nothing writes this here", not "you were never throttled"."""
        for where in ("tip", "note"):
            with self.subTest(where=where):
                self.assertIn(
                    "Only Claude Code", self.codex[where],
                    f"the Codex Rate Limits {where} ({self.codex[where]!r}) "
                    "does not say the notices come from one assistant, so the "
                    'card\'s empty table reads as a measurement of a '
                    "throttling that is simply never recorded here")
                self.assertIn("Codex", self.codex[where])

    def test_the_claude_page_is_not_told_about_an_assistant_it_does_not_have(self):
        """The other direction of the same defect, and the reason this moved out
        of static markup: one string cannot serve both installs."""
        for where in ("tip", "note"):
            with self.subTest(where=where):
                text = self.claude[where]
                self.assertNotIn(
                    "Only Claude Code", text,
                    f"the Claude Rate Limits {where} ({text!r}) explains that "
                    "another assistant would leave this table empty, to a "
                    "reader who has only this one")
                self.assertNotIn("Codex", text)
                self.assertIn("Claude Code", text,
                              "it still has to say whose notices these are")

    def test_the_note_does_not_contradict_the_panel_it_points_at(self):
        """Carried forward: `#plan-note` states the provenance per source, so
        this paragraph must not state a different one for the same figures —
        on EITHER source now, not just in the markup."""
        for view in (self.claude, self.codex):
            for claim in ("Claude Code's own cache", "Claude Code refreshed"):
                with self.subTest(source=view["source"], claim=claim):
                    self.assertNotIn(
                        claim, view["note"],
                        f"the Rate Limits note still sources Plan Limits from "
                        f"{claim!r}, which on Codex contradicts the #plan-note "
                        "directly above it — and sends the reader to wait for a "
                        "refresh that will never come instead of rescanning")

    def test_the_note_keeps_the_caveats_the_older_suite_pins(self):
        """Carried forward. tests/test_rate_limits.py asserts these against the
        whole assembled template and cannot see which file they live in — so
        moving them from the markup into the renderer keeps it green only while
        every branch still carries them."""
        import dashboard
        for view in (self.claude, self.codex):
            for phrase in ("no figure in this table is a percentage of a quota",
                           "remaining headroom", "Plan Limits"):
                with self.subTest(source=view["source"], phrase=phrase):
                    self.assertIn(phrase, view["note"])
                    self.assertIn(phrase, dashboard.HTML_TEMPLATE)

    def test_switching_source_rewrites_the_copy_in_place(self):
        """The dual-source install. `renderLimits` runs on every `applyFilter`,
        which every source switch reaches — so the copy has to be rewritten,
        not merely written once at load."""
        self.assertNotEqual(self.claude["note"], self.codex["note"])
        self.assertNotEqual(self.claude["tip"], self.codex["tip"])
        self.assertEqual(
            self.claude["note"], self.switched_back["note"],
            "switching Claude -> Codex -> Claude left the Codex explanation on "
            "the Claude page")
        self.assertEqual(self.claude["tip"], self.switched_back["tip"])
        self.assertEqual(self.claude["summary"], self.switched_back["summary"])

    def test_the_card_is_explained_in_place_rather_than_hidden(self):
        """Hiding #sec-limits on Codex was the reporter's first suggestion and
        all three refuters rejected it: the jump link, the collapse key and the
        reader's sense that the card exists all depend on it staying. Explain
        the emptiness instead."""
        self.assertRegex(HTML, r'<div class="table-card" id="sec-limits"[^>]*>')
        card = re.search(r'<div class="table-card" id="sec-limits"([^>]*)>', HTML)
        self.assertNotIn("hidden", card.group(1))
        self.assertIn('data-target="sec-limits"', HTML)
        # assertFalse, not assertNotIn: the container here is the whole 13 KB
        # file, and unittest would print all of it above the message.
        js = (WEB / "js" / "56-plan.js").read_text(encoding="utf-8")
        self.assertFalse("sec-limits" in js,
                         "renderLimits reaches for the card itself; the only "
                         "thing it should ever do with it is fill it in")


@requires_node
class TestTheHeadlineFigureNeverAssertsSomethingWasFree(unittest.TestCase):
    """`$0.00` is a claim, and the Est. Cost tile made it about real spend.

    This page already refuses that claim at the other end of the scale:
    `renderModelCostTotals`, `dailySeries()` and `noCostReason` all print `n/a`
    rather than `$0.00` when nothing is priced, because "we have no rate for
    this" is not "this cost nothing". The tile's two-decimal format asserted it
    anyway for anything under half a cent.

    Sub-cent estimates retain enough digits to distinguish a nonzero cost
    from zero and agree with the detailed cards and exports.

    Raised with ONE vote, and that vote said not-real. Adjudicated against the
    code instead of re-run: the doctrine is written down, four other call sites
    enforce it, and the self-contradiction is reproducible.
    """

    def test_a_sub_cent_total_shows_its_real_digits(self):
        got = run_js(
            "console.log(JSON.stringify({"
            "  real: fmtCostBig(0.002340), justUnder: fmtCostBig(0.0049)}))")
        self.assertEqual(got["real"], "$0.0023",
                         "the tile asserted a real charge was free")
        self.assertEqual(got["justUnder"], "$0.0049")

    def test_it_agrees_with_the_card_underneath_it(self):
        """The tile and the table must print the SAME digits for one figure —
        that agreement is the whole point of falling back to `fmtCost` rather
        than to a `< $0.01` form."""
        got = run_js(
            "console.log(JSON.stringify({"
            "  tile: fmtCostBig(0.002340), table: fmtCost(0.002340)}))")
        self.assertEqual(got["tile"], got["table"])

    def test_zero_still_reads_zero_and_big_figures_are_unchanged(self):
        """Anti-vacuity in both directions. A genuinely free total SHOULD say
        `$0.00`; anything that rounds to a nonzero cent keeps the two-decimal
        headline form, grouping included."""
        got = run_js(
            "console.log(JSON.stringify({"
            "  zero: fmtCostBig(0), boundary: fmtCostBig(0.005),"
            "  cent: fmtCostBig(0.01), big: fmtCostBig(1500.5)}))")
        self.assertEqual(got, {"zero": "$0.00", "boundary": "$0.01",
                               "cent": "$0.01", "big": "$1,500.50"})


if __name__ == "__main__":
    unittest.main()
