"""Reserved trading after unknown transfers, using offline live brokers."""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_margin_poll_efficiency as fixtures
from trading.exchange import ExchangeError
from trading.margin_balance import MarginBalancer
from trading.models import TradingError, dec, wire
from trading.pair_execution import empty_progress, runtime_default


SYMBOL = "XAUUSD1"
ORDER = "/fapi/v3/order"


class PairMarginResumeExecutionTests(TestCase):
    live_brokers = fixtures.PairMarginPollEfficiencyTests.live_brokers
    pending = fixtures.PairMarginPollEfficiencyTests.pending
    calls = fixtures.PairMarginPollEfficiencyTests.calls
    tick = fixtures.PairMarginPollEfficiencyTests.tick

    def setUp(self):
        fixtures.PairMarginPollEfficiencyTests.setUp(self)
        # Only the current tier has capacity: no unrelated leverage upgrade.
        self.engine.capacities = lambda symbol: {5: dec(1000000), 10: dec(0), 20: dec(0)}

    def unknown(self, amount="20000", *, ready=True):
        pending = self.pending("unknown", transaction=False)
        pending["amount"] = amount
        if ready:
            pending["trading_baseline_at"] = self.wall - 1
        self.store.put("pair_margin:gold", {"pending": pending, "next_check_at": self.wall + 30})
        return deepcopy(pending)

    def posts(self):
        return [row for row in self.calls() if row[0] != "GET"]

    def assert_original_transfer_retained(self, original):
        self.assertEqual(self.store.get("pair_margin:gold")["pending"], original)
        self.assertTrue(all(path == ORDER for _, path, _ in self.posts()))

    def assert_flat(self):
        for broker in self.paper.values():
            self.assertTrue(all(not row.qty for row in broker.snapshot([SYMBOL]).positions))

    def baseline(self):
        owned = {"LONG": "0.01", "SHORT": "0.01"}
        for side, broker in self.paper.items():
            broker.state["positions"][SYMBOL + ":" + side.upper()].update(
                qty=owned[side.upper()], entry=wire(self.market.book(SYMBOL).mark))
            broker.save()
        self.store.put("pair_runtime:gold", {**runtime_default(), "owned": owned,
                                            "progress": empty_progress(owned)})
        return owned

    def assert_baseline(self, owned):
        for side, index in (("long", 0), ("short", 1)):
            actual = self.paper[side].snapshot([SYMBOL]).pair(SYMBOL)[index].qty
            self.assertEqual(actual, dec(owned[side.upper()]))

    def publish(self, side):
        broker = self.brokers[side]
        cache = broker.cycle_cache
        snapshot = replace(self.paper[side].snapshot([SYMBOL]), open_orders=None)
        self.assertTrue(cache.publish(cache.begin_refresh(), snapshot, self.ticks))

    def test_unknown_debit_reservation_prevents_opening_when_cash_is_insufficient(self):
        original = self.unknown("24990")
        state = self.tick()
        self.assertFalse(state["margin"]["blocks_trading"], state)
        self.assertIsNone(state["pending"])
        self.assertEqual(state["owned"], {"LONG": "0", "SHORT": "0"})
        self.assertIn("暂不新增", state["reason"])
        self.assertEqual(self.posts(), [])
        self.assert_flat()
        self.assert_original_transfer_retained(original)

    def assert_opened_pair(self, state):
        self.assertIsNone(state["pending"], state)
        self.assertTrue(state["last_batch"]["completed"], state)
        self.assertGreater(dec(state["owned"]["LONG"]), 0)
        self.assertEqual(state["owned"]["LONG"], state["owned"]["SHORT"])
        self.assertEqual(len(self.posts()), 2)
        for side, broker in self.paper.items():
            receipts = list(broker.state["orders"].values())
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["status"], "FILLED")
            self.assertEqual(receipts[0]["side"], "BUY" if side == "long" else "SELL")
        self.assert_baseline(state["owned"])

    def test_balance_preparation_error_still_allows_real_paired_opening(self):
        self.paper["long"].state["wallet"] = "30000"
        self.paper["long"].save()
        with patch.object(MarginBalancer, "_live", side_effect=ExchangeError(
                "offline balance preparation unavailable")) as preparation:
            state = self.tick()
        preparation.assert_called_once()
        self.assert_opened_pair(state)
        self.assertFalse(state["margin"]["blocks_trading"])
        journal = self.store.get("pair_margin:gold")
        self.assertIsNone(journal.get("pending"))
        self.assertTrue(journal["retry_without_blocking"])
        self.assertTrue(all(path == ORDER for _, path, _ in self.posts()))

    def test_unknown_without_baseline_refreshes_both_accounts_and_really_opens(self):
        original = self.unknown(ready=False)
        self.store.put("pair_margin:gold", {"pending": original, "next_check_at": 0})
        with ExitStack() as stack:
            baseline_reads = [stack.enter_context(patch.object(broker, "margin_snapshot", wraps=broker.margin_snapshot))
                              for broker in self.brokers.values()]
            transfer = stack.enter_context(patch("trading.margin_balance.TransferAPI",
                side_effect=AssertionError("an unknown transfer must never be replayed")))
            state = self.tick()
            self.assert_opened_pair(state)
            self.assertTrue(all(read.call_count == 1 for read in baseline_reads))
            self.assertTrue(all(read.call_args.kwargs["fresh_modes"] for read in baseline_reads))
            resumed = self.store.get("pair_margin:gold")["pending"]
            self.assertEqual({key: value for key, value in resumed.items() if key != "trading_baseline_at"}, original)
            self.assertEqual(resumed["trading_baseline_at"], self.wall)
            self.assertFalse(state["margin"]["blocks_trading"])
            self.assertTrue(state["margin"]["trading_resume_allowed"])
            self.tick()
            transfer.assert_not_called()
            self.assertTrue(all(read.call_count == 1 for read in baseline_reads))
        self.assert_original_transfer_retained(resumed)

    def test_hot_replacement_replans_with_the_same_unknown_debit_reservation(self):
        original = self.unknown("23000")
        self.pair["ordinary"]["enabled"] = False
        self.pair["cycle"]["enabled"] = True
        self.pair = self.store.save_pair(self.pair)
        for side, broker in self.brokers.items():
            broker.cycle_cache.configure([SYMBOL])
            broker.cycle_cache.set_connected(True)
            self.publish(side)
        save = self.trader._save
        replaced = []

        def replace_after_intent(pair, state):
            save(pair, state)
            pending = state.get("pending")
            if not replaced and pending and all(leg["dispatch"] == "prepared" for leg in pending["legs"]):
                replaced.append(pending["id"])
                self.paper["long"].state["wallet"] = "23010"
                self.paper["long"].save()
                self.publish("long")

        with patch.object(self.trader, "_save", side_effect=replace_after_intent), \
                patch.object(self.trader, "_read", wraps=self.trader._read) as read:
            state = self.tick()
        self.assertEqual(len(replaced), 1, state)
        self.assertEqual(sum(call.kwargs.get("hot", False) for call in read.call_args_list), 2)
        self.assertEqual(self.posts(), [])
        self.assertFalse(state["last_batch"]["completed"])
        self.assert_flat()
        self.assert_original_transfer_retained(original)

    def test_postfill_reserved_margin_violation_reduces_only_this_batch(self):
        owned = self.baseline()
        original = self.unknown()
        submit = self.brokers["long"].submit
        raw_after_fill = []

        def lose_cash_after_fill(orders, **kwargs):
            receipt = submit(orders, **kwargs)
            if orders[0]["side"] == "BUY":
                self.paper["long"].state["wallet"] = "20100"
                self.paper["long"].save()
                raw_after_fill.append(self.paper["long"].snapshot([SYMBOL]))
            return receipt

        with patch.object(self.brokers["long"], "submit", side_effect=lose_cash_after_fill):
            state = self.tick()
        self.assertEqual(len(raw_after_fill), 1, state)
        self.assertFalse(raw_after_fill[0].margin_exceeds(dec("0.5")))
        self.assertEqual(state["phase"], "repairing", state)
        self.assertIn("成交后子账户保证金超限", state["pending"]["last_error"])
        self.assertEqual(len(state["pending"]["repairs"]), 2)
        state = self.tick()
        self.assertIsNone(state["pending"], state)
        self.assertFalse(state["last_batch"]["completed"])
        batch = self.store.get("pair_batch:" + state["last_batch"]["id"])
        for leg, repair in zip(batch["legs"], batch["repairs"]):
            self.assertEqual(leg["key"], repair["key"])
            self.assertEqual(leg["receipt"]["executedQty"], repair["order"]["quantity"])
            self.assertNotEqual(leg["order"]["side"], repair["order"]["side"])
        self.assertEqual(len(self.posts()), 4)
        self.assertEqual(state["owned"], owned)
        self.assert_baseline(owned)
        self.assert_original_transfer_retained(original)

    def test_balanced_partial_fills_with_unknown_transfer_reduce_without_adoption(self):
        owned = self.baseline()
        original = self.unknown()
        # Thin execution liquidity only in the ledger; the real planner still
        # sees the earlier full book, just as a market order can lose depth.
        for broker in self.paper.values():
            broker.market = SimpleNamespace(rules=self.market.rules,
                book=lambda symbol: replace(self.market.book(symbol), bid_qty=dec("0.001"), ask_qty=dec("0.001")))
        state = self.tick()
        self.assertEqual(state["phase"], "repairing", state)
        pending = state["pending"]
        self.assertEqual([dec(leg["receipt"]["executedQty"]) for leg in pending["legs"]], [dec("0.001")] * 2)
        self.assertTrue(all(leg["receipt"]["status"] != "FILLED" for leg in pending["legs"]))
        self.assertEqual(len(pending["repairs"]), 2)
        state = self.tick()
        self.assertIsNone(state["pending"], state)
        self.assertFalse(state["last_batch"]["completed"])
        self.assertEqual(state["owned"], owned)
        batch = self.store.get("pair_batch:" + state["last_batch"]["id"])
        self.assertNotIn("position_review", batch)
        self.assertNotIn("manual_position_reconciliation", batch)
        self.assertTrue(all(dec(leg["order"]["quantity"]) == dec("0.001") for leg in batch["repairs"]))
        self.assertEqual(len(self.posts()), 4)
        self.assert_baseline(owned)
        self.assert_original_transfer_retained(original)

    def test_paused_enable_requires_baseline_and_does_not_unlock_configure_or_delete(self):
        self.engine.pairs.enable("gold", False)
        original = self.unknown(ready=False)
        with self.assertRaisesRegex(TradingError, "划转"):
            self.engine.pairs.enable("gold", True)
        self.assertFalse(self.store.pair("gold")["enabled"])
        self.assert_original_transfer_retained(original)
        original = self.unknown()

        class Master:
            def __init__(inner):
                inner.budget = self.budget

            def call(inner, method, path, *, signed=False, weight=1):
                self.assertEqual((method, path), ("GET", "/fapi/v3/getSubAccountList"))
                inner.budget.reserve(weight)
                return [{"accountId": 1, "parentAccount": True}] + [
                    {"accountId": index, "parentAccount": False, "sourceAddr": credentials["user"]}
                    for index, credentials in enumerate(self.children.values(), 10)]

        @contextmanager
        def master_context(balancer, pair, members):
            yield Master(), self.master, self.children

        for broker in self.brokers.values():
            call = broker.api.call

            def with_open_orders(method, path, params=None, *, signed=False, weight=1, timeout=None,
                                 broker=broker, call=call):
                if path == "/fapi/v3/openOrders":
                    broker.api.budget.reserve(weight)
                    broker.api.calls.append((method, path, broker.api.budget._priority_flags()))
                    return []
                return call(method, path, params, signed=signed, weight=weight, timeout=timeout)

            broker.api.call = with_open_orders
        with patch.object(MarginBalancer, "_master", master_context):
            enabled = self.engine.pairs.enable("gold", True)
            self.assertTrue(enabled["enabled"])
            self.engine.pairs.enable("gold", False)
            for action in (lambda: self.engine.pairs.configure("gold", {"name": "must remain unchanged"}),
                           lambda: self.engine.pairs.delete("gold")):
                with self.assertRaisesRegex(TradingError, "划转"):
                    action()
        self.assertEqual(self.store.pair("gold")["name"], self.pair["name"])
        self.assertFalse(self.store.pair("gold")["enabled"])
        self.assertEqual(self.posts(), [])
        self.assert_flat()
        self.assert_original_transfer_retained(original)
