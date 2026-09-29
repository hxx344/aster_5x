"""Unknown acknowledgments refresh visible positions without authorizing writes."""
from copy import deepcopy
from contextlib import ExitStack
import time
import unittest
from unittest.mock import patch

from tests import test_pair_integration as integration
from tests import test_pair_order_recovery as recovery
from trading.exchange import BudgetWait, ExchangeError
from trading.pair_execution import PairTrader


class PairUnknownObservationTests(unittest.TestCase):
    setUp = integration.PairIntegrationTests.setUp
    seed = recovery.PairOrderRecoveryTests.seed
    state = recovery.PairOrderRecoveryTests.state
    broker = recovery.PairOrderRecoveryTests.broker
    no_writes = recovery.PairOrderRecoveryTests.no_writes

    def unknown(self, error=None):
        stack = ExitStack()
        stack.enter_context(self.no_writes())
        for side in ("long", "short"):
            stack.enter_context(patch.object(self.broker(side), "query", side_effect=error or
                ExchangeError("Order does not exist", code=-2013)))
        return stack

    def test_unknown_equal_positions_refresh_but_never_authorize_completion(self):
        original = self.seed()
        for side in ("long", "short"):
            broker = self.broker(side)
            broker.state["positions"]["XAUUSD1:" + side.upper()]["qty"] = "12"
            broker.save()
        trader = PairTrader(self.engine)
        self.engine.pairs.trader = trader
        with self.unknown(), patch.object(trader, "_read", wraps=trader._read) as read:
            self.engine.pairs.tick("gold")
            current = self.state()
            self.assertIsNotNone(current["pending"])
            self.assertEqual(current["owned"], original["owned"])
            for side in ("long", "short"):
                self.assertTrue(any(row["qty"] == "12" for row in current["snapshots"][side]["positions"]))
            first = deepcopy(current["snapshots"])
            # Future-but-near timestamps avoid relying on test execution speed.
            current["pending"]["observation_attempt_at"] = time.time() + 1
            self.f.store.put("pair_runtime:gold", current)
            # Backwards wall-clock movement must not freeze observations.
            self.engine.pairs.tick("gold")
            self.assertEqual(read.call_count, 2)
            current = self.state()
            with patch("trading.pair_execution.time.time", return_value=current["pending"]["observation_attempt_at"] + 1):
                self.engine.pairs.tick("gold")
            self.assertEqual(read.call_count, 2)
            current = self.state()
            current["pending"]["observation_attempt_at"] = time.time() - 4
            self.f.store.put("pair_runtime:gold", current)
            self.engine.pairs.tick("gold")
            self.assertEqual(read.call_count, 3)
            self.assertEqual(self.state()["pending"]["legs"], current["pending"]["legs"])
            self.assertIsNone(self.f.store.get("pair_batch:" + original["pending"]["id"]))
            self.assertTrue(first)

    def test_query_budget_wait_does_not_spend_more_on_observations(self):
        self.seed()
        trader = PairTrader(self.engine)
        self.engine.pairs.trader = trader
        with self.unknown(BudgetWait("本地 API 请求权重预算不足", retry_after=15)), \
                patch.object(trader, "_read", side_effect=AssertionError("quota backoff must not refresh")):
            self.engine.pairs.tick("gold")
        self.assertEqual(self.state()["api_notice"]["kind"], "budget")
        self.assertNotIn("observation_attempt_at", self.state()["pending"])

    def test_failed_observation_preserves_old_snapshot_and_known_receipt(self):
        original = self.seed()
        receipt = recovery.receipt_for(original["pending"]["legs"][0], status="REJECTED")
        original["pending"]["legs"][0]["receipt"] = receipt
        original["snapshots"] = {"sentinel": "old-display"}
        self.f.store.put("pair_runtime:gold", original)
        trader = PairTrader(self.engine)
        self.engine.pairs.trader = trader
        with self.unknown(), patch.object(trader, "_read", side_effect=BudgetWait("budget", retry_after=10)):
            self.engine.pairs.tick("gold")
        current = self.state()
        self.assertEqual(current["snapshots"], original["snapshots"])
        self.assertEqual(current["pending"]["legs"][0]["receipt"], receipt)
        self.assertEqual(current["api_notice"]["kind"], "budget")
        self.assertEqual(current["pending"]["repairs"], [])


if __name__ == "__main__":
    unittest.main()
