"""Account read failures identify the field without exposing raw response data."""
import copy
import unittest

from trading.engine import Engine
from trading.exchange import LiveBroker, RateBudget
from trading.models import TradingError, positive
from trading.paper import DemoMarket
from .helpers import Fixture
from .test_exchange_hardening import FixtureAPI, account_responses
from .test_cycle_account_snapshot import ACCOUNT, RISK, SYMBOL, LocalQuoteMarket, cycle_account_responses, risk_row


class AccountNumericErrorTests(unittest.TestCase):
    def test_position_errors_identify_symbol_side_field_and_numeric_value(self):
        cases = (("entryPrice", "0", "正数"), ("markPrice", "0", "正数"),
                 ("liquidationPrice", "-1", "非负数"), ("leverage", "0", "正数"))
        for field, value, requirement in cases:
            with self.subTest(field=field):
                responses = account_responses()
                responses[RISK][0][field] = value
                api = FixtureAPI(responses)
                broker = LiveBroker({}, DemoMarket(), api=api)
                with self.assertRaises(TradingError) as caught:
                    broker.snapshot([SYMBOL])
                for text in (SYMBOL, "LONG", field, requirement, f"收到 {value}"):
                    self.assertIn(text, str(caught.exception))
                self.assertIsNone(broker.leverage_snapshot)
                self.assertTrue(all(call[0] == "GET" for call in api.calls))

    def test_asset_and_bracket_errors_identify_fields(self):
        cases = ((ACCOUNT, "maintMargin", "-1"),
                 ("/fapi/v3/leverageBracket", "notionalCap", "0"),
                 ("/fapi/v3/leverageBracket", "maintMarginRatio", "0"),
                 ("/fapi/v3/leverageBracket", "cum", "-1"))
        for path, field, value in cases:
            with self.subTest(field=field):
                responses = copy.deepcopy(account_responses())
                rows = responses[path]["assets" if path == ACCOUNT else "brackets"]
                rows[0][field] = value
                broker = LiveBroker({}, DemoMarket(), api=FixtureAPI(responses))
                with self.assertRaises(TradingError) as caught:
                    broker.snapshot([SYMBOL])
                self.assertIn(field, str(caught.exception))
                self.assertIn("USD1" if path == ACCOUNT else SYMBOL, str(caught.exception))

    def test_cycle_risk_fallback_has_the_same_position_diagnostic(self):
        responses = cycle_account_responses()
        responses[RISK] = [risk_row(row) for row in responses[ACCOUNT]["positions"]]
        responses[RISK][1]["liquidationPrice"] = "-2"
        market = LocalQuoteMarket()
        market.connected.clear()
        broker = LiveBroker({}, market, api=FixtureAPI(responses))
        with self.assertRaisesRegex(TradingError, "XAUUSD1 SHORT.*liquidationPrice.*非负数.*-2"):
            broker.cycle_snapshot([SYMBOL])
        self.assertIsNone(broker.leverage_snapshot)

    def test_malformed_value_is_not_echoed_and_zero_semantics_are_preserved(self):
        with self.assertRaises(TradingError) as caught:
            positive("private-response-text", field="标记价（markPrice）")
        self.assertIn("markPrice", str(caught.exception))
        self.assertNotIn("private-response-text", str(caught.exception))
        self.assertEqual(positive("0", True, field="强平价"), 0)
        with self.assertRaisesRegex(TradingError, "数值必须为正数"):
            positive("0")

    def test_new_account_failure_reaches_dashboard_and_stays_paused(self):
        fixture = Fixture()
        self.addCleanup(fixture.close)
        engine = Engine(fixture.store, market=fixture.market)
        engine.ready = True
        engine.add_account({"id": "new", "name": "New account", "env_prefix": "ASTER_NEW", "mode": "live"})
        responses = account_responses()
        responses[RISK][0]["liquidationPrice"] = "-1"
        api = FixtureAPI(responses)
        api.budget = RateBudget()
        engine.brokers["new"] = LiveBroker({}, fixture.market, api=api)
        engine.tick_account("new")
        state = engine.state()
        new = next(account for account in state["accounts"] if account["id"] == "new")
        self.assertIn("XAUUSD1 LONG", new["reason"])
        self.assertIn("liquidationPrice", new["reason"])
        self.assertFalse(new["enabled"])
        self.assertFalse(fixture.store.account("new")["enabled"])
        self.assertIsNone(fixture.store.intent("new"))
        self.assertTrue(all(call[0] == "GET" for call in api.calls))


if __name__ == "__main__":
    unittest.main()
