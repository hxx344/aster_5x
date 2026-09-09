"""Account snapshots never query all open orders or live commission rates."""
import copy
import unittest

from trading.exchange import LiveBroker
from trading.models import SYMBOLS, dec
from trading.paper import DemoMarket
from .test_exchange_hardening import FixtureAPI, account_responses


class RemovedRESTReadTests(unittest.TestCase):
    def test_three_market_snapshot_uses_fixed_fees_and_marks_orders_unqueried(self):
        responses = account_responses()
        responses.pop("/fapi/v3/openOrders")
        responses.pop("/fapi/v3/commissionRate")
        for path in ("/fapi/v3/accountWithJoinMargin", "/fapi/v3/positionRisk"):
            rows = responses[path]["positions"] if isinstance(responses[path], dict) else responses[path]
            rows.extend({**row, "symbol": symbol} for symbol in SYMBOLS[1:] for row in copy.deepcopy(rows[:2]))
        class AccountAPI(FixtureAPI):
            def call(self, method, path, *args, **kwargs):
                response = super().call(method, path, *args, **kwargs)
                if path == "/fapi/v3/leverageBracket":
                    response["symbol"] = args[0]["symbol"]
                return response
        api = AccountAPI(responses)
        broker = LiveBroker({}, DemoMarket(), api=api)
        self.assertEqual(broker.snapshot_weight(SYMBOLS), 79)
        snapshot = broker.snapshot(SYMBOLS)
        self.assertIsNone(snapshot.open_orders)
        self.assertEqual(snapshot.fees, dict.fromkeys(SYMBOLS, dec(".0004")))
        self.assertEqual(len(api.calls), 7)
        self.assertEqual(sum(call[3].get("weight", 1) for call in api.calls), 73)
        self.assertEqual(len(snapshot.positions), 6)
        self.assertEqual(broker.snapshot_weight(SYMBOLS), 19)
        api.calls.clear()
        broker.snapshot(SYMBOLS)
        self.assertEqual([call[1] for call in api.calls], ["/fapi/v3/accountWithJoinMargin", "/fapi/v3/positionRisk"])

    def test_old_fee_cache_does_not_replace_fixed_estimate_or_require_removed_endpoints(self):
        responses = account_responses()
        responses["/fapi/v3/openOrders"] = AssertionError("must not request all-account orders")
        responses["/fapi/v3/commissionRate"] = AssertionError("must not request commission rates")
        api = FixtureAPI(responses)
        broker = LiveBroker({}, DemoMarket(), api=api)
        broker.cached["fee:XAUUSD1"] = {"takerCommissionRate": "0"}
        snapshot = broker.snapshot(["XAUUSD1"], fresh_modes=True)
        self.assertEqual(snapshot.fees["XAUUSD1"], dec(".0004"))
        self.assertIsNone(snapshot.open_orders)
        snapshot.require_ready("XAUUSD1")


if __name__ == "__main__":
    unittest.main()
