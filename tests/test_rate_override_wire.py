"""The user's rates must reach the browser, not just the CLI.

`pricing.load_rate_overrides` replaces entries in the server's price table, but
the dashboard bills every figure in the browser from a *second* copy of that
table. So a rate that stopped at the server made `cli.py stats` and the page's
Est. Cost tile quote different money for the identical turns — measured at
$1.00 against $5.00, a 5x divergence, underneath a footer telling the reader to
set the very variable that was being ignored.

The gap survived the fix that introduced `applyRateOverrides` because the test
covering that function substitutes its own `APP_CONFIG` object. That proves the
page applies what it is given; it cannot prove the server gives it anything.
These tests read the **served document** instead, which is the only place the
two halves meet.
"""
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard  # noqa: E402
import pricing  # noqa: E402


class TestTheOverrideReachesTheServedDocument(unittest.TestCase):
    """Fetched over HTTP from a real server, deliberately.

    The first version of this file built the config dict itself and searched the
    template for it — and it could not fail: reverting the handler's wiring
    entirely left all four tests green, because the test was serving itself the
    value it then asserted on. That is the identical defect that let the original
    bug through, reproduced inside its own regression test. The only honest
    assertion is against bytes the handler actually wrote.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = dashboard.DashboardHTTPServer(
            ("127.0.0.1", 0), dashboard.DashboardHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def served_app_config(self):
        """APP_CONFIG as parsed out of a document fetched over the wire."""
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as response:
            document = response.read().decode("utf-8")
        match = re.search(r"APP_CONFIG\s*=\s*(\{.*?\});", document, re.S)
        if match is None:
            raise AssertionError("no APP_CONFIG assignment in the served document")
        return json.loads(match.group(1))

    def served_rate_overrides(self):
        """The entry transport restored to the model-keyed table it carries."""
        return dict(self.served_app_config()["rate_overrides"])

    def setUp(self):
        """`load_rate_overrides` mutates module globals; put them back."""
        self._pricing = {model: dict(rates) for model, rates in pricing.PRICING.items()}
        self._estimated = set(pricing.ESTIMATED_RATE_MODELS)
        self._override_rate_objects = dict(pricing._OVERRIDE_RATE_OBJECTS)
        self._applied = set(dashboard._APPLIED_RATES)

    def tearDown(self):
        pricing.PRICING.clear()
        pricing.PRICING.update(self._pricing)
        pricing.ESTIMATED_RATE_MODELS.clear()
        pricing.ESTIMATED_RATE_MODELS.update(self._estimated)
        pricing._OVERRIDE_RATE_OBJECTS.clear()
        pricing._OVERRIDE_RATE_OBJECTS.update(self._override_rate_objects)
        dashboard._APPLIED_RATES = self._applied

    def _apply(self, table):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        try:
            json.dump(table, handle)
            handle.close()
            dashboard._APPLIED_RATES = pricing.load_rate_overrides(path=handle.name)
        finally:
            os.unlink(handle.name)

    def test_an_overridden_rate_is_in_the_document_the_browser_receives(self):
        self._apply({"gpt-5.6-sol": {"input": 1.0}})
        served = self.served_app_config()
        self.assertIn(
            "rate_overrides", served,
            "the served APP_CONFIG carries no rate_overrides key, so the page "
            "bills from its built-in table and disagrees with the CLI")
        self.assertEqual(
            dict(served["rate_overrides"]).get("gpt-5.6-sol", {}).get("input"), 1.0,
            "the rate the user supplied did not reach the browser")

    def test_the_served_rate_is_exactly_what_the_cli_bills(self):
        """The whole point: one number, both surfaces.

        Asserting the served table against `calc_cost` rather than against the
        literal from the file, because the file may set one field and the other
        four must come through resolved — which is the half a JavaScript
        reimplementation of the merge rule would get wrong.
        """
        self._apply({"gpt-5.6-sol": {"input": 1.0}})
        served = self.served_rate_overrides()["gpt-5.6-sol"]
        # Keep this wire-parity fixture below the per-request long-context
        # threshold; that tier is covered independently by the pricing tests.
        tokens = 100_000
        cli_cost = pricing.calc_cost("gpt-5.6-sol", tokens, 0, 0, 0)
        page_cost = served["input"] * tokens / 1_000_000
        self.assertEqual(
            page_cost, cli_cost,
            f"page would bill {page_cost} where the CLI bills {cli_cost}")
        for field in pricing.RATE_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(
                    served[field], pricing.PRICING["gpt-5.6-sol"][field],
                    "every field must arrive resolved, not only the one the "
                    "override file named")

    def test_no_override_ships_an_empty_table_rather_than_omitting_the_key(self):
        """An ordinary install must keep working, and keep its shape.

        `applyRateOverrides` early-returns on a falsy argument, so `{}` and a
        missing key behave identically today — but the key's presence is what
        lets a reader of the document tell "nothing was overridden" from "this
        server is too old to send them".
        """
        dashboard._APPLIED_RATES = set()
        served = self.served_app_config()
        self.assertEqual(served.get("rate_overrides"), [])

    def test_a_broken_override_table_does_not_take_the_page_down(self):
        """This runs inside the request handler, so it must never raise."""
        dashboard._APPLIED_RATES = None  # not a set: resolved_rates will raise
        self.assertEqual(dashboard._rate_overrides_for_page(), {})

    def test_a_magic_model_name_remains_an_entry_in_the_served_document(self):
        self._apply({"__proto__": {"input": 1.0}})
        self.assertEqual(self.served_rate_overrides()["__proto__"]["input"], 1.0)


if __name__ == "__main__":
    unittest.main()
