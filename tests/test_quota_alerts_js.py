"""Properties of the quota threshold alerts that nothing pinned.

All were found the same way — break the production code in a sandbox copy and
run the whole suite — and all stayed green, which is what "unprotected" means
in practice:

* **The crossing boundary.** `crossedThresholds` fires on `seen < t && percent >= t`.
  Nothing exercised `seen === t`, so relaxing `<` to `<=` passed `Ran 896 tests
  … OK` while reintroducing the exact bug the feature exists to prevent: after a
  crossing the high-water mark IS the threshold, so a percentage that merely
  rests on 80 matches again and notifies on every 30-second poll. The existing
  tests miss it by construction — one crosses at 85 and one sits at 82, so
  `seen` is never equal to the threshold.

* **The delivery channels.** `deliverAlert` had no coverage at all. Replacing
  `showAlertBanner`'s body with a bare `return` — deleting the in-page banner
  outright — also passed `Ran 896 tests … OK`, and so did gating the banner on
  the notification having failed. Which channel fires under which permission
  state was therefore free to change silently, including in the direction that
  drops the alert entirely: `Notification.permission === 'granted'` says the
  user once clicked Allow, not that the toast was rendered, and Focus / Do Not
  Disturb, a backgrounded tab and `tag` coalescing all swallow it. The banner is
  the channel that can be relied on, so it is delivered unconditionally.

* **The clock the guard asks.** Both alert paths gated on the server's
  `expired` flag while the panel beside them re-derives it from the viewer's
  clock, so a tab left open past a reset announced a window that had already
  rolled over. Dropping `window.expired ||` from either line passed
  `Ran 896 tests … OK`.

* **The documented delivery.** The README described the banner as a fallback,
  which it has never been. Prose is not code, but the README is the user's copy
  of the contract `TestAlertDeliveryChannels` pins, and it had been wrong since
  the feature shipped.
"""

import unittest
from pathlib import Path

from tests.test_dashboard_js import emit, requires_node, run_js

README = Path(__file__).resolve().parent.parent / "docs" / "README.md"

# Runs the real `deliverAlert` against a readable banner and a stubbed
# Notification constructor, once per permission state. `document.getElementById`
# is overridden rather than extended because the harness's stub returns a FRESH
# element on every call, so what the banner ended up holding cannot otherwise be
# read back.
_DELIVER_UNDER_EACH_PERMISSION = """
  (() => {
    const notified = [];
    let banner = null;
    document.getElementById = (id) => (id === 'quota-alert' ? banner : null);
    const stubNotification = (permission, throws) => {
      globalThis.Notification = function (title, opts) {
        if (throws) throw new TypeError('Illegal constructor');
        notified.push(title + ' :: ' + (opts || {}).body);
      };
      globalThis.Notification.permission = permission;
    };
    const run = (setup) => {
      banner = { textContent: '', hidden: true };
      notified.length = 0;
      setup();
      deliverAlert('Claude Code quota at 85%', 'Session (5-hour) has passed 80%');
      return { notified: [...notified], banner: banner.textContent,
               shown: !banner.hidden };
    };
    return {
      granted:  run(() => stubNotification('granted', false)),
      denied:   run(() => stubNotification('denied', false)),
      absent:   run(() => { globalThis.Notification = undefined; }),
      throwing: run(() => stubNotification('granted', true)),
    };
  })()"""


@requires_node
class TestTheCrossingBoundary(unittest.TestCase):
    """A percentage that comes to rest exactly ON a chosen threshold.

    The panel polls far faster than a quota moves, so an integer percentage
    sitting on the threshold it just passed is the common state, not the corner
    case. It is also the one reading for which "fire on the crossing" and "fire
    on every reading" produce different behaviour from the same `seen`.
    """

    def test_a_reading_resting_on_a_threshold_does_not_cross_it_again(self):
        """`seen === t` is the boundary. Landing on the threshold is the
        crossing; still being there on the next poll is not."""
        got = run_js(emit("""
          (() => {
            const t = [80];
            return { onto:    crossedThresholds(79, 80, t),
                     resting: crossedThresholds(80, 80, t),
                     onwards: crossedThresholds(80, 81, t) };
          })()"""))
        self.assertEqual(got["onto"], [80],
                         "landing exactly on the threshold IS the crossing")
        self.assertEqual(got["resting"], [],
                         "it re-fired while merely resting on the threshold")
        self.assertEqual(got["onwards"], [],
                         "it re-fired for a threshold already crossed")

    def test_a_percentage_parked_on_the_threshold_notifies_once(self):
        """End to end, because that is where it would be felt: the same reading
        arriving on poll after poll must produce one notification, not one every
        thirty seconds. This also pins the high-water-mark recording that makes
        the unit case above reachable at all."""
        got = run_js(emit("""
          (() => {
            localStorage.clear();
            saveThresholds([80]);
            const at = (p) => ({ available: true, windows: [
              { kind: 'session', group: 'session', scope: '', resets_at: 'W',
                percent: p, expired: false }] });
            return { baseline: checkQuotaAlerts(at(70), 'claude'),
                     crossing: checkQuotaAlerts(at(80), 'claude'),
                     parked:   checkQuotaAlerts(at(80), 'claude'),
                     still:    checkQuotaAlerts(at(80), 'claude') };
          })()"""))
        self.assertEqual(got["baseline"], [])
        self.assertEqual(got["crossing"], [80], "the crossing was not announced")
        self.assertEqual(got["parked"], [],
                         "it announced again while the reading had not moved")
        self.assertEqual(got["still"], [])


@requires_node
class TestAlertDeliveryChannels(unittest.TestCase):
    """How an alert actually reaches the user, under each permission state.

    The contract is that a threshold the user asked for is ALWAYS delivered by
    at least one channel — including when `Notification` is missing, blocked, or
    throws from its constructor.
    """

    def setUp(self):
        self.got = run_js(emit(_DELIVER_UNDER_EACH_PERMISSION))

    def test_the_banner_delivers_in_every_permission_state(self):
        """The banner is unconditional precisely because the notification is not
        trustworthy evidence of delivery. This is the guarantee; everything else
        in this class is detail."""
        for state in ("granted", "denied", "absent", "throwing"):
            with self.subTest(notification=state):
                self.assertIn("85%", self.got[state]["banner"],
                              "the alert reached no channel at all")
                self.assertTrue(self.got[state]["shown"],
                                "the banner was written but left hidden")

    def test_a_permitted_notification_does_not_replace_the_banner(self):
        """Both channels, not either/or. Gating the banner on the notification
        having been constructed looks equivalent and is not: a constructor that
        returns without throwing says nothing about whether the OS displayed
        anything, so gating drops the alert entirely under Do Not Disturb."""
        self.assertEqual(len(self.got["granted"]["notified"]), 1,
                         "an allowed notification was not raised")
        self.assertIn("Session (5-hour) has passed 80%",
                      self.got["granted"]["notified"][0],
                      "the notification carried no body")
        self.assertTrue(self.got["granted"]["shown"],
                        "the banner was suppressed when notifications work")

    def test_no_notification_is_raised_when_it_is_not_permitted(self):
        """The other half: asking for one where permission was never granted is
        what makes browsers auto-deny the origin."""
        for state in ("denied", "absent", "throwing"):
            with self.subTest(notification=state):
                self.assertEqual(self.got[state]["notified"], [])


@requires_node
class TestAWindowThatEndedWhileTheReadingSatStill(unittest.TestCase):
    """The alert path must ask the same question the panel asks, of the same
    clock.

    `expired` is stamped by the server when the payload is built — right at that
    instant and wrong from the next second onwards, which is why `windowHasEnded`
    exists and why the panel re-runs it on every paint. The alerts module was
    reading the raw flag. That is reachable, not theoretical: `REFRESH_DEFAULT`
    is 0, so `/api/data` is never re-fetched, and Codex's window projection comes
    from that payload — so a tab left open past a reset holds `expired: false`
    for a window that ended hours ago, while the panel next to it renders
    "Window ended".

    Both call sites are covered because both fire on it, contrary to the
    intuition that a frozen payload freezes `percent` too and so can never cross
    again: the rollover branch sets `seen = -1` for a window whose KIND is on
    record, which makes the FIRST sighting of a stale window a crossing. The
    page opens on Claude, so Codex's frozen windows are not read by the alert
    state until the user switches source — by which time they can be hours past
    their reset.
    """

    # Reset times are derived from the clock rather than written down: a live
    # window's reset is in the future, and every hardcoded date in this file's
    # neighbour rotted into the past, which is the only reason the flag-trusting
    # behaviour looked correct to the suite.
    _STALE = """
      (() => {
        localStorage.clear();
        saveThresholds([80]);
        const said = [];
        deliverAlert = (t, b) => said.push(t + ' | ' + b);
        const ago   = new Date(Date.now() - 3600e3).toISOString();
        const ahead = new Date(Date.now() + 3600e3).toISOString();
        const win = (r, p) => ({ kind: 'weekly', group: 'weekly', scope: '',
                                 resets_at: r, percent: p, expired: false });
        const info = (w) => ({ available: true, windows: [w] });
        const stale = win(ago, 95);
        lastPlanInfo = info(stale);
        selectedSource = 'codex';
        const ticked = announceIfAlreadyPast(80);
        localStorage.clear();
        saveThresholds([80]);
        checkQuotaAlerts(info(win(ahead, 12)), 'codex');   // an earlier window
        const polled = checkQuotaAlerts(info(stale), 'codex');
        const saidOfTheEndedWindow = [...said];
        // The live control: the identical reading whose reset has NOT passed
        // must still announce, or the guard could be satisfied by silence.
        said.length = 0;
        localStorage.clear();
        lastPlanInfo = info(win(ahead, 95));
        const live = announceIfAlreadyPast(80);
        return { panelSaysEnded: windowHasEnded(stale),
                 serverFlagExpired: stale.expired,
                 ticked, polled, live, said: saidOfTheEndedWindow,
                 saidOfTheLiveWindow: said };
      })()"""

    def setUp(self):
        self.got = run_js(emit(self._STALE))

    def test_the_panel_and_the_alerts_disagree_about_nothing(self):
        """The premise: the flag says live, the clock says ended. If this ever
        stops being true the tests below prove nothing."""
        self.assertTrue(self.got["panelSaysEnded"])
        self.assertFalse(self.got["serverFlagExpired"])

    def test_ticking_a_threshold_on_an_ended_window_announces_nothing(self):
        """It would announce a percentage belonging to a window that has already
        rolled over, quoting a reset time in the past."""
        self.assertEqual(self.got["ticked"], 0)

    def test_polling_an_ended_window_announces_nothing(self):
        """The same for the 30-second path. Its first sighting of the stale
        window counts as a rollover, so `seen` is -1 and the whole reading looks
        like a crossing."""
        self.assertEqual(self.got["polled"], [])

    def test_nothing_at_all_was_said(self):
        self.assertEqual(self.got["said"], [])

    def test_a_window_that_has_not_ended_still_announces(self):
        """The control. A guard that suppressed everything would satisfy the
        three assertions above."""
        self.assertEqual(self.got["live"], 1)
        self.assertIn("95%", (self.got["saidOfTheLiveWindow"] or [""])[0])


class TestTheReadmeDescribesTheDeliveryTheCodeImplements(unittest.TestCase):
    """The shipped documentation promised an either/or the code never had.

    Four independent surfaces said the banner appears only when notifications
    are blocked — the in-code comment, the feature commit, the CHANGELOG entry
    and README.md — while `deliverAlert` has shown it unconditionally since the
    day it shipped. `TestAlertDeliveryChannels` above is what pins the
    behaviour; this is what stops the prose drifting away from it again, on the
    one surface a user reads.

    Deliberately narrow: it reads the *Quota alerts* paragraph only, so nothing
    else in the README can turn it red, and it bans the exact phrasings the
    four surfaces used rather than the word "otherwise" in general.
    """

    # "…and an in-page banner otherwise", "…an in-page banner when not",
    # "falls back to an in-page banner otherwise" — the three real ones.
    FALLBACK_WORDING = ("otherwise", "fall back", "falls back", "fallback",
                        "when not")

    def paragraph(self):
        text = README.read_text(encoding="utf-8")
        start = text.find("**Quota alerts.**")
        self.assertNotEqual(start, -1, "the README no longer documents the alerts")
        end = text.find("\n\n", start)
        return text[start:end if end != -1 else None].lower()

    def test_the_banner_is_not_documented_as_a_fallback(self):
        para = self.paragraph()
        for wording in self.FALLBACK_WORDING:
            with self.subTest(wording=wording):
                self.assertNotIn(
                    wording, para,
                    "the README makes the in-page banner conditional on the "
                    "notification, which deliverAlert has never done")

    def test_the_banner_is_documented_as_unconditional(self):
        """The other half — a paragraph that simply stopped mentioning the
        banner would satisfy the test above."""
        para = self.paragraph()
        self.assertIn("banner", para)
        self.assertIn("always", para,
                      "the README must say the banner is always shown, because "
                      "that is the channel the alert can be relied on to reach")


if __name__ == "__main__":
    unittest.main()
